"""Bounded public-site check behavior and Jev route selection."""

from typing import Any, Dict, Optional

from .errors import HarnessError, ToolCancelled
from .events import emit
from .history import save_chat_turn
from .session import jev_face_governor, jev_for, ledger_for


class SimpleActionLifecycleMixin:
    """Own the bounded, read-only site-check lane."""

    def _run_simple_action_lifecycle(
        self, prompt: str, session_id: Optional[str] = None, cancel_check=None
    ) -> Dict[str, Any]:
        """Use one typed Choice before the probe, then one evidence Noul.

        The deterministic classifier identifies a candidate site-check
        request; Jev selects whether it really belongs in the one-probe
        simple-action tier. Other Jev choices do not trigger an OpenRouter
        or plan escalation from this bounded lane.
        """
        sid = session_id or "default"
        if not self._live_jev_available():
            return self.run_simple_action(prompt, session_id=sid, cancel_check=cancel_check)

        from .web import extract_site_target as _extract_target
        from .web import preflight_public_site as _preflight

        target = _extract_target(prompt)
        if target is None:
            return self.run_simple_action(prompt, session_id=sid, cancel_check=cancel_check)
        emit("simple_action_start", prompt=prompt, session_id=sid)
        emit("simple_action_preflight_start", target=target)
        try:
            preflight_plan = _preflight(target, cancel_check=cancel_check)
        except HarnessError as exc:
            return self._simple_action_preflight_refusal(prompt, sid, target, str(exc))
        emit("simple_action_preflight_complete", target=target)

        policy = jev_for(
            self.settings,
            transport=self.transport,
            governor=jev_face_governor(self.settings),
            ledger=ledger_for(self.settings, caller="agent"),
            request_timeout=10.0,
            single_attempt=True,
        )
        emit("simple_action_route_start", session_id=sid)
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        try:
            route_result, _route_structural = policy.evaluate_request_workflow(
                {"request": prompt}, task_id=sid, max_input_tokens=512
            )
        except ToolCancelled:
            raise
        except Exception as exc:
            emit("simple_action_route_unavailable", error_type=type(exc).__name__)
            result = {
                "status": "deferred",
                "intent": "simple-action",
                "workflow_tier": "simple-action",
                "prompt": prompt,
                "response": (
                    "Inconclusive -- Jev could not select the "
                    "site-check workflow; no request was sent."
                ),
                "model": None,
                "cost": 0.0,
                "defer_reason": "Jev workflow selection was unavailable",
                "next_step": "retry the bounded site-check request",
                "jev": {"workflow_tier": None, "is_fallback": True},
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("simple_action_complete", status="deferred", reason_code="jev_route_unavailable")
            return result

        route_answer = (
            route_result.answers.get("workflow_tier")
            if isinstance(route_result.answers, dict)
            else None
        )
        choice = route_answer.get("choice") if isinstance(route_answer, dict) else None
        if choice not in {
            "answer",
            "simple-action",
            "workspace-inspection",
            "web-research",
            "plan",
            "clarify",
        }:
            choice = None
        fallback = bool(getattr(route_result, "is_fallback", True))
        emit("simple_action_route_complete", selected=choice, fallback=fallback)
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        if fallback or choice != "simple-action":
            reason = (
                "Jev did not select the bounded site-check workflow"
                if not fallback
                else "Jev did not provide a usable workflow choice"
            )
            result = {
                "status": "deferred",
                "intent": "simple-action",
                "workflow_tier": "simple-action",
                "prompt": prompt,
                "response": ("Inconclusive -- " + reason + "; no request was sent."),
                "model": getattr(route_result, "model", None),
                "cost": float(getattr(route_result, "cost", 0.0) or 0.0),
                "defer_reason": reason,
                "next_step": "rephrase as a single current site-availability check",
                "jev": {"workflow_tier": choice, "is_fallback": fallback},
            }
            save_chat_turn(sid, result, self.history_dir)
            emit(
                "simple_action_complete",
                status="deferred",
                reason_code="jev_route_not_simple_action",
            )
            return result
        return self.run_simple_action(
            prompt,
            session_id=sid,
            cancel_check=cancel_check,
            jev_policy=policy,
            workflow_choice=choice,
            prior_jev_cost=float(getattr(route_result, "cost", 0.0) or 0.0),
            preflight_plan=preflight_plan,
            started=True,
        )

    def _simple_action_preflight_refusal(self, prompt, session_id, target, reason):
        try:
            ledger_for(self.settings, caller="agent").append(
                "simple_action",
                task_id=session_id,
                event_note="preflight",
                status="refused",
                reason=reason,
                target=target,
                cost=0.0,
            )
        except Exception:
            pass
        result = {
            "status": "deferred",
            "intent": "simple-action",
            "workflow_tier": "simple-action",
            "prompt": prompt,
            "response": f"I can't check that site: {reason}",
            "model": None,
            "cost": 0.0,
            "target": target,
            "defer_reason": reason,
            "next_step": "provide a public https URL without credentials",
        }
        save_chat_turn(session_id, result, self.history_dir)
        emit("simple_action_probe_refused", target=target, reason_code="preflight_refused")
        emit("simple_action_complete", status="deferred", reason_code="preflight_refused")
        return result

    def run_simple_action(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        cancel_check=None,
        probe_fn=None,
        resolve_fn=None,
        jev_policy=None,
        workflow_choice=None,
        prior_jev_cost: float = 0.0,
        preflight_plan=None,
        started=False,
    ) -> Dict[str, Any]:
        """Execute one bounded read-only site-availability check (#202/#203).

        Deterministic capability preflight runs before any model call; a
        known mismatch returns an honest terminal result plus a ledger event
        with zero model or decomposition calls. On success exactly one
        bounded HTTPS probe runs -- no repo triage, DAG, or waist work --
        and the verdict comes from observed probe evidence, never from
        search snippets. ``probe_fn``/``resolve_fn`` are hermetic seams."""
        from .web import extract_site_target as _extract_target
        from .web import probe_public_site as _probe

        sid = session_id or "default"
        if not started:
            emit("simple_action_start", prompt=prompt, session_id=sid)
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        target = _extract_target(prompt)
        if target is None:
            result = {
                "status": "deferred",
                "intent": "simple-action",
                "workflow_tier": "simple-action",
                "prompt": prompt,
                "response": (
                    "I could not find a site to check in that request. "
                    "Name a URL or host such as https://example.com."
                ),
                "model": None,
                "cost": 0.0,
                "defer_reason": "no site target in prompt",
                "next_step": "rephrase with a URL or host to check",
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("simple_action_complete", status="deferred", reason_code="no_site_target")
            return result
        emit("simple_action_probe_start", target=target)
        try:
            probe_kwargs = {"cancel_check": cancel_check}
            if probe_fn is not None:
                probe_kwargs.update(resolve_fn=resolve_fn, connect_fn=probe_fn)
            elif resolve_fn is not None:
                probe_kwargs["resolve_fn"] = resolve_fn
            if preflight_plan is not None:
                probe_kwargs["preflight_plan"] = preflight_plan
            probe = _probe(target, **probe_kwargs)
        except HarnessError as e:
            return self._simple_action_preflight_refusal(prompt, sid, target, str(e))
        emit(
            "simple_action_probe_complete",
            target=target,
            verdict=probe.get("verdict"),
            http_status=probe.get("http_status"),
            latency_s=probe.get("latency_s"),
        )
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        verdict = probe.get("verdict")
        http_status = probe.get("http_status")
        response_received = (
            isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
        )
        latency = probe.get("latency_s")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
            latency = None
        status_class = f"{http_status // 100}xx" if response_received else None
        if response_received:
            if verdict == "denied":
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status}. This confirms endpoint "
                    "reachability; page access was denied."
                )
            elif verdict == "redirect":
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status} redirect; it is reachable, "
                    "but the destination was not checked."
                )
            elif verdict in ("server_error", "rate_limited", "http_error"):
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status}; it is reachable but returned "
                    "an HTTP error."
                )
            else:
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status}; it is reachable."
                )
        else:
            evidence_summary = (
                f"No HTTP response was received; the probe ended with "
                f"{verdict or 'an unknown transport result'}."
            )
        probe_evidence = {
            "host": target,
            "probe_outcome": "http_response"
            if response_received
            else (verdict if verdict in ("timeout", "network") else "no_http_response"),
            "http_status": http_status if response_received else None,
            "http_status_received": response_received,
            "http_status_class": status_class,
            "probe_verdict": verdict,
            "evidence_summary": evidence_summary,
            "latency_s": latency,
        }
        action_ledger = getattr(jev_policy, "ledger", None)
        jev_result = None
        try:
            if action_ledger is None:
                action_ledger = ledger_for(self.settings, caller="agent")
            # This route only resolves the local OpenRouter key for spend
            # accounting; JevEvaluator sends the sole model request to
            # System One. The site check never calls OpenRouter.
            policy = jev_policy or jev_for(
                self.settings,
                transport=self.transport,
                governor=jev_face_governor(self.settings),
                ledger=action_ledger,
                request_timeout=10.0,
                single_attempt=True,
            )
            emit("simple_action_jev_start", target=target)
            jev_result, _structural = policy.evaluate_site_reachability(
                probe_evidence, task_id=sid, max_input_tokens=512
            )
        except Exception as exc:
            emit("simple_action_jev_unavailable", error_type=type(exc).__name__)
        answer = (
            jev_result.answers.get("http_response_received")
            if jev_result and isinstance(jev_result.answers, dict)
            else None
        )
        probability = answer.get("noul") if isinstance(answer, dict) else None
        if (
            jev_result is None
            or jev_result.is_fallback
            or isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not 0.0 <= float(probability) <= 1.0
        ):
            probability = None
        else:
            probability = float(probability)
        emit(
            "simple_action_jev_complete",
            target=target,
            http_response_received=probability,
            fallback=bool(getattr(jev_result, "is_fallback", True)),
        )

        answer_text = None
        if response_received and probability is not None and probability >= 0.99:
            status = "ok"
            answer_text = "yes"
            response = "Yes."
        elif not response_received and probability is not None and probability <= 0.01:
            status = "ok"
            answer_text = "no"
            response = "No."
        else:
            status = "deferred"
            if response_received:
                reason = (
                    f"HTTPS {http_status} was received, but Jev's "
                    "reachability judgment did not meet the 99% yes threshold"
                )
            else:
                reason = (
                    f"the probe returned no HTTPS status ({verdict or 'unknown'}), "
                    "so it cannot establish that the site is up"
                )
            if probability is None:
                reason += "; Jev did not provide a usable Noul judgment"
            else:
                reason += f" (Jev yes probability: {probability:.3f})"
            response = f"Inconclusive -- {reason}."
        try:
            if action_ledger is None:
                action_ledger = ledger_for(self.settings, caller="agent")
            action_ledger.append(
                "simple_action",
                task_id=sid,
                event_note="probe",
                status=status,
                verdict=verdict,
                target=target,
                http_status=probe.get("http_status"),
                latency_s=probe.get("latency_s"),
                jev_noul=probability,
                jev_model=getattr(jev_result, "model", None),
                jev_fallback=bool(getattr(jev_result, "is_fallback", True)),
                cost=0.0,
            )
        except Exception:
            pass
        result = {
            "status": status,
            "intent": "simple-action",
            "workflow_tier": "simple-action",
            "prompt": prompt,
            "answer": answer_text,
            "response": response,
            "model": getattr(jev_result, "model", None),
            "cost": (float(prior_jev_cost or 0.0) + float(getattr(jev_result, "cost", 0.0) or 0.0)),
            "target": target,
            "probe": probe,
            "jev": {
                "workflow_tier": workflow_choice,
                "http_response_received": probability,
                "threshold_yes": 0.99,
                "threshold_no": 0.01,
                "is_fallback": bool(getattr(jev_result, "is_fallback", True)),
            },
            **({"defer_reason": response} if status != "ok" else {}),
        }
        save_chat_turn(sid, result, self.history_dir)
        emit("simple_action_complete", status=status, answer=answer_text, verdict=verdict)
        return result
