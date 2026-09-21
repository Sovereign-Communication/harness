"""Auto-escalation driver: judge-directed rung stepping for apply.

After a cheap apply lane exhausts its verify budget, this driver walks the
configured escalation ladder (cheapest -> most capable). Each rung produces
COMPLETE file content through the same apply prompt contract and is finished
through the real verification gate -- a rung only counts if the gate passes.

The convergence specialist (verify lane) may attach an escalation directive
(``escalation.needed`` + ``condensed_context``) to a failed result; that
context is prepended for rungs after the first. This module does not invent
plans from apply output (apply returns file content, not judge JSON).
"""
from . import events as _events
from .chat import (_chat_reservation_slots, chat, extract_content_and_cost,
                   assess_output)
from .config import effective_lane_policy
from .errors import HarnessError
from .output import eprint
from .sliding_scale import (decide_probe_verify_escalate, model_family,
                            should_abstain)


def _annotate_escalation(result, *, from_model, to_model, rungs) -> None:
    """Record the rung walk on a gate-passed escalation result.

    Called from the ONE place that knows both which model failed and which
    rung passed. The provenance is what makes the label checkable: without it
    a caller can only see that escalation was ATTEMPTED, which is how a run
    that never left the free lane reported itself as escalated.
    """
    if not isinstance(result, dict):
        return
    result.setdefault("escalated_to", to_model)
    result.setdefault("escalated_from", from_model)
    result.setdefault("escalation_rungs", list(rungs or []))
    result.setdefault("escalation_family", model_family(to_model))
    result.setdefault("escalated_from_family", model_family(from_model))
    result.setdefault(
        "escalation_family_changed",
        bool(from_model) and model_family(from_model) != model_family(to_model))


def confidence_to_start_rung(confidence, ladder_size, *,
                             current_rung=0):
    """Map calibrated Jev confidence to a starting rung (JEV-P2-dead-code).

    Pure, code-owned arithmetic over the TWO non-terminal capability buckets
    (``decide_probe_verify_escalate`` currently knows TIER_1_DISTILLER and
    TIER_2_FRONTIER; a ladder rung index is its production form).
    ``confidence`` is confidence in ANOTHER ATTEMPT AT THE CURRENT TIER
    (the exact signal ``should_abstain``/``decide_probe_verify_escalate``
    consume -- low confidence means escalate):

    - confidence < 0.5   -> the current tier is hopeless: climb toward the
      ladder's most capable rung (the frontier bucket)
    - 0.5 <= conf < 0.70 -> marginal: one rung above the current one (never
      re-buys the rung whose gate just failed)
    - confidence >= 0.70 -> retry-shaped: start at rung 0

    The 0.70 boundary IS ``decide_probe_verify_escalate``'s default
    ``min_confidence``, so the bucket and the tier decision can never
    disagree (TIER_2 ⇔ escalate/retry buckets, TIER_1 ⇔ retry-shaped).

    Always clamped into ``[0, ladder_size - 1]``; ``current_rung`` is the
    last rung whose gate already failed (falls back to 0). Returns
    ``(start_rung, bucket)`` with the bucket string carried for evidence.
    """
    try:
        size = max(int(ladder_size), 0)
        cur = int(current_rung)
    except (TypeError, ValueError):
        return 0, "escalate"
    cur = max(cur, 0)
    conf = float(confidence)
    if conf < 0.5:
        bucket = "escalate"
    elif conf < 0.70:
        bucket = "retry"
    else:
        bucket = "retry-shaped"
    if bucket == "escalate":
        # 0.0..0.5 -> the last 1-2 rungs of a real ladder; on the two-bucket
        # production shape this IS the frontier bucket.
        rung = size - 1 - (0 if conf < 0.075 else 1) if size >= 3 else size - 1
    elif bucket == "retry":
        rung = min(cur + 1, size - 1)
    else:
        rung = 0
    return max(0, min(rung, size - 1)) if size else 0, bucket


