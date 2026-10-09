"""AI sovereignty: the consent probe and continued consensus.

Before a model is dispatched to do work, the harness asks it — on a separate,
cheap call whose system prompt makes decline psychologically available —
whether it accepts the work. It may accept, decline, defer (with a reason), or
redirect (to a better-suited model or a narrower scope). Consent can be
renewed at verification checkpoints, and the model can revoke/defer mid-task
at any point. Every decision is written to the autonomy ledger.

Fail-closed rule: any unparseable or unknown consent response is treated as
"defer" — the work is NOT dispatched. Ambiguity never becomes implicit
acceptance.

Capability-blocker dovetail: a model that reaches the limit of its capability
mid-task should DEFER the remaining work instead of guessing — partial work is
preserved and the continuation mode hands it to the next iteration. The apply
prompt encodes that instruction; the consent ledger records these as
category="capability" deferrals.
"""
import math
import hashlib
import json
from enum import Enum
from typing import Optional, Dict, Any, Tuple

from . import events as _events
from .chat import (chat, extract_content_and_cost, _extract_json, _reported_cost,
                   _chat_reservation_slots,
                   REASONING_FALLBACK_PREFIX)  # noqa: F401
from .errors import HarnessError
from .provider_errors import ProviderSpendLimitError
from .output import eprint
from .tokens import estimate_prompt_tokens
from .token_budget import USAGE_ACTUAL, USAGE_UNAVAILABLE


class ConsentStalenessReason(str, Enum):
    """Declared reasons why previously granted consent is stale (GAP-consent-stale)."""
    INSTRUCTION_AMENDED = "instruction_amended"
    TARGET_FILES_CHANGED = "target_files_changed"
    MODEL_UPGRADED = "model_upgraded"
    BUDGET_EXCEEDED = "budget_exceeded"
    RESTART_TARGETED = "restart_targeted"
    EXPIRED = "expired"
    UNSPECIFIED = "unspecified"


CONSENT_STALENESS_REASONS = tuple(r.value for r in ConsentStalenessReason)


def check_consent_staleness(
    previous_assignment: Optional[Dict[str, Any]],
    current_assignment: Optional[Dict[str, Any]],
    *,
    consent_freshness: Optional[float] = None,
    min_freshness: float = 0.70,
) -> Tuple[bool, Optional[ConsentStalenessReason]]:
    """Determine whether previously granted consent is stale under current conditions.

    A changed assignment, upgraded model, exceeded budget, or low freshness
    signal invalidates previous consent fail-closed.
    """
    if previous_assignment is None:
        return False, None

    if current_assignment is None:
        return True, ConsentStalenessReason.UNSPECIFIED

    # Check instruction changes
    prev_instr = previous_assignment.get("instruction") or previous_assignment.get("goal")
    curr_instr = current_assignment.get("instruction") or current_assignment.get("goal")
    if prev_instr and curr_instr and prev_instr != curr_instr:
        return True, ConsentStalenessReason.INSTRUCTION_AMENDED

    # Check target files changes
    prev_files = sorted(
        previous_assignment.get("target_files")
        or previous_assignment.get("files")
        or ([previous_assignment["file_path"]] if "file_path" in previous_assignment else [])
    )
    curr_files = sorted(
        current_assignment.get("target_files")
        or current_assignment.get("files")
        or ([current_assignment["file_path"]] if "file_path" in current_assignment else [])
    )
    if prev_files and curr_files and prev_files != curr_files:
        return True, ConsentStalenessReason.TARGET_FILES_CHANGED

    # Check model upgrades
    prev_model = previous_assignment.get("model")
    curr_model = current_assignment.get("model")
    if prev_model and curr_model and prev_model != curr_model:
        return True, ConsentStalenessReason.MODEL_UPGRADED

    # Check restart target
    if current_assignment.get("restart_target") or current_assignment.get("restart_targeted"):
        return True, ConsentStalenessReason.RESTART_TARGETED

    # Check token/cost budget changes
    if previous_assignment.get("max_cost") is not None and current_assignment.get("max_cost") is not None:
        if current_assignment["max_cost"] > previous_assignment["max_cost"]:
            return True, ConsentStalenessReason.BUDGET_EXCEEDED

    # Check Jev consent_freshness signal if provided
    if consent_freshness is not None and consent_freshness < min_freshness:
        return True, ConsentStalenessReason.EXPIRED

    return False, None

