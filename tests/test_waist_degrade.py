"""Hermetic unit tests for GAP-waist-degrade: honest degradation envelope.

Validates that when the frontier model or decomposition lane is unreachable or
exhausted (HTTP 429 / provider failure):
1. In execute mode, planning honestly reports degradation with `degraded: True`,
   `degrade_reason: "waist_unavailable"` (or "decomposition_unavailable"), and an
   explicit resume token.
2. In plan-only preview mode (execute=False), planning fails closed with HarnessError,
   refusing silent promotion of unconfirmed plans.
"""
import unittest
from unittest import mock

from harness.errors import HarnessError
from harness.waist import compose_plan
from tests._fake import FakeTransport, m, _gov


class TestWaistDegrade(unittest.TestCase):
    """Test degradation contract in harness/waist.py."""

    def test_waist_unreachable_degrade_execute_mode(self):
        """When confirmation ladder is unreachable in execute mode, degrade honestly."""
        fake = FakeTransport(models=[m("frontier/test")], posts=[])
        gov = _gov(fake)

        with mock.patch("harness.waist.confirm_plan") as mock_confirm:
            mock_confirm.side_effect = HarnessError("HTTP 429: Provider rate limited")
            res = compose_plan(
                transport=fake,
                api_key="fake-key",
                governor=gov,
                ledger=None,
                opts_goal="Refactor worker pool",
                candidate_files=["harness/sync.py"],
                confirm=True,
                execute=True,
                frontier_model="frontier/test",
            )

        self.assertEqual(res["status"], "planned")
        self.assertTrue(res.get("degraded"))
        self.assertEqual(res.get("degrade_reason"), "waist_unavailable")
        self.assertTrue(res.get("resume_token", "").startswith("waist-resume-"))
        self.assertIn("confirmation", res)
        conf = res["confirmation"]
        self.assertEqual(conf["verdict"], "unavailable")
        self.assertTrue(conf.get("degraded"))
        self.assertEqual(conf.get("degrade_reason"), "waist_unavailable")
        self.assertEqual(conf.get("resume_token"), res["resume_token"])

    def test_waist_unreachable_fail_closed_preview_mode(self):
        """In plan-only preview mode (execute=False), unreachable waist fails closed."""
        fake = FakeTransport(models=[m("frontier/test")], posts=[])
        gov = _gov(fake)

        with mock.patch("harness.waist.confirm_plan") as mock_confirm:
            mock_confirm.side_effect = HarnessError("HTTP 429: Provider rate limited")
            with self.assertRaises(HarnessError) as ctx:
                compose_plan(
                    transport=fake,
                    api_key="fake-key",
                    governor=gov,
                    ledger=None,
                    opts_goal="Refactor worker pool",
                    candidate_files=["harness/sync.py"],
                    confirm=True,
                    execute=False,
                    frontier_model="frontier/test",
                )
            self.assertIn("waist confirmation could not run", str(ctx.exception))

    def test_decomposition_failed_degrade_execute_mode(self):
        """When LLM decomposition fails in execute mode, degrade to heuristic loudly."""
        fake = FakeTransport(models=[m("cheap/decomp")], posts=[])
        gov = _gov(fake)

        with mock.patch("harness.waist.decompose_via_llm") as mock_decomp:
            mock_decomp.side_effect = HarnessError("HTTP 429: rate limited")
            res = compose_plan(
                transport=fake,
                api_key="fake-key",
                governor=gov,
                ledger=None,
                opts_goal="Add logging",
                candidate_files=["harness/sync.py"],
                decompose_llm=True,
                execute=True,
                confirm=False,
            )

        self.assertEqual(res["decomposition"], "heuristic")
        self.assertTrue(res.get("degraded"))
        self.assertEqual(res.get("degrade_reason"), "decomposition_unavailable")

    def test_decomposition_failed_fail_closed_preview_mode(self):
        """In plan-only preview mode without allow_heuristic_preview, decomposition fails closed."""
        fake = FakeTransport(models=[m("cheap/decomp")], posts=[])
        gov = _gov(fake)

        with mock.patch("harness.waist.decompose_via_llm") as mock_decomp:
            mock_decomp.side_effect = HarnessError("HTTP 429: rate limited")
            with self.assertRaises(HarnessError) as ctx:
                compose_plan(
                    transport=fake,
                    api_key="fake-key",
                    governor=gov,
                    ledger=None,
                    opts_goal="Add logging",
                    candidate_files=["harness/sync.py"],
                    decompose_llm=True,
                    execute=False,
                    confirm=False,
                    allow_heuristic_preview=False,
                )
            self.assertIn("refusing to silently degrade", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
