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
from .results import (_defer_result, _http_error, _round_entry)
from .chat import (
    chat, extract_content_and_cost, _extract_json,
    REASONING_FALLBACK_PREFIX, _reported_cost, _chat_reservation_slots,
)
from . import events as _events
from .consent import probe_consent, consent_renew
from .errors import HarnessError, ToolCancelled
from .output import eprint
from .prompts import build_apply_prompt, consent_mechanics_text
from .tokens import estimate_prompt_tokens
from .escalation import EscalationDriver

VERIFY_FEEDBACK_CHARS = 6000


class ApplyEngineMixin:
    """Mixin: the apply engine's per-round machinery (see module docstring)."""

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
            rounds=list(req.continuation.get("history") or []),
            history=list(req.continuation.get("history") or []),
            # Capability deferrals keep un-gated partial output out of the
            # target, but it is still useful context for the next model. The
            # target hash remains the original on-disk baseline; only the
            # in-memory proposal starts from this saved partial.
            current_content=req.continuation.get("partial_content") or req.original)
        consent = self._initial_consent(req)
        if consent is not None and consent.get("decision") != "accept":
            return {"status": "consent_blocked", "task_id": req.task_id, **consent}
        state.consent_attempts = (consent.get("attempts", [])
                                  if isinstance(consent, dict) else [])

        self.ledger.append("dispatch_start", task_id=req.task_id, model=req.model,
                           continuation=bool(req.continuation))

        for round_no in range(1, req.max_rounds + 1):
            if req.cancel_check and req.cancel_check():
                raise ToolCancelled()
            state.round_no = round_no
            if req.renew:
                deferral = self._renew_consent(req, state)
                if deferral is not None:
                    return deferral

            state.round_ctx, state.gate_broken = self._round_context(req, state)
            if state.gate_broken:
                break

            state.candidates = self._candidate_models(req)
            attempt_model = next(
                (m_ for m_ in state.candidates if m_ not in state.failed_models), None)
            if attempt_model is None:
                raise HarnessError("no available apply model (pool exhausted).")

            outcome = self._attempt_round(req, state, attempt_model)
            if outcome.model_used is None:
                if outcome.last_defer_reason is not None:
                    return self._readiness_deferral(req, state, outcome)
                state.rounds.append(_round_entry(
                    round_no, req.model or outcome.model, "api_error",
                    cost=_reported_cost(outcome.resp), verify_output="",
                    error=(outcome.last_error or "no model reachable")))
                break

            if CAPABILITY_MARKER in outcome.content:
                return self._capability_deferral(req, state, outcome)

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
            result = self.gate.apply_candidate(req, state, outcome, new_content)
            if result is not None:
                return result

        return self._escalate(req, state) or self.gate.terminal_failure(req, state)

    # ---------------- phases ----------------------------------------------

    def _initial_consent(self, req):
        """Sovereignty gate: the judge model accepts the visible work or the
        task never dispatches. Returns the consent record, or None when the
        probe is disabled or this is a continuation (already consented)."""
        if not req.want_consent or req.continuation:
            return None
        consent = probe_consent(
            transport=self.transport, api_key=self.api_key, governor=self.governor,
            task_id=req.task_id, task=consent_mechanics_text(
                req.file_path, req.original, req.instruction),
            model=self.router.judge, ledger=self.ledger, required=True,
            fallback_pool=self.router.panel_pool)
        if self.governor.spent - req.task_start_spent > req.task_max_cost:
            raise HarnessError(
                f"consent cost exceeded task ceiling ${req.task_max_cost:.6f}; refusing to dispatch")
        return consent

    def _renew_consent(self, req, state):
        """Continued consensus before each round; a revocation defers the task
        with partial work preserved. Returns a terminal deferral or None."""
        # Skip re-asking models already shown unable to answer the consent
        # probe this run (e.g. reasoning-only emitters): the primary just
        # fails again and the rotation ladder absorbs it.
        consent_unusable = {
            a["model"] for a in (state.consent_attempts or [])
            if a.get("status") == "error"}
        renew_pool = [m_ for m_ in self.router.panel_pool
                      if m_ not in consent_unusable]
        cr = consent_renew(
            transport=self.transport, api_key=self.api_key, governor=self.governor,
            task_id=req.task_id, task=consent_mechanics_text(
                req.file_path, state.current_content, req.instruction),
            model=self.router.judge, ledger=self.ledger, required=True,
            fallback_pool=renew_pool)
        if self.governor.spent - req.task_start_spent > req.task_max_cost:
            raise HarnessError(
                f"consent renewal exceeded task ceiling ${req.task_max_cost:.6f}; refusing to continue")
        if cr["decision"] != "accept":
            self.ledger.append("defer_midtask", task_id=req.task_id, category="consent",
                               reason=cr["reason"], model=self.router.judge)
            return _defer_result(
                task_id=req.task_id, file_path=req.file_path, category="consent",
                reason=cr["reason"], remaining_scope=req.instruction,
                rounds=state.rounds, history=state.history, cost=self.governor.spent,
                backend=req.backend, verify_only=req.verify_only, max_lines=req.max_lines,
                edit_snippet=req.edit_snippet, verify_cmd=req.verify_cmd)
        return None

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
            prompt = build_apply_prompt(req.file_path, req.instruction, req.edit_snippet,
                                        state.current_content, state.round_ctx,
                                        req.continuation, backend=req.backend)
            a_pp, a_cp = self.governor.fetch_pricing([attempt_model])[attempt_model]
            est = estimate_prompt_tokens(prompt)
            slots = _chat_reservation_slots(attempt_model, req.reasoning, 0)
            per_call_estimate = slots * (est * a_pp + req.max_tokens * a_cp)
            if (self.governor.spent - req.task_start_spent
                    + per_call_estimate > req.task_max_cost):
                raise HarnessError(
                    f"task worst-case ${self.governor.spent - req.task_start_spent + per_call_estimate:.6f} "
                    f"exceeds --task-max-cost ${req.task_max_cost:.6f}. Refusing.")
            # This reservation is made immediately before every candidate,
            # including dynamically rotated models and reasoning fallbacks.
            # The actual-cost guard in _record_billable remains authoritative
            # if provider billing exceeds the live pricing estimate.
            self.governor.preflight(
                prompt,
                [(f"apply attempt {i + 1}/{slots}", attempt_model, req.max_tokens, 0)
                 for i in range(slots)],
            )
            _events.emit("attempt_start", task_id=req.task_id, model=attempt_model,
                         round=state.round_no, backend=req.backend)
            status, resp = chat(self.transport, self.api_key, attempt_model,
                                [{"role": "user", "content": prompt}], req.max_tokens,
                                req.reasoning, self.reasoning_token_budget, self.governor)
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
            edit_snippet=req.edit_snippet, verify_cmd=req.verify_cmd)

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
            partial_content=deferred_partial)

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
        self.governor.preflight(
            prompt,
            [(f"escalation attempt {i + 1}/{esc_slots}", esc["model"], req.max_tokens, 0)
             for i in range(esc_slots)],
        )
        status, resp = chat(self.transport, self.api_key, esc["model"],
                            [{"role": "user", "content": prompt}], req.max_tokens,
                            "high", self.reasoning_token_budget, self.governor)
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
        return self.gate.finish_escalation(
            req, state, esc["model"], new_content, cost, bool(content))


