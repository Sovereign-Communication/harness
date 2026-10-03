"""Tests for driver tools on harness.mcp (driver_step, driver_health, etc.)."""
import unittest
from unittest.mock import patch

from tests.test_mcp import make_server


class McpDriverToolsTest(unittest.TestCase):
    def setUp(self):
        _, self.server = make_server()

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
                     "params": {"path": "/tmp"}, "by": "operator"},
            prefer=(),
            require_stable=True,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["step_id"], "mcp-step-1")

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
