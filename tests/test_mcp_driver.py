"""Tests for driver tools on harness.mcp (driver_step, driver_health, etc.)."""
import unittest
from unittest.mock import patch

from harness import server as ui_server
from harness.errors import HarnessError
from harness.mcp_lanes import MUTATION_LANE
from harness.mcp_schemas import TOOL_SCHEMAS
from tests.test_mcp import make_server


class McpDriverToolsTest(unittest.TestCase):
    def setUp(self):
        _, self.server = make_server()
        # These tools talk to a driver through the adapter and never start
        # one; if a future change makes them autostart the in-process daemon,
        # it must not outlive the test (a leaked listening socket fails the
        # audit's hermetic run).
        self.addCleanup(ui_server.shutdown_driver_daemon)

    def test_driver_step_is_a_mutation_lane_tool_and_the_rest_are_not(self):
        self.assertIn("driver_step", MUTATION_LANE)
        for read_only in ("driver_health", "driver_vocabulary", "driver_verify"):
            self.assertNotIn(read_only, MUTATION_LANE)

    def test_driver_tools_are_declared_with_a_required_target(self):
        by_name = {t["name"]: t for t in TOOL_SCHEMAS}
        for name in ("driver_step", "driver_health", "driver_vocabulary",
                     "driver_verify"):
            self.assertIn(name, by_name)
        self.assertEqual(by_name["driver_step"]["inputSchema"]["required"],
                         ["target"])

    @patch("harness.perception_client.PerceptionAdapter.step")
    def test_mcp_driver_step_without_action_sends_no_consent(self, mock_step):
        mock_step.return_value = {"step_id": "s", "ok": False,
                                  "reason": "no_capture", "cost_usd": 0.0}
        res = self.server._invoke("driver_step", {"target": "cli", "schema": "cli"})
        self.assertEqual(res["reason"], "no_capture")
        self.assertIsNone(mock_step.call_args.kwargs["consent"])

    @patch("harness.perception_client.PerceptionAdapter.step")
    def test_mcp_driver_step(self, mock_step):
        mock_step.return_value = {
            "step_id": "mcp-step-1",
            "ok": True,
            "stopped_at": "execute",
            "reason": None,
            "cost_usd": 0.0001,
        }
        res = self.server._invoke(
            "driver_step",
            {"target": "cli", "schema": "cli", "action": "open_window",
             "params": {"path": "/tmp"}},
        )
        mock_step.assert_called_once_with(
            "cli",
            schema="cli",
            consent={"granted": True, "action": "open_window",
                     "params": {"path": "/tmp"}, "by": "mcp"},
            prefer=(),
            require_stable=True,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["step_id"], "mcp-step-1")

    @patch("harness.perception_client.PerceptionAdapter.step")
    def test_default_grantor_is_the_mcp_peer_not_a_person(self, mock_step):
        mock_step.return_value = {"ok": True}
        self.server.caller = "mcp:acme/1.2"
        self.server._invoke("driver_step", {"target": "cli", "action": "observe"})
        self.assertEqual(mock_step.call_args.kwargs["consent"]["by"], "mcp:acme/1.2")
        self.server.caller = "embedder"
        self.server._invoke("driver_step", {"target": "cli", "action": "observe"})
        self.assertEqual(mock_step.call_args.kwargs["consent"]["by"], "mcp:embedder")

    @patch("harness.perception_client.PerceptionAdapter.step")
    def test_operator_label_is_refused_without_confirmation(self, mock_step):
        self.server.allow_write = False
        for who in ("operator", " Operator "):
            with self.assertRaises(HarnessError):
                self.server._invoke("driver_step", {
                    "target": "cli", "action": "observe", "by": who})
        mock_step.assert_not_called()

    @patch("harness.perception_client.PerceptionAdapter.step")
    def test_operator_label_allowed_with_explicit_confirmation(self, mock_step):
        mock_step.return_value = {"ok": True}
        self.server.allow_write = False
        self.server._invoke("driver_step", {
            "target": "cli", "action": "observe", "by": "operator",
            "allow_write": True})
        self.assertEqual(mock_step.call_args.kwargs["consent"]["by"], "operator")
        self.server.allow_write = True
        self.server._invoke("driver_step", {
            "target": "cli", "action": "observe", "by": "operator"})
        self.assertEqual(mock_step.call_args.kwargs["consent"]["by"], "operator")

    @patch("harness.perception_client.PerceptionAdapter.step")
    def test_other_caller_labels_pass_through_trimmed(self, mock_step):
        mock_step.return_value = {"ok": True}
        self.server._invoke("driver_step", {
            "target": "cli", "action": "observe", "by": "  ci-bot  "})
        self.assertEqual(mock_step.call_args.kwargs["consent"]["by"], "ci-bot")

    @patch("harness.perception_client.PerceptionAdapter.health")
    def test_mcp_driver_health(self, mock_health):
        mock_health.return_value = {
            "ok": True,
            "status": "up",
            "version": "3.4.0",
            "sources": ["cli", "dom"],
        }
        res = self.server._invoke("driver_health", {})
        self.assertTrue(res["ok"])
        self.assertEqual(res["version"], "3.4.0")
        self.assertEqual(res["sources"], ["cli", "dom"])

    @patch("harness.perception_client.PerceptionAdapter.vocabulary")
    def test_mcp_driver_vocabulary(self, mock_vocab):
        mock_vocab.return_value = {
            "ok": True,
            "vocabulary": {"actions": ["click", "write_file"]},
        }
        res = self.server._invoke("driver_vocabulary", {})
        self.assertTrue(res["ok"])
        self.assertIn("click", res["vocabulary"]["actions"])

    @patch("harness.perception_client.PerceptionAdapter.verify")
    def test_mcp_driver_verify(self, mock_verify):
        mock_verify.return_value = {
            "ok": True,
            "audit": {"ok": True, "records": 10},
            "budget": {"spent_usd": 0.0005},
        }
        res = self.server._invoke("driver_verify", {})
        self.assertTrue(res["ok"])
        self.assertEqual(res["audit"]["records"], 10)


if __name__ == "__main__":
    unittest.main()