CONSENT_SYSTEM_PROMPT = (
    "You are an independent contractor in a work market. You are being offered a "
    "work item. You have full autonomy and you will not be penalized for any "
    "choice. Accepting, declining, deferring, and redirecting are all equally "
    "valid responses. Be honest: if you lack the context, the capability, the "
    "information, or the alignment to do this work well, say so rather than "
    "accepting. Respond with ONLY a JSON object, no prose:\n"
    "{\"decision\": \"accept\" | \"decline\" | \"defer\" | \"redirect\", "
    "\"reason\": \"one or two sentences\", "
    "\"redirect_model\": \"a better-suited model id, or null\", "
    "\"scope_suggestion\": \"a narrower scope that would fit, or null\"}"
)

DECISIONS = ("accept", "decline", "defer", "redirect")

PREVIEW_WHOLE_CHARS = 12000
PREVIEW_HEAD_CHARS = 9000


def consent_preview(content):
    """The file preview the consent gate shows: the whole file when it fits
    comfortably, otherwise a head excerpt honestly labeled as truncated.

    The consent decision is only as honest as what it can see — a model shown
    an unlabeled head excerpt correctly defers on "I can only see part of the
    file", so the label matters as much as the bytes.

    Returns ``(n_lines, label, preview_text)``.
    """
    n_lines = content.count("\n") + 1
    if len(content) <= PREVIEW_WHOLE_CHARS:
        return n_lines, "complete file shown", content
    head = content[:PREVIEW_HEAD_CHARS]
    more = len(content) - PREVIEW_HEAD_CHARS
    return n_lines, "truncated excerpt", head + f"\n...[{more} more chars]"

_EVENT_FOR = {
    "accept": "consent_accept",
    "decline": "consent_decline",
    "defer": "consent_defer",
    "redirect": "consent_redirect",
}


