"""Hermetic contracts for the all-request answer/reiterate lifecycle."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport


class _AnswerTransport:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, {
            "model": "jev-test",
            "answers": self.answers,
            "usage": {"input_tokens": 20, "output_tokens": 2},
        }


def _noul(value):
    return {"type": "noul", "noul": value}


class AnswerPolicyTests(unittest.TestCase):
    def test_keyed_answer_policy_exposes_native_signals_and_one_ledger_event(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        transport = _AnswerTransport({
            "answer_sufficient": _noul(0.995),
            "iteration_required": _noul(0.01),
            "plan_required": _noul(0.01),
        })
        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        ledger = AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))
        policy = policy_for(settings, transport=transport, governor=governor,
                            ledger=ledger)

        result, structural = policy.evaluate_answer(
            "What does this do?", "A grounded answer.", "retained context",
            task_id="answer-1")

        self.assertFalse(result.is_fallback)
        self.assertTrue(structural["native"])
        self.assertEqual(structural["pack_version"], "answer-sufficiency-v1")
        self.assertAlmostEqual(structural["answer_sufficient"], 0.995)
        self.assertFalse(structural["iteration_required"])
        self.assertFalse(structural["plan_required"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual([e["event"] for e in ledger.entries()], ["jev_eval"])
        self.assertEqual(ledger.entries()[0]["site"], "answer")

    def test_unkeyed_policy_cannot_claim_sufficiency(self):
        settings = load_settings()
        settings.jev_api_key = None
        result, structural = policy_for(settings).evaluate_answer(
            "question", "candidate", "context")
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural["native"])
        self.assertIsNone(structural["answer_sufficient"])
        self.assertTrue(structural["iteration_required"])

    def test_missing_answer_key_is_a_fallback_not_a_live_pass(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        transport = _AnswerTransport({
            "answer_sufficient": _noul(0.999),
            "iteration_required": _noul(0.01),
        })
        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        policy = policy_for(settings, transport=transport, governor=governor)
        result, structural = policy.evaluate_answer("q", "a", "c")
        self.assertFalse(result.is_fallback)
        self.assertFalse(structural["native"])
        self.assertEqual(structural["iteration_required"], True)

    def test_out_of_range_noul_is_treated_as_unparseable(self):
        # A genuine TypeSafe response is already range-validated on the wire
        # (harness.jev._number); the only way an out-of-range noul reaches
        # this policy's own probability() guard is a non-conforming
        # evaluator, so this pins that defense-in-depth check directly.
        class _OutOfRangeEvaluator:
            api_key = "k"
            model = "stub"

            def evaluate(self, state, questions):
                return JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"answer_sufficient": 1.5, "iteration_required": 0.2,
                    "plan_required": 0.3},
                    ["ok"], is_fallback=False, model="stub")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        policy = policy_for(settings, governor=governor,
                            evaluator=_OutOfRangeEvaluator())
        result, structural = policy.evaluate_answer("q", "a", "c")
        self.assertFalse(structural["native"])
        self.assertIsNone(structural["answer_sufficient"])

    def test_empty_reasons_get_an_honest_default(self):
        class _EmptyReasonsEvaluator:
            api_key = "k"
            model = "stub"

            def evaluate(self, state, questions):
                return JevEvaluationResult(
                    "fail", 0.0, 0.0, {}, [], is_fallback=True, model="stub")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        policy = policy_for(settings, governor=governor,
                            evaluator=_EmptyReasonsEvaluator())
        result, structural = policy.evaluate_answer("q", "a", "c")
        self.assertFalse(structural["native"])
        self.assertEqual(result.reasons,
                         ["answer judgment unavailable or malformed"])

    def test_transport_error_mid_evaluation_settles_the_reservation(self):
        class _RaisingEvaluator:
            api_key = "k"
            model = "stub"

            def evaluate(self, state, questions):
                raise HarnessError("TypeSafe transport exploded")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        policy = policy_for(settings, governor=governor,
                            evaluator=_RaisingEvaluator())
        result, structural = policy.evaluate_answer("q", "a", "c")
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural["native"])
        self.assertIn("TypeSafe transport exploded", result.reasons[0])

    def test_reconcile_failure_after_a_mid_evaluation_error_is_swallowed(self):
        class _RaisingEvaluator:
            api_key = "k"
            model = "stub"

            def evaluate(self, state, questions):
                raise HarnessError("TypeSafe transport exploded")

        class _ReserveThenFailToReconcile:
            def reserve(self, amount, label):
                return "reservation-token"

            def reconcile(self, token, actual):
                raise HarnessError("reconcile refused")

        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_ReserveThenFailToReconcile(),
                            evaluator=_RaisingEvaluator())
        result, structural = policy.evaluate_answer("q", "a", "c")
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural["native"])
        self.assertIn("TypeSafe transport exploded", result.reasons[0])


class _FakePolicy:
    def __init__(self, assessments):
        self.assessments = list(assessments)
        self.calls = []

    def evaluate_answer(self, prompt, answer, context, **kwargs):
        self.calls.append((prompt, answer, context, kwargs))
        sufficient, iteration, plan = self.assessments.pop(0)
        result = JevEvaluationResult(
            "pass" if sufficient >= 0.99 and not iteration else "fail",
            0.0, sufficient,
            {"answer_sufficient": _noul(sufficient),
             "iteration_required": _noul(1.0 if iteration else 0.0),
             "plan_required": _noul(1.0 if plan else 0.0)},
            ["test assessment"], model="jev-test")
        return result, {
            "capability": "answer", "pack_version": "answer-sufficiency-v1",
            "native": True, "answer_sufficient": sufficient,
            "iteration_required": iteration, "plan_required": plan,
            "cost": 0.001, "input_tokens": 10, "output_tokens": 1,
        }


def _ladder_settings(**overrides):
    overrides.setdefault("panel_pool", "p1,p2")
    overrides.setdefault("judge", "j1")
    overrides.setdefault("convergence_model", "c1")
    overrides.setdefault("escalation_pool", "e1")
    return load_settings(overrides)


class HourglassAnswerOnceTests(unittest.TestCase):
    def _agent(self, root):
        return AutonomousAgent(settings=_ladder_settings(), root_dir=root,
                               history_dir=root)

    def test_rotates_through_failures_and_returns_the_first_usable_rung(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            responses = [
                (429, {"error": {"message": "rate limited"}}),
                (200, {"choices": [{"message": {"content": "byok"},
                                    "finish_reason": "stop"}],
                      "usage": {"cost": 0.0, "is_byok": True}}),
                (200, {"choices": [{"message": {
                    "content": "```never closes"}, "finish_reason": "stop"}],
                      "usage": {"cost": 0.001}}),
                (200, {"choices": [{"message": {"content": "final answer"},
                                    "finish_reason": "stop"}],
                      "usage": {"cost": 0.002}}),
            ]
            gov = MagicMock()
            with patch("harness.agent.chat", side_effect=responses):
                result = agent._hourglass_answer_once(
                    "q", "context", governor=gov, api_key="k")
        self.assertEqual(result["model"], "c1")
        self.assertEqual(result["answer"], "final answer")
        gov.record_byok.assert_called_once_with("p2")
        gov.record_actual.assert_any_call(0.002, "c1")

    def test_every_rung_fails_raises_with_attempt_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            with patch("harness.agent.chat",
                      return_value=(429, {"error": {"message": "down"}})):
                with self.assertRaises(HarnessError) as ctx:
                    agent._hourglass_answer_once(
                        "q", "", governor=gov, api_key="k")
        self.assertIn("answer failed on every ladder model", str(ctx.exception))

    def test_every_rung_truncated_raises_truncated_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            truncated = (200, {"choices": [{"message": {
                "content": "```never closes and has enough body to matter"},
                "finish_reason": "stop"}], "usage": {"cost": 0.0}})
            with patch("harness.agent.chat", return_value=truncated):
                with self.assertRaises(HarnessError) as ctx:
                    agent._hourglass_answer_once(
                        "q", "", governor=gov, api_key="k")
        self.assertIn("truncated on every usable ladder rung", str(ctx.exception))

    def test_empty_ladder_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings()
            settings.panel_pool = []
            settings.judge = None
            settings.convergence_model = None
            settings.escalation_pool = []
            agent = AutonomousAgent(settings=settings, root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            with self.assertRaises(HarnessError) as ctx:
                agent._hourglass_answer_once(
                    "q", "", governor=MagicMock(), api_key="k")
        self.assertIn("answer ladder is empty", str(ctx.exception))

    def test_cancel_before_dispatch_raises_cancelled(self):
        from harness.errors import ToolCancelled
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with self.assertRaises(ToolCancelled):
                agent._hourglass_answer_once(
                    "q", "", governor=MagicMock(), api_key="k",
                    cancel_check=lambda: True)

    def test_cancel_mid_ladder_raises_cancelled(self):
        from harness.errors import ToolCancelled
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            checks = iter([False, True])
            with patch("harness.agent.chat",
                      return_value=(429, {"error": {"message": "down"}})):
                with self.assertRaises(ToolCancelled):
                    agent._hourglass_answer_once(
                        "q", "", governor=MagicMock(), api_key="k",
                        cancel_check=lambda: next(checks))

    def test_preflight_harness_error_rotates_to_next_rung(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            gov.preflight.side_effect = [HarnessError("ceiling refused"), None]
            ok = (200, {"choices": [{"message": {"content": "answer"},
                                     "finish_reason": "stop"}],
                       "usage": {"cost": 0.001}})
            with patch("harness.agent.chat", return_value=ok):
                result = agent._hourglass_answer_once(
                    "q", "", governor=gov, api_key="k")
        self.assertEqual(result["model"], "p2")

    def test_no_governor_resolves_one_via_governor_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            ok = (200, {"choices": [{"message": {"content": "answer"},
                                     "finish_reason": "stop"}],
                       "usage": {"cost": 0.0}})
            with patch("harness.agent.governor_for", return_value=("k", gov)), \
                 patch("harness.agent.chat", return_value=ok):
                result = agent._hourglass_answer_once("q", "")
        self.assertEqual(result["model"], "p1")


class HourglassRequestContextTests(unittest.TestCase):
    def test_reads_discovered_candidate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widget.py").write_text("def widget(): pass\n",
                                            encoding="utf-8")
            agent = AutonomousAgent(settings=_ladder_settings(),
                                    root_dir=root, history_dir=root)
            context, files, brief, web_sources = \
                agent._hourglass_request_context("Explain widget.py")
        self.assertIn("widget.py", files)
        self.assertEqual(web_sources, [])
        self.assertIsInstance(context, str)

    def test_web_true_attaches_ok_sources_to_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_ladder_settings(),
                                    root_dir=root, history_dir=root)
            with patch.object(agent, "_gather_web_context",
                              return_value=[{"kind": "web", "ok": True,
                                            "url": "https://example.com",
                                            "text": "web evidence body"}]):
                context, files, brief, web_sources = \
                    agent._hourglass_request_context("what is new?", web=True)
        self.assertEqual(len(web_sources), 1)
        self.assertIn("WEB EVIDENCE", context)
        self.assertIn("web evidence body", context)

    def test_web_true_gather_failure_is_disclosed_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_ladder_settings(),
                                    root_dir=root, history_dir=root)
            with patch.object(agent, "_gather_web_context",
                              side_effect=RuntimeError("network down")):
                context, files, brief, web_sources = \
                    agent._hourglass_request_context("what is new?", web=True)
        self.assertEqual(len(web_sources), 1)
        self.assertFalse(web_sources[0]["ok"])
        self.assertIn("RuntimeError", web_sources[0]["note"])


class AgentAnswerLoopTests(unittest.TestCase):
    def test_loop_reiterates_until_native_threshold(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root,
                                history_dir=root)
        policy = _FakePolicy([(0.70, True, False), (0.995, False, False)])
        answers = iter([
            {"model": "cheap", "answer": "first", "cost": 0.001},
            {"model": "capable", "answer": "second", "cost": 0.002},
        ])
        with patch("harness.agent.policy_for", return_value=policy), \
             patch("harness.agent.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          side_effect=lambda *a, **k: next(answers)), \
             patch("harness.agent.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="answer-loop")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], "second")
        self.assertEqual(result["model"], "capable")
        self.assertEqual(len(policy.calls), 2)
        self.assertEqual(result["confidence"]["threshold"], 0.99)
        self.assertTrue(result["confidence"]["passed"])
        self.assertEqual(len(result["hourglass"]["rounds"]), 2)

    def test_edit_request_uses_existing_hourglass_executor_with_jev_completion(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        with patch.object(agent, "_handle_edit", return_value={
                "status": "ok", "response": "done"}) as edit:
            result = agent.run_hourglass_request("Update util.py")
        edit.assert_called_once()
        self.assertTrue(edit.call_args.kwargs["use_jev_completion"])
        self.assertEqual(result["status"], "ok")
        self.assertIn("planning_waist", result["hourglass"]["stages"])

    def test_empty_prompt_raises(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        with self.assertRaises(HarnessError):
            agent.run_hourglass_request("   ")

    def test_bad_confidence_threshold_raises(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        with self.assertRaises(HarnessError):
            agent.run_hourglass_request("hi", confidence_threshold=1.5)

    def test_audit_intent_routes_to_handle_audit(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        with patch.object(agent, "_handle_audit",
                          return_value={"status": "ok"}) as audit:
            result = agent.run_hourglass_request("check ledger status")
        audit.assert_called_once()
        self.assertEqual(result["status"], "ok")

    def test_cancel_mid_round_loop_raises_cancelled(self):
        from harness.errors import ToolCancelled
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)
        with patch("harness.agent.policy_for", return_value=_FakePolicy([])), \
             patch("harness.agent.governor_for", return_value=(None, object())):
            with self.assertRaises(ToolCancelled):
                agent.run_hourglass_request(
                    "How does the router work?", cancel_check=lambda: True)

    def test_deferred_when_answer_judgment_is_not_native(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)

        class _UnkeyedPolicy:
            def evaluate_answer(self, prompt, answer, context, **kwargs):
                result = JevEvaluationResult(
                    "fail", 0.0, 0.0, {}, ["unkeyed"], is_fallback=True,
                    model="jev-latest")
                return result, {"capability": "answer", "native": False,
                               "answer_sufficient": None,
                               "iteration_required": True, "plan_required": None,
                               "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
                               "pack_version": "answer-sufficiency-v1"}

        with patch("harness.agent.policy_for", return_value=_UnkeyedPolicy()), \
             patch("harness.agent.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.agent.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="deferred-1")

        self.assertEqual(result["status"], "deferred")

    def test_plan_required_stops_before_any_write(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)
        policy = _FakePolicy([(0.995, False, True)])
        with patch("harness.agent.policy_for", return_value=policy), \
             patch("harness.agent.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.agent.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="plan-1")

        self.assertEqual(result["status"], "plan_required")

    def test_needs_iteration_when_rounds_are_exhausted(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)
        policy = _FakePolicy([(0.2, True, False)] * 3)
        with patch("harness.agent.policy_for", return_value=policy), \
             patch("harness.agent.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.agent.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="needs-iter-1",
                max_rounds=3)

        self.assertEqual(result["status"], "needs_iteration")
        self.assertIn("remaining_scope", result)

    def test_budget_deferred_before_a_second_round(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)
        policy = _FakePolicy([(0.2, True, False)])
        gov = MagicMock()
        gov.working_remaining.return_value = 0.0
        with patch("harness.agent.policy_for", return_value=policy), \
             patch("harness.agent.governor_for", return_value=(None, gov)), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.agent.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="budget-1", max_rounds=2)

        self.assertEqual(result["status"], "deferred")
        self.assertIn("remaining_scope", result)
        self.assertIn("Jev budget", result["remaining_scope"])


class HandleEditJevCompletionTests(unittest.TestCase):
    def test_use_jev_completion_wires_threshold_into_drive_and_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calc_file = root / "calc.py"
            calc_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")

            settings = load_settings()
            settings.jev_api_key = None
            settings.hourglass_confirm = False
            settings.hourglass_isolate = False
            settings.hourglass_parallel = False
            settings.hourglass_require_attestation = False
            agent = AutonomousAgent(settings=settings, root_dir=root,
                                    history_dir=root)

            def fake_apply_edit(file_path, instruction, **kwargs):
                content = "def add(a: int, b: int) -> int: return a + b\n"
                (root / file_path).write_text(content, encoding="utf-8")
                return {"status": "ok", "cost": 0.001, "content": content,
                       "diff": "--- a\n+++ b\n"}

            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = fake_apply_edit
            test_gov = SpendGovernor(FakeTransport(), "sk-test", max_cost=1.0)

            with patch("harness.agent.apply_session", return_value=mock_engine), \
                 patch("harness.agent.governor_for",
                      return_value=(None, test_gov)), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                result = agent.run_hourglass_request(
                    "Update calc.py with type annotations")

        self.assertEqual(result["status"], "ok")
        self.assertIn("completion_jev", result["hourglass"]["stages"])


if __name__ == "__main__":
    unittest.main()
