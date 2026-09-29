"""Apply-engine internals: per-round flow, consent, rotation, escalation.

One owner for the engine's round machinery -- billing, the edit loop,
consent offer/renewal, judge context, candidate selection, the attempt
round, honest deferrals, diff merge, and the escalation ladder. Methods
here are mixed into :class:`harness.apply.ApplyEngine` verbatim, so every
call site and test keeps its shape; apply.py owns the lifecycle surface
(construction, apply_edit entry, batch dispatch) and this module owns the
per-round flow. Prompt text stays in :mod:`harness.prompts`; filesystem
and gate-execution policy stays in :mod:`harness.filesafety` and
:mod:`harness.apply_gate`.
"""
from .apply_state import AttemptOutcome, RunState
from .prompts import (
    CAPABILITY_MARKER,
    _extract_file_content, _parse_ready, _apply_unified_diff,
)
from .results import (_defer_result, _http_error, _round_entry,
                       model_envelope)
from .chat import (
    chat, extract_content_and_cost, _extract_json,
    REASONING_FALLBACK_PREFIX, _reported_cost, _chat_reservation_slots,
)
from . import events as _events
from .consent import (
    probe_consent, consent_renew, make_consent_binding,
    consent_binding_is_fresh, consent_binding_changes,
)
from .errors import HarnessError, ToolCancelled
from .output import eprint
from .prompts import build_apply_prompt, consent_mechanics_text
from .tokens import estimate_prompt_tokens
from .escalation import (EscalationDriver, _annotate_escalation,
                         jev_escalation_directive)

VERIFY_FEEDBACK_CHARS = 6000