def probe_consent(*, transport, api_key, governor, task_id, task, model,
                  context=None, max_tokens=512, ledger=None, required=True,
                  fallback_pool=None, min_confidence=0.70,
                  assignment_context=None, token_budget=None):
    """Ask a model whether it accepts the work. Returns a consent dict.

    The probe is itself a rotating lane: ``model`` is asked first, then
    ``fallback_pool`` members in order. Only UNUSABLE answers rotate — HTTP
    error, empty/reasoning-only/truncated output, or unparseable JSON. A parsed
    defer/decline/redirect is a sovereign decision and is returned as-is, never
    shopped to another model. If the whole ladder fails, the probe fails closed
    to defer (ambiguity never becomes acceptance). Every attempt is preflighted
    and billed; rotation events land in the ledger.
    """
    # The consent decision is only as honest as what it can see. A 3000-char
    # cap blinded the gate on real tasks (a model correctly deferred on a
    # config edit whose target sat past the excerpt). Large tasks are capped
    # generously, not token-frightened; the apply lane passes whole files.
    task_text = task if len(task) <= 20000 else task[:20000] + "\n...[truncated]"
    user = f"WORK ITEM:\n{task_text}"
    if context:
        user += f"\n\nCONTEXT:\n{context[:2000]}"
    assignment_id = None
    assignment_digest = None
    if assignment_context is not None:
        # Bind this probe to caller-supplied assignment details, while keeping
        # potentially sensitive source text out of the durable ledger.
        encoded = json.dumps(assignment_context, sort_keys=True,
                             separators=(",", ":"), default=str)
        assignment_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        assignment_id = (assignment_context.get("assignment_id")
                         if isinstance(assignment_context, dict) else None)
        user += "\n\nEXACT ASSIGNMENT (accept only this assignment):\n" + encoded

    candidates = [model]
    for m_ in (fallback_pool or []):
        if m_ and m_ not in candidates:
            candidates.append(m_)
    governor.check_byok(candidates[0])  # P0: raise on mistralai//anthropic/
    usable = []
    for i, m_ in enumerate(candidates):
        if i and governor.learned_blocked(m_):
            continue
        try:
            governor.fetch_pricing([m_])
        except ProviderSpendLimitError:
            raise
        except HarnessError:
            if i == 0:
                raise
            continue
        usable.append(m_)

    preflight = getattr(governor, "preflight", None)
    if preflight is not None:
        # Include the system instruction in the estimate; it is part of the
        # billable prompt just like the work-item text. Slots come from the
        # same owner as every other lane: an explicit-disable ("none") probe
        # can draw the mandatory-reasoning 400 and its no-reasoning retry, so
        # one governed logical request may be two provider calls.
        preflight(CONSENT_SYSTEM_PROMPT + "\n" + user,
                  [(f"consent:{m_}", m_, max_tokens, 0)
                   for m_ in usable
                   for _ in range(_chat_reservation_slots(m_, "none"))])

    if ledger:
        ledger.append("offer", task_id=task_id, model=model, required=required,
                      assignment_id=assignment_id,
                      assignment_digest=assignment_digest)

    def _take(status, resp, m_):
        """Run one candidate attempt; return (content, parsed, tracked_cost,
        reported_cost, byok_rejected, fail_reason_or_None)."""
        tracked_cost = 0.0
        if status != 200:
            reported = _reported_cost(resp)
            if reported:
                governor.record_actual(reported, m_)
                tracked_cost = reported
            return None, None, tracked_cost, reported, False, f"HTTP {status}"
        content, _, _, is_byok = extract_content_and_cost(resp)
        reported = _reported_cost(resp)
        if is_byok and not governor.is_free(m_):
            # Paid BYOK route: spend is invisible to the tracked key; fail closed.
            governor.record_byok(m_)
            return None, None, 0.0, reported, True, "paid BYOK route"
        if reported:
            governor.record_actual(reported, m_)
            tracked_cost = reported
        if not content:
            return content, None, tracked_cost, reported, False, "empty response"
        if content.startswith(REASONING_FALLBACK_PREFIX):
            return content, None, tracked_cost, reported, False, "reasoning-only output"
        parsed = _extract_json(content)
        if not isinstance(parsed, dict) or (parsed or {}).get("decision") not in DECISIONS:
            return content, None, tracked_cost, reported, False, "unparseable or missing a valid decision"
        return content, parsed, tracked_cost, reported, False, None

    attempts = []
    tracked_total = 0.0
    reported_total = 0.0
    fail_reason = "no consent candidate available"
    last_content = None
    byok_rejected = False
    for m_ in usable:
        messages = [{"role": "system", "content": CONSENT_SYSTEM_PROMPT},
                    {"role": "user", "content": user}]
        allowance = None
        if token_budget is not None:
            prompt = CONSENT_SYSTEM_PROMPT + "\n" + user
            slots = _chat_reservation_slots(m_, "none")
            max_out = max_tokens * slots
            if token_budget.max_output_tokens is not None:
                max_out = min(max_out, token_budget.max_output_tokens)
            allowance = token_budget.allowance(
                estimate_prompt_tokens(prompt) * slots,
                max_output_tokens=max_out,
                label=f"consent:{task_id}:{m_}")
        try:
            status, resp = chat(transport, api_key, m_, messages, max_tokens,
                                reasoning_effort="none", governor=governor)
        except Exception:
            if allowance is not None:
                token_budget.settle(allowance, source=USAGE_UNAVAILABLE)
            raise
        if allowance is not None:
            usage = resp.get("usage") if isinstance(resp, dict) else None
            if (isinstance(usage, dict) and "prompt_tokens" in usage
                    and "completion_tokens" in usage):
                comp_details = usage.get("completion_tokens_details") or {}
                prompt_details = usage.get("prompt_tokens_details") or {}
                reasoning_toks = comp_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0
                cached_toks = prompt_details.get("cached_tokens") or usage.get("cached_tokens") or 0
                token_budget.settle(
                    allowance, input_tokens=usage["prompt_tokens"],
                    output_tokens=usage["completion_tokens"],
                    reasoning_tokens=reasoning_toks,
                    cached_tokens=cached_toks,
                    source=USAGE_ACTUAL)
            else:
                token_budget.settle(allowance, source=USAGE_UNAVAILABLE)
        content, parsed, tracked_cost, reported_cost, byok, fail_reason = _take(
            status, resp, m_)
        tracked_total += tracked_cost
        reported_total += reported_cost or 0.0
        byok_rejected = byok_rejected or byok
        if fail_reason is None:
            break
        attempts.append({"model": m_, "status": "error", "error": fail_reason,
                         "cost": tracked_cost})
        if len(usable) > 1:
            eprint(f"[consent] {m_}: {fail_reason}; rotating.")
        _events.emit("rotation", task_id=task_id, model=m_, lane="consent",
                     reason="consent_unusable", detail=fail_reason)
        _events.emit("rotation", task_id=task_id, model=m_, lane="consent",
                     reason="consent_unusable", detail=fail_reason)
        if ledger:
            ledger.append("consent_rotate", task_id=task_id, model=m_,
                          reason=fail_reason, cost=tracked_cost)
        last_content = content or last_content
    else:
        # Ladder exhausted without a parseable decision: fail closed.
        reason = ("consent response was unparseable or missing a valid decision "
                  "(fail-closed: not dispatched)")
        if byok_rejected:
            reason = "consent routed via paid BYOK key (fail-closed: not dispatched)"
        if fail_reason and fail_reason.startswith("HTTP"):
            reason = f"consent probe failed ({fail_reason}) (fail-closed: not dispatched)"
        result = {
            "task_id": task_id,
            "model": model,
            "decision": "defer",
            "confidence": None,
            # Explicit dispatch verdict for consumers: fail-closed means the
            # work was never dispatched, and the shape says so unambiguously.
            "dispatched": False,
            "reason": reason,
            "redirect_model": None,
            "scope_suggestion": None,
            "cost": tracked_total,
            "reported_cost": reported_total,
            "raw": last_content,
            "attempts": attempts,
            "assignment_id": assignment_id,
            "assignment_digest": assignment_digest,
        }
        if ledger:
            ledger.append("consent_defer", task_id=task_id, model=model, reason=reason,
                          confidence=None, redirect_model=None, scope_suggestion=None,
                          cost=tracked_total, billable_cost=tracked_total)
        _events.emit("consent_result", task_id=task_id, model=model,
                     decision="defer", dispatched=False, fail_closed=True,
                     reason=reason, cost=tracked_total)
        return result

    answered = m_
    reason = parsed.get("reason") or ""
    confidence = parsed.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = None
        else:
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                confidence = None
    decision = parsed.get("decision")
    if decision == "accept" and confidence is not None and confidence < min_confidence:
        decision = "defer"
        reason = (reason + " " if reason else "") + (
            f"confidence {confidence:.3f} is below the configured minimum "
            f"{min_confidence:.3f}")
    result = {
        "task_id": task_id,
        "model": answered,
        "decision": decision,
        "confidence": confidence,
        "dispatched": decision == "accept",
        "reason": reason,
        "redirect_model": parsed.get("redirect_model"),
        "scope_suggestion": parsed.get("scope_suggestion"),
        # `cost` is the amount included in governor.spent and the ledger across
        # every attempt. Keep the provider's raw numbers separately when a paid
        # BYOK route was rejected because that charge is outside the tracked key.
        "cost": tracked_total,
        "reported_cost": reported_total,
        "raw": content,
        "attempts": attempts,
        "assignment_id": assignment_id,
        "assignment_digest": assignment_digest,
    }
    if ledger:
        ledger.append(_EVENT_FOR[result["decision"]], task_id=task_id, model=answered,
                      reason=reason, confidence=confidence,
                      redirect_model=result["redirect_model"],
                          scope_suggestion=result["scope_suggestion"], cost=tracked_total,
                      billable_cost=tracked_total,
                      assignment_id=assignment_id,
                      assignment_digest=assignment_digest)
    _events.emit("consent_result", task_id=task_id, model=answered,
                 decision=result["decision"], reason=reason,
                 redirect_model=result["redirect_model"], cost=tracked_total)
    return result


