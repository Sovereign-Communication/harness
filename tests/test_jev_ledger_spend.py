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

    def test_apply_task_max_cost_preflight_folds_jev_worst_case(self):
        from harness.apply import ApplyEngine
        from harness.errors import HarnessError
        from harness.router import Router
        from tests._fake import comp

        settings = load_settings({"jev_api_key": "jev-key"})
        spend_transport = FakeTransport(
            models=[m("apply/model", prompt="0", completion="0"),
                    m("jev-test", prompt="0", completion="0")],
            posts=[comp("def x(): pass\n"), comp("def x(): pass\n")])
        governor = SpendGovernor(spend_transport, "sk-test", max_cost=1.0)
        ledger = self._ledger()
        policy = policy_for(settings, transport=FakeTransport(posts=[{
            "model": "jev-test",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }, {
            "model": "jev-test",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }]), governor=governor, ledger=ledger)

        target = os.path.join(self.tmp.name, "target.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        router = Router(["apply/model"], "apply/model", "apply/model")
        engine = ApplyEngine(spend_transport, "k", governor, ledger, router,
                             jev_policy=policy, default_require_consent=False,
                             default_renew_consent=False)

        # When task_max_cost is smaller than Jev worst-case (~$0.043), preflight must refuse
        with self.assertRaises(HarnessError) as ctx:
            engine.apply_edit(file_path=target, instruction="edit x",
                              task_max_cost=0.001, require_consent=False,
                              renew_consent=False)
        self.assertIn("exceeds --task-max-cost", str(ctx.exception))

        # With sufficient budget and governor having preflight_jev, line 315 executes
        from unittest import mock
        with mock.patch.object(governor, "preflight_jev", wraps=governor.preflight_jev) as mock_preflight:
            engine.apply_edit(file_path=target, instruction="edit x",
                              verify_cmd="python -c pass",
                              task_max_cost=0.10, require_consent=False,
                              renew_consent=False)
            self.assertTrue(any(call.kwargs.get("label") == "apply_candidate"
                                for call in mock_preflight.call_args_list))

    def test_apply_escalation_folds_jev_worst_case_and_preflights(self):
        from unittest import mock
        from harness.apply import ApplyEngine
        from harness.errors import HarnessError
        from harness.router import Router
        from tests._fake import comp

        settings = load_settings({"jev_api_key": "jev-key"})
        spend_transport = FakeTransport(
            models=[m("apply/model", prompt="0", completion="0"),
                    m("esc/model", prompt="0", completion="0"),
                    m("jev-test", prompt="0", completion="0")],
            posts=[comp("x = 2\n"), comp("x = 3\n")])
        governor = SpendGovernor(spend_transport, "sk-test", max_cost=1.0)
        ledger = self._ledger()
        policy = policy_for(settings, transport=FakeTransport(posts=[{
            "model": "jev-test",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }, {
            "model": "jev-test",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }]), governor=governor, ledger=ledger)

        target = os.path.join(self.tmp.name, "target_esc.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        router = Router(["apply/model"], "apply/model", "apply/model",
                        allow_escalation=True, escalation_model="esc/model")
        engine = ApplyEngine(spend_transport, "k", governor, ledger, router,
                             jev_policy=policy, default_require_consent=False,
                             default_renew_consent=False)

        # 1. When task_max_cost is small during escalation (0.046), lines 623-628 execute and raise
        with self.assertRaises(HarnessError) as ctx:
            engine.apply_edit(file_path=target, instruction="edit x",
                              verify_cmd="python -c exit(1)", max_rounds=1,
                              task_max_cost=0.046, require_consent=False,
                              renew_consent=False)
        self.assertIn("exceeds --task-max-cost", str(ctx.exception))

        # 2. When task_max_cost is sufficient (0.060), lines 623-625, 631-632 execute
        spend_transport2 = FakeTransport(
            models=[m("apply/model", prompt="0", completion="0"),
                    m("esc/model", prompt="0", completion="0"),
                    m("jev-test", prompt="0", completion="0")],
            posts=[comp("def x(): pass\n"), comp("def x(): pass\n")])
        governor2 = SpendGovernor(spend_transport2, "sk-test", max_cost=1.0)
        policy2 = policy_for(settings, transport=FakeTransport(posts=[{
            "model": "jev-test",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }, {
            "model": "jev-test",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 2},
        }]), governor=governor2, ledger=ledger)
        engine2 = ApplyEngine(spend_transport2, "k", governor2, ledger, router,
                              jev_policy=policy2, default_require_consent=False,
                              default_renew_consent=False)

        with mock.patch.object(governor2, "preflight_jev", wraps=governor2.preflight_jev) as mock_preflight:
            engine2.apply_edit(file_path=target, instruction="edit x",
                               verify_cmd="python -c exit(1)", max_rounds=1,
                               task_max_cost=0.060, require_consent=False,
                               renew_consent=False)
            self.assertTrue(any(call.kwargs.get("label") == "apply_escalation"
                                for call in mock_preflight.call_args_list))


if __name__ == "__main__":
    unittest.main()

