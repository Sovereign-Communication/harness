"""Hermetic contracts for the all-request answer/reiterate lifecycle."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.errors import HarnessError, ToolCancelled
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
        self.assertEqual(result.fallback_reason, "missing_key")
        self.assertEqual(structural["fallback_reason"], "missing_key")
        self.assertFalse(structural["native"])
        self.assertIsNone(structural["answer_sufficient"])
        self.assertTrue(structural["iteration_required"])

    def test_explicitly_disabled_answer_reports_disable_reason(self):
        with patch.dict(os.environ, {"HARNESS_JEV_DISABLE": "1"}):
            settings = load_settings({"jev_api_key": "configured"})
        result, structural = policy_for(settings).evaluate_answer(
            "question", "candidate", "context")
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.fallback_reason, "explicit_disable")
        self.assertEqual(structural["fallback_reason"], "explicit_disable")

    def test_answer_transport_fallback_reason_survives_policy_wrapper(self):
        class BrokenTransport:
            def post(self, *args, **kwargs):
                raise OSError("offline")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        result, structural = policy_for(
            settings, transport=BrokenTransport(), governor=governor
        ).evaluate_answer("question", "candidate", "context")
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.fallback_reason, "transport_failure")
        self.assertEqual(structural["fallback_reason"], "transport_failure")

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
        self.route_calls = 0

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

    def evaluate_request_workflow(self, state, **kwargs):
        self.route_calls += 1
        choice = ("workspace-inspection"
                  if "this repo" in str(state.get("request", "")).lower()
                  else "answer")
        return SimpleNamespace(
            answers={"workflow_tier": {"choice": choice}},
            is_fallback=False, cost=0.0, model="jev-test", reasons=[]), {
                "cost": 0.0, "native": True,
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
            with patch("harness.hourglass_lifecycle.chat", side_effect=responses):
                result = agent._hourglass_answer_once(
                    "q", "context", governor=gov, api_key="k")
        self.assertEqual(result["model"], "c1")
        self.assertEqual(result["answer"], "final answer")
        gov.record_byok.assert_called_once_with("p2")
        gov.record_actual.assert_any_call(0.002, "c1")

    def test_answer_once_with_web_sources_discloses_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            sources = [
                {"ok": True, "kind": "fetch", "title": "Example", "url": "https://example.com", "text": "web info"},
                {"ok": False, "kind": "search", "url": "https://bad.com", "note": "failed"},
            ]
            with patch("harness.hourglass_lifecycle.chat", return_value=(200, {"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.0}})) as mc:
                agent._hourglass_answer_once(
                    "q", "context", governor=gov, api_key="k", web=True, web_sources=sources)
            sysmsg = mc.call_args[1]["messages"][0]["content"]
            self.assertIn("SOURCE (fetch): Example https://example.com", sysmsg)
            self.assertIn("SOURCE (search) FAILED: https://bad.com failed", sysmsg)

    def test_every_rung_fails_raises_with_attempt_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            with patch("harness.hourglass_lifecycle.chat",
                      return_value=(429, {"error": {"message": "down"}})):
                with self.assertRaises(HarnessError) as ctx:
                    agent._hourglass_answer_once(
                        "q", "", governor=gov, api_key="k")
        self.assertIn("answer failed on every ladder model", str(ctx.exception))

    def test_every_rung_truncated_returns_substantive_truncated_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            gov = MagicMock()
            cut_text = "```never closes and has enough body to matter"
            truncated = (200, {"choices": [{"message": {
                "content": cut_text},
                "finish_reason": "stop"}], "usage": {"cost": 0.0}})
            with patch("harness.hourglass_lifecycle.chat", return_value=truncated):
                res = agent._hourglass_answer_once(
                    "q", "", governor=gov, api_key="k")
            self.assertTrue(res.get("truncated"))
            self.assertIn(cut_text, res["answer"])
            self.assertIn("truncated", res["answer"])

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
            with patch("harness.hourglass_lifecycle.chat",
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
            with patch("harness.hourglass_lifecycle.chat", return_value=ok):
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
            with patch("harness.hourglass_lifecycle.governor_for", return_value=("k", gov)), \
                 patch("harness.hourglass_lifecycle.chat", return_value=ok):
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

    def test_broad_workspace_review_uses_balanced_context_and_discloses_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "AGENTS.md").write_text(
                "Repository instructions.\n", encoding="utf-8")
            (root / "README.md").write_text(
                "Project overview.\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[project]\nname='sample'\n", encoding="utf-8")
            (root / "docs" / "roadmap.md").write_text(
                "# Remaining improvements\n", encoding="utf-8")
            (root / "src" / "main.py").write_text(
                "def main():\n    return True\n", encoding="utf-8")
            (root / "src" / "service.py").write_text(
                "class Service:\n    pass\n", encoding="utf-8")
            (root / "tests" / "test_service.py").write_text(
                "def test_service():\n    assert True\n", encoding="utf-8")
            for index in range(24):
                (root / "src" / f"module_{index}.py").write_text(
                    f"def module_{index}():\n    return {index}\n",
                    encoding="utf-8")
            agent = AutonomousAgent(settings=_ladder_settings(),
                                    root_dir=root, history_dir=root)
            with patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()):
                context, files, _brief, web_sources = \
                    agent._hourglass_request_context(
                        "Audit this repo and determine the most valuable "
                        "remaining improvements.")

        self.assertLessEqual(len(files), 20)
        self.assertGreater(len(files), 8)
        self.assertIn("AGENTS.md", files)
        self.assertIn("README.md", files)
        self.assertIn("pyproject.toml", files)
        self.assertIn("docs/roadmap.md", files)
        self.assertIn("src/main.py", files)
        self.assertIn("tests/test_service.py", files)
        self.assertIn("bounded review sample", context)
        self.assertEqual(web_sources, [])

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
    def test_invalid_continue_checkpoint_fails_locally_without_provider_work(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        with patch("harness.request_router.load_chat_checkpoint",
                   side_effect=HarnessError("corrupt")), \
             patch("harness.simple_answer_lifecycle.policy_for",
                   side_effect=AssertionError("resume failure is local")), \
             patch("harness.simple_answer_lifecycle.governed_completion",
                   side_effect=AssertionError("resume failure is local")):
            result = agent.run_prompt("continue", session_id="broken")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["defer_reason"], "invalid chat checkpoint")

    def test_continue_dispatches_saved_simple_answer_route(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        checkpoint = {"route": "simple-answer", "prompt": "Explain photosynthesis"}
        with patch("harness.request_router.load_chat_checkpoint", return_value=checkpoint), \
             patch.object(agent, "run_simple_request",
                          return_value={"status": "ok"}) as run:
            result = agent.run_prompt("continue", session_id="resume-simple")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(run.call_args.args[0], "Explain photosynthesis")
        self.assertEqual(run.call_args.kwargs["resume_state"], checkpoint)

    def test_force_conversation_routes_local_review_to_workspace_context(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        with patch.object(agent, "_workspace_context_deferred",
                          return_value={"status": "deferred"}) as defer:
            result = agent.run_prompt(
                "Audit this repo", force_conversation=True,
                session_id="chat-audit")
        self.assertEqual(result["status"], "deferred")
        defer.assert_called_once_with("Audit this repo", "chat-audit")

    def test_workspace_selection_defer_recovers_corrupt_checkpoint_and_stage(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        existing = {"route": "hourglass", "status": "running",
                    "prompt": "old request"}
        with patch("harness.chat_lifecycle.load_chat_checkpoint",
                   side_effect=HarnessError("corrupt")), \
             patch.object(agent, "_persist_chat_checkpoint") as persist, \
             patch("harness.chat_lifecycle.save_chat_turn"):
            result = agent._workspace_context_deferred(
                "Audit this repo", "select-workspace")
        self.assertEqual(result["status"], "deferred")
        saved = persist.call_args.args[1]
        self.assertEqual(saved["prompt"], "Audit this repo")
        self.assertEqual(saved["stage"], "workspace_selection")

        with patch("harness.chat_lifecycle.load_chat_checkpoint", return_value=existing), \
             patch.object(agent, "_persist_chat_checkpoint") as persist, \
             patch("harness.chat_lifecycle.save_chat_turn"):
            agent._workspace_context_deferred("old request", "select-workspace")
        saved = persist.call_args.args[1]
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["stage"], "workspace_selection")

    def test_pre_generation_simple_strategy_falls_through_with_route_evidence(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        deferred = {"status": "deferred", "simple_request": {
            "generation_calls": 0,
            "pre": {"strategy": "direct_answer", "native": True,
                    "cost": 0.00001},
        }}
        for force_conversation in (False, True):
            with self.subTest(force_conversation=force_conversation), \
                 patch.object(agent, "run_simple_request", return_value=deferred), \
                 patch.object(agent, "_live_jev_available", return_value=True), \
                 patch.object(agent, "run_hourglass_request",
                              return_value={"status": "ok"}) as hourglass:
                result = agent.run_prompt(
                    "Explain photosynthesis", force_conversation=force_conversation)
            self.assertEqual(result["status"], "ok")
            evidence = hourglass.call_args.kwargs["request_preflight"]
            self.assertEqual(evidence["source"], "simple_strategy")
            self.assertEqual(evidence["choice"], "direct_answer")

    def test_simple_answer_resume_reviews_candidate_and_restores_cost_liability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _ladder_settings()
            agent = AutonomousAgent(settings=settings, root_dir=root,
                                    history_dir=root)
            prompt = "Explain photosynthesis"
            checkpoint = {
                "route": "simple-answer", "status": "interrupted",
                "stage": "answer_review", "prompt": prompt,
                "known_cost": 0.003, "unknown_liability": 0.002,
                "pre": {"strategy": "direct_answer", "native": True,
                        "cost": 0.001},
                "answer": "Plants convert light into chemical energy.",
                "answer_model": "p1", "answer_cost": 0.001,
                "generation_calls": 1,
            }
            policy = MagicMock()
            policy.evaluate_answer.return_value = (None, {
                "native": True, "verdict": "pass",
                "answer_sufficient": 0.99, "iteration_required": False,
                "plan_required": False, "cost": 0.0001,
            })
            governor = MagicMock()
            history = [
                {"prompt": prompt, "response": "duplicate current turn"},
                {"prompt": "Explain light energy", "response": "Prior context"},
            ]
            with patch("harness.simple_answer_lifecycle.governor_for", return_value=(None, governor)), \
                 patch("harness.simple_answer_lifecycle.policy_for", return_value=policy), \
                 patch("harness.simple_answer_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.simple_answer_lifecycle.load_chat_history", return_value=history), \
                 patch("harness.simple_answer_lifecycle.governed_completion",
                       side_effect=AssertionError("saved candidate must be reused")), \
                 patch("harness.simple_answer_lifecycle.save_chat_turn"), \
                 patch.object(agent, "_clear_chat_checkpoint"):
                result = agent.run_simple_request(
                    prompt, session_id="simple-review", resume_state=checkpoint)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], checkpoint["answer"])
        governor.record_actual.assert_called_once_with(
            0.003, "chat-continuation", lane="answer")
        governor.retain_unknown.assert_called_once_with(
            0.002, "cancelled-chat-provider-call")
        reviewed_context = policy.evaluate_answer.call_args.args[2]
        self.assertNotIn("duplicate current turn", reviewed_context)

    def test_simple_strategy_workspace_choice_defers_without_selected_root(self):
        agent = AutonomousAgent(settings=_ladder_settings(), root_dir=None)
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = (None, {
            "native": True, "strategy": "workspace_inspection",
            "cost": 0.00001,
        })
        with patch("harness.simple_answer_lifecycle.governor_for", return_value=(None, MagicMock())), \
             patch("harness.simple_answer_lifecycle.policy_for", return_value=policy), \
             patch("harness.simple_answer_lifecycle.ledger_for", return_value=MagicMock()), \
             patch("harness.simple_answer_lifecycle.save_chat_turn"), \
             patch.object(agent, "_persist_chat_checkpoint"), \
             patch.object(agent, "_workspace_context_deferred",
                          return_value={"status": "deferred"}) as defer:
            result = agent.run_simple_request("Explain this project")
        self.assertEqual(result["status"], "deferred")
        defer.assert_called_once()

    def test_checkpoint_storage_failures_are_reported_without_raising(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        with patch("harness.hourglass_lifecycle.save_chat_checkpoint",
                   side_effect=OSError("disk full")), \
             patch("harness.hourglass_lifecycle.clear_chat_checkpoint",
                   side_effect=HarnessError("unsafe")), \
             patch("harness.hourglass_lifecycle.emit") as emit_event:
            agent._persist_chat_checkpoint("session", {"prompt": "x"})
            agent._clear_chat_checkpoint("session")
        self.assertEqual([call.args[0] for call in emit_event.call_args_list], [
            "chat_checkpoint_unavailable", "chat_checkpoint_clear_failed"])

    def test_checkpoint_interruption_accumulates_unknown_provider_liability(self):
        exc = ToolCancelled("provider cancellation", usage_unknown=True)
        exc.reserved_cost = 0.004
        checkpoint = {"unknown_liability": 0.002, "known_cost": 0.0}
        AutonomousAgent._checkpoint_interruption(checkpoint, exc, 0.003)
        self.assertEqual(checkpoint["known_cost"], 0.003)
        self.assertEqual(checkpoint["unknown_liability"], 0.006)
        self.assertTrue(checkpoint["usage_unknown"])

    def test_workspace_checkpoint_hash_validation_covers_stale_and_bad_entries(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "router.py"
            source.write_text("def route(): return 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            checkpoint = {
                "workspace_root": str(root), "workspace_context_used": False,
                "file_hashes": {"router.py": digest},
            }
            self.assertTrue(agent._hourglass_context_matches(checkpoint))
            checkpoint["file_hashes"] = {"router.py": "wrong"}
            self.assertFalse(agent._hourglass_context_matches(checkpoint))
            checkpoint["file_hashes"] = ["malformed"]
            self.assertFalse(agent._hourglass_context_matches(checkpoint))
            checkpoint["workspace_context_used"] = True
            checkpoint["prompt"] = None
            checkpoint["file_hashes"] = {}
            self.assertFalse(agent._hourglass_context_matches(checkpoint))

    def test_checkpoint_workspace_root_comparison_is_canonical_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)
            checkpoint = {
                "workspace_root": str(root / ".." / root.name),
                "file_hashes": {},
            }
            self.assertTrue(agent._hourglass_context_matches(checkpoint))

            checkpoint["workspace_root"] = ""
            self.assertFalse(agent._hourglass_context_matches(checkpoint))
            checkpoint["workspace_root"] = "\x00"
            self.assertFalse(agent._hourglass_context_matches(checkpoint))

    def test_missing_root_returns_no_workspace_candidates(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        self.assertEqual(agent._hourglass_workspace_candidates("Audit this repo"), [])

    def test_native_web_research_and_simple_strategy_routes_enable_web(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_ladder_settings(),
                                    root_dir=Path(tmp), history_dir=Path(tmp))
            cases = (
                {"source": "jev_request_workflow", "choice": "web-research",
                 "native": True, "cost": 0.0},
                {"source": "simple_strategy", "choice": "web_research",
                 "native": False, "cost": 0.0},
            )
            for index, route in enumerate(cases):
                policy = _FakePolicy([(0.995, False, False)])
                with self.subTest(route=route), \
                     patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                     patch("harness.hourglass_lifecycle.governor_for",
                           return_value=(None, MagicMock())), \
                     patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                     patch.object(agent, "_hourglass_request_context", return_value=(
                         "web context", {}, MagicMock(estimated_tokens=2), [])) as context, \
                     patch.object(agent, "_hourglass_answer_once", return_value={
                         "model": "m1", "answer": "grounded answer", "cost": 0.0}), \
                     patch("harness.hourglass_lifecycle.save_chat_turn"):
                    result = agent.run_hourglass_request(
                        "Explain a current question", session_id=f"web-{index}",
                        request_preflight=route)
                self.assertEqual(result["status"], "ok")
                self.assertTrue(context.call_args.kwargs["web"])

    def test_native_clarify_route_returns_local_question_without_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=load_settings(), root_dir=None, history_dir=Path(tmp))
            route = {"source": "jev_request_workflow", "choice": "clarify",
                     "native": True, "cost": 0.0}
            with patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.hourglass_lifecycle.policy_for", return_value=MagicMock()), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.hourglass_lifecycle.save_chat_turn") as save_turn, \
                 patch.object(agent, "_clear_chat_checkpoint") as clear, \
                 patch.object(agent, "_hourglass_request_context",
                              side_effect=AssertionError("clarification is local")), \
                 patch.object(agent, "_hourglass_answer_once",
                              side_effect=AssertionError("clarification is local")):
                result = agent.run_hourglass_request(
                    "Explain this", session_id="clarify",
                    request_preflight=route)
            state = agent.lifecycle.load("clarify")
        self.assertEqual(result["workflow_tier"], "clarification")
        save_turn.assert_called_once()
        clear.assert_not_called()
        self.assertEqual(state["status"], "deferred")
        self.assertEqual(state["stage"], "terminal")

    def test_native_workspace_route_without_root_is_deferred(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        route = {"source": "jev_request_workflow",
                 "choice": "workspace-inspection", "native": True,
                 "cost": 0.0}
        with patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
             patch("harness.hourglass_lifecycle.policy_for", return_value=MagicMock()), \
             patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
             patch.object(agent, "_workspace_context_deferred",
                          return_value={"status": "deferred"}) as defer:
            result = agent.run_hourglass_request(
                "Explain this project", request_preflight=route)
        self.assertEqual(result["status"], "deferred")
        defer.assert_called_once()

    def test_route_policy_cancellation_persists_interruption(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=None)
        policy = MagicMock()
        policy.evaluate_request_workflow.side_effect = ToolCancelled("stopped")
        with patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
             patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
             patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
             patch.object(agent, "_persist_chat_checkpoint") as persist, \
             self.assertRaises(ToolCancelled):
            agent.run_hourglass_request("Explain a topic", session_id="route-cancel")
        checkpoint = persist.call_args.args[1]
        self.assertEqual(checkpoint["status"], "interrupted")
        self.assertEqual(checkpoint["stage"], "route")

    def test_hourglass_resume_restores_cost_and_reuses_current_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=_ladder_settings(), root_dir=root,
                                    history_dir=root)
            checkpoint = {
                "route": "hourglass", "status": "interrupted",
                "stage": "jev_review", "prompt": "Explain photosynthesis",
                "intent": "conversation", "web_requested": False,
                "workspace_root": str(root), "workspace_context_used": False,
                "known_cost": 0.005, "unknown_liability": 0.002,
                "route_preflight": {"source": "jev_request_workflow",
                                    "choice": "answer", "native": True,
                                    "cost": 0.0},
                "context": "retained context", "context_files": [],
                "context_tokens": 4, "web_sources": [], "file_hashes": {},
                "answer": None, "rounds": [], "next_round": 1,
            }
            policy = _FakePolicy([(0.995, False, False)])
            governor = MagicMock()
            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, governor)), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch.object(agent, "_hourglass_request_context",
                              side_effect=AssertionError("current context should be reused")), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "model": "m1", "answer": "Plants store light energy.",
                     "cost": 0.001}), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request(
                    checkpoint["prompt"], session_id="hourglass-cost-resume",
                    resume_state=checkpoint)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(policy.calls[0][2], "retained context")
            governor.record_actual.assert_called_once_with(
                0.005, "chat-continuation", lane="answer")
            governor.retain_unknown.assert_called_once_with(
                0.002, "cancelled-chat-provider-call")

    def test_workspace_audit_routes_to_local_inspection_without_web(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=load_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            with patch.object(agent, "run_hourglass_request",
                              return_value={"status": "ok"}) as run, \
                 patch("harness.simple_answer_lifecycle.save_chat_turn"):
                result = agent.run_prompt(
                    "Can you audit this repo and find the biggest issues?",
                    session_id="repo-audit")
            self.assertEqual(result["status"], "ok")
            self.assertFalse(run.call_args.kwargs["web"])

    def test_workspace_intent_overrides_advisory_web_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)
            policy = _FakePolicy([(0.90, False, False)])
            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "model": "m1", "answer": "local review", "cost": 0.001}), \
                 patch.object(agent, "_gather_web_context",
                              side_effect=AssertionError("workspace work must not search")), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request(
                    "Can you audit this repo?", web=True,
                    request_preflight={
                        "source": "jev_request_workflow",
                        "choice": "web-research", "native": True,
                        "cost": 0.0,
                    })

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["workflow_tier"], "workspace-inspection")
            self.assertFalse(result["web_used"])

    def test_continue_without_saved_interruption_is_a_local_terminal_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=load_settings(),
                                    root_dir=Path(tmp), history_dir=Path(tmp))
            with patch("harness.simple_answer_lifecycle.policy_for",
                       side_effect=AssertionError("must not call Jev")), \
                 patch("harness.simple_answer_lifecycle.governed_completion",
                       side_effect=AssertionError("must not generate")):
                result = agent.run_prompt("continue", session_id="no-checkpoint")
            self.assertEqual(result["status"], "deferred")
            self.assertEqual(result["cost"], 0.0)
            self.assertIn("no interrupted request", result["response"])

    def test_workspace_selection_defer_is_resumable_after_folder_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=load_settings(), root_dir=None,
                                    history_dir=root)
            deferred = agent.run_prompt(
                "Can you audit this repo?", session_id="workspace-later")
            self.assertEqual(deferred["status"], "deferred")
            agent.root_dir = root
            policy = _FakePolicy([(0.90, False, False)])
            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "model": "m1", "answer": "local audit", "cost": 0.001}), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_prompt(
                    "continue", session_id="workspace-later")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["prompt"], "Can you audit this repo?")
            self.assertEqual(result["response"], "local audit")

    def test_continue_resumes_cancelled_hourglass_without_losing_original_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)
            policy = _FakePolicy([(0.90, False, False)])
            answers = iter([
                ToolCancelled("cancelled after dispatch", usage_unknown=True),
                {"model": "m1", "answer": "repo answer", "cost": 0.001},
            ])

            def answer_once(*args, **kwargs):
                value = next(answers)
                if isinstance(value, Exception):
                    raise value
                return value

            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch.object(agent, "_hourglass_answer_once",
                              side_effect=answer_once), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                with self.assertRaises(ToolCancelled):
                    agent.run_hourglass_request(
                        "Explain the router", session_id="resume-hourglass")
                result = agent.run_prompt(
                    "continue", session_id="resume-hourglass")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["prompt"], "Explain the router")
            self.assertEqual(result["response"], "repo answer")
            self.assertEqual(len(policy.calls), 1)

    def test_continue_reuses_candidate_when_jev_review_was_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)

            class CancelThenReview(_FakePolicy):
                def evaluate_answer(self, prompt, answer, context, **kwargs):
                    if not self.calls:
                        self.calls.append((prompt, answer, context, kwargs))
                        raise ToolCancelled("review cancelled", usage_unknown=True)
                    return super().evaluate_answer(prompt, answer, context, **kwargs)

            policy = CancelThenReview([(0.90, False, False)])
            answer = {"model": "m1", "answer": "saved candidate", "cost": 0.001}
            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch.object(agent, "_hourglass_answer_once",
                              return_value=answer) as generate, \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                with self.assertRaises(ToolCancelled):
                    agent.run_hourglass_request(
                        "Explain the router", session_id="resume-review")
                result = agent.run_prompt("continue", session_id="resume-review")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["response"], "saved candidate")
            generate.assert_called_once()
            self.assertEqual(len(policy.calls), 2)

    def test_workspace_context_checkpoint_invalidates_on_added_relevant_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "router.py").write_text("def route(): return 'old'\n")
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)
            prompt = "Can you inspect this repo: router.py?"
            checkpoint = {
                "prompt": prompt, "workspace_root": str(root),
                "workspace_context_used": True,
                "context_files": [], "file_hashes": {},
            }
            self.assertFalse(agent._hourglass_context_matches(checkpoint))

    def test_workspace_context_checkpoint_invalidates_when_selected_root_changes(self):
        with tempfile.TemporaryDirectory() as first, \
             tempfile.TemporaryDirectory() as second:
            agent = AutonomousAgent(
                settings=load_settings(), root_dir=Path(second),
                history_dir=Path(second))
            checkpoint = {
                "prompt": "Can you inspect this repo?",
                "workspace_root": first,
                "workspace_context_used": True,
                "context_files": [], "file_hashes": {},
            }
            self.assertFalse(agent._hourglass_context_matches(checkpoint))

    def test_loop_reiterates_only_when_jev_requests_it(self):
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
        with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          side_effect=lambda *a, **k: next(answers)), \
             patch("harness.hourglass_lifecycle.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="answer-loop")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], "second")
        self.assertEqual(result["model"], "capable")
        self.assertEqual(len(policy.calls), 2)
        self.assertEqual(result["confidence"]["threshold"], 0.70)
        self.assertTrue(result["confidence"]["passed"])
        self.assertEqual(len(result["hourglass"]["rounds"]), 2)

    def test_high_jev_score_stops_without_independent_completion_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings()
            agent = AutonomousAgent(settings=settings, root_dir=root,
                                    history_dir=root)
            policy = _FakePolicy([(0.90, False, False)])
            attempts = iter([{"model": "m1", "answer": "complete", "cost": 0}])
            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
                 patch.object(agent, "_hourglass_answer_once", side_effect=lambda *a, **k: next(attempts)), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request("Explain router behavior", max_rounds=2)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["hourglass"]["rounds"]), 1)
            self.assertEqual(result["verification"]["status"], "jev_verified")
            self.assertEqual(policy.route_calls, 1)
            self.assertEqual(len(policy.calls), 1)
            state = agent.lifecycle.load("default")
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["review"]["decision"], "pass")
            self.assertEqual(state["review"]["rounds"], 1)
            self.assertTrue(all(
                call["status"] == "completed"
                for call in state["usage"]["provider_calls"].values()))

    def test_low_score_without_iteration_request_stops_without_extra_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=load_settings(), root_dir=root,
                                    history_dir=root)
            policy = _FakePolicy([(0.60, False, False)])
            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.hourglass_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "model": "m1", "answer": "candidate", "cost": 0.001}) as generate, \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request(
                    "Explain router behavior", max_rounds=3)
            self.assertEqual(result["status"], "needs_iteration")
            self.assertEqual(len(policy.calls), 1)
            generate.assert_called_once()

    def test_fallback_reason_is_visible_without_a_second_generic_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=load_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            class FallbackPolicy:
                def evaluate_answer(self, *args, **kwargs):
                    result = JevEvaluationResult(
                        "fail", 0.0, 0.0, {}, ["Jev unavailable"],
                        is_fallback=True, fallback_reason="transport_error")
                    return result, {"native": False, "answer_sufficient": None,
                                    "iteration_required": True, "plan_required": None,
                                    "cost": 0.0, "input_tokens": 0,
                                    "output_tokens": 0,
                                    "pack_version": "answer-sufficiency-v1"}
            with patch("harness.hourglass_lifecycle.policy_for", return_value=FallbackPolicy()), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "model": "m", "answer": "verified answer", "cost": 0}), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request("Answer a question")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["jev"]["fallback_reason"], "transport_error")
            self.assertEqual(result["verification"]["status"], "unverified")

    def test_unusable_live_jev_response_defers_without_completion_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=load_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))

            class RejectedPolicy:
                def evaluate_answer(self, *args, **kwargs):
                    result = JevEvaluationResult(
                        "fail", 0.0, 0.0, {},
                        ["TypeSafe request rejected (HTTP 401)"],
                        is_fallback=False, fallback_reason=None)
                    return result, {"native": False, "is_fallback": False,
                                    "fallback_reason": None,
                                    "answer_sufficient": None,
                                    "iteration_required": True,
                                    "plan_required": None, "cost": 0.0,
                                    "input_tokens": 0, "output_tokens": 0,
                                    "pack_version": "answer-sufficiency-v1"}

            with patch("harness.hourglass_lifecycle.policy_for", return_value=RejectedPolicy()), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
                 patch.object(agent, "_hourglass_answer_once", return_value={
                     "model": "m", "answer": "candidate", "cost": 0.0}), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request("Answer a question")
            self.assertEqual(result["status"], "deferred")
            self.assertIn("HTTP 401", result["remaining_scope"])

    def test_default_entrypoints_require_opt_in_before_jev_apply(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        with patch.object(agent, "_live_jev_available", return_value=True), \
             patch.object(agent, "_handle_edit", return_value={"status": "ok"}) as edit:
            agent.run_prompt("Update widget.py")
        self.assertFalse(edit.call_args.args[2])
        self.assertFalse(edit.call_args.kwargs["use_jev_completion"])
        with patch.object(agent, "_live_jev_available", return_value=True), \
             patch.object(agent, "_handle_edit", return_value={"status": "ok"}) as edit:
            agent.run_prompt("Update widget.py", force_conversation=True,
                             auto_apply=True)
        self.assertTrue(edit.call_args.kwargs["use_jev_completion"])
        with patch.object(agent, "_live_jev_available", return_value=True), \
             patch.object(agent, "_handle_conversation", return_value={"status": "preview"}) as convo, \
             patch.object(agent, "_handle_edit", return_value={"status": "preview"}) as edit:
            agent.run_prompt("Update widget.py", force_conversation=True,
                             auto_apply=False)
        edit.assert_not_called()
        convo.assert_called_once()

    def test_live_jev_non_simple_conversation_routes_to_hourglass(self):
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path("."))
        with patch.object(agent, "_live_jev_available", return_value=True), \
             patch.object(agent, "run_hourglass_request", return_value={"status": "ok"}) as run:
            agent.run_prompt("What is happening tomorrow?")
        run.assert_called_once()

    def test_answer_generation_keeps_history_web_but_jev_payload_excludes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_ladder_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            policy = _FakePolicy([(0.2, True, False), (0.999, False, False)])
            seen_generation = []
            attempts = iter([{"model": "m1", "answer": "retry me", "cost": 0},
                             {"model": "m2", "answer": "answered", "cost": 0}])

            def answer_once(*args, **kwargs):
                seen_generation.append(kwargs)
                return next(attempts)

            with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
                 patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
                 patch("harness.hourglass_lifecycle.load_chat_history", return_value=[
                     {"prompt": "private prior question", "response": "prior answer"}]), \
                 patch.object(agent, "_hourglass_request_context", return_value=(
                     "WEB EVIDENCE: fetched secret body", {},
                     MagicMock(estimated_tokens=4), [{"kind": "web", "ok": True,
                         "url": "https://example.test", "title": "Result",
                         "text": "fetched secret body"}])), \
                 patch.object(agent, "_hourglass_answer_once", side_effect=answer_once), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request("current question", web=True,
                                                     max_rounds=2)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(seen_generation), 2)
            self.assertEqual(seen_generation[0]["past_turns"][0]["prompt"],
                             "private prior question")
            self.assertIn("fetched secret body",
                          seen_generation[0]["web_sources"][0]["text"])
            self.assertTrue(all("WEB EVIDENCE" in call[2]
                                for call in policy.calls))

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
        with patch("harness.hourglass_lifecycle.policy_for", return_value=_FakePolicy([])), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())):
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
                    model="jev-latest", fallback_reason="missing_key")
                return result, {"capability": "answer", "native": False,
                               "answer_sufficient": None,
                               "iteration_required": True, "plan_required": None,
                               "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
                               "pack_version": "answer-sufficiency-v1"}

        with patch("harness.hourglass_lifecycle.policy_for", return_value=_UnkeyedPolicy()), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.hourglass_lifecycle.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="deferred-1")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verification"]["status"], "unverified")
        self.assertTrue(result["jev"]["fallback_reason"])

    def test_plan_required_stops_before_any_write(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)
        policy = _FakePolicy([(0.995, False, True)])
        with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.hourglass_lifecycle.save_chat_turn"):
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
        with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.hourglass_lifecycle.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="needs-iter-1",
                max_rounds=3)

        self.assertEqual(result["status"], "needs_iteration")
        self.assertIn("remaining_scope", result)

    def _exhausted_rounds(self, completion=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        settings = load_settings()
        settings.jev_api_key = None
        agent = AutonomousAgent(settings=settings, root_dir=root, history_dir=root)
        policy = _FakePolicy([(0.2, True, False)] * 2)
        with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, object())), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.hourglass_lifecycle.save_chat_turn"):
            result = agent.run_hourglass_request(
                "How does the router work?", session_id="last-round",
                max_rounds=2)
        return result, policy

    def test_last_round_does_not_call_generic_completion_judge(self):
        result, policy = self._exhausted_rounds()
        self.assertEqual(result["status"], "needs_iteration")
        self.assertEqual(len(policy.calls), 2)

    def test_iteration_rounds_remain_bounded_by_jev_request(self):
        result, policy = self._exhausted_rounds()
        self.assertEqual(result["status"], "needs_iteration")
        self.assertEqual(len(policy.calls), 2)

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
        with patch("harness.hourglass_lifecycle.policy_for", return_value=policy), \
             patch("harness.hourglass_lifecycle.governor_for", return_value=(None, gov)), \
             patch.object(agent, "_hourglass_answer_once",
                          return_value={"model": "m", "answer": "a", "cost": 0.0}), \
             patch("harness.hourglass_lifecycle.save_chat_turn"):
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

            with patch("harness.edit_lifecycle.apply_session", return_value=mock_engine), \
                 patch("harness.edit_lifecycle.governor_for",
                      return_value=(None, test_gov)), \
                 patch("harness.hourglass_lifecycle.governor_for",
                      return_value=(None, test_gov)), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                result = agent.run_hourglass_request(
                    "Update calc.py with type annotations", auto_apply=True)

        self.assertEqual(result["status"], "ok")
        self.assertIn("completion_jev", result["hourglass"]["stages"])


if __name__ == "__main__":
    unittest.main()
