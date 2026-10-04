"""Execution tests for the Phase B Python paths.

`tests/test_phase_b_dogfood_defects.py` pins the UI fixes by reading their
source, which is right for JavaScript but executes nothing. These tests drive
the changed Python for real so D12 can see the lines actually run: the task
scope, the service wiring, and the MCP faces.
"""

import unittest
from unittest.mock import patch

from harness.errors import HarnessError
from harness.spend import SpendGovernor
from harness import service
from tests.test_mcp import make_server


def _governor(max_cost=1.0):
    return SpendGovernor(transport=None, api_key="k", max_cost=max_cost)


class TaskScopeExecutionTests(unittest.TestCase):
    """SpendGovernor.task_scope, exercised rather than introspected."""

    def test_scope_narrows_remaining_and_releases_after(self):
        gov = _governor()
        with gov.task_scope(0.05, label="verify"):
            self.assertAlmostEqual(gov.remaining(), 0.05, places=6)
        self.assertAlmostEqual(gov.remaining(), 1.0, places=6)

    def test_try_phase_reserve_and_reconcile_inside_a_scope(self):
        gov = _governor()
        with gov.task_scope(0.05, label="verify"):
            token = gov.reserve(0.04, "panel")
            gov.reconcile(token, 0.04)
            self.assertAlmostEqual(gov.remaining(), 0.01, places=6)

    def test_try_finally_releases_the_scope_even_when_the_body_raises(self):
        gov = _governor()
        with self.assertRaises(RuntimeError):
            with gov.task_scope(0.05, label="verify"):
                raise RuntimeError("boom")
        # The scope must not survive the exception and keep narrowing later work.
        self.assertAlmostEqual(gov.remaining(), 1.0, places=6)

    def test_try_finally_does_not_double_remove_a_nested_scope(self):
        gov = _governor()
        with gov.task_scope(0.05, label="outer"):
            with gov.task_scope(0.02, label="inner"):
                self.assertAlmostEqual(gov.remaining(), 0.02, places=6)
            self.assertAlmostEqual(gov.remaining(), 0.05, places=6)

    def test_try_the_non_terminal_phase_ceiling_path_under_a_scope(self):
        gov = _governor(max_cost=1.0)
        gov.terminal_reserve = 0.10
        with gov.task_scope(0.50, label="verify"):
            # min(phase ceiling 0.90, baseline 0 + 0.50)
            self.assertAlmostEqual(gov.remaining(), 0.50, places=6)


class ServiceTaskCapExecutionTests(unittest.TestCase):
    """service.run_verify must open the scope it advertises."""

    def _patched(self):
        captured = {}

        def fake_panel_judge(**kwargs):
            gov = kwargs["governor"]
            captured["remaining_in_scope"] = gov.remaining()
            captured["calls"] = captured.get("calls", 0) + 1
            return {"verdict": "ok", "status": "ok"}

        return captured, fake_panel_judge

    def test_an_injected_governor_with_a_task_cap_narrows_the_lane(self):
        captured, fake = self._patched()
        gov = _governor()
        with patch.object(service, "panel_judge", side_effect=fake):
            service.run_verify(prompt="hi", api_key="k",
                               governor=gov, ledger=_Ledger(), free_tier=False,
                               task_max_cost=0.05)
        self.assertAlmostEqual(captured["remaining_in_scope"], 0.05, places=6)
        self.assertEqual(captured["calls"], 1)

    def test_no_task_cap_leaves_the_session_ceiling_in_force(self):
        captured, fake = self._patched()
        gov = _governor()
        with patch.object(service, "panel_judge", side_effect=fake):
            service.run_verify(prompt="hi", api_key="k",
                               governor=gov, ledger=_Ledger(), free_tier=False)
        self.assertAlmostEqual(captured["remaining_in_scope"], 1.0, places=6)

    def test_max_cost_on_the_injected_path_is_refused_not_silently_dropped(self):
        gov = _governor()
        with self.assertRaises(ValueError) as ctx:
            service.run_verify(prompt="hi", api_key="k", governor=gov,
                               ledger=_Ledger(), free_tier=False, max_cost=0.05)
        self.assertIn("ignored when a governor is injected", str(ctx.exception))