class ApplyEngineMixin:
    """Mixin: the apply engine's per-round machinery (see module docstring)."""

    @staticmethod
    def _attach_envelopes(result, state):
        """Keep the shared structural + model-selection (MS) envelopes on
        every apply terminal -- one seam, both envelopes."""
        if isinstance(result, dict):
            if state.structural is not None:
                result.setdefault("structural", state.structural)
            rounds = result.get("rounds") or state.rounds
            result.update(model_envelope(
                model_requested=state.model_requested,
                model_observed=[r.get("model") for r in rounds
                                if isinstance(r, dict)]))
        return result

    def _record_billable(self, req, model_id, amount, status, **fields):
        """Record every billable apply attempt, including rotated failures."""
        try:
            amount = float(amount or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        event_fields = {
            "model": model_id, "task_type": "code", "json_expected": False,
            "json_ok": None, "status": status, "cost": amount,
            "tracked_cost": amount, "backend": req.backend,
        }
        event_fields.update(fields)
        try:
            self.governor.record_actual(amount, model_id)
        except HarnessError:
            # Preserve an auditable attempted charge without claiming it was
            # tracked. The governor remains authoritative for the hard key
            # ceiling, so the ledger's tracked total still equals spent.
            rejected = dict(event_fields)
            rejected["status"] = "rejected"
            rejected["cost"] = 0.0
            rejected["tracked_cost"] = 0.0
            rejected["reported_cost"] = amount
            self.ledger.append("model_result", task_id=req.task_id,
                               event_note="apply", **rejected)
            raise
        self.ledger.append("model_result", task_id=req.task_id,
                           event_note="apply", **event_fields)
        if self.governor.spent - req.task_start_spent > req.task_max_cost:
            raise HarnessError(
                f"actual task cost would exceed --task-max-cost ${req.task_max_cost:.6f}; refusing")

    # ---------------- orchestration ---------------------------------------

    def _apply_edit(self, req):
        """The round loop: initial consent, then per round -- renewed consent,
        retry context, candidate selection, dispatch, merge, preview/write +
        gate -- then gated escalation and the honest terminal assembly.
        Single exit per outcome; every mutation lives in ``state``."""
        state = RunState(
            model_requested=req.model,
            rounds=list(req.continuation.get("history") or []),
            history=list(req.continuation.get("history") or []),
            # Capability deferrals keep un-gated partial output out of the
            # target, but it is still useful context for the next model. The
            # target hash remains the original on-disk baseline; only the
            # in-memory proposal starts from this saved partial.
            current_content=req.continuation.get("partial_content") or req.original)
        state.consent_binding = req.continuation.get("consent_binding")
        if not req.want_consent:
            state.consent_binding = None
        state.candidates = self._candidate_models(req)
        first_model = next(iter(state.candidates), req.model)
        consent = self._initial_consent(req, state, first_model)
        if consent is not None and consent.get("decision") != "accept":
            if req.continuation:
                return self._consent_deferral(req, state, consent.get("reason"))
            return {"status": "consent_blocked", "task_id": req.task_id, **consent}
        state.consent_attempts = (consent.get("attempts", [])
                                  if isinstance(consent, dict) else [])

        dispatch_started = False
        for round_no in range(1, req.max_rounds + 1):
            if req.cancel_check and req.cancel_check():
                raise ToolCancelled()
            state.round_no = round_no
            if req.renew:
                round_model = next((m_ for m_ in state.candidates
                                    if m_ not in state.failed_models
                                    and m_ not in state.deferred_models), first_model)
                deferral = self._renew_consent(req, state, round_model)
                if deferral is not None:
                    return self._attach_envelopes(deferral, state)

            if not dispatch_started:
                self.ledger.append(
                    "dispatch_start", task_id=req.task_id, model=req.model,
                    continuation=bool(req.continuation),
                    worker_model=first_model,
                    consent_binding=(state.consent_binding or {}).get("digest"))
                dispatch_started = True

            state.round_ctx, state.gate_broken = self._round_context(req, state)
            if state.gate_broken:
                break

            state.candidates = self._candidate_models(req)
            attempt_model = next(
                (m_ for m_ in state.candidates if m_ not in state.failed_models), None)
            if attempt_model is None:
                # Pool exhausted (e.g. the cheap tier saturated with 429s):
                # the escalation ladder may still have rungs -- walk it or
                # land the honest terminal failure. Never a bare raise here:
                # that would bypass the ladder entirely and kill the task
                # while the lowest paid rung was still untried.
                break

            outcome = self._attempt_round(req, state, attempt_model)
            if outcome.consent_blocked:
                return self._attach_envelopes(
                    self._consent_deferral(req, state, outcome.last_defer_reason),
                    state)
            if outcome.model_used is None:
                if outcome.last_defer_reason is not None:
                    # A readiness defer is evidence that the current rung
                    # cannot finish the task, not a terminal handoff. Give
                    # the configured paid ladder its chance before returning
                    # the honest deferral envelope.
                    escalated = self._escalate(req, state)
                    if escalated is not None:
                        return escalated
                    return self._attach_envelopes(
                        self._readiness_deferral(req, state, outcome), state)
                state.rounds.append(_round_entry(
                    round_no, req.model or outcome.model, "api_error",
                    cost=_reported_cost(outcome.resp), verify_output="",
                    error=(outcome.last_error or "no model reachable")))
                break

            if CAPABILITY_MARKER in outcome.content:
                deferred = self._capability_deferral(req, state, outcome)
                escalated = self._escalate(req, state)
                return self._attach_envelopes(escalated or deferred, state)

            if req.backend == "diff":
                # Strict-match merge (#11): a non-matching or malformed diff is
                # never written; it is recorded as a failed attempt WITH the
                # error as next-round feedback (the promise the strict contract
                # was built on -- dogfooding caught it propagating as FATAL).
                new_content = self._merge_or_extract(req, state, outcome)
                if new_content is None:
                    continue
            else:
                new_content = _extract_file_content(outcome.content)
            if req.want_consent or req.renew:
                write_binding = self._consent_binding(
                    req, state, outcome.model_used)
                if not consent_binding_is_fresh(
                        state.consent_binding, write_binding):
                    self.ledger.append(
                        "consent_stale_before_write", task_id=req.task_id,
                        package_id=req.package_id,
                        accepted_binding=(state.consent_binding or {}).get("digest"),
                        current_binding=write_binding["digest"],
                        events=sorted(event.value for event in
                                      consent_binding_changes(
                                          state.consent_binding, write_binding)))
                    return self._attach_envelopes(
                        self._consent_deferral(
                            req, state,
                            "consent binding changed before the candidate write"),
                        state)
            result = self.gate.apply_candidate(req, state, outcome, new_content)
            if result is not None:
                return self._attach_envelopes(result, state)

        result = self._escalate(req, state) or self.gate.terminal_failure(req, state)
        return self._attach_envelopes(result, state)

    # ---------------- phases ----------------------------------------------

    def _consent_binding(self, req, state, worker_model):
        return make_consent_binding(
            file_path=req.file_path, source_content=state.current_content,
            instruction=req.instruction, context=req.consent_context,
            package_id=req.package_id, selected_model=worker_model,
            max_tokens=req.max_tokens, token_budget=req.token_budget,
            task_max_cost=req.task_max_cost,
            run_max_cost=getattr(self.governor, "max_cost", None))

    def _consent_task(self, req, state, worker_model, binding):
        token_budget = req.token_budget
        input_limit = (getattr(token_budget, "max_input_tokens", None)
                       if token_budget is not None else None)
        output_limit = (getattr(token_budget, "max_output_tokens", None)
                        if token_budget is not None else None)
        mechanics = consent_mechanics_text(
            req.file_path, state.current_content, req.instruction)
        package = (
            "\n\nEXACT DISPATCH PACKAGE:\n"
            f"Package: {req.package_id or req.task_id}\n"
            f"Selected worker: {worker_model}\n"
            f"Binding SHA-256: {binding['digest']}\n"
            f"Maximum output tokens per call: {req.max_tokens}\n"
            f"Stage maximum input tokens: {input_limit}\n"
            f"Stage maximum output tokens: {output_limit}\n"
            f"Task dollar ceiling: {req.task_max_cost}\n"
            f"Run dollar ceiling: {getattr(self.governor, 'max_cost', None)}\n"
            "This consent authorizes only this package, source, worker, and limits."
        )
        return mechanics + package

    def _initial_consent(self, req, state, worker_model):
        """Authorize this exact worker package before the first dispatch."""
        if not req.want_consent:
            return None
        return self._authorize_package(req, state, worker_model)

    def _authorize_package(self, req, state, worker_model, *, force=False):
        """Authorize the exact current package before provider dispatch.

        A prior acceptance is reusable only for an identical, integrity-checked
        binding. Rotations, continuation changes, or new source content require
        an explicit new decision before the worker call.
        """
        if not req.want_consent and not req.renew and not force:
            return None
        binding = self._consent_binding(req, state, worker_model)
        previous = state.consent_binding
        if (not force and consent_binding_is_fresh(previous, binding)):
            return {"task_id": req.task_id, "model": worker_model,
                    "decision": "accept", "dispatched": True,
                    "reason": "unchanged consent binding", "attempts": [],
                    "binding": binding}
        changes = consent_binding_changes(previous, binding)
        if ((previous is not None and changes)
                or (req.continuation and previous is None)):
            self.ledger.append(
                "consent_stale", task_id=req.task_id,
                package_id=req.package_id,
                previous_binding=previous.get("digest")
                if isinstance(previous, dict) else None,
                proposed_binding=binding["digest"],
                events=sorted(event.value for event in changes))
        consent_task = self._consent_task(req, state, worker_model, binding)
        consent_context = req.consent_context
        if previous is not None or force:
            consent_model = (
                getattr(self.router, "cheap_judge", None)
                if getattr(self.router, "cheap_judge", None)
                and not req.allow_escalation else self.router.judge)
            consent = consent_renew(
                transport=self.transport, api_key=self.api_key,
                governor=self.governor, task_id=req.task_id,
                task=consent_task, model=consent_model,
                context=consent_context, ledger=self.ledger, required=True,
                fallback_pool=[
                    model for model in self.router.panel_pool
                    if model not in {
                        attempt.get("model")
                        for attempt in (state.consent_attempts or [])
                        if attempt.get("status") == "error"}],
                min_confidence=req.min_confidence,
                token_budget=req.token_budget)
        else:
            consent = probe_consent(
                transport=self.transport, api_key=self.api_key,
                governor=self.governor, task_id=req.task_id,
                task=consent_task, model=self.router.judge,
                context=consent_context, ledger=self.ledger, required=True,
                fallback_pool=self.router.panel_pool,
                min_confidence=req.min_confidence,
                token_budget=req.token_budget)
        if self.governor.spent - req.task_start_spent > req.task_max_cost:
            raise HarnessError(
                f"consent cost exceeded task ceiling ${req.task_max_cost:.6f}; refusing to dispatch")
        if consent.get("decision") == "accept":
            state.consent_binding = binding
            consent["binding"] = binding
            state.consent_attempts = consent.get("attempts", [])
            self.ledger.append(
                "consent_binding", task_id=req.task_id,
                package_id=req.package_id,
                worker_model=worker_model, binding=binding["digest"],
                changed_files=binding["changed_files"],
                context=binding["context"], instruction=binding["instruction"],
                token_limit=binding["token_limit"],
                monetary_limit=binding["monetary_limit"])
        return consent

    def _renew_consent(self, req, state, worker_model):
        """Continue consensus at a round boundary and renew the package bind."""
        consent = self._authorize_package(
            req, state, worker_model, force=True)
        if consent is None or consent.get("decision") == "accept":
            return None
        reason = consent.get("reason") or "consent was deferred or declined"
        self.ledger.append("defer_midtask", task_id=req.task_id, category="consent",
                           reason=reason, confidence=consent.get("confidence"),
                           model=consent.get("model") or self.router.judge)
        return self._consent_deferral(req, state, reason)

    def _consent_deferral(self, req, state, reason):
        return _defer_result(
            task_id=req.task_id, file_path=req.file_path, category="consent",
            reason=reason or "consent blocked dispatch",
            remaining_scope=req.instruction, rounds=state.rounds,
            history=state.history, cost=self.governor.spent,
            backend=req.backend, verify_only=req.verify_only,
            max_lines=req.max_lines, edit_snippet=req.edit_snippet,
            verify_cmd=req.verify_cmd,
            partial_content=(state.current_content
                             if state.current_content != req.original else None),
            consent_binding=state.consent_binding,
            token_budget_remaining=self._token_budget_remaining(req),
            task_cost_remaining=self._task_cost_remaining(req))

    @staticmethod
    def _token_budget_remaining(req):
        if req.token_budget is None:
            return None
        snapshot = req.token_budget.snapshot()
        return {
            "max_input_tokens": snapshot["remaining_input_tokens"],
            "max_output_tokens": snapshot["remaining_output_tokens"],
        }

    def _task_cost_remaining(self, req):
        remaining = max(0.0, req.task_max_cost
                        - (self.governor.spent - req.task_start_spent))
        run_max = getattr(self.governor, "max_cost", None)
        if run_max is not None:
            remaining = min(remaining, max(0.0, run_max - self.governor.spent))
        return remaining

    def _round_context(self, req, state):
        """Retry context for this round, plus the broken-gate stop: a gate
        that fails with the SAME output on consecutive rounds is broken (bad
        path, missing dependency, wrong interpreter), not something the model
        can fix by rewording code -- stop instead of burning the remaining
        rounds on it. Only REAL gate outputs participate: merge-failure
        feedback is model error, not gate evidence (two identical merge
        errors must retry with feedback, not abort), and history entries
        restored from a continuation carry no verify_output; an all-empty
        pair must not read as 'the gate failed identically'.
        Returns (round_ctx, gate_broken); records the stop when broken."""
        if not state.rounds:
            return None, False
        last = state.rounds[-1]
        tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
        prev_outputs = [r.get("verify_output") or "" for r in state.rounds
                        if r.get("status") == "verify_failed"]
        if (len(prev_outputs) >= 2 and prev_outputs[-1] == prev_outputs[-2]
                and prev_outputs[-1]):
            state.rounds.append(_round_entry(
                state.round_no, req.model or "(none)", "gate_broken",
                cost=0.0, verify_output=tail,
                reason="verification gate failed identically "
                       "on consecutive rounds; the gate itself "
                       "appears broken, not the edit"))
            state.history.append({"round": state.round_no, "status": "gate_broken"})
            return None, True
        closing = ("Return the corrected unified diff against the current content."
                   if req.backend == "diff" else
                   "Return the corrected COMPLETE file content.")
        round_ctx = (
            "Your previous attempt was applied but did not pass verification.\n"
            f"Verification command: {req.verify_cmd}\n"
            f"Last {VERIFY_FEEDBACK_CHARS} chars of output:\n```\\n{tail}\\n```\\n\\n"
            + closing)
        return round_ctx, False

    def _candidate_models(self, req):
        """Rotation order for this round: the primary followed by the pool,
        minus ids the live catalog does not know. Pools are live-validated at
        the public routing boundary, but a library caller can still supply
        stale ids. Exclude those ids from the reservation and rotation
        sequence rather than discovering the problem only after a failed
        model has already consumed a call."""
        pool_order = req.ordered if req.profiles is not None else self.router.apply_pool
        candidates = []
        for m_ in ([req.model] + list(pool_order)):
            if m_ not in candidates:
                candidates.append(m_)
        try:
            known_models = {entry.get("id") for entry in self.governor.fetch_models()}
            candidates = [m_ for m_ in candidates if m_ in known_models]
        except HarnessError:
            pass
        return candidates

    def _attempt_round(self, req, state, attempt_model):
        """Dispatch candidates until one yields usable content or the
        rotation budget is spent. Records every billable attempt; rotation
        across the pool on error / BYOK / reasoning-only / readiness-defer.
        Returns the attempt outcome; ``model_used`` is None when no model
        produced usable content."""
        outcome = AttemptOutcome(model=attempt_model)
        while attempt_model is not None:
            if req.cancel_check and req.cancel_check():
                raise ToolCancelled()
            consent = (self._authorize_package(req, state, attempt_model)
                       if req.want_consent or req.renew else None)
            if consent is not None and consent.get("decision") != "accept":
                outcome.consent_blocked = True
                outcome.last_defer_reason = consent.get("reason") or (
                    "consent blocked the assigned worker")
                return outcome
            prompt = build_apply_prompt(req.file_path, req.instruction, req.edit_snippet,
                                        state.current_content, state.round_ctx,
                                        req.continuation, backend=req.backend)
            a_pp, a_cp = self.governor.fetch_pricing([attempt_model])[attempt_model]
            est = estimate_prompt_tokens(prompt)
            slots = _chat_reservation_slots(attempt_model, req.reasoning, 0)
            attempt_max_tokens = req.max_tokens
            if req.token_budget is not None:
                attempt_max_tokens = min(
                    attempt_max_tokens, req.token_budget.remaining_output())
            if attempt_max_tokens <= 0:
                raise HarnessError(
                    "execution token budget has no output allowance remaining; refusing dispatch")
            per_call_estimate = slots * (est * a_pp + attempt_max_tokens * a_cp)
            jev_worst = 0.0
            if getattr(self, "jev_policy", None) is not None and getattr(self.jev_policy, "keyed", False):
                from .jev import jev_cost
                from .jev_policy import JEV_MAX_INPUT_TOKENS
                jev_worst = jev_cost(JEV_MAX_INPUT_TOKENS)
            if (self.governor.spent - req.task_start_spent
                    + per_call_estimate + jev_worst > req.task_max_cost):
                raise HarnessError(
                    f"task worst-case ${self.governor.spent - req.task_start_spent + per_call_estimate + jev_worst:.6f} "
                    f"exceeds --task-max-cost ${req.task_max_cost:.6f}. Refusing.")
            if jev_worst > 0.0 and hasattr(self.governor, "preflight_jev"):
                self.governor.preflight_jev(JEV_MAX_INPUT_TOKENS, label="apply_candidate")
            # This reservation is made immediately before every candidate,
            # including dynamically rotated models and reasoning fallbacks.
            # The actual-cost guard in _record_billable remains authoritative
            # if provider billing exceeds the live pricing estimate.
            self.governor.preflight(
                prompt,
                [(f"apply attempt {i + 1}/{slots}", attempt_model,
                  attempt_max_tokens, 0)
                 for i in range(slots)],
            )
            _events.emit("attempt_start", task_id=req.task_id, model=attempt_model,
                         round=state.round_no, backend=req.backend)
            status, resp = chat(self.transport, self.api_key, attempt_model,
                                [{"role": "user", "content": prompt}], attempt_max_tokens,
                                req.reasoning, self.reasoning_token_budget,
                                self.governor, token_budget=req.token_budget,
                                token_label=f"apply:{req.task_id}")
            outcome.resp = resp
            if status != 200:
                err = _http_error(status, resp)
                outcome.last_error = err
                self._record_billable(req, attempt_model, _reported_cost(resp), "error",
                                      error=err, http_status=status,
                                      retryable=status == 429)
                state.failed_models.add(attempt_model)
                state.rotations += 1
                eprint(f"[apply] {attempt_model} FAILED: {err} -- rotating.")
                _events.emit("rotation", task_id=req.task_id, model=attempt_model,
                             reason="http_error", http_status=status, error=err,
                             round=state.round_no)
            else:
                content, _, cost, is_byok = extract_content_and_cost(resp)
                outcome.cost = cost
                if is_byok and not self.governor.is_free(attempt_model):
                    # Paid BYOK route: spend is invisible to the tracked key.
                    self.governor.record_byok(attempt_model)
                    self.ledger.append(
                        "model_result", task_id=req.task_id, event_note="apply",
                        model=attempt_model, task_type="code", json_expected=False,
                        json_ok=None, status="error", cost=0.0, tracked_cost=0.0,
                        reported_cost=cost, backend=req.backend,                        reason="paid BYOK route")
                    outcome.last_error = "model is BYOK-routed (paid); no tracked output"
                    state.failed_models.add(attempt_model)
                    state.rotations += 1
                    eprint(f"[apply] {attempt_model} is BYOK-routed (paid); recorded and rotating.")
                    _events.emit("rotation", task_id=req.task_id, model=attempt_model,
                                 reason="paid_byok", round=state.round_no)
                elif not content or content.startswith(REASONING_FALLBACK_PREFIX):
                    # No usable output: a reasoning-only response must NOT be
                    # treated as file content (it would corrupt the target).
                    self._record_billable(req, attempt_model, cost, "error",
                                          reason="no usable content")
                    outcome.last_error = ("model returned a reasoning-only response "
                                          "(no usable content)")
                    state.failed_models.add(attempt_model)
                    state.rotations += 1
                    eprint(f"[apply] {attempt_model} returned no content (reasoning-only); rotating.")
                    _events.emit("rotation", task_id=req.task_id, model=attempt_model,
                                 reason="reasoning_only", round=state.round_no)
                else:
                    ready, ready_reason, content = _parse_ready(content)
                    if ready == "defer":
                        self._record_billable(req, attempt_model, cost, "deferred",
                                              readiness="defer",
                                              reason=ready_reason or "model declared not ready")
                        self.ledger.append("readiness", task_id=req.task_id,
                                           model=attempt_model, round=state.round_no,
                                           decision="defer")
                        # The model declines on capability grounds; rotate to the
                        # next pool model before accepting the deferral.
                        state.deferred_models[attempt_model] = ready_reason
                        outcome.last_defer_reason = ready_reason or "model declared not ready"
                        state.rotations += 1
                        eprint(f"[apply] {attempt_model} declares HARNESS_READY: defer "
                               f"({(ready_reason or '')[:70]}) -- rotating.")
                        _events.emit("rotation", task_id=req.task_id, model=attempt_model,
                                     reason="readiness_defer", detail=ready_reason,
                                     round=state.round_no)
                    else:
                        self._record_billable(req, attempt_model, cost, "ok", readiness=ready)
                        if ready == "missing":
                            eprint(f"[apply] {attempt_model} did not emit HARNESS_READY; "
                                   f"treating as confident (verify + DEFER still guard).")
                            _events.emit("readiness", task_id=req.task_id,
                                         model=attempt_model, round=state.round_no,
                                         decision="missing")
                        else:
                            _events.emit("readiness", task_id=req.task_id,
                                         model=attempt_model, round=state.round_no,
                                         decision="confident")
                            self.ledger.append("readiness", task_id=req.task_id,
                                               model=attempt_model, round=state.round_no,
                                               decision="confident")
                        outcome.model_used = attempt_model
                        outcome.content = content
                        outcome.ready = ready
                        break
            if state.rotations > req.max_rot:
                break
            attempt_model = next(
                (m_ for m_ in state.candidates
                 if m_ not in state.failed_models and m_ not in state.deferred_models),
                None)
        outcome.model = attempt_model
        return outcome

    def _readiness_deferral(self, req, state, outcome):
        """Every reachable model declined on readiness grounds; accept the
        deferral with partial work preserved."""
        reason = outcome.last_defer_reason
        self.ledger.append("defer_midtask", task_id=req.task_id, category="readiness",
                           reason=reason, model=req.model or outcome.model)
        state.rounds.append(_round_entry(state.round_no, req.model or outcome.model,
                                         "deferred", cost=0.0, verify_output="",
                                         reason=reason))
        state.history.append({"round": state.round_no, "model": req.model or outcome.model,
                              "status": "deferred", "reason": reason})
        return _defer_result(
            task_id=req.task_id, file_path=req.file_path, category="readiness",
            reason=reason, remaining_scope=req.instruction,
            rounds=state.rounds, history=state.history, cost=self.governor.spent,
            backend=req.backend, verify_only=req.verify_only, max_lines=req.max_lines,
            edit_snippet=req.edit_snippet, verify_cmd=req.verify_cmd,
            partial_content=(state.current_content
                             if state.current_content != req.original else None),
            consent_binding=state.consent_binding,
            token_budget_remaining=self._token_budget_remaining(req),
            task_cost_remaining=self._task_cost_remaining(req))

    def _capability_deferral(self, req, state, outcome):
        """The model hit its capability limit (HARNESS_DEFER marker): stop,
        preserve the partial in the continuation state -- never the tree."""
        round_no = state.round_no
        head, _, tail = outcome.content.partition(CAPABILITY_MARKER)
        partial = _extract_file_content(head).strip("\n")
        info = _extract_json(tail)
        # Models often defer in prose rather than strict JSON; keep the
        # model's own words when we can't parse structured JSON.
        prose = " ".join(tail.strip().split())[:200] if tail.strip() else ""
        reason = (info or {}).get("reason") or prose or "model reached its capability limit"
        remaining = (info or {}).get("remaining_scope") or prose or req.instruction
        # Dogfood finding (round-1 self-apply): a deferred run must
        # NEVER write its partial to the target -- no gate has seen
        # it, and a later run in the continuation chain silently
        # builds on ungated code (this corrupted config.py mid-chain
        # and the corruption surfaced as an unrelated verify_failed).
        # The partial travels in the result/state instead.
        deferred_partial = None
        if partial and partial != state.current_content and not req.verify_only:
            deferred_partial = partial
        self.ledger.append("defer_midtask", task_id=req.task_id, category="capability",
                           reason=reason, model=outcome.model_used)
        state.rounds.append(_round_entry(round_no, outcome.model_used, "deferred",
                                         cost=outcome.cost, verify_output="", reason=reason))
        state.history.append({"round": round_no, "model": outcome.model_used,
                              "status": "deferred", "reason": reason})
        return _defer_result(
            task_id=req.task_id, file_path=req.file_path, category="capability",
            reason=reason, remaining_scope=remaining, rounds=state.rounds,
            history=state.history, cost=self.governor.spent,
            backend=req.backend, verify_only=req.verify_only, max_lines=req.max_lines,
            edit_snippet=req.edit_snippet, verify_cmd=req.verify_cmd,
            partial_content=deferred_partial,
            consent_binding=state.consent_binding,
            token_budget_remaining=self._token_budget_remaining(req),
            task_cost_remaining=self._task_cost_remaining(req))

    def _merge_or_extract(self, req, state, outcome):
        """Diff backend only: merge the proposed unified diff. A malformed or
        non-matching diff is recorded as a failed attempt (with the error as
        next-round feedback) and returns None; the caller continues."""
        try:
            return _apply_unified_diff(state.current_content, outcome.content)
        except HarnessError as merge_err:
            state.rounds.append(_round_entry(state.round_no, outcome.model_used,
                                             "merge_failed",
                                             cost=outcome.cost,
                                             verify_output=f"diff merge failed: {merge_err}",
                                             changed=False, verify_passed=None))
            state.history.append({"round": state.round_no, "model": outcome.model_used,
                                  "status": "merge_failed"})
            return None

    def _escalate(self, req, state):
        """Multi-rung escalation when a ladder is configured; else legacy rung."""
        if req.verify_only or not req.verify_cmd or state.gate_broken:
            return None
        if not self.router.escalation_pool:
            return self._escalate_legacy(req, state)
        allowed = req.allow_escalation if req.allow_escalation is not None \
            else self.router.allow_escalation
        if not allowed:
            return None

        # Seed judge condensed context / preferred rung from the verify lane
        # so later rungs actually see the failure guidance.
        for rnd in reversed(state.rounds):
            esc = rnd.get("escalation") or {}
            if isinstance(esc, dict) and esc.get("needed"):
                state.escalation_condensed_context = esc.get("condensed_context") or ""
                if esc.get("target_rung") is not None:
                    state.de_escalation_target_rung = int(esc.get("target_rung") or 0)
                break
            # Specialist directives also live under the verify result.
            spec = rnd.get("specialist") or {}
            if isinstance(spec, dict):
                esc = spec.get("escalation") or {}
                if isinstance(esc, dict) and esc.get("needed"):
                    state.escalation_condensed_context = esc.get("condensed_context") or ""
                    if esc.get("target_rung") is not None:
                        state.de_escalation_target_rung = int(esc.get("target_rung") or 0)
                    break

        # JEV-P2-dead-code (Jev-directed escalation): when the verify lane
        # gave nothing to steer with, ask the shared Jev policy whether the
        # next candidate should escalate, at what rung to resume, or abstain
        # entirely. The directive is parked on state for the driver (ONE
        # consumer); lane directives keep priority inside the driver.
        lane_directed = bool(getattr(state, "escalation_condensed_context", "") or "")
        if not lane_directed and self.jev_policy is not None:
            try:
                tail = (state.rounds[-1].get("verify_output") or "")[-600:] \
                    if state.rounds else ""
                failure_context = "\n".join(
                    [f"instruction: {req.instruction}",
                     f"rounds tried: {state.round_no}",
                     f"last verify output: {tail}"])
                jev_result, _structural = self.jev_policy.evaluate_escalation_decision(
                    failure_context, task_id=req.task_id)
                if jev_result is not None:
                    state.pending_jev_directive = jev_escalation_directive(
                        jev_result, ladder_size=len(self.router.escalation_pool),
                        current_rung=max(state.round_no - 1, 0),
                        condensed_context=failure_context)
            except HarnessError:
                state.pending_jev_directive = None

        def base_prompt_fn(st, rung_context):
            last = st.rounds[-1] if st.rounds else {}
            tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
            round_ctx = (
                "A cheaper model exhausted its retry budget without passing verification.\n"
                f"Verification command: {req.verify_cmd}\n"
                f"Last {VERIFY_FEEDBACK_CHARS} chars:\n```\\n{tail}\\n```\\n\\n"
                "Return the corrected COMPLETE file content.")
            if rung_context:
                round_ctx = rung_context + "\n\n" + round_ctx
            return build_apply_prompt(req.file_path, req.instruction, req.edit_snippet,
                                      st.current_content, round_ctx, req.continuation,
                                      backend=req.backend)

        def finish_fn(model, content, cost):
            if not content:
                return self.gate.finish_escalation(
                    req, state, model, state.current_content, cost, False)
            if CAPABILITY_MARKER in content:
                outcome = AttemptOutcome(
                    model=model, model_used=model, content=content, cost=cost)
                return self._capability_deferral(req, state, outcome)
            if req.backend == "diff":
                new_content = _apply_unified_diff(state.current_content, content)
                if new_content is None:
                    state.rounds.append(_round_entry(
                        "escalation", model, "verify_failed",
                        changed=False, verify_passed=False, cost=cost,
                        verify_output="escalation returned a non-matching unified diff"))
                    return None
            else:
                new_content = _extract_file_content(content)
            self.ledger.append("escalate", task_id=req.task_id,
                               from_model=req.model, to_model=model)
            return self.gate.finish_escalation(
                req, state, model, new_content, cost, bool(new_content))

        driver = EscalationDriver(
            router=self.router,
            transport=self.transport,
            api_key=self.api_key,
            governor=self.governor,
            ledger=self.ledger,
            task_id=req.task_id,
            reasoning_token_budget=self.reasoning_token_budget,
            # Apply keeps its normal 4096-token behavior; an ESCALATION rung
            # is deep adjudication and gets the synthesis lane budget (>=8192,
            # auto) inside the driver via the ONE lane-policy owner.
            max_tokens=req.max_tokens,
            reasoning_effort=req.reasoning,
            task_start_spent=req.task_start_spent,
            task_max_cost=req.task_max_cost,
        )
        return driver.run_with_escalation(req, state, base_prompt_fn, finish_fn)

    def _escalate_legacy(self, req, state):
        """Legacy single-rung escalation (kept for backward compatibility)."""
        if req.verify_only or not req.verify_cmd or state.gate_broken:
            return None
        esc = self.router.escalation(override=req.allow_escalation)
        if not (esc and state.rounds
                and state.rounds[-1].get("status") in ("verify_failed", "api_error")):
            return None
        self.ledger.append("escalate", task_id=req.task_id, from_model=req.model,
                           to_model=esc["model"])
        last = state.rounds[-1]
        tail = (last.get("verify_output") or "")[-VERIFY_FEEDBACK_CHARS:]
        round_ctx = (
            "A cheaper model exhausted its retry budget without passing verification.\n"
            f"Verification command: {req.verify_cmd}\n"
            f"Last {VERIFY_FEEDBACK_CHARS} chars:\n```\\n{tail}\\n```\\n\\n"
            "Return the corrected COMPLETE file content.")
        prompt = build_apply_prompt(req.file_path, req.instruction, req.edit_snippet,
                                    state.current_content, round_ctx, req.continuation,
                                    backend=req.backend)
        esc_slots = _chat_reservation_slots(esc["model"], "high", 0)
        e_pp, e_cp = self.governor.fetch_pricing([esc["model"]])[esc["model"]]
        est = estimate_prompt_tokens(prompt)
        esc_estimate = esc_slots * (est * e_pp + req.max_tokens * e_cp)
        jev_worst = 0.0
        if getattr(self, "jev_policy", None) is not None and getattr(self.jev_policy, "keyed", False):
            from .jev import jev_cost
            from .jev_policy import JEV_MAX_INPUT_TOKENS
            jev_worst = jev_cost(JEV_MAX_INPUT_TOKENS)
        if (self.governor.spent - req.task_start_spent
                + esc_estimate + jev_worst > req.task_max_cost):
            raise HarnessError(
                f"task worst-case ${self.governor.spent - req.task_start_spent + esc_estimate + jev_worst:.6f} "
                f"exceeds --task-max-cost ${req.task_max_cost:.6f}. Refusing.")
        if jev_worst > 0.0 and hasattr(self.governor, "preflight_jev"):
            self.governor.preflight_jev(JEV_MAX_INPUT_TOKENS, label="apply_escalation")
        self.governor.preflight(
            prompt,
            [(f"escalation attempt {i + 1}/{esc_slots}", esc["model"], req.max_tokens, 0)
             for i in range(esc_slots)],
        )
        status, resp = chat(self.transport, self.api_key, esc["model"],
                            [{"role": "user", "content": prompt}], req.max_tokens,
                            "high", self.reasoning_token_budget, self.governor,
                            token_budget=req.token_budget,
                            token_label=f"apply-escalation:{req.task_id}")
        if status != 200:
            err = _http_error(status, resp)
            self._record_billable(req, esc["model"], _reported_cost(resp), "error",
                                  escalation=True, error=err, http_status=status)
            state.rounds.append(_round_entry("escalation", esc["model"], "api_error",
                                             cost=_reported_cost(resp), verify_output="",
                                             error=_http_error(status, resp)))
            return None
        content, _, cost, is_byok = extract_content_and_cost(resp)
        if is_byok and not self.governor.is_free(esc["model"]):
            self.governor.record_byok(esc["model"])
            self.ledger.append(
                "model_result", task_id=req.task_id, event_note="escalation",
                model=esc["model"], task_type="code", json_expected=False,
                json_ok=None, status="error", cost=0.0, tracked_cost=0.0,
                reported_cost=cost, backend=req.backend, reason="paid BYOK route")
            content = None
            cost = 0.0
        else:
            usable = bool(content and not content.startswith(REASONING_FALLBACK_PREFIX))
            self._record_billable(req, esc["model"], cost,
                                  "ok" if usable else "error", escalation=True)
            if not usable:
                content = None
        new_content = _extract_file_content(content) if content else state.current_content
        result = self.gate.finish_escalation(
            req, state, esc["model"], new_content, cost, bool(content))
        if result and result.get("status") == "ok":
            # Same provenance contract as the ladder: a single-rung escalation
            # is a real rung walk too, so it must carry its evidence.
            _annotate_escalation(result, from_model=req.model,
                                 to_model=esc["model"], rungs=[esc["model"]])
        return result
