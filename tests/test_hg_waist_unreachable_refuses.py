"""HG: confirm-armed waist unreachable across the full ladder.

Operator no-interruptions ruling (2026-09-22): execute mode DEGRADES to
local-gate execution with unavailable-verdict provenance; plan-only mode
still fails closed (nothing to protect by degrading there).
"""
import unittest
from unittest.mock import patch

from harness.errors import HarnessError
from harness.spend import SpendGovernor
from harness.waist import compose_plan

from tests._fake import FakeTransport, m


class WaistUnreachableRefusesTests(unittest.TestCase):
    def test_confirm_armed_all_rungs_fail_refuses_execute(self):
        fake = FakeTransport(models=[
            m("google/gemma-4-31b-it:free", "0", "0"),
            m("m/front", "0.000001", "0.000002"),
        ])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)

        def broken(transport, api_key, governor, model, prompt, tokens, label=None):
            raise HarnessError("HTTP 429: Provider returned error")

        with patch("harness.waist.governed_text", side_effect=broken):
            plan = compose_plan(
                transport=fake, api_key="k", governor=gov, ledger=None,
                opts_goal="Split the work",
                candidate_files=["harness/sync.py"],
                confirm=True, execute=True, use_free=True)

        self.assertEqual(plan["status"], "planned")
        confirmation = plan.get("confirmation") or {}
        self.assertEqual(confirmation.get("verdict"), "unavailable")
        self.assertIn("unreachable", confirmation.get("reason", ""))
        self.assertIn("429", confirmation.get("evidence", ""))
        # The degraded plan must still be executable: real nodes, real DAG,
        # and a monetary ceiling the executor + composed-ceiling guard use.
        self.assertGreaterEqual(plan["total_nodes"], 1)
        self.assertTrue(plan.get("dag", {}).get("nodes"))

    def test_plan_only_unreachable_still_raises(self):
        fake = FakeTransport(models=[m("google/gemma-4-31b-it:free", "0", "0")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)

        def broken(transport, api_key, governor, model, prompt, tokens, label=None):
            raise HarnessError("HTTP 429: Provider returned error")

        with patch("harness.waist.governed_text", side_effect=broken):
            with self.assertRaises(HarnessError) as ctx:
                compose_plan(
                    transport=fake, api_key="k", governor=gov, ledger=None,
                    opts_goal="Split the work",
                    candidate_files=["harness/sync.py"],
                    confirm=True, execute=False)
        self.assertIn("plan NOT executed", str(ctx.exception))

    def test_confirm_off_does_not_require_waist(self):
        plan = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="Fix typo", candidate_files=["a.py"],
            confirm=False)
        self.assertEqual(plan["status"], "planned")


if __name__ == "__main__":
    unittest.main()
