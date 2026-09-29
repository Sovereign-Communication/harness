"""Hermetic JEV-P1 policy-owner contract tests."""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.errors import HarnessError, JevSettlementError
from harness.jev import JevEvaluationResult, jev_cost
from harness.jev_policy import (
    JEV_MAX_INPUT_TOKENS,
    aggregate_structural,
    jev_cost_ceiling,
    policy_for,
)
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


class JevPolicyTests(unittest.TestCase):
    def _response(self, *, tokens=100, verdict_noul=0.95, confidence=0.9):
        return {
            "model": "jev-test",
            "answers": {
                "instruction_matches": {
                    "type": "noul", "noul": verdict_noul,
                },
            },
            "usage": {"input_tokens": tokens, "output_tokens": 3},
        }

    def _ledger(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))

    @staticmethod
    def _unkeyed_settings():
        settings = load_settings()
        settings.jev_api_key = None
        return settings

    def test_unkeyed_fallback_is_zero_cost_and_enveloped(self):
        settings = self._unkeyed_settings()
        policy = policy_for(settings, evaluator=None)
        result, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.cost, 0.0)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(structural["input_tokens"], 0)
        self.assertEqual(structural["site"], "apply")

    def test_live_call_reserves_and_records_actual_once(self):
        transport = _JevTransport(self._response(tokens=100))
        models = [m("jev-test", prompt="0", completion="0")]
        governor = SpendGovernor(
            FakeTransport(models=models),            "sk-test", max_cost=0.10)

        # SpendGovernor's catalog transport and Jev transport are independent.
        ledger = self._ledger()
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=governor,
                            ledger=ledger)
        result, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply", task_id="t1")
        expected = 100 * 42 / 1_000_000
        self.assertFalse(result.is_fallback)
        self.assertAlmostEqual(result.cost, expected)
        self.assertAlmostEqual(governor.spent, expected)
        self.assertEqual(len(transport.calls), 1)
        events = ledger.entries()
        self.assertEqual([e["event"] for e in events], ["jev_eval"])
        self.assertEqual(events[0]["site"], "apply")
        self.assertEqual(events[0]["input_tokens"], 100)
        self.assertAlmostEqual(events[0]["cost"], expected)
        self.assertEqual(structural["cost"], result.cost)

    def test_preflight_refuses_before_transport(self):
        transport = _JevTransport(self._response(tokens=100))
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.001)
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=governor)
        result, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(transport.calls, [])
        self.assertEqual(structural["site"], "apply")

    def test_evaluate_diff_propagates_settlement_overrun_after_ledgering(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            byok_path = os.path.join(tmp, "byok.json")
            with open(byok_path, "w", encoding="utf-8") as stream:
                stream.write("[]")
            governor = SpendGovernor(
                None, None, max_cost=0.05, byok_prefixes_path=byok_path)
            settings = load_settings({"jev_api_key": "jev-key"})
            result = JevEvaluationResult(
                "pass", 0.9, 0.9, {}, [], cost=jev_cost(1322),
                input_tokens=1322, model="jev-test")

            class FixedEvaluator:
                api_key = "jev-key"
                model = "jev-test"

                def verify_diff_mechanics(self, *_args, preflight=None,
                                          **_kwargs):
                    preflight()
                    return result

            policy = policy_for(
                settings, governor=governor, ledger=ledger,
                evaluator=FixedEvaluator())

            with self.assertRaisesRegex(HarnessError, "actual running cost"):
                policy.evaluate_diff(
                    "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
                    "change x", "x.py", site="apply")

            events = [event for event in ledger.entries()
                      if event["event"] == "jev_eval"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["input_tokens"], 1322)
            self.assertAlmostEqual(events[0]["cost"], jev_cost(1322))
            self.assertFalse(events[0]["is_fallback"])
            self.assertAlmostEqual(governor.spent, governor.max_cost)
            self.assertEqual(governor.outstanding, 0.0)
            with self.assertRaisesRegex(HarnessError, "prior billed response"):
                governor.reserve(0.000001, "jev:after-overrun")

    def test_public_evaluators_propagate_settlement_errors(self):
        result = JevEvaluationResult(
            "pass", 0.9, 0.9, {
                "answer_sufficient": {"noul": 0.9},
                "iteration_required": {"noul": 0.1},
                "plan_required": {"noul": 0.1},
                "route": {"choice": "diff"},
                "requires_iteration": {"noul": 0.1},
                "escalation_decision": {"noul": 0.9},
                "capability_budget": {"noul": 0.9},
                "file_0_relevant": {"noul": 0.9},
                "claim_0_supported": {"noul": 0.9},
                "named_artifacts_present": {"noul": 0.9},
                "goal_achieved": {"noul": 0.9},
                "scope_coverage": {"score": 0.9},
                "success_definition_met": {"noul": 0.9},
                "claims_supported": {"noul": 0.9},
                "needs_human": {"noul": 0.1},
                "complexity_class": {"choice": "medium"},
            }, [], cost=jev_cost(32), input_tokens=32,
            output_tokens=4, model="jev-test")

        class SettlementEvaluator:
            api_key = "jev-test-key"
            model = "jev-test"

            def evaluate(self, *_args, **_kwargs):
                return result

            def evaluate_plan_requirements(self, *_args, **_kwargs):
                return result

        class ReservationGovernor:
            @staticmethod
            def reserve(_amount, label):
                return ("reservation", label)

            @staticmethod
            def reconcile(_token, _cost):
                raise HarnessError("settlement denied")

        policy = policy_for(
            self._unkeyed_settings(), evaluator=SettlementEvaluator(),
            governor=ReservationGovernor())
        methods = (
            ("answer", lambda: policy.evaluate_answer("request", "answer")),
            ("triage", lambda: policy.evaluate_triage("change", ["src/a.py"])),
            ("escalation", lambda: policy.evaluate_escalation_decision("failed gate")),
            ("plan", lambda: policy.evaluate_plan("change", ["src/a.py"])),
            ("route", lambda: policy.evaluate_route("change", ["src/a.py"])),
            ("file triage", lambda: policy.evaluate_file_triage(
                "change", ["src/a.py"], ["src/a.py"])),
            ("claim support", lambda: policy.evaluate_claim_support(
                ["the change is safe"], "review evidence", enabled=True)),
            ("completion", lambda: policy.evaluate_completion_nouls(
                "complete the change", "work finished", named_artifacts=[])),
            ("scope", lambda: policy.evaluate_scope({
                "mission_id": "mission-1", "request": "change",
                "success_definition": "the change is complete",
                "scope": {"in_scope": ["src/a.py"], "out_of_scope": []},
                "verifier_holds": True,
            })),
        )
        self.assertEqual(JevSettlementError.kind, "jev_settlement_error")
        for name, invoke in methods:
            with self.subTest(method=name):
                with self.assertRaises(JevSettlementError) as caught:
                    invoke()
                self.assertIsInstance(caught.exception.__cause__, HarnessError)

    def test_over_budget_response_is_ledgered_before_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            byok_path = os.path.join(tmp, "byok.json")
            with open(byok_path, "w", encoding="utf-8") as stream:
                stream.write("[]")
            governor = SpendGovernor(
                None, None, max_cost=0.05, byok_prefixes_path=byok_path)
            settings = load_settings({"jev_api_key": "jev-key"})
            policy = policy_for(settings, governor=governor, ledger=ledger)
            reservation = governor.reserve(jev_cost(1024), "jev:audit_dimensions")
            result = JevEvaluationResult(
                "pass", 0.9, 0.9, {}, [], cost=jev_cost(1322),
                input_tokens=1322, model="jev-test")

            with self.assertRaisesRegex(HarnessError, "actual running cost"):
                policy._account(
                    result, site="audit_dimensions", reservation=reservation)

            events = [event for event in ledger.entries()
                      if event["event"] == "jev_eval"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["input_tokens"], 1322)
            self.assertAlmostEqual(events[0]["cost"], jev_cost(1322))
            self.assertEqual(governor.outstanding, 0.0)
            self.assertAlmostEqual(governor.spent, governor.max_cost)
            with self.assertRaisesRegex(HarnessError, "prior billed response"):
                governor.reserve(0.000001, "jev:after-overrun")

    def test_over_budget_unreserved_response_is_ledgered_before_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            byok_path = os.path.join(tmp, "byok.json")
            with open(byok_path, "w", encoding="utf-8") as stream:
                stream.write("[]")
            governor = SpendGovernor(
                None, None, max_cost=0.05, byok_prefixes_path=byok_path)
            settings = load_settings({"jev_api_key": "jev-key"})
            policy = policy_for(settings, governor=governor, ledger=ledger)
            result = JevEvaluationResult(
                "pass", 0.9, 0.9, {}, [], cost=jev_cost(1322),
                input_tokens=1322, model="jev-test")

            with self.assertRaisesRegex(HarnessError, "actual running cost"):
                policy._account(result, site="audit_dimensions")

            events = [event for event in ledger.entries()
                      if event["event"] == "jev_eval"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["input_tokens"], 1322)
            self.assertAlmostEqual(events[0]["cost"], jev_cost(1322))
            self.assertAlmostEqual(governor.spent, governor.max_cost)
            self.assertEqual(governor.outstanding, 0.0)
            with self.assertRaisesRegex(HarnessError, "prior billed response"):
                governor.reserve(0.000001, "jev:after-overrun")

    def test_paid_fallback_response_is_settled_against_governor(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            byok_path = os.path.join(tmp, "byok.json")
            with open(byok_path, "w", encoding="utf-8") as stream:
                stream.write("[]")
            governor = SpendGovernor(
                None, None, max_cost=0.05, byok_prefixes_path=byok_path)
            settings = load_settings({"jev_api_key": "jev-key"})
            policy = policy_for(settings, governor=governor, ledger=ledger)
            reservation = governor.reserve(
                jev_cost(1024), "jev:audit_dimensions")
            cost = jev_cost(200)
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, ["invalid response"], cost=cost,
                input_tokens=200, is_fallback=True, model="jev-test")

            policy._account(
                result, site="audit_dimensions", reservation=reservation)

            events = [event for event in ledger.entries()
                      if event["event"] == "jev_eval"]
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["is_fallback"])
            self.assertAlmostEqual(events[0]["cost"], cost)
            self.assertAlmostEqual(governor.spent, cost)
            self.assertEqual(governor.outstanding, 0.0)

    def test_unreserved_paid_fallback_response_is_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            byok_path = os.path.join(tmp, "byok.json")
            with open(byok_path, "w", encoding="utf-8") as stream:
                stream.write("[]")
            governor = SpendGovernor(
                None, None, max_cost=0.05, byok_prefixes_path=byok_path)
            settings = load_settings({"jev_api_key": "jev-key"})
            policy = policy_for(settings, governor=governor, ledger=ledger)
            cost = jev_cost(200)
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, ["invalid response"], cost=cost,
                input_tokens=200, is_fallback=True, model="jev-test")

            policy._account(result, site="audit_dimensions")

            events = [event for event in ledger.entries()
                      if event["event"] == "jev_eval"]
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["is_fallback"])
            self.assertAlmostEqual(events[0]["cost"], cost)
            self.assertAlmostEqual(governor.spent, cost)
            self.assertEqual(governor.outstanding, 0.0)

    def test_cost_ceiling_helper_is_fixed_input_math(self):
        self.assertEqual(jev_cost_ceiling(),
                         JEV_MAX_INPUT_TOKENS * 42 / 1_000_000)

    def test_aggregate_empty_and_mixed_models(self):
        self.assertIsNone(aggregate_structural([], site="batch"))
        combined = aggregate_structural([
            {"structural": {
                "verdict": "pass", "confidence": 0.9, "supported": 0.9,
                "cost": 0.001, "input_tokens": 10,
                "is_fallback": False, "model": "a", "site": "x",
            }},
            {"structural": {
                "verdict": "pass", "confidence": 0.8, "supported": 0.95,
                "cost": 0.002, "input_tokens": 20,
                "is_fallback": True, "model": "b", "site": "x",
            }},
        ], site="batch")
        self.assertEqual(combined["model"], "mixed")
        self.assertEqual(combined["input_tokens"], 30)
        self.assertAlmostEqual(combined["cost"], 0.003)
        self.assertFalse(combined["is_fallback"])

    def test_invalid_question_pack_returns_fail_not_exception(self):
        transport = _JevTransport(self._response())
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport)
        result = policy.evaluator.evaluate({"x": 1}, {"bad": {"type": "text"}})
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
