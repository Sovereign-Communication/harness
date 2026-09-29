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

PREVIEW_WHOLE_CHARS = 12000
PREVIEW_HEAD_CHARS = 9000


class ConsentStalenessEvent(str, Enum):
    """Declared changes that invalidate consent for an offered work package.

    Values are stable ledger/API vocabulary. Unknown values must not be treated
    as fresh consent; callers can use ``consent_staleness_events`` to validate
    a proposed event set before acting on it.
    """

    CHANGED_FILES = "changed_files"
    CONTEXT = "context"
    INSTRUCTION = "instruction"
    SELECTED_MODEL = "selected_model"
    TOKEN_LIMIT = "token_limit"
    MONETARY_LIMIT = "monetary_limit"


def consent_staleness_events(events):
    """Normalize declared staleness events or raise on invalid input.

    Accepts enum members or their exact serialized values. Strings and
    iterables are handled deliberately: a bare string is one event, while
    other iterables are treated as collections. No unknown kind is ignored.
    """
    if isinstance(events, (str, ConsentStalenessEvent)):
        events = (events,)
    try:
        return frozenset(ConsentStalenessEvent(event) for event in events)
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown consent staleness event") from exc


def consent_is_fresh(consented, current, events):
    """Return whether the declared consent bindings still match.

    ``consented`` and ``current`` map the stable event values to the values
    authorized and now proposed. Missing bindings are invalid and fail closed.
    """
    kinds = consent_staleness_events(events)
    try:
        return all(consented[kind.value] == current[kind.value]
                   for kind in kinds)
    except (KeyError, TypeError):
        return False


_BINDING_EVENTS = tuple(event.value for event in ConsentStalenessEvent)


def _binding_hash(value):
    """Hash canonical JSON without retaining source text in continuation state."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_consent_binding(*, file_path, source_content, instruction,
                         context=None, package_id=None, selected_model,
                         max_tokens, token_budget=None, task_max_cost,
                         run_max_cost=None):
    """Build the stable, payload-free identity of one proposed dispatch.

    The accepted object binds the exact offered file bytes, request context,
    instruction, worker identity, and immutable token/dollar ceilings. Mutable
    usage counters are intentionally excluded: they describe consumption,
    not the limits the worker was offered.
    """
    path = os.path.normcase(os.path.abspath(os.fspath(file_path)))
    content_hash = hashlib.sha256(
        str(source_content).encode("utf-8")).hexdigest()
    context_hash = _binding_hash({
        "package_id": package_id,
        "context": context,
    })
    token_limits = {
        "max_tokens": int(max_tokens),
        "max_input_tokens": (getattr(token_budget, "max_input_tokens", None)
                             if token_budget is not None else None),
        "max_output_tokens": (getattr(token_budget, "max_output_tokens", None)
                              if token_budget is not None else None),
    }
    money_limits = {
        "task_max_cost": (float(task_max_cost)
                          if task_max_cost is not None else None),
        "run_max_cost": (float(run_max_cost)
                         if run_max_cost is not None else None),
    }
    binding = {
        "version": 1,
        "changed_files": _binding_hash([{
            "path": path,
            "content_sha256": content_hash,
        }]),
        "context": context_hash,
        "instruction": _binding_hash(str(instruction)),
        "selected_model": _binding_hash(str(selected_model)),
        "token_limit": _binding_hash(token_limits),
        "monetary_limit": _binding_hash(money_limits),
        "worker_model": str(selected_model),
        "token_limits": token_limits,
        "monetary_limits": money_limits,
    }
    binding["digest"] = _binding_hash(binding)
    return binding


def valid_consent_binding(binding):
    """Whether a serialized consent binding is complete and untampered."""
    if not isinstance(binding, dict) or binding.get("version") != 1:
        return False
    expected_keys = {"version", "digest", *_BINDING_EVENTS,
                     "worker_model", "token_limits", "monetary_limits"}
    if set(binding) != expected_keys:
        return False
    for key in (*_BINDING_EVENTS, "digest"):
        value = binding.get(key)
        if (not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)):
            return False
    unsigned = {key: binding[key] for key in binding if key != "digest"}
    try:
        return (
            isinstance(binding.get("worker_model"), str)
            and bool(binding.get("worker_model"))
            and isinstance(binding.get("token_limits"), dict)
            and set(binding["token_limits"]) == {
                "max_tokens", "max_input_tokens", "max_output_tokens"}
            and isinstance(binding["token_limits"].get("max_tokens"), int)
            and not isinstance(binding["token_limits"].get("max_tokens"), bool)
            and binding["token_limits"]["max_tokens"] >= 0
            and all(value is None or (
                isinstance(value, int) and not isinstance(value, bool)
                and value >= 0)
                    for key, value in binding["token_limits"].items()
                    if key != "max_tokens")
            and isinstance(binding.get("monetary_limits"), dict)
            and set(binding["monetary_limits"]) == {
                "task_max_cost", "run_max_cost"}
            and all(value is None or (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and float(value) >= 0)
                    for value in binding["monetary_limits"].values())
            and binding["selected_model"] == _binding_hash(binding["worker_model"])
            and binding["token_limit"] == _binding_hash(binding["token_limits"])
            and binding["monetary_limit"] == _binding_hash(binding["monetary_limits"])
            and _binding_hash(unsigned) == binding["digest"]
        )
    except (TypeError, ValueError):
        return False


def consent_binding_is_fresh(consented, current):
    """Compare two complete package bindings; missing/old state is stale."""
    return (valid_consent_binding(consented)
            and valid_consent_binding(current)
            and consented["digest"] == current["digest"]
            and consent_is_fresh(consented, current, _BINDING_EVENTS))


def consent_binding_changes(consented, current):
    """Return the declared changed binding dimensions, fail-closed on bad state."""
    if not valid_consent_binding(consented) or not valid_consent_binding(current):
        return frozenset(ConsentStalenessEvent)
    return frozenset(event for event in ConsentStalenessEvent
                     if consented[event.value] != current[event.value])


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
        status, resp = chat(transport, api_key, m_,
                            [{"role": "system", "content": CONSENT_SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                            max_tokens, reasoning_effort="none", governor=governor,
                            token_budget=token_budget,
                            token_label=f"consent:{task_id}")
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
