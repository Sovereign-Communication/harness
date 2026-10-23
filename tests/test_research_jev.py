"""Research judgments exercise the shared typed evaluator and spend owner."""
import os
import tempfile
import unittest
from unittest.mock import patch

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev_packs import research_plan_question_pack
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport


def _choice(value, options):
    return {
        "type": "choice", "choice": value, "confidence": 0.99,
        "probabilities": {
            option: 0.99 if option == value else 0.01 / (len(options) - 1)
            for option in options
        },
    }


class _Transport:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, {
            "model": "jev-research-test", "answers": self.answers,
            "usage": {"input_tokens": 1000, "output_tokens": 30},
        }


class ResearchJevTests(unittest.TestCase):
    def _policy(self, answers, *, keyed=True, max_cost=0.05):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch("harness.config.CONFIG_DIR", temp.name):
            settings = load_settings({"jev_api_key": "jev-research-test"})
        settings.jev_api_key = "jev-research-test" if keyed else None
        settings.jev_disabled = False
        transport = _Transport(answers)
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=max_cost)
        ledger = AutonomyLedger(os.path.join(temp.name, "ledger.jsonl"))
        policy = policy_for(settings, transport=transport,
                            governor=governor, ledger=ledger)
        return policy, transport, governor, ledger

    def _state(self):
        return {
            "request": "Audit target.example calculator copy: " + self.id(),
            "now_utc": "2026-10-22T12:00:00Z",
            "limits": {"max_queries": 2, "max_results": 8},
            "plan": {
                "needed": True, "reason": "Need actual calculator copy",
                "queries": ["site:target.example calculator"],
                "target_domains": ["target.example"],
                "urls": ["https://target.example/"],
                "freshness": {"max_age_days": None, "require_date": False},
                "evidence": {"requirements": ["actual calculator labels"],
                             "min_sources": 1, "require_page_content": True},
                "budget": {"max_queries": 1, "max_results": 3,
                           "max_fetches": 2, "max_cost": 0.01,
                           "max_seconds": 15, "max_chars": 3000},
            },
        }

    def test_plan_review_uses_normalized_choices_and_one_accounted_call(self):
        questions = research_plan_question_pack()
        answers = {
            "decision": _choice("accept", questions["decision"]["criteria"]),
            "revision_focus": _choice(
                "none", questions["revision_focus"]["criteria"]),
        }
        policy, transport, governor, ledger = self._policy(answers)
        state = self._state()
        result, structural = policy.evaluate_research_plan(
            state, task_id="research-request")

        self.assertFalse(result.is_fallback)
        self.assertEqual(result.answers["decision"]["choice"], "accept")
        self.assertEqual(result.answers["decision"]["unmatched_options"], [])
        self.assertEqual(result.answers["revision_focus"]["choice"], "none")
        self.assertEqual(transport.calls[0]["state"], state)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(structural["site"], "research_plan")
        self.assertAlmostEqual(governor.spent, result.cost)
        events = ledger.entries()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], "research_plan")
        self.assertEqual(events[0]["task_id"], "research-request")

    def test_result_batch_keeps_relevance_and_uncertainty_separate(self):
        choices = ("relevant", "irrelevant", "uncertain")
        answers = {"r0": _choice("relevant", choices),
                   "r1": _choice("irrelevant", choices),
                   "r2": _choice("uncertain", choices)}
        policy, transport, governor, ledger = self._policy(answers)
        state = self._state()
        state["candidates"] = [
            {"id": "r0", "url": "https://target.example/", "kind": "page",
             "title": "Calculator", "text": "Battery capacity in kWh"},
            {"id": "r1", "url": "https://unrelated.example/audit", "kind": "page",
             "title": "Audit", "text": "Microsoft Exchange audit logs"},
            {"id": "r2", "url": "https://target.example/", "kind": "snippet",
             "title": "Calculator", "text": "A calculator exists"},
        ]
        result, structural = policy.evaluate_research_results(state)

        self.assertFalse(result.is_fallback)
        self.assertEqual({key: answer["choice"] for key, answer
                          in result.answers.items()},
                         {"r0": "relevant", "r1": "irrelevant", "r2": "uncertain"})
        questions = transport.calls[0]["questions"]
        self.assertEqual(set(questions), {"r0", "r1", "r2"})
        for index in range(3):
            instructions = questions["r" + str(index)]["instructions"]
            self.assertIn("candidates[" + str(index) + "]", instructions)
            self.assertIn("untrusted", instructions)
            self.assertIn("require_page_content", instructions)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(structural["site"], "research_results")
        self.assertAlmostEqual(governor.spent, result.cost)
        self.assertEqual(len(ledger.entries()), 1)

    def test_no_key_yields_no_approval_answers_for_either_stage(self):
        policy, transport, governor, _ = self._policy({}, keyed=False)
        state = self._state()
        state["candidates"] = [{"id": "r0"}]
        for method in (policy.evaluate_research_plan,
                       policy.evaluate_research_results):
            with self.subTest(stage=method.__name__):
                result, structural = method(state)
                self.assertTrue(result.is_fallback)
                self.assertEqual(result.answers, {})
                self.assertTrue(structural["is_fallback"])
                self.assertEqual(structural["fallback_reason"], "missing_key")
        self.assertEqual(transport.calls, [])
        self.assertEqual(governor.spent, 0)

    def test_plan_preflight_refusal_has_no_native_approval(self):
        policy, transport, governor, _ = self._policy({}, max_cost=0.000001)
        result, structural = policy.evaluate_research_plan(self._state())
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.answers, {})
        self.assertEqual(transport.calls, [])
        self.assertEqual(structural["cost"], 0)
        self.assertEqual(governor.spent, 0)

    def test_missing_result_answer_invalidates_batch_in_shared_parser(self):
        policy, transport, governor, _ = self._policy({
            "r0": _choice("relevant", ("relevant", "irrelevant", "uncertain")),
        })
        state = self._state()
        state["candidates"] = [{"id": "r0"}, {"id": "r1"}]
        result, _ = policy.evaluate_research_results(state)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.answers, {})
        self.assertTrue(result.discarded)
        self.assertGreater(governor.spent, 0)

    def test_out_of_vocabulary_decision_is_not_an_approval(self):
        questions = research_plan_question_pack()
        policy, transport, _, _ = self._policy({
            "decision": _choice("approve", ("approve", "revise", "decline")),
            "revision_focus": _choice("none", questions["revision_focus"]["criteria"]),
        })
        result, _ = policy.evaluate_research_plan(self._state())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.answers, {})

    def test_invalid_candidate_batches_refuse_before_dispatch(self):
        policy, transport, _, _ = self._policy({})
        for candidates in ([], [{"id": "r0"}] * 2,
                           [{"id": "r" + str(i)} for i in range(9)],
                           [{}], [{"id": " "}]):
            with self.subTest(candidates=candidates):
                with self.assertRaises(HarnessError):
                    policy.evaluate_research_results({"candidates": candidates})
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