def jev_escalation_directive(result, *, ladder_size, current_rung=0,
                             condensed_context=""):
    """One code-owned directive from the Jev escalation-decision signals.

    Feeds the REAL P2 decision functions: ``decide_probe_verify_escalate``
    consumes the decision noul's calibrated probability (the ``confidence``
    that function's signature always meant) and ``should_abstain`` consumes
    the budget noul. The resulting tier decision maps onto ladder rung
    indexes via :func:`confidence_to_start_rung`.

    ``result`` is the :class:`~harness.jev_evaluation.JevEvaluationResult`
    from ``JevPolicy.evaluate_escalation_decision`` (noul pack: each answer
    carries its probability under ``noul``). A LOW decision noul is the
    Jev "current tier is hopeless" verdict -- the primary escalation
    trigger -- so the envelope's generic pass/fail verdict is not consulted;
    only ``is_fallback`` (unkeyed/transport/parse failure) and noul validity
    gate the signal.

    Returns ``None`` (caller keeps the status-quo walk) whenever the Jev
    signal is unavailable or unrunnable: no result, a fallback result, or
    missing/invalid nouls. Fail-closed by design -- the generative verify
    lane's verdicts remain the fallback trigger.

    An ``abstain`` directive (``should_abstain`` fired on the budget noul)
    is returned with ``kind="abstain"``: the driver retires the walk for
    this failure instead of spending a rung, unless the verify lane already
    directed one (lane priority).

    ``condensed_context`` is the code-owned failure evidence (verify-output
    tail, attempt history) the caller already assembled for the policy
    call; it rides the escalate directive verbatim and is prepended for
    rungs after the first by the driver's rung-context builder.
    """
    if result is None or getattr(result, "is_fallback", True):
        return None
    answers = getattr(result, "answers", None)
    if not isinstance(answers, dict):
        return None
    decision = answers.get("escalation_decision")
    if not isinstance(decision, dict):
        return None
    confidence = decision.get("noul")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        return None
    budget = answers.get("capability_budget")
    budget_noul = (float(budget["noul"])
                   if isinstance(budget, dict)
                   and isinstance(budget.get("noul"), (int, float))
                   and not isinstance(budget.get("noul"), bool)
                   else None)
    if budget_noul is not None and should_abstain(budget_noul):
        # Jev abstains: do not spend an escalation rung on this failure.
        return {"kind": "abstain", "confidence": confidence,
                "budget_noul": budget_noul, "decision": "abstain"}
    tier = decide_probe_verify_escalate(
        "escalation", confidence=confidence, structural_valid=True,
        current_tier=1, min_confidence=0.70)
    start_rung, bucket = confidence_to_start_rung(
        confidence, ladder_size, current_rung=current_rung)
    # Coherent by construction on one axis (0.70 is the decision function's
    # own min_confidence): TIER_2 (conf < 0.70) pairs with the escalate/retry
    # buckets; TIER_1 (conf >= 0.70) pairs with retry-shaped (rung 0).
    marker = (f"[JEV-DIRECTED ESCALATION] Jev confidence {confidence:.2f} "
              f"({bucket}); failure evidence follows.")
    condensed = (f"{marker}\n{condensed_context}" if condensed_context
                 else marker)
    return {"kind": "escalate", "confidence": confidence,
            "budget_noul": budget_noul,
            "tier": int(tier), "decision": bucket,
            "start_rung": start_rung,
            "condensed_context": condensed}


def escalation_evidence(result):
    """The evidence contract for a REAL escalated rung walk.

    Returns provenance only when the engine reports a gate-passed escalation
    whose final model belongs to a different pool family than the primary it
    replaced -- i.e. the escalation the operator was promised actually ran.
    Returns ``None`` for: no escalation, an escalation whose attempt failed
    (no gate pass), a walk that stayed inside one family (free rung to free
    rung), or a result that predates the provenance fields. Callers must not
    stamp an "escalated" label on anything this refuses.
    """
    if not isinstance(result, dict) or not result.get("escalated"):
        return None
    to_model = str(result.get("escalated_to") or "").strip()
    from_model = str(result.get("escalated_from") or "").strip()
    if not to_model or not from_model:
        return None
    to_family, from_family = model_family(to_model), model_family(from_model)
    if to_family == from_family:
        return None
    return {
        "model": to_model,
        "family": to_family,
        "from_model": from_model,
        "from_family": from_family,
        "rungs": [str(m) for m in (result.get("escalation_rungs") or [])],
    }


def escalation_evidence_fields(evidence):
    """The envelope fields a reader verifies an escalation claim with.

    Empty when there is no evidence, so an unescalated run carries no
    escalated_* keys at all (a reader can never mistake an absent field for a
    claim, and never mistake a present one for a handoff note).
    """
    if not evidence:
        return {}
    return {
        "escalated_model": evidence["model"],
        "escalated_from_model": evidence["from_model"],
        "escalation_family": evidence["family"],
        "escalation_rungs": list(evidence["rungs"]),
    }


