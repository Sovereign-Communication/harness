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
from .chat import (_chat_reservation_slots, chat, extract_content_and_cost,
                   assess_output)
from .output import eprint


class EscalationDriver:
    """Walk the escalation ladder and finish each candidate through the gate."""

    def __init__(self, router, transport, api_key, governor, ledger, task_id,
                 reasoning_token_budget=0.4, max_tokens=4096):
        self.router = router
        self.transport = transport
        self.api_key = api_key
        self.governor = governor
        self.ledger = ledger
        self.task_id = task_id
        self.reasoning_token_budget = reasoning_token_budget
        self.max_tokens = max_tokens
        self.escalation_history = []

    def run_with_escalation(self, req, state, base_prompt_fn, finish_fn):
        """Try each escalation rung until a gated success or the ladder ends.

        ``base_prompt_fn(state, rung_context) -> prompt``
        ``finish_fn(model, content, cost) -> terminal result | None``
        """
        allowed = req.allow_escalation if req.allow_escalation is not None \
            else self.router.allow_escalation
        if not allowed or not self.router.escalation_pool:
            return None

        # Honor the judge's preferred starting rung when the verify lane
        # already attached a condensed context (target_rung 0 is the first
        # paid/capable rung in the ladder).
        start_rung = 0
        condensed = getattr(state, "escalation_condensed_context", "") or ""
        target = getattr(state, "de_escalation_target_rung", 0) or 0
        # de_escalation_target_rung is the resume rung; escalation starts there
        # when a prior plan asked us to. Otherwise start at 0.
        if condensed and 0 <= target < len(self.router.escalation_pool):
            start_rung = target

        for rung in range(start_rung, len(self.router.escalation_pool)):
            if not self.router.de_escalate_to_rung(rung):
                break
            esc_spec = self.router.escalation(override=True)
            if not esc_spec:
                break
            model = esc_spec["model"]

            rung_context = self._get_rung_context(state, rung, condensed)
            prompt = base_prompt_fn(state, rung_context)

            slots = _chat_reservation_slots(model, "high", 0)
            calls = [(f"escalation rung {rung + 1}/{len(self.router.escalation_pool)}",
                      model, self.max_tokens, 0)
                     for _ in range(slots)]
            self.governor.preflight(prompt, calls)

            status, resp = chat(self.transport, self.api_key, model,
                                [{"role": "user", "content": prompt}],
                                self.max_tokens, "high",
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
                # Fail closed: stop the ladder on transport/auth failure.
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
                return result
            # Gate failed: try the next (more capable) rung.

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
