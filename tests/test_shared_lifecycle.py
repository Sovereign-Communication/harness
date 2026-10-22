"""Behavior checks for the typed lifecycle across each task lane."""
import tempfile
import time
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from harness.history import (
    CHAT_CHECKPOINT_SCHEMA,
    load_chat_checkpoint,
    save_chat_checkpoint,
)
from harness.lifecycle import (
    LifecycleCoordinator,
    adapter_for,
    validate_lifecycle_state,
)
from harness import provision as pv
from harness.jev_policy import JevPolicy
from harness.ledger import AutonomyLedger
from harness.server import run_driver_task
from tests.test_provision import FakeRunner, fake_which


def _settings():
    settings = load_settings()
    settings.jev_api_key = None
    settings.jev_disabled = True
    settings.allow_escalation = False
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    return settings


class TypedLifecycleContractTests(unittest.TestCase):
    def test_parallel_effect_receipts_are_serialized_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = coordinator.begin("auto_edit", "parallel-edit", "edit files")
            worker_count = 12

            def record_effect(index):
                effect_id = f"edit:{index}"
                action, _ = coordinator.begin_effect(
                    state, effect_id, "edit.apply",
                    approval_state={"required": True})
                if action != "run":
                    return action
                coordinator.complete_effect(
                    state, effect_id, {"node": index, "status": "ok"})
                return "completed"

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                results = list(pool.map(record_effect, range(worker_count)))

            reloaded = coordinator.load("parallel-edit")
            self.assertEqual(results, ["completed"] * worker_count)
            self.assertEqual(
                set(reloaded["execution"]["effects"]),
                {f"edit:{index}" for index in range(worker_count)})
            self.assertTrue(all(
                effect["status"] == "completed"
                for effect in reloaded["execution"]["effects"].values()))

    def test_terminal_transition_settles_unknown_provider_usage_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "chat").begin(
                "usage-terminal", "answer the question")
            coordinator.provider_started(state, "answer-call", 0.025)
            coordinator.finish(state, "complete", candidate="answer")
            saved = coordinator.load("usage-terminal")
            self.assertEqual(saved["usage"]["provider_calls"][
                "answer-call"]["status"], "unknown")
            self.assertEqual(saved["usage"]["unknown_liability"], 0.025)

            bad = dict(saved)
            bad["usage"] = dict(saved["usage"])
            bad["usage"]["provider_calls"] = {
                "answer-call": dict(saved["usage"]["provider_calls"]["answer-call"])}
            bad["usage"]["provider_calls"]["answer-call"]["status"] = "dispatched"
            with self.assertRaises(HarnessError):
                validate_lifecycle_state(bad)

    def test_unknown_provider_liability_is_not_charged_again_at_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "audit").begin(
                "usage-unknown", "audit the ledger")
            coordinator.provider_started(state, "review-call", 0.01)
            coordinator.interrupt(
                state, ProviderUsageUnknown("usage response lost",
                                            reserved_cost=0.01),
                call_id="review-call")
            coordinator.finish(state, "complete", result={"status": "ok"})
            saved = coordinator.load("usage-unknown")
            self.assertEqual(saved["usage"]["unknown_liability"], 0.01)
            self.assertEqual(saved["usage"]["provider_calls"][
                "review-call"]["status"], "unknown")

    def test_provider_call_reuses_receipts_and_retries_unknown_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "chat").begin(
                "provider-receipt", "answer the question")
            calls = []
            result = coordinator.provider_call(
                state, "jev-review", lambda: calls.append("first") or
                {"verdict": "pass"}, cost=lambda _value: 0.002)
            replay = coordinator.provider_call(
                state, "jev-review",
                lambda: calls.append("replayed") or {"verdict": "fail"})
            self.assertEqual(result, replay)
            self.assertEqual(calls, ["first"])

            self.assertTrue(coordinator.provider_started(
                state, "answer", 0.004))
            coordinator.interrupt(
                state, ProviderUsageUnknown(
                    "usage response lost", reserved_cost=0.004),
                call_id="answer")
            resumed = coordinator.resume("provider-receipt")
            retry_calls = []
            retried = coordinator.provider_call(
                resumed, "answer", lambda: retry_calls.append("retry") or
                {"answer": "recovered"}, cost=lambda _value: 0.003,
                retry_unknown=True)
            saved_retry = coordinator.provider_call(
                resumed, "answer",
                lambda: retry_calls.append("replayed") or {},
                retry_unknown=True)
            saved = coordinator.load("provider-receipt")
            self.assertEqual(retried, saved_retry)
            self.assertEqual(retry_calls, ["retry"])
            self.assertEqual(saved["usage"]["unknown_liability"], 0.004)
            self.assertEqual(saved["usage"]["known_cost"], 0.005)
            self.assertEqual(saved["usage"]["provider_calls"][
                "answer"]["status"], "unknown")
            self.assertEqual(saved["usage"]["provider_calls"][
                "answer:retry:1"]["status"], "completed")

    def test_malformed_provider_probabilities_abstain_without_invalid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "audit").begin(
                "malformed-jev", "review the result")
            transition = coordinator.review(state, "bad-probability", {
                "verdict": "pass", "native": True,
                "supported": True, "confidence": float("nan"),
            })
            saved = coordinator.load("malformed-jev")
            self.assertEqual(transition.decision, "abstain")
            self.assertIsNone(saved["review"]["history"][0]["supported"])
            self.assertIsNone(saved["review"]["history"][0]["confidence"])

    def test_legacy_chat_reviews_are_reconstructed_as_typed_judgments(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = coordinator.adopt_chat_payload("legacy-chat", {
                "prompt": "Explain photosynthesis",
                "route": "hourglass",
                "status": "interrupted",
                "stage": "answer_review",
                "max_review_rounds": 3,
                # A legacy boolean threshold is invalid; adoption uses the
                # conservative default instead of treating True as numeric 1.
                "confidence_threshold": True,
                "rounds": [
                    "missing old review envelope",
                    {
                        "round": 2,
                        "jev": {
                            "native": True,
                            "verdict": "fail",
                            "confidence": {"observed": 0.82},
                            "supported": 0.81,
                            "answers": {
                                "iteration_required": False,
                                "plan_required": False,
                            },
                            "cost": 0.001,
                        },
                    },
                    {
                        "round": 3,
                        "native": False,
                        "confidence": True,
                        "supported": True,
                        "iteration_required": True,
                    },
                ],
            })

            self.assertEqual(state["review"]["max_rounds"], 3)
            self.assertEqual(state["review"]["rounds"], 2)
            self.assertEqual(
                [item["decision"] for item in state["review"]["history"]],
                ["pass", "revise"],
            )
            self.assertEqual(state["review"]["history"][0]["supported"], 0.82)
            self.assertIsNone(state["review"]["history"][1]["confidence"])
            self.assertIsNone(state["review"]["history"][1]["supported"])

    def test_lane_adapter_rejects_invalid_reviews_and_refuses_provider_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            adapter = adapter_for(coordinator, "chat")
            state = adapter.begin("provider-boundary", "answer safely")
            with self.assertRaisesRegex(HarnessError, "threshold"):
                adapter.review(
                    state, "bad-threshold", {"native": True},
                    confidence_threshold=True)

            invoked = []
            coordinator.provider_started(state, "in-flight")
            with self.assertRaisesRegex(HarnessError, "not replayed"):
                adapter.provider_call(
                    state, "in-flight", lambda: invoked.append("dispatch"),
                    retry_unknown=True)
            self.assertEqual(invoked, [])

            coordinator.interrupt(
                state, ProviderUsageUnknown("provider reply lost",
                                            reserved_cost=0.004),
                call_id="in-flight")
            resumed = adapter.resume("provider-boundary")
            with self.assertRaisesRegex(HarnessError, "not replayed"):
                adapter.provider_call(
                    resumed, "in-flight", lambda: invoked.append("retry"),
                    retry_unknown=False)
            self.assertEqual(invoked, [])

            race_state = adapter.begin("provider-race", "answer once")

            def concurrent_completion(_state, call_id):
                _state["usage"]["provider_calls"][call_id] = {
                    "status": "completed", "reserved_cost": 0.0,
                    "actual_cost": 0.001, "result": {"answer": "saved"},
                }
                return False

            with patch.object(coordinator, "provider_started",
                              side_effect=concurrent_completion):
                raced = adapter.provider_call(
                    race_state, "same-call", lambda: invoked.append("race"))
            self.assertEqual(raced, {"answer": "saved"})
            with patch.object(coordinator, "provider_started", return_value=False):
                with self.assertRaisesRegex(HarnessError, "not replayed"):
                    adapter.provider_call(
                        race_state, "unreserved", lambda: invoked.append("unreserved"))
            self.assertEqual(invoked, [])

    def test_lifecycle_rejects_non_boolean_resumable_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = coordinator.begin("chat", "bad-resume-marker", "answer")
            state["data"]["resumable"] = "yes"
            with self.assertRaisesRegex(HarnessError, "resumable state"):
                validate_lifecycle_state(state)

class SharedLaneBehaviorTests(unittest.TestCase):
    def test_chat_helpers_return_existing_terminal_and_recognize_only_bare_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), history_dir=Path(tmp))
            self.assertIsNone(agent._finish_chat_request(
                "missing-chat", {"status": "ok", "response": "done"}))
            self.assertTrue(agent._is_continue_command("Continue!"))
            self.assertFalse(agent._is_continue_command("continue with the tests"))

            state = agent.lifecycle.begin("chat", "terminal-chat", "say hello")
            agent.lifecycle.finish(state, "complete", candidate="Hello.")
            terminal = agent._finish_chat_request(
                "terminal-chat", {"status": "ok", "response": "Hello."})
            self.assertEqual(terminal["status"], "complete")
            self.assertEqual(terminal["candidate"], "Hello.")

    def test_chat_finalizer_does_not_finish_new_same_lane_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), history_dir=Path(tmp))
            original = agent.lifecycle.begin(
                "chat", "reused-session", "first request")
            replacement = agent.lifecycle.begin(
                "chat", "reused-session", "replacement request")

            result = agent._finish_chat_request(
                "reused-session", {"status": "ok", "response": "stale"},
                expected_request_id=original["request_id"])

            self.assertIsNone(result)
            saved = agent.lifecycle.load("reused-session")
            self.assertEqual(saved["request_id"], replacement["request_id"])
            self.assertEqual(saved["status"], "running")
            self.assertIsNone(saved["candidate"])

    def test_continuation_router_restores_legacy_chat_driver_and_provision_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_settings(), history_dir=root)

            legacy_path = agent.lifecycle._path("legacy-hourglass", create=True)
            legacy_path.write_text(json.dumps({
                "schema": CHAT_CHECKPOINT_SCHEMA,
                "session_id": "legacy-hourglass",
                "route": "hourglass", "status": "interrupted",
                "stage": "answer_review", "prompt": "Summarize this project",
                "answer": "A saved partial answer.",
            }), encoding="utf-8")
            with patch.object(agent, "run_hourglass_request",
                              return_value={"status": "ok"}) as hourglass:
                result = agent.run_prompt(
                    "continue", session_id="legacy-hourglass")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                hourglass.call_args.kwargs["resume_state"]["route"],
                "hourglass")

            for lane, handler_name in (
                    ("provisioning", "_handle_provision"),
                    ("driver", "_handle_driver_task")):
                session_id = f"resume-{lane}"
                state = adapter_for(agent.lifecycle, lane).begin(
                    session_id, f"continue {lane}")
                agent.lifecycle.interrupt(state, ToolCancelled("interrupted"))
                with patch.object(agent, handler_name,
                                  return_value={"status": "ok", "intent": lane}) as handler:
                    resumed_result = agent.run_prompt(
                        "continue", session_id=session_id)
                self.assertEqual(resumed_result["intent"], lane)
                self.assertEqual(
                    handler.call_args.kwargs["lifecycle_state"]["lane"], lane)

    def test_hourglass_review_continuation_reuses_answer_and_saved_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_settings(), history_dir=root)
            prompt = "What is 2 + 2?"
            agent.lifecycle.begin(
                "chat", "hourglass-resume", prompt, stage="strategy",
                data={"route": "simple-answer"})
            policy = MagicMock()
            policy.evaluate_request_workflow.return_value = (
                SimpleNamespace(
                    answers={"workflow_tier": {"choice": "answer"}},
                    is_fallback=False),
                {"cost": 0.0},
            )
            review_calls = []

            def review_once(*_args, **_kwargs):
                review_calls.append("review")
                if len(review_calls) == 1:
                    raise ToolCancelled("review interrupted after dispatch")
                return (
                    SimpleNamespace(verdict="pass", is_fallback=False,
                                     confidence=0.95),
                    {"cost": 0.001, "answer_sufficient": 0.95,
                     "iteration_required": False, "plan_required": False},
                )

            policy.evaluate_answer.side_effect = review_once
            brief = SimpleNamespace(estimated_tokens=5)
            answer_calls = []
            envelope = {
                "native": True, "is_fallback": False, "verdict": "pass",
                "confidence": {"observed": 0.95}, "supported": 0.95,
                "reasons": [],
            }
            preflight = {
                "source": "simple_strategy", "choice": "direct_answer",
                "native": False, "cost": 0.0,
            }
            with patch("harness.hourglass_lifecycle.governor_for",
                       return_value=("key", MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for",
                       return_value=MagicMock()), \
                 patch("harness.hourglass_lifecycle.policy_for",
                       return_value=policy), \
                 patch.object(agent, "_hourglass_request_context",
                              return_value=("retained context", {}, brief, [])) as context, \
                 patch.object(agent, "_hourglass_answer_once",
                              side_effect=lambda *_a, **_k: (
                                  answer_calls.append("answer") or {
                                      "answer": "2 + 2 = 4", "model": "cheap",
                                      "cost": 0.002,
                                  })), \
                 patch.object(agent, "_jev_answer_envelope",
                              return_value=envelope):
                with self.assertRaises(ToolCancelled):
                    agent.run_hourglass_request(
                        prompt, session_id="hourglass-resume", max_rounds=1,
                        request_preflight=preflight)

                interrupted = agent.lifecycle.load("hourglass-resume")
                self.assertEqual(interrupted["status"], "interrupted")
                self.assertEqual(interrupted["lane"], "chat")
                self.assertEqual(interrupted["candidate"], "2 + 2 = 4")
                self.assertEqual(interrupted["execution"]["effects"], {})

                result = agent.run_prompt(
                    "continue", session_id="hourglass-resume")

            saved = agent.lifecycle.load("hourglass-resume")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["response"], "2 + 2 = 4")
            self.assertEqual(answer_calls, ["answer"])
            self.assertEqual(context.call_count, 1)
            self.assertEqual(review_calls, ["review", "review"])
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["candidate"], "2 + 2 = 4")
            self.assertTrue(any(key.endswith(":retry:1") for key in
                                saved["usage"]["provider_calls"]))

    def test_hourglass_restores_legacy_checkpoint_after_corrupt_typed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_settings(), history_dir=root)
            session_id = "legacy-hourglass-restore"
            prompt = "What is 2 + 2?"
            agent.lifecycle._path(session_id, create=True).write_text(
                "{not a lifecycle", encoding="utf-8")
            checkpoint = {
                "route": "hourglass", "status": "interrupted",
                "stage": "jev_review", "prompt": prompt,
                "context": "saved context", "context_files": [],
                "context_tokens": 5, "file_hashes": {},
                "workspace_root": "", "context_updated_at": time.time(),
                "route_preflight": {
                    "source": "simple_strategy", "choice": "direct_answer",
                    "native": False, "cost": 0.0,
                },
                "answer": "2 + 2 = 4", "answer_model": "cheap",
                "answer_cost": 0.001, "known_cost": 0.001,
                "unknown_liability": 0.0, "usage_unknown": False,
                "next_round": 1, "rounds": [],
            }
            policy = MagicMock()
            policy.evaluate_answer.return_value = (
                SimpleNamespace(verdict="pass", is_fallback=False,
                                 confidence=0.95),
                {"cost": 0.001, "answer_sufficient": 0.95,
                 "iteration_required": False, "plan_required": False},
            )
            envelope = {
                "native": True, "is_fallback": False, "verdict": "pass",
                "confidence": {"observed": 0.95}, "supported": 0.95,
                "reasons": [],
            }
            with patch("harness.hourglass_lifecycle.governor_for",
                       return_value=("key", MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for",
                       return_value=MagicMock()), \
                 patch("harness.hourglass_lifecycle.policy_for",
                       return_value=policy), \
                 patch.object(agent, "_hourglass_request_context",
                              side_effect=AssertionError("saved context was lost")), \
                 patch.object(agent, "_hourglass_answer_once",
                              side_effect=AssertionError("saved answer was replayed")), \
                 patch.object(agent, "_jev_answer_envelope",
                              return_value=envelope):
                result = agent.run_hourglass_request(
                    prompt, session_id=session_id, max_rounds=1,
                    resume_state=checkpoint)

            saved = agent.lifecycle.load(session_id)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["response"], "2 + 2 = 4")
            self.assertEqual(saved["lane"], "chat")
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["candidate"], "2 + 2 = 4")
            self.assertFalse(agent.lifecycle._path(session_id).read_text(
                encoding="utf-8").startswith("{not a lifecycle"))

    def test_new_hourglass_request_replaces_corrupt_stale_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_settings(), history_dir=root)
            session_id = "fresh-hourglass-after-corrupt"
            prompt = "What is 2 + 2?"
            agent.lifecycle._path(session_id, create=True).write_text(
                "{stale typed state", encoding="utf-8")
            policy = MagicMock()
            policy.evaluate_answer.return_value = (
                SimpleNamespace(verdict="pass", is_fallback=False,
                                 confidence=0.95),
                {"cost": 0.001, "answer_sufficient": 0.95,
                 "iteration_required": False, "plan_required": False},
            )
            envelope = {
                "native": True, "is_fallback": False, "verdict": "pass",
                "confidence": {"observed": 0.95}, "supported": 0.95,
                "reasons": [],
            }
            brief = SimpleNamespace(estimated_tokens=5)
            with patch("harness.hourglass_lifecycle.governor_for",
                       return_value=("key", MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for",
                       return_value=MagicMock()), \
                 patch("harness.hourglass_lifecycle.policy_for",
                       return_value=policy), \
                 patch.object(agent, "_hourglass_request_context",
                              return_value=("fresh context", {}, brief, [])), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "answer": "2 + 2 = 4", "model": "cheap", "cost": 0.002}), \
                 patch.object(agent, "_jev_answer_envelope",
                              return_value=envelope):
                result = agent.run_hourglass_request(
                    prompt, session_id=session_id, max_rounds=1,
                    request_preflight={
                        "source": "simple_strategy", "choice": "direct_answer",
                        "native": False, "cost": 0.0,
                    })

            saved = agent.lifecycle.load(session_id)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["original_request"], prompt)
            self.assertFalse(agent.lifecycle._path(session_id).read_text(
                encoding="utf-8").startswith("{stale typed state"))

    def test_hourglass_unknown_route_receipt_does_not_replay_route_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_settings(), history_dir=root)
            session_id = "unknown-route-review"
            prompt = "What is 2 + 2?"
            state = agent.lifecycle.begin("chat", session_id, prompt,
                                          stage="route")
            route_call_id = state["request_id"] + ":chat-route-review"
            agent.lifecycle.provider_started(state, route_call_id, 0.004)
            policy = MagicMock()
            policy.evaluate_answer.return_value = (
                SimpleNamespace(verdict="pass", is_fallback=False,
                                 confidence=0.95),
                {"cost": 0.001, "answer_sufficient": 0.95,
                 "iteration_required": False, "plan_required": False},
            )
            envelope = {
                "native": True, "is_fallback": False, "verdict": "pass",
                "confidence": {"observed": 0.95}, "supported": 0.95,
                "reasons": [],
            }
            checkpoint = {
                "route": "hourglass", "status": "interrupted",
                "stage": "route", "prompt": prompt,
                "context": "saved context", "context_files": [],
                "context_tokens": 5, "file_hashes": {},
                "workspace_root": "", "context_updated_at": time.time(),
                "known_cost": 0.0, "unknown_liability": 0.004,
                "usage_unknown": True, "next_round": 1, "rounds": [],
            }
            answer_calls = []
            with patch("harness.hourglass_lifecycle.governor_for",
                       return_value=("key", MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for",
                       return_value=MagicMock()), \
                 patch("harness.hourglass_lifecycle.policy_for",
                       return_value=policy), \
                 patch.object(agent, "_hourglass_request_context",
                              side_effect=AssertionError("saved context was lost")), \
                 patch.object(agent, "_hourglass_answer_once",
                              side_effect=lambda *_a, **_k: (
                                  answer_calls.append("answer") or {
                                      "answer": "2 + 2 = 4", "model": "cheap",
                                      "cost": 0.002,
                                  })), \
                 patch.object(agent, "_jev_answer_envelope",
                              return_value=envelope):
                result = agent.run_hourglass_request(
                    prompt, session_id=session_id, max_rounds=1,
                    resume_state=checkpoint)

            saved = agent.lifecycle.load(session_id)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["response"], "2 + 2 = 4")
            self.assertEqual(answer_calls, ["answer"])
            policy.evaluate_request_workflow.assert_not_called()
            self.assertEqual(
                saved["usage"]["provider_calls"][route_call_id]["status"],
                "unknown")
            self.assertEqual(saved["usage"]["unknown_liability"], 0.004)

    def test_forced_workspace_review_cancels_before_context_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            prompt = "Can you audit this repo and find the biggest remaining issues?"
            with self.assertRaises(ToolCancelled):
                agent.run_prompt(prompt, session_id="workspace-cancel",
                                 force_conversation=True,
                                 cancel_check=lambda: True)

            with patch.object(agent, "run_hourglass_request",
                              return_value={"status": "ok"}) as hourglass:
                result = agent.run_prompt(
                    prompt, session_id="workspace-review",
                    force_conversation=True, cancel_check=lambda: False)
            self.assertEqual(result["status"], "ok")
            hourglass.assert_called_once()
            self.assertEqual(hourglass.call_args.args[0], prompt)

    def test_scope_triage_uses_local_fallback_when_jev_selection_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            with patch("harness.request_router.discover_target_files",
                       return_value=[]), \
                 patch("harness.request_router.enumerate_repo_files",
                       return_value=["app.py"]), \
                 patch("harness.request_router.governor_for",
                       return_value=(None, None)), \
                 patch.object(agent, "_orchestrator_chat_fn", return_value=MagicMock()), \
                 patch("harness.request_router.triage_files",
                       side_effect=HarnessError("review unavailable")), \
                 patch("harness.request_router.keyword_fallback",
                       return_value=["app.py"]) as fallback:
                self.assertEqual(agent._triage_scope("update app.py"), ["app.py"])
            fallback.assert_called_once_with("update app.py", ["app.py"])

    def test_audit_review_outage_preserves_local_result_and_cancellation_interrupts(self):
        for failure in (
                ProviderUsageUnknown("review receipt lost", reserved_cost=0.006),
                RuntimeError("review unavailable")):
            with self.subTest(error=type(failure).__name__), \
                 tempfile.TemporaryDirectory() as tmp:
                agent = AutonomousAgent(settings=_settings(), history_dir=Path(tmp))
                ledger = MagicMock()
                ledger.verify.return_value = (True, None)
                ledger.chain_status.return_value = {"latest_seq": 1, "quarantined": 0}
                policy = MagicMock()
                policy.evaluate_answer.side_effect = failure
                with patch.object(agent, "_live_jev_available", return_value=True), \
                     patch("harness.task_lifecycle.jev_face_governor",
                           return_value=MagicMock()), \
                     patch("harness.task_lifecycle.ledger_for",
                           return_value=ledger), \
                     patch("harness.task_lifecycle.policy_for", return_value=policy):
                    result = agent._handle_audit("audit the ledger", "audit-outage")
                saved = agent.lifecycle.load("audit-outage")
                self.assertEqual(result["status"], "ok")
                self.assertEqual(saved["status"], "complete")
                self.assertEqual(policy.evaluate_answer.call_count, 1)
                call = saved["usage"]["provider_calls"][
                    saved["request_id"] + ":audit-review"]
                self.assertEqual(call["status"], "unknown")

        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), history_dir=Path(tmp))
            ledger = MagicMock()
            ledger.verify.return_value = (True, None)
            ledger.chain_status.return_value = {"latest_seq": 1, "quarantined": 0}
            policy = MagicMock()
            policy.evaluate_answer.side_effect = ToolCancelled("review cancelled")
            with patch.object(agent, "_live_jev_available", return_value=True), \
                 patch("harness.task_lifecycle.jev_face_governor",
                       return_value=MagicMock()), \
                 patch("harness.task_lifecycle.ledger_for", return_value=ledger), \
                 patch("harness.task_lifecycle.policy_for", return_value=policy):
                with self.assertRaises(ToolCancelled):
                    agent._handle_audit("audit the ledger", "audit-cancelled")
            self.assertEqual(
                agent.lifecycle.load("audit-cancelled")["status"], "interrupted")

    def test_chat_checkpoint_merges_partial_updates_and_followup_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_chat_checkpoint("chat", {
                "route": "simple-answer", "status": "interrupted",
                "stage": "answer_review", "prompt": "Explain photosynthesis",
                "answer": "Keep this candidate.", "known_cost": 0.002,
                "unknown_liability": 0.004, "usage_unknown": True,
            }, root)
            # Incremental legacy writers may omit the original request.
            save_chat_checkpoint("chat", {
                "status": "interrupted", "stage": "answer_review",
                "post_skipped": "deadline exhausted",
            }, root)
            payload = load_chat_checkpoint("chat", root)
            self.assertEqual(payload["prompt"], "Explain photosynthesis")
            self.assertEqual(payload["answer"], "Keep this candidate.")
            self.assertEqual(payload["unknown_liability"], 0.004)

            coordinator = LifecycleCoordinator(history_dir=root)
            resumed = coordinator.resume("chat", followup="include the limits")
            self.assertEqual(resumed["lane"], "chat")
            self.assertEqual(resumed["usage"]["unknown_liability"], 0.004)
            self.assertEqual(
                coordinator.active_request(resumed),
                "Explain photosynthesis\n\nFollow-up instruction: include the limits")

    def test_audit_is_local_and_persists_a_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), history_dir=Path(tmp))
            ledger = MagicMock()
            ledger.verify.return_value = (True, None)
            ledger.chain_status.return_value = {"latest_seq": 4, "quarantined": 0}
            with patch("harness.task_lifecycle.ledger_for", return_value=ledger), \
                 patch("harness.web.gather_web_context",
                       side_effect=AssertionError("audit must stay local")):
                result = agent._handle_audit("audit this ledger", "audit-1")
            state = agent.lifecycle.load("audit-1")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(state["lane"], "audit")
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["data"]["result"], result)
            ledger.verify.assert_called_once_with()

    def test_audit_continuation_reuses_completed_jev_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings()
            settings.jev_disabled = False
            settings.jev_api_key = "jev-test-key"
            agent = AutonomousAgent(settings=settings, history_dir=root)
            ledger = MagicMock()
            ledger.verify.return_value = (True, None)
            ledger.chain_status.return_value = {"latest_seq": 2, "quarantined": 0}
            policy = MagicMock()
            policy.evaluate_answer.return_value = (
                SimpleNamespace(verdict="pass", is_fallback=False,
                                 confidence=0.94),
                {"answer_sufficient": 0.94, "cost": 0.001,
                 "iteration_required": False, "plan_required": False},
            )
            with patch.object(agent, "_live_jev_available", return_value=True), \
                 patch("harness.task_lifecycle.ledger_for", return_value=ledger), \
                 patch("harness.task_lifecycle.policy_for", return_value=policy), \
                 patch("harness.task_lifecycle.save_chat_turn",
                       side_effect=RuntimeError("simulated process stop")), \
                 patch.object(agent.lifecycle, "finish",
                              side_effect=RuntimeError("simulated process stop")):
                with self.assertRaises(RuntimeError):
                    agent._handle_audit("audit this ledger", "audit-resume")

            interrupted_window = agent.lifecycle.load("audit-resume")
            self.assertEqual(interrupted_window["status"], "running")
            self.assertEqual(interrupted_window["review"]["decision"], "pass")
            with patch.object(agent, "_live_jev_available", return_value=True), \
                 patch("harness.task_lifecycle.ledger_for", return_value=ledger), \
                 patch("harness.task_lifecycle.policy_for", return_value=policy), \
                 patch("harness.task_lifecycle.save_chat_turn"):
                result = agent.run_prompt("continue", session_id="audit-resume")
            saved = agent.lifecycle.load("audit-resume")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(policy.evaluate_answer.call_count, 1)
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["review"]["rounds"], 1)
            self.assertEqual(saved["review"]["decision"], "pass")
            self.assertEqual(saved["usage"]["provider_calls"][
                saved["request_id"] + ":audit-review"]["status"], "completed")

    def test_auto_edit_continue_restores_the_preview_without_replanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_settings(), root_dir=root,
                                    history_dir=root)
            plan = {
                "status": "ok", "total_nodes": 1, "total_cost_ceiling": 0.01,
                "target_files": ["app.py"],
                "dag": {"nodes": [{"node_id": "edit-1"}]},
                "nodes": [{"node_id": "edit-1", "instruction": "review app.py",
                           "target_files": ["app.py"]}],
                "composition": {"stages": [{"stage": "execution"}]},
                "planning": {}, "stage_judgments": {},
            }
            policy = SimpleNamespace(evaluate_plan=lambda *a, **k: (
                SimpleNamespace(answers={"requires_iteration": False},
                                verdict="pass", is_fallback=False,
                                supported=0.95, confidence=0.92),
                {"cost": 0.0}))
            with patch.object(agent, "_triage_scope", return_value=["app.py"]), \
                 patch.object(agent, "_plan_round", return_value=plan) as planning, \
                 patch("harness.edit_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.edit_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.edit_lifecycle.policy_for", return_value=policy), \
                 patch("harness.edit_lifecycle.discover_verification_gate", return_value=None):
                preview = agent._handle_edit("review app.py", "edit-1", False)
                state = agent.lifecycle.load("edit-1")
                self.assertEqual(preview["status"], "preview_ready")
                self.assertEqual(state["lane"], "auto_edit")
                self.assertEqual(state["status"], "deferred")
                self.assertEqual(state["review"]["rounds"], 1)
                self.assertEqual(state["review"]["decision"], "pass")
                self.assertTrue(state["data"]["resumable"])
                continued = agent.run_prompt("continue", session_id="edit-1")
            self.assertEqual(continued, preview)
            planning.assert_called_once()

    def test_auto_edit_continue_reuses_completed_apply_and_consent_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_settings(), root_dir=root,
                                    history_dir=root)
            plan = {
                "status": "ok", "total_nodes": 1, "total_cost_ceiling": 0.01,
                "dag": {"nodes": [{
                    "node_id": "edit-1", "instruction": "set value to 2",
                    "target_files": ["app.py"], "dependencies": [],
                    "complexity_tier": 1,
                }]},
                "nodes": [{"node_id": "edit-1", "instruction": "set value to 2",
                           "target_files": ["app.py"], "local_gate": None}],
                "composition": {"stages": [{"stage": "execution",
                                               "max_input_tokens": None,
                                               "max_output_tokens": None}]},
                "planning": {}, "stage_judgments": {},
            }
            policy = SimpleNamespace(evaluate_plan=lambda *a, **k: (
                SimpleNamespace(answers={}), {"cost": 0.0}))
            engine = SimpleNamespace()
            engine.default_task_max_cost = 0.01
            engine.router = SimpleNamespace(apply_pool=["test-model"])
            engine.governor = SimpleNamespace(snapshot=lambda: {})
            engine.jev_policy = JevPolicy(_settings())
            calls = []

            def apply_edit(**kwargs):
                calls.append(kwargs)
                Path(kwargs["file_path"]).write_text("value = 2\n", encoding="utf-8")
                return {"status": "ok", "diff": "-value = 1\n+value = 2\n",
                        "content": "value = 2\n", "cost": 0.001,
                        "model_requested": "test-model",
                        "model_observed": ["test-model"]}

            engine.apply_edit = apply_edit
            budget = MagicMock()
            budget.stage.return_value = None
            budget.snapshot.return_value = {}
            drive_calls = {"count": 0}

            class FakePlanExecutor:
                def __init__(self, _engine, routes, *, apply, **kwargs):
                    self.routes, self.apply = routes, apply

                def execute(self, dag):
                    node = next(iter(dag.nodes.values()))
                    result = self.apply(
                        node.target_files[0], node, {}, None)
                    return {node.node_id: result}

            def fake_drive(**kwargs):
                drive_calls["count"] += 1
                applied = kwargs["execute_plan"](plan)
                if drive_calls["count"] == 1:
                    raise ToolCancelled("interrupted after the saved edit receipt")
                return {
                    "status": "ok", "all_results": applied,
                    "total_cost": 0.001, "rounds_history": [],
                    "final_all_ok": True, "remaining_scope": "",
                    "handoff": None, "plan": plan,
                }

            with patch.object(agent, "_triage_scope", return_value=["app.py"]), \
                 patch.object(agent, "_plan_round", return_value=plan), \
                 patch("harness.edit_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.edit_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.edit_lifecycle.policy_for", return_value=policy), \
                 patch("harness.edit_lifecycle.budget_from_settings", return_value=budget), \
                 patch("harness.edit_lifecycle.apply_session", return_value=engine), \
                 patch("harness.edit_lifecycle.PlanExecutor", FakePlanExecutor), \
                 patch("harness.edit_lifecycle.drive", side_effect=fake_drive), \
                 patch("harness.edit_lifecycle.discover_verification_gate", return_value=None):
                with self.assertRaises(ToolCancelled):
                    agent._handle_edit("update app.py", "edit-apply-1", True)
                state = agent.lifecycle.load("edit-apply-1")
                self.assertEqual(state["status"], "interrupted")
                self.assertEqual(state["execution"]["effects"][
                    "edit:" + state["request_id"] + ":edit-1:1"]["status"],
                    "completed")
                result = agent.run_prompt("continue", session_id="edit-apply-1")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]["require_consent"])

    def test_driver_reuses_completed_step_and_does_not_expand_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "driver").begin("driver-1", "inspect")
            driver = MagicMock()
            driver.step.return_value = {
                "step_id": "stable", "ok": True, "stopped_at": "execute",
                "reason": None, "cost_usd": 0.001,
                "decision": {"usable": True, "native": True,
                             "confidence": 0.95, "cost": 0.0},
            }
            driver.verify.return_value = {"ok": True}
            args = {"goal": "inspect", "max_steps": 3,
                    "request_id": state["request_id"]}
            with patch("harness.server._driver_adapter", return_value=driver):
                first = run_driver_task("driver-1", args, None,
                                        lifecycle=coordinator,
                                        lifecycle_state=state)
                coordinator.interrupt(state, ToolCancelled("resume after receipt"))
                resumed = coordinator.resume("driver-1")
                second = run_driver_task("driver-1", args, None,
                                         lifecycle=coordinator,
                                         lifecycle_state=resumed)
            self.assertEqual(first["status"], "done")
            self.assertEqual(second["steps"], first["steps"])
            self.assertEqual(driver.step.call_count, 1)
            self.assertIsNone(driver.step.call_args.kwargs["consent"])
            self.assertEqual(state["review"]["rounds"], 1)
            self.assertEqual(state["review"]["decision"], "pass")

    def test_unknown_edit_effect_stops_continue_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            state = adapter_for(agent.lifecycle, "auto_edit").begin(
                "edit-unknown", "update app.py")
            effect_id = "edit:" + state["request_id"] + ":edit-1:1"
            agent.lifecycle.begin_effect(
                state, effect_id, "auto_edit.apply",
                approval_state={"required": True})
            agent.lifecycle.unknown_effect(state, effect_id)
            agent.lifecycle.interrupt(
                state, ToolCancelled("provider disconnected after dispatch"))
            with patch.object(agent, "_handle_edit",
                              side_effect=AssertionError("must reconcile before replay")):
                result = agent.run_prompt("continue", session_id="edit-unknown")
            self.assertEqual(result["status"], "deferred")
            self.assertIn(effect_id, result["lifecycle"]["requires_reconciliation"])

    def test_provisioning_keeps_approval_and_verified_done_receipts(self):
        # Provisioning path validation rejects Windows 8.3 aliases. Keep the
        # approved project root under the checkout so it uses a canonical path.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp) / "approved"
            root.mkdir()
            probe = pv.HostProbe(
                "Linux", "x86_64", "3.12.1", "python", False, (), 10**9,
                (pv.PackageManager("pip", "/usr/bin/pip", "24.0", True),),
                str(root))
            policy = pv.policy_for_probe(probe, [str(root)])
            plan = pv.plan_provision("provision ruff==0.6.1", probe,
                                     policy=policy)
            gate = pv.ApprovalGate()
            gate.approve_plan(plan, "operator-test")
            ledger = AutonomyLedger(str(Path(tmp) / "provision-ledger.jsonl"))
            runner = FakeRunner()
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "provisioning").begin(
                "provision-1", plan.goal, stage="approval",
                data={"plan": plan.to_dict(), "plan_digest": plan.digest})
            report = pv.execute_plan(
                plan, gate, policy=policy, ledger=ledger, dry_run=False,
                runner=runner, which=fake_which, lifecycle=coordinator,
                lifecycle_state=state)
            call_count = len(runner.calls)
            replay = pv.execute_plan(
                plan, gate, policy=policy, ledger=ledger, dry_run=False,
                runner=runner, which=fake_which, lifecycle=coordinator,
                lifecycle_state=state)
            self.assertEqual(report.outcome, pv.COMPLETED)
            self.assertEqual(replay.outcome, pv.COMPLETED)
            self.assertEqual(len(runner.calls), call_count)
            self.assertTrue(state["execution"]["approval_state"])
            self.assertTrue(all(
                item["status"] == "completed"
                for item in state["execution"]["effects"].values()))

    def test_provision_lane_records_jev_plan_review_without_auto_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "approved"
            root.mkdir()
            settings = _settings()
            settings.jev_disabled = False
            settings.jev_api_key = "jev-test-key"
            agent = AutonomousAgent(settings=settings, root_dir=root,
                                    history_dir=root)
            probe = pv.HostProbe(
                "Linux", "x86_64", "3.12.1", "python", False, (), 10**9,
                (pv.PackageManager("pip", "/usr/bin/pip", "24.0", True),),
                str(root))
            policy = SimpleNamespace(evaluate_provision=lambda *a, **k: (
                SimpleNamespace(
                    answers={
                        "satisfies_goal": {"noul": 0.95},
                        "exceeds_goal": {"noul": 0.10},
                    },
                    is_fallback=False,
                ), {"cost": 0.002}))
            with patch.object(agent, "_live_jev_available", return_value=True), \
                 patch("harness.provision.probe_host", return_value=probe), \
                 patch("harness.task_lifecycle.policy_for", return_value=policy), \
                 patch("harness.task_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.task_lifecycle.save_chat_turn"):
                result = agent._handle_provision(
                    "provision ruff==0.6.1", "provision-jev")

            state = agent.lifecycle.load("provision-jev")
            self.assertEqual(result["status"], "planned")
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["review"]["rounds"], 1)
            self.assertEqual(state["review"]["decision"], "pass")
            self.assertAlmostEqual(state["review"]["confidence"], 0.90)
            self.assertEqual(state["execution"]["effects"], {})


if __name__ == "__main__":
    unittest.main()
