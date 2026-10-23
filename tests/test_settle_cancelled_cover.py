"""Hermetic cover for the three settle-cancelled handler bodies (D12).

Each ``except ProviderUsageUnknown`` / ``except ToolCancelled`` body in
``jev_policy.py`` that settles a live reservation must be executed by the
suite: ``evaluate_answer`` + usage-unknown, ``evaluate_issue_sort`` +
usage-unknown, and ``evaluate_provision`` + cancellation. The sibling
combinations were already covered; these three were the remaining D12 gaps
on the GUI-cancellation lane.
"""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.errors import ProviderUsageUnknown, ToolCancelled
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport


class _LostResponseTransport:
    """Transport whose billable request loses its usage response."""

    def post(self, *args, **kwargs):
        raise ProviderUsageUnknown(
            "provider response ended before usage arrived",
            known_cost=0.004)


class _UnknownUsageEvaluator:
    api_key = "jev-key"
    model = "jev-test"

    def __init__(self):
        self.calls = 0

    def evaluate(self, state, questions=None):
        self.calls += 1
        raise ProviderUsageUnknown(
            "provider response ended before usage arrived",
            known_cost=0.004)


class _CancellingEvaluator:
    api_key = "jev-key"
    model = "jev-test"

    def __init__(self):
        self.calls = 0

    def evaluate(self, state, questions=None):
        self.calls += 1
        raise ToolCancelled()


class _CountingGovernor:
    def __init__(self):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.max_cost = 1.0
        self.spent = 0.0

    def reserve(self, worst, label):
        self.reserved.append((worst, label))
        return {"label": label, "worst": worst}

    def reconcile(self, reservation, cost):
        self.reconciled.append((reservation, cost))
        self.spent += float(cost or 0.0)

    def record_actual(self, cost, model):
        self.actual.append((cost, model))
        self.spent += float(cost or 0.0)


def _sample_pack():
    return {
        "id": "ops-attention-v1",
        "buckets": {
            "auth": {
                "label": "Auth / token trouble",
                "kind": "trouble_area",
                "path_id": "path/auth",
                "keywords": ["auth", "token", "login"],
                "suggested_next_action": "open auth backlog",
                "attention": "high",
            },
            "perf": {
                "label": "Performance alternate path",
                "kind": "alternate_path",
                "path_id": "path/perf",
                "keywords": ["latency", "slow", "timeout"],
                "suggested_next_action": "profile hot path",
                "attention": "medium",
            },
        },
    }


class SettleCancelledCoverTests(unittest.TestCase):
    def _ledger(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))

    def test_answer_settles_lost_usage_instead_of_zero_release(self):
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        policy = policy_for(
            load_settings({"jev_api_key": "jev-key"}),
            transport=_LostResponseTransport(), governor=governor)

        with self.assertRaises(ProviderUsageUnknown) as raised:
            policy.evaluate_answer("question", "candidate", "context")

        self.assertAlmostEqual(raised.exception.known_cost, 0.004)
        self.assertTrue(raised.exception.cost_accounted)
        self.assertAlmostEqual(governor.spent, 0.004)
        self.assertGreater(governor.outstanding, 0.0)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_issue_sort_settles_lost_usage_instead_of_zero_release(self):
        governor = _CountingGovernor()
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_UnknownUsageEvaluator(), governor=governor,
            ledger=self._ledger())

        with self.assertRaises(ProviderUsageUnknown) as raised:
            policy.evaluate_issue_sort("auth token fail", _sample_pack())

        self.assertAlmostEqual(raised.exception.known_cost, 0.004)
        self.assertTrue(raised.exception.cost_accounted)
        self.assertAlmostEqual(governor.spent, 0.004)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_provision_cancellation_releases_reservation_once(self):
        governor = _CountingGovernor()
        ledger = self._ledger()
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_CancellingEvaluator(), governor=governor,
            ledger=ledger)

        with self.assertRaises(ToolCancelled):
            policy.evaluate_provision(
                {"task": "cancelled provision"},
                {"answer": {"type": "noul",
                            "instructions": "Is it usable?"}})

        self.assertEqual(len(governor.reconciled), 1)
        self.assertEqual(governor.reconciled[0][1], 0.0)
        self.assertEqual(
            [e for e in ledger.entries() if e["event"] == "jev_eval"], [])
        self.assertIsNone(getattr(policy._tl, "token", None))


if __name__ == "__main__":
    unittest.main()
