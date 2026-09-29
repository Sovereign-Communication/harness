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
import hashlib
import json
import math
import os
from enum import Enum

from . import events as _events
from .chat import (chat, extract_content_and_cost, _extract_json, _reported_cost,
                   _chat_reservation_slots,
                   REASONING_FALLBACK_PREFIX)  # noqa: F401
from .errors import HarnessError
from .output import eprint

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


class ConsentStalenessEvent(str, Enum):
    """Stable reasons that make an existing consent decision stale."""

    CHANGED_FILES = "changed_files"
    CHANGED_INSTRUCTION = "changed_instruction"
    CHANGED_MODEL = "changed_model"
    CHANGED_TOKEN_COST_LIMITS = "changed_token_cost_limits"
    CHANGED_PACKAGE = "changed_package"

    def __str__(self):
        return self.value


def consent_binding(*, file_path, file_content, proposed_content=None,
                    instruction, edit_snippet,
                    backend, max_lines, execution_models, consent_models,
                    max_tokens, task_max_cost, run_max_cost=None,
                    token_budget=None, package=None):
    """Fingerprint the work and limits covered by one consent decision.

    The stored values are hashes, so a continuation records what changed
    without copying source text or prompts into its metadata.
    """
    def digest(value):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    path = os.path.normcase(os.path.normpath(str(file_path))).replace("\\", "/")
    file_fact = {
        "path": path,
        "sha256": hashlib.sha256(
            str(file_content or "").encode("utf-8")).hexdigest(),
        "proposed_sha256": hashlib.sha256(
            str(file_content if proposed_content is None else proposed_content
                ).encode("utf-8")).hexdigest(),
    }
    instruction_fact = {
        "instruction": str(instruction or ""),
        "edit_snippet": edit_snippet,
        "backend": backend,
        "max_lines": max_lines,
    }
    model_fact = {
        "execution": list(execution_models or []),
        "consent": list(consent_models or []),
    }
    token_limits = None
    if token_budget is not None:
        snapshot = token_budget.snapshot()
        token_limits = {
            "input": snapshot.get("max_input_tokens"),
            "output": snapshot.get("max_output_tokens"),
        }
    limits_fact = {
        "request_max_tokens": max_tokens,
        "task_max_cost": task_max_cost,
        "run_max_cost": run_max_cost,
        "token_budget": token_limits,
    }
    return {
        "version": 1,
        "fingerprints": {
            ConsentStalenessEvent.CHANGED_FILES.value: digest(file_fact),
            ConsentStalenessEvent.CHANGED_INSTRUCTION.value: digest(
                instruction_fact),
            ConsentStalenessEvent.CHANGED_MODEL.value: digest(model_fact),
            ConsentStalenessEvent.CHANGED_TOKEN_COST_LIMITS.value: digest(
                limits_fact),
            ConsentStalenessEvent.CHANGED_PACKAGE.value: digest(
                package or {}),
        },
    }


def consent_staleness(previous, current):
    """Return the stable reasons a saved consent binding no longer applies.

    Legacy continuations have no binding, so every dimension is stale and a
    fresh consent probe is required before dispatch.
    """
    events = tuple(ConsentStalenessEvent)
    if (not isinstance(previous, dict) or previous.get("version") != 1
            or not isinstance(previous.get("fingerprints"), dict)):
        return events
    old = previous["fingerprints"]
    new = current.get("fingerprints", {}) if isinstance(current, dict) else {}
    return tuple(event for event in events
                 if old.get(event.value) != new.get(event.value))

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
                  token_budget=None):
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
        ledger.append("offer", task_id=task_id, model=model, required=required)

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
        token_kwargs = ({"token_budget": token_budget,
                         "token_label": "consent"}
                        if token_budget is not None else {})
        status, resp = chat(transport, api_key, m_,
                            [{"role": "system", "content": CONSENT_SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                            max_tokens, reasoning_effort="none", governor=governor,
                            **token_kwargs)
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
    }
    if ledger:
        ledger.append(_EVENT_FOR[result["decision"]], task_id=task_id, model=answered,
                      reason=reason, confidence=confidence,
                      redirect_model=result["redirect_model"],
                      scope_suggestion=result["scope_suggestion"], cost=tracked_total,
                      billable_cost=tracked_total)
    _events.emit("consent_result", task_id=task_id, model=answered,
                 decision=result["decision"], reason=reason,
                 redirect_model=result["redirect_model"], cost=tracked_total)
    return result


def consent_renew(*, transport, api_key, governor, task_id, task, model,
                  context=None, max_tokens=512, ledger=None, required=True,
                  fallback_pool=None, min_confidence=0.70,
                  token_budget=None):
    """Re-check consent at a verification checkpoint (continued consensus).

    Returns the probe result; records a consent_renew_* event. Any deferral
    here is honored immediately by the caller (the task stops with partial
    work preserved).
    """
    base = probe_consent(transport=transport, api_key=api_key, governor=governor,
                          task_id=task_id, task=task, model=model, context=context,
                          max_tokens=max_tokens, ledger=None, required=required,
                          fallback_pool=fallback_pool, min_confidence=min_confidence,
                          token_budget=token_budget)
    if ledger:
        # Attribute to the model that ANSWERED (post-rotation), not the
        # requested primary: billing a rotated renewal to the wrong model
        # corrupts per-model calibration. Attempts ride along so the
        # renewal's rotation evidence is not silently dropped (the probe
        # runs ledger-less here to avoid double-counting offers).
        event = "consent_renew_accept" if base["decision"] == "accept" else "consent_renew_defer"
        ledger.append(event, task_id=task_id, model=base.get("model") or model,
                      reason=base["reason"], confidence=base.get("confidence"),
                      cost=base["cost"], attempts=base.get("attempts") or [])
    return base
