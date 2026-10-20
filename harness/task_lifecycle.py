"""Workspace task-lane adapters for audit, provisioning, and driver."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from .errors import ProviderUsageUnknown, ToolCancelled
from .events import emit
from .history import save_chat_turn
from .lifecycle import adapter_for
from .jev_policy import policy_for
from .session import jev_face_governor, ledger_for
from .workspace import require_workspace


class TaskLaneLifecycleMixin:
    """Route-local behavior over the shared lifecycle coordinator."""

    def _handle_audit(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
        lifecycle_state=None,
    ) -> Dict[str, Any]:
        adapter = adapter_for(self.lifecycle, "audit")
        state = lifecycle_state or adapter.begin(session_id, prompt)
        saved = state["data"].get("result")
        if isinstance(saved, dict) and state["status"] == "complete":
            return saved
        if cancel_check and cancel_check():
            exc = ToolCancelled("Prompt execution was cancelled by user")
            self.lifecycle.interrupt(state, exc)
            raise exc
        adapter.advance(state, "inspect")
        ledger = ledger_for(self.settings)
        ok, bad_seq = ledger.verify()
        chain_info = ledger.chain_status()
        msg = (f"Cryptographic ledger chain is clean and verified (0 quarantined "
               f"entries, latest seq {chain_info.get('latest_seq', 0)})." if ok
               else f"Ledger verification failed at record sequence {bad_seq}. "
                    "Check quarantined entries.")
        context = {"chain_status": chain_info, "verified": bool(ok),
                   "failed_sequence": bad_seq}
        adapter.advance(state, "review", context=context, candidate=msg)
        if self._live_jev_available():
            call_id = state["request_id"] + ":audit-review"
            if self.lifecycle.provider_started(state, call_id):
                try:
                    policy = policy_for(
                        self.settings, transport=self.transport,
                        governor=jev_face_governor(self.settings),
                        ledger=ledger_for(self.settings, caller="agent"))
                    jev_result, structural = policy.evaluate_answer(
                        prompt, msg, json.dumps(context, ensure_ascii=False),
                        site="audit-summary", task_id=state["request_id"])
                    supported = structural.get("answer_sufficient")
                    native = not bool(getattr(jev_result, "is_fallback", True))
                    self.lifecycle.provider_completed(
                        state, call_id, structural,
                        float(structural.get("cost") or 0.0))
                    self.lifecycle.review(state, call_id, {
                        "verdict": getattr(jev_result, "verdict", "fail"),
                        "native": native, "supported": supported,
                        "confidence": getattr(jev_result, "confidence", None),
                        "cost": structural.get("cost", 0.0),
                    }, confidence_threshold=0.8)
                except (ToolCancelled, ProviderUsageUnknown) as exc:
                    self.lifecycle.interrupt(state, exc, call_id=call_id)
                except Exception as exc:
                    emit("lifecycle_review_unavailable", lane="audit",
                         error_type=type(exc).__name__)
        result = {
            "status": "ok" if ok else "audit_failed",
            "intent": "audit", "prompt": prompt, "response": msg,
            "chain_status": chain_info,
            "cost": state["usage"]["known_cost"],
        }
        self.lifecycle.finish(state, "complete", candidate=msg, result=result)
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="audit", verified=ok)
        return result

    def _handle_provision(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
        lifecycle_state=None,
    ) -> Dict[str, Any]:
        """Plan host provisioning setup safely without automatic execution."""
        from . import provision as pv
        adapter = adapter_for(self.lifecycle, "provisioning")
        state = lifecycle_state or adapter.begin(session_id, prompt)
        prior = state["data"].get("plan")
        if isinstance(prior, dict):
            plan = pv.plan_from_dict(prior)
        else:
            if cancel_check and cancel_check():
                exc = ToolCancelled("Prompt execution was cancelled by user")
                self.lifecycle.interrupt(state, exc)
                raise exc
            adapter.advance(state, "probe")
            probe = pv.probe_host()
            root = str(require_workspace(self.root_dir))
            policy = pv.policy_for_probe(probe, [root])
            context = {"host": probe.jev_facts(), "workspace_root": root}
            adapter.advance(state, "plan", context=context)
            jev_policy = None
            if self._live_jev_available():
                jev_policy = policy_for(
                    self.settings, transport=self.transport,
                    governor=jev_face_governor(self.settings),
                    ledger=ledger_for(self.settings, caller="agent"))
            plan = pv.plan_provision(
                prompt, probe, policy=policy, jev=jev_policy,
                task_id=state["request_id"], lifecycle=self.lifecycle,
                lifecycle_state=state)
        rendered = pv.render_plan(plan)
        result = {
            "status": "planned",
            "intent": "provision",
            "prompt": prompt,
            "response": rendered,
            "plan": plan.to_dict(),
            "cost": state["usage"]["known_cost"],
        }
        state["data"]["plan"] = plan.to_dict()
        state["data"]["approval_state"] = {
            "review_required": bool(plan.review_required),
            "plan_digest": plan.digest,
        }
        state["execution"]["approval_state"] = dict(state["data"]["approval_state"])
        review = dict(plan.review or {})
        if review and not review.get("is_fallback", True):
            satisfies = review.get("satisfies_goal")
            exceeds = review.get("exceeds_goal")
            self.lifecycle.review(state, "provision-plan:" + plan.digest, {
                "verdict": "fail" if plan.review_required else "pass",
                "native": True, "supported": satisfies,
                "confidence": None,
                "iteration_required": bool(plan.review_required),
                "cost": 0.0,
            }, confidence_threshold=0.5)
            state["review"]["history"][-1]["confidence"] = (
                None if exceeds is None else max(0.0, min(1.0, 1.0 - float(exceeds))))
            state["review"]["confidence"] = state["review"]["history"][-1]["confidence"]
        self.lifecycle.finish(state, "complete", candidate=rendered, result=result)
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="provision", planned=True)
        return result

    def _handle_driver_task(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
        lifecycle_state=None,
    ) -> Dict[str, Any]:
        """Drive machine requests autonomously through Jev Driver across perception aspects."""
        from .server import run_driver_task
        adapter = adapter_for(self.lifecycle, "driver")
        state = lifecycle_state or adapter.begin(
            session_id, prompt, max_review_rounds=5)
        task_id = f"drv/{session_id or 'default'}"
        try:
            res = run_driver_task(
                task_id,
                {"goal": prompt, "max_steps": 5, "target": "cli",
                 "request_id": state["request_id"]},
                cancel_check=cancel_check,
                lifecycle=self.lifecycle, lifecycle_state=state,
            )
        except Exception as exc:
            self.lifecycle.interrupt(state, exc)
            raise
        total_steps = res.get("total_steps", 0)
        summary = res.get("summary") or f"Jev driver completed {total_steps} step(s)."
        steps = res.get("steps") or []
        step_lines = []
        for s in steps:
            env = s.get("envelope") or {}
            step_lines.append(
                f"- **Step #{s.get('step_number', 1)}** ({s.get('aspect', 'cli')} ➔ {s.get('target', 'cli')}): "
                f"stopped at `{env.get('stopped_at', 'complete')}`, "
                f"status `{'ok' if env.get('ok') else env.get('reason')}`"
            )
        body_text = f"{summary}\n\n" + "\n".join(step_lines) if step_lines else summary
        audit = res.get("audit") or {}
        if audit.get("ok"):
            body_text += (
                f"\n\n---\n**Cryptographic Audit:** Verified clean (0 quarantined entries, "
                f"status `{'ok' if audit.get('ok') else 'unverified'}`)."
            )
        result = {
            "status": res.get("status", "done"),
            "intent": "driver",
            "prompt": prompt,
            "response": body_text,
            "total_steps": total_steps,
            "steps": steps,
            "cost": res.get("total_cost_usd", 0.0),
        }
        if result["status"] == "cancelled":
            self.lifecycle.interrupt(
                state, ToolCancelled("Driver request was cancelled"))
        elif result["status"] == "needs_reconciliation":
            self.lifecycle.finish(state, "deferred", candidate=body_text,
                                  result=result)
        else:
            self.lifecycle.finish(state, "complete", result=result,
                                  candidate=body_text)
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="driver", status=result["status"], cost=result["cost"])
        return result
