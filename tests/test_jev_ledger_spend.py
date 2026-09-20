"""Hermetic JEV-P1 spend and ledger contracts."""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


class JevLedgerSpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _ledger(self):
        return AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))

    @staticmethod
    def _diff():
        return ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
                "-x = 1\n+x = 2\n")

    def test_live_policy_spends_exactly_once_and_ledgers_once(self):
        jev_transport = FakeTransport(posts=[{
            "model": "jev-test",
            "answers": {"instruction_matches": {
                "type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 120, "output_tokens": 4},
        }])
        spend_transport = FakeTransport(models=[m("jev-test", prompt="0",
                                                  completion="0")])
        governor = SpendGovernor(spend_transport, "sk-test", max_cost=0.10)
        ledger = self._ledger()
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=jev_transport,
                            governor=governor, ledger=ledger)

        result, envelope = policy.evaluate_diff(
            self._diff(), "change x", "x.py", site="agent-apply",
            task_id="task-1", node_id="node-1")

        expected = 120 * 42 / 1_000_000
        self.assertEqual(result.cost, expected)
        self.assertEqual(governor.spent, expected)
        events = [entry for entry in ledger.entries()
                  if entry["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["input_tokens"], 120)
        self.assertEqual(events[0]["cost"], expected)
        self.assertEqual(envelope["site"], "agent-apply")

    def test_unkeyed_fallback_never_spends_or_bills(self):
        settings = load_settings()
        settings.jev_api_key = None
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        ledger = self._ledger()
        result, envelope = policy_for(
            settings, governor=governor, ledger=ledger).evaluate_diff(
                self._diff(), "change x", "x.py", site="apply")
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(governor.spent, 0.0)
        self.assertEqual([e for e in ledger.entries()
                          if e["event"] == "jev_eval"][0]["cost"], 0.0)
        self.assertTrue(envelope["is_fallback"])

    def test_preflight_reservation_is_released_on_transport_failure(self):
        class FailingTransport(FakeTransport):
            def post(self, *args, **kwargs):
                raise RuntimeError("provider unavailable")

        settings = load_settings({"jev_api_key": "jev-key"})
        governor = SpendGovernor(FakeTransport(models=[m("jev-test")]),
                                 "sk-test", max_cost=0.10)
        policy = policy_for(settings, transport=FailingTransport(),
                            governor=governor, ledger=self._ledger())
        result, _ = policy.evaluate_diff(
            self._diff(), "change x", "x.py", site="apply")
        self.assertTrue(result.is_fallback)
        self.assertEqual(governor.spent, 0.0)
        self.assertEqual(governor.outstanding, 0.0)


if __name__ == "__main__":
    unittest.main()
