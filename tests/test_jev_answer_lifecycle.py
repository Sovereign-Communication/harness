"""Hermetic contracts for the all-request answer/reiterate lifecycle."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
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


if __name__ == "__main__":
    unittest.main()