class _Ledger:
    """The smallest ledger stand-in the verify service will accept."""

    def append(self, *a, **k):
        pass

    def participation_report(self):
        return {}


class McpSpendStatusExecutionTests(unittest.TestCase):
    """spend_status must actually build and return the Jev credit block."""

    def setUp(self):
        _, self.server = make_server()

    def test_spend_status_returns_a_jev_credit_block_with_a_month(self):
        with patch.object(type(self.server.governor), "key_status",
                          return_value={"limit": 1.0, "remaining": 0.9}):
            out = self.server._invoke("spend_status", {})
        self.assertIn("jev_credit", out)
        credit = out["jev_credit"]
        for field in ("month", "spent_usd", "monthly_credit",
                      "remaining_credit", "used_percent"):
            self.assertIn(field, credit)
        # A calendar month, not a free-text string.
        self.assertRegex(credit["month"], r"^\d{4}-\d{2}$")
        # The session fields still come through untouched.
        self.assertEqual(out["limit"], 1.0)

    def test_the_block_is_composed_from_the_existing_owners(self):
        with patch.object(type(self.server.governor), "key_status",
                          return_value={"limit": 1.0}), \
             patch.object(type(self.server.ledger), "cost_report",
                          return_value={"jev": {"cost": 0.25, "calls": 3,
                                                "input_tokens": 1234,
                                                "remaining_credit": 4.75,
                                                "used_percent": 5.0}}) as rep:
            out = self.server._invoke("spend_status", {})
        rep.assert_called_once()
        credit = out["jev_credit"]
        self.assertAlmostEqual(credit["spent_usd"], 0.25, places=6)
        self.assertEqual(credit["calls"], 3)
        self.assertEqual(credit["input_tokens"], 1234)

    def test_a_failing_ledger_report_still_answers_with_the_pricing_constants(self):
        """A broken analytics call must not take the whole tool down."""
        with patch.object(type(self.server.governor), "key_status",
                          return_value={"limit": 1.0}), \
             patch.object(type(self.server.ledger), "cost_report",
                          side_effect=RuntimeError("analytics down")):
            out = self.server._invoke("spend_status", {})
        self.assertIn("jev_credit", out)
        self.assertEqual(out["jev_credit"]["spent_usd"], 0.0)


class McpPanelVerifyCapExecutionTests(unittest.TestCase):
    """panel_verify must forward task_max_cost rather than drop it."""

    def setUp(self):
        _, self.server = make_server()

    def test_task_max_cost_is_forwarded_to_the_service(self):
        seen = {}

        def fake_run_verify(_settings, **kwargs):
            seen.update(kwargs)
            return {"status": "ok", "verdict": "ok"}

        with patch("harness.mcp._service_run_verify", side_effect=fake_run_verify):
            self.server._invoke("panel_verify", {"prompt": "p",
                                                 "task_max_cost": 0.05})
        self.assertEqual(seen.get("task_max_cost"), 0.05)

    def test_no_task_max_cost_forwards_none_rather_than_a_stale_value(self):
        seen = {}

        def fake_run_verify(_settings, **kwargs):
            seen.update(kwargs)
            return {"status": "ok", "verdict": "ok"}

        with patch("harness.mcp._service_run_verify", side_effect=fake_run_verify):
            self.server._invoke("panel_verify", {"prompt": "p"})
        self.assertIsNone(seen.get("task_max_cost"))

    def test_a_cap_larger_than_the_session_budget_is_still_refused(self):
        self.server.governor.max_cost = 0.02
        with self.assertRaises(HarnessError) as ctx:
            self.server._invoke("panel_verify", {"prompt": "p",
                                                 "task_max_cost": 0.50})
        self.assertIn("exceeds remaining session budget", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
