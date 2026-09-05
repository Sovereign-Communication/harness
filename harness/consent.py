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
from .core import chat, extract_content_and_cost, _extract_json, _reported_cost  # noqa: F401

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

_EVENT_FOR = {
    "accept": "consent_accept",
    "decline": "consent_decline",
    "defer": "consent_defer",
    "redirect": "consent_redirect",
}


def probe_consent(*, transport, api_key, governor, task_id, task, model,
                  context=None, max_tokens=200, ledger=None, required=True):
    """Ask the model whether it accepts the work. Returns a consent dict."""
    task_text = task if len(task) <= 3000 else task[:3000] + "\n...[truncated]"
    user = f"WORK ITEM:\n{task_text}"
    if context:
        user += f"\n\nCONTEXT:\n{context[:2000]}"

    preflight = getattr(governor, "preflight", None)
    if preflight is not None:
        # Include the system instruction in the estimate; it is part of the
        # billable prompt just like the work-item text.
        preflight(CONSENT_SYSTEM_PROMPT + "\n" + user,
                  [(f"consent:{model}", model, max_tokens, 0)])

    status, resp = chat(transport, api_key, model,
                        [{"role": "system", "content": CONSENT_SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
                        max_tokens, reasoning_effort="none", governor=governor)
    if status == 200:
        content, _, _, is_byok = extract_content_and_cost(resp)
        reported_cost = _reported_cost(resp)
    else:
        content, is_byok = None, False
        reported_cost = _reported_cost(resp)
    tracked_cost = 0.0
    if status == 200 and is_byok and not governor.is_free(model):
        # Paid BYOK route: spend is invisible to the tracked key; fail closed.
        governor.record_byok(model)
        status = 0
    elif reported_cost:
        # Error responses can still carry billable usage (for example a
        # provider-side rejection after tokenization). Count it too.
        tracked_cost = reported_cost
        record_actual = getattr(governor, "record_actual", None)
        if record_actual is not None:
            record_actual(tracked_cost, model)

    if ledger:
        ledger.append("offer", task_id=task_id, model=model, required=required)

    parsed = _extract_json(content) if status == 200 else None
    decision = (parsed or {}).get("decision")
    if decision not in DECISIONS:
        decision = "defer"
        reason = ("consent response was unparseable or missing a valid decision "
                  "(fail-closed: not dispatched)")
        if content:
            reason += f"; raw: {content[:200]}"
    else:
        reason = (parsed or {}).get("reason") or ""

    result = {
        "task_id": task_id,
        "model": model,
        "decision": decision,
        "reason": reason,
        "redirect_model": (parsed or {}).get("redirect_model"),
        "scope_suggestion": (parsed or {}).get("scope_suggestion"),
        # `cost` is the amount included in governor.spent and the ledger. Keep
        # the provider's raw number separately when a paid BYOK route was
        # rejected because that charge is outside the tracked key.
        "cost": tracked_cost,
        "reported_cost": reported_cost,
        "raw": content,
    }
    if ledger:
        ledger.append(_EVENT_FOR[decision], task_id=task_id, model=model, reason=reason,
                      redirect_model=result["redirect_model"],
                      scope_suggestion=result["scope_suggestion"], cost=tracked_cost,
                      billable_cost=tracked_cost)
    return result


def consent_renew(*, transport, api_key, governor, task_id, task, model,
                  context=None, max_tokens=200, ledger=None, required=True):
    """Re-check consent at a verification checkpoint (continued consensus).

    Returns the probe result; records a consent_renew_* event. Any deferral
    here is honored immediately by the caller (the task stops with partial
    work preserved).
    """
    base = probe_consent(transport=transport, api_key=api_key, governor=governor,
                         task_id=task_id, task=task, model=model, context=context,
                         max_tokens=max_tokens, ledger=None, required=required)
    if ledger:
        event = "consent_renew_accept" if base["decision"] == "accept" else "consent_renew_defer"
        ledger.append(event, task_id=task_id, model=model, reason=base["reason"],
                      cost=base["cost"])
    return base