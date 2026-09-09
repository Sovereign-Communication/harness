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
from .chat import (chat, extract_content_and_cost, _extract_json, _reported_cost,
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
                  fallback_pool=None):
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
        # billable prompt just like the work-item text. One slot per candidate
        # (the probe runs with reasoning disabled, so no reasoning fallback).
        preflight(CONSENT_SYSTEM_PROMPT + "\n" + user,
                  [(f"consent:{m_}", m_, max_tokens, 0) for m_ in usable])

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
                            max_tokens, reasoning_effort="none", governor=governor)
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
                          redirect_model=None, scope_suggestion=None,
                          cost=tracked_total, billable_cost=tracked_total)
        return result

    answered = m_
    reason = parsed.get("reason") or ""
    result = {
        "task_id": task_id,
        "model": answered,
        "decision": parsed.get("decision"),
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
                      reason=reason, redirect_model=result["redirect_model"],
                      scope_suggestion=result["scope_suggestion"], cost=tracked_total,
                      billable_cost=tracked_total)
    return result


def consent_renew(*, transport, api_key, governor, task_id, task, model,
                  context=None, max_tokens=512, ledger=None, required=True,
                  fallback_pool=None):
    """Re-check consent at a verification checkpoint (continued consensus).

    Returns the probe result; records a consent_renew_* event. Any deferral
    here is honored immediately by the caller (the task stops with partial
    work preserved).
    """
    base = probe_consent(transport=transport, api_key=api_key, governor=governor,
                          task_id=task_id, task=task, model=model, context=context,
                          max_tokens=max_tokens, ledger=None, required=required,
                          fallback_pool=fallback_pool)
    if ledger:
        # Attribute to the model that ANSWERED (post-rotation), not the
        # requested primary: billing a rotated renewal to the wrong model
        # corrupts per-model calibration. Attempts ride along so the
        # renewal's rotation evidence is not silently dropped (the probe
        # runs ledger-less here to avoid double-counting offers).
        event = "consent_renew_accept" if base["decision"] == "accept" else "consent_renew_defer"
        ledger.append(event, task_id=task_id, model=base.get("model") or model,
                      reason=base["reason"], cost=base["cost"],
                      attempts=base.get("attempts") or [])
    return base
