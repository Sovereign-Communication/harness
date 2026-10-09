"""Hermetic JEV-P1 policy-owner contract tests."""
import os
import tempfile
import unittest
from unittest.mock import patch

from harness.config import load_settings
from harness.errors import ToolCancelled
from harness.jev import JevEvaluationResult
from harness.jev_policy import (
    JEV_MAX_INPUT_TOKENS,
    aggregate_structural,
    jev_cost_ceiling,
    policy_for,
)
from harness.ledger import AutonomyLedger
from harness.provider_errors import ProviderSpendLimitError
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
        self.assertEqual(structural["fallback_reason"], "missing_key")
        self.assertEqual(structural["input_tokens"], 0)
        self.assertEqual(structural["site"], "apply")

    def test_explicit_disable_ignores_injected_keyed_evaluator(self):
        class KeyedEvaluator:
            api_key = "separately-supplied"
            def evaluate(self, *args, **kwargs):
                raise AssertionError("disabled policy invoked injected evaluator")

        with patch.dict(os.environ, {"HARNESS_JEV_DISABLE": "1"}):
            settings = load_settings({"jev_api_key": "configured"})
        policy = policy_for(settings, evaluator=KeyedEvaluator())
        result, _ = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertFalse(policy.keyed)
        self.assertEqual(result.fallback_reason, "explicit_disable")

    def test_fallback_reason_is_recorded_in_ledger(self):
        ledger = self._ledger()
        policy = policy_for(self._unkeyed_settings(), ledger=ledger)
        _, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertEqual(structural["fallback_reason"], "missing_key")
        event = ledger.entries()[0]
        self.assertEqual(event["fallback_reason"], "missing_key")

    def test_completion_transport_fallback_reason_survives_policy_wrapper(self):
        class BrokenTransport:
            def post(self, *args, **kwargs):
                raise OSError("offline")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.50)
        result, structural = policy_for(
            settings, transport=BrokenTransport(), governor=governor
        ).evaluate_completion_nouls(
            "fulfill the request", "work is done", named_artifacts=[])
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.fallback_reason, "transport_failure")
        self.assertEqual(structural["fallback_reason"], "transport_failure")

    def test_answer_preflight_refusal_is_not_a_fallback(self):
        class NeverCalledTransport:
            def post(self, *args, **kwargs):
                raise AssertionError("preflight refusal must happen before dispatch")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.000001)
        result, structural = policy_for(
            settings, transport=NeverCalledTransport(), governor=governor
        ).evaluate_answer("question", "candidate", "context")
        self.assertFalse(result.is_fallback)
        self.assertFalse(structural["is_fallback"])
        self.assertFalse(structural["native"])

    def test_answer_cancellation_releases_reservation_once_and_propagates(self):
        class CancellingEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, *args, **kwargs):
                raise ToolCancelled()

        class CountingGovernor:
            def __init__(self):
                self.reconciled = []

            def reserve(self, worst, label):
                return (label, worst)

            def reconcile(self, token, actual):
                self.reconciled.append((token, actual))

        governor = CountingGovernor()
        policy = policy_for(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=CancellingEvaluator(), governor=governor)

        with self.assertRaises(ToolCancelled):
            policy.evaluate_answer("question", "candidate", "context")

        self.assertEqual(len(governor.reconciled), 1)
        self.assertEqual(governor.reconciled[0][1], 0.0)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_provider_spend_limit_propagates_and_settles_known_usage(self):
        class CappedEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, *args, **kwargs):
                raise ProviderSpendLimitError("cap", known_cost=0.000004)

        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        ledger = self._ledger()
        policy = policy_for(
            load_settings({"jev_api_key": "jev-key"}), evaluator=CappedEvaluator(),
            governor=governor, ledger=ledger)
        with self.assertRaises(ProviderSpendLimitError) as ctx:
            policy.evaluate_answer("question", "candidate", "context",
                                   task_id="provider-cap-task")
        self.assertEqual(ctx.exception.kind, "provider_spend_limit")
        self.assertTrue(ctx.exception.cost_accounted)
        self.assertAlmostEqual(governor.spent, 0.000004)
        self.assertAlmostEqual(governor.outstanding, 0.0)
        rows = [entry for entry in ledger.entries()
                if entry["event"] == "jev_eval"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "provider-cap-task")
        self.assertEqual(rows[0]["result_state"], "provider_spend_limit")
        self.assertEqual(rows[0]["error_kind"], "provider_spend_limit")
        self.assertAlmostEqual(rows[0]["cost"], 0.000004)
        self.assertAlmostEqual(ledger.cost_report()["total_cost"], 0.000004)

    def test_cancellation_after_settlement_does_not_reconcile_twice(self):
        class Evaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, *args, **kwargs):
                return JevEvaluationResult(
                    "pass", 0.9, 0.9,
                    {"answer_sufficient": {"noul": 0.9},
                     "iteration_required": {"noul": 0.1},
                     "plan_required": {"noul": 0.1}},
                    [], input_tokens=10, model=self.model)

        class CountingGovernor:
            def __init__(self):
                self.reconciled = []

            def reserve(self, worst, label):
                return (label, worst)

            def reconcile(self, token, actual):
                self.reconciled.append((token, actual))

        class CancellingLedger:
            def append(self, *args, **kwargs):
                raise ToolCancelled()

        governor = CountingGovernor()
        policy = policy_for(
            load_settings({"jev_api_key": "jev-key"}), evaluator=Evaluator(),
            governor=governor, ledger=CancellingLedger())

        with self.assertRaises(ToolCancelled):
            policy.evaluate_answer("question", "candidate", "context")

        self.assertEqual(len(governor.reconciled), 1)
        self.assertIsNone(getattr(policy._tl, "token", None))

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
        expected = 100 * 0.042 / 1_000_000
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
            "sk-test", max_cost=0.000001)
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

    def test_cost_ceiling_helper_is_fixed_input_math(self):
        self.assertEqual(jev_cost_ceiling(),
                         JEV_MAX_INPUT_TOKENS * 0.042 / 1_000_000)

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