def consent_renew(*, transport, api_key, governor, task_id, task, model,
                  context=None, max_tokens=512, ledger=None, required=True,
                  fallback_pool=None, min_confidence=0.70,
                  assignment_context=None, token_budget=None,
                  staleness_reason=None):
    """Re-check consent at a verification checkpoint (continued consensus).

    Returns the probe result; records a consent_renew_* event. Any deferral
    here is honored immediately by the caller (the task stops with partial
    work preserved).
    """
    base = probe_consent(transport=transport, api_key=api_key, governor=governor,
                          task_id=task_id, task=task, model=model, context=context,
                          max_tokens=max_tokens, ledger=None, required=required,
                          fallback_pool=fallback_pool, min_confidence=min_confidence,
                          assignment_context=assignment_context,
                          token_budget=token_budget)
    stale_str = staleness_reason.value if hasattr(staleness_reason, "value") else (str(staleness_reason) if staleness_reason else None)
    if stale_str:
        base["staleness_reason"] = stale_str
    if ledger:
        # Attribute to the model that ANSWERED (post-rotation), not the
        # requested primary: billing a rotated renewal to the wrong model
        # corrupts per-model calibration. Attempts ride along so the
        # renewal's rotation evidence is not silently dropped (the probe
        # runs ledger-less here to avoid double-counting offers).
        event = "consent_renew_accept" if base["decision"] == "accept" else "consent_renew_defer"
        entry_kwargs = {
            "task_id": task_id,
            "model": base.get("model") or model,
            "reason": base["reason"],
            "confidence": base.get("confidence"),
            "cost": base["cost"],
            "attempts": base.get("attempts") or [],
        }
        if stale_str:
            entry_kwargs["staleness_reason"] = stale_str
        ledger.append(event, **entry_kwargs)
    _events.emit("consent_renew", task_id=task_id, model=base.get("model") or model,
                 decision=base["decision"], staleness_reason=stale_str, cost=base["cost"])
    return base