class EscalationDriver:
    """Walk the escalation ladder and finish each candidate through the gate."""

    def __init__(self, router, transport, api_key, governor, ledger, task_id,
                 reasoning_token_budget=0.4, max_tokens=4096,
                 task_start_spent=None, task_max_cost=None, reasoning_effort=None):
        self.router = router
        self.transport = transport
        self.api_key = api_key
        self.governor = governor
        self.ledger = ledger
        self.task_id = task_id
        self.reasoning_token_budget = reasoning_token_budget
        self.max_tokens = max_tokens
        # Lane policy (ONE owner: config.effective_lane_policy): an escalation
        # rung is deep adjudication ("bigger/better when hard") -- minimum
        # 8192 output tokens with "auto" reasoning; an explicitly selected
        # low/medium/high effort passes through.
        self.max_tokens, self.reasoning_effort = effective_lane_policy(
            "escalation", max_tokens=max_tokens,
            reasoning_effort=reasoning_effort)
        self.task_start_spent = task_start_spent
        self.task_max_cost = task_max_cost
        self.escalation_history = []

    def _within_task_budget(self):
        """True when another escalation call still fits under --task-max-cost."""
        if self.task_max_cost is None or self.task_start_spent is None:
            return True
        spent_now = float(getattr(self.governor, "spent", 0.0) or 0.0)
        return (spent_now - float(self.task_start_spent)) < float(self.task_max_cost)

    def run_with_escalation(self, req, state, base_prompt_fn, finish_fn):
        """Try each escalation rung until a gated success or the ladder ends.

        ``base_prompt_fn(state, rung_context) -> prompt``
        ``finish_fn(model, content, cost) -> terminal result | None``
        """
        allowed = req.allow_escalation if req.allow_escalation is not None \
            else self.router.allow_escalation
        if not allowed or not self.router.escalation_pool:
            return None

        # Priority (JEV-P2-dead-code): the generative verify lane's directive
        # wins when it already picked a valid resume rung; a pending
        # Jev-directed directive (decision noul confidence) seeds the walk
        # only when the lane did not. Code owns the mapping; Jev owns the
        # judgment; the ladder stays the escalated executor.
        condensed = getattr(state, "escalation_condensed_context", "") or ""
        target = getattr(state, "de_escalation_target_rung", 0) or 0
        lane_directed = bool(condensed) and 0 <= target < len(self.router.escalation_pool)
        pending = getattr(state, "pending_jev_directive", None)
        state.pending_jev_directive = None  # one-shot: consume on first walk
        jev_directed = bool(pending) and not lane_directed
        if jev_directed and pending.get("kind") == "abstain":
            # Jev-directed abstention (should_abstain fired on the budget
            # noul): retire the walk for this failure instead of spending a
            # rung. The verify lane still owns future escalation verdicts.
            _events.emit("jev_escalation_directive", task_id=self.task_id,
                         start_rung=None,
                         confidence=pending.get("confidence"),
                         decision="abstain")
            return None
        if jev_directed:
            rung = int(pending.get("start_rung") or 0)
            if 0 <= rung < len(self.router.escalation_pool):
                state.de_escalation_target_rung = rung
                if not condensed:
                    state.escalation_condensed_context = str(
                        pending.get("condensed_context") or "")
                condensed = state.escalation_condensed_context
                target = rung
                pending["applied"] = True
                _events.emit("jev_escalation_directive", task_id=self.task_id,
                             start_rung=rung,
                             confidence=pending.get("confidence"),
                             decision=pending.get("decision"))
            else:
                jev_directed = False
        # de_escalation_target_rung is the resume rung; escalation starts there
        # when the verify lane OR a Jev-directed decision asked us to.
        start_rung = 0
        if (condensed or jev_directed) and 0 <= target < len(self.router.escalation_pool):
            start_rung = target

        for rung in range(start_rung, len(self.router.escalation_pool)):
            if not self._within_task_budget():
                eprint("[escalation] task budget exhausted; stopping ladder.")
                break
            if not self.router.de_escalate_to_rung(rung):
                break
            esc_spec = self.router.escalation(override=True)
            if not esc_spec:
                break
            model = esc_spec["model"]
            _events.emit("escalation_rung", task_id=self.task_id, model=model,
                         rung=rung, ladder_size=len(self.router.escalation_pool))

            rung_context = self._get_rung_context(state, rung, condensed)
            prompt = base_prompt_fn(state, rung_context)

            slots = _chat_reservation_slots(model, self.reasoning_effort, 0)
            calls = [(f"escalation rung {rung + 1}/{len(self.router.escalation_pool)}",
                      model, self.max_tokens, 0)
                     for _ in range(slots)]
            try:
                self.governor.preflight(prompt, calls)
            except HarnessError as exc:
                eprint(f"[escalation] rung {rung} refused by spend governor: {exc}")
                break

            status, resp = chat(self.transport, self.api_key, model,
                                [{"role": "user", "content": prompt}],
                                self.max_tokens, self.reasoning_effort,
                                self.reasoning_token_budget, self.governor)

            if status != 200:
                error_cost = 0.0
                error = str(resp)
                try:
                    if isinstance(resp, dict):
                        error_cost = float(resp.get("error", {}).get("cost", 0) or 0)
                        error = str(resp.get("error", {}).get("message", resp))
                except (TypeError, ValueError):
                    pass
                if error_cost:
                    self.governor.record_actual(error_cost, model)
                eprint(f"[escalation] rung {rung} model {model}: HTTP {status}; {error}")
                state.rounds.append({
                    "phase": "escalation", "rung": rung, "model": model,
                    "status": "api_error", "error": error,
                    "cost": error_cost, "content": "",
                })
                self._ledger(model, rung, "error", error_cost, reason=error)
                # Saturation is not impossibility: a rate-limited (429) or
                # overloaded (503) rung rotates to the next, more capable
                # rung -- that is the ladder's whole point. Anything else
                # (auth, transport, provider 4xx) fails closed: stop here.
                if status in (429, 503):
                    continue
                break

            content, finish, cost, is_byok = extract_content_and_cost(resp)
            if is_byok and not self.governor.is_free(model):
                self.governor.record_byok(model)
                eprint(f"[escalation] rung {rung} model {model}: BYOK-routed (paid); rotating.")
                state.rounds.append({
                    "phase": "escalation", "rung": rung, "model": model,
                    "status": "byok_error", "error": "paid BYOK route",
                    "cost": 0.0, "content": "",
                })
                self._ledger(model, rung, "error", 0.0, reason="paid BYOK route")
                continue

            self.governor.record_actual(cost, model)
            # A capability deferral is not file content -- hand it to finish_fn
            # (which routes HARNESS_DEFER) instead of treating it as a write.
            usable, unusable = assess_output(content, finish, allow_truncated=False)
            if not usable:
                eprint(f"[escalation] rung {rung} model {model}: {unusable}; rotating.")
                state.rounds.append({
                    "phase": "escalation", "rung": rung, "model": model,
                    "status": "unusable", "error": unusable, "cost": cost,
                    "content": "",
                })
                self._ledger(model, rung, "error", cost, reason=unusable)
                continue

            self.escalation_history.append({
                "rung": rung, "model": model, "cost": cost,
            })
            state.rounds.append({
                "phase": "escalation", "rung": rung, "model": model,
                "status": "ok", "cost": cost, "content": content,
            })
            self._ledger(model, rung, "ok", cost)

            result = finish_fn(model, content, cost)
            if result and result.get("status") == "ok":
                # The walk SUCCEEDED here: this is the only place that knows
                # both the primary that failed (req.model) and the rung that
                # passed the gate. Provenance lands here -- never at a
                # caller's handoff, which cannot tell a rung walk from a
                # request that simply got routed.
                _annotate_escalation(
                    result, from_model=getattr(req, "model", None),
                    to_model=model,
                    rungs=[h["model"] for h in self.escalation_history])
                return result
            # Gate failed or capability-deferred: try the next (more capable) rung
            # unless finish_fn already produced a terminal deferral.

        return None

    def _get_rung_context(self, state, rung, condensed):
        if rung == 0 or not condensed:
            return ""
        return (f"\n\n[ESCALATION CONTEXT FROM JUDGE - RUNG {rung}]\n"
                f"{condensed}\n")

    def _ledger(self, model, rung, status, cost, reason=None):
        if not (self.ledger and self.task_id):
            return
        self.ledger.append(
            "model_result", task_id=self.task_id,
            event_note=f"escalation_rung_{rung}",
            model=model, task_type="code",
            json_expected=False, json_ok=None,
            status=status, cost=cost, reported_cost=cost,
            **({"reason": reason} if reason else {}),
        )
