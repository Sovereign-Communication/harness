"""Tests for /api/driver endpoints on harness.server.UiRequestHandler."""
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from harness.perception_client import PerceptionUnavailable
from harness.server import make_server


class ServerDriverEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.httpd = make_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_health_success(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.health.return_value = {
            "ok": True,
            "status": "up",
            "version": "3.4.0",
            "sources": ["cli"],
        }
        mock_get_adapter.return_value = adapter

        from urllib.request import urlopen
        res = urlopen(f"http://127.0.0.1:{self.port}/api/driver/health")
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], "3.4.0")
        self.assertIn("cli", data["sources"])

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_health_down_returns_graceful_status(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.health.side_effect = PerceptionUnavailable("connection refused")
        mock_get_adapter.return_value = adapter

        from urllib.request import urlopen
        res = urlopen(f"http://127.0.0.1:{self.port}/api/driver/health")
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "down")
        self.assertIn("connection refused", data["error"])

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_vocabulary(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.vocabulary.return_value = {
            "ok": True,
            "vocabulary": {"actions": ["click", "write_file"]},
        }
        mock_get_adapter.return_value = adapter

        from urllib.request import urlopen
        res = urlopen(f"http://127.0.0.1:{self.port}/api/driver/vocabulary")
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertIn("click", data["vocabulary"]["actions"])

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_schemas(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.schemas.return_value = {
            "ok": True,
            "schemas": {"screen": {}, "cli": {}},
        }
        mock_get_adapter.return_value = adapter

        from urllib.request import urlopen
        res = urlopen(f"http://127.0.0.1:{self.port}/api/driver/schemas")
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertIn("cli", data["schemas"])

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_verify(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.verify.return_value = {
            "ok": True,
            "audit": {"ok": True, "records": 42},
            "budget": {"spent_usd": 0.001},
        }
        mock_get_adapter.return_value = adapter

        from urllib.request import urlopen
        res = urlopen(f"http://127.0.0.1:{self.port}/api/driver/verify")
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(data["audit"]["ok"])
        self.assertEqual(data["audit"]["records"], 42)

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_step_success(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.step.return_value = {
            "step_id": "test-step-123",
            "ok": True,
            "stopped_at": "execute",
            "reason": None,
            "cost_usd": 0.0002,
        }
        mock_get_adapter.return_value = adapter

        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/driver/step",
            data=json.dumps({"target": "cli", "schema": "cli"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        res = urllib.request.urlopen(req)
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertEqual(data["step_id"], "test-step-123")
        self.assertTrue(data["ok"])

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_step_refusal_is_200(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.step.return_value = {
            "step_id": "test-step-refused",
            "ok": False,
            "stopped_at": "capture",
            "reason": "no_capture",
            "cost_usd": 0.0,
        }
        mock_get_adapter.return_value = adapter

        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/driver/step",
            data=json.dumps({"target": "unknown", "schema": "screen"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        res = urllib.request.urlopen(req)
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no_capture")

    def test_api_driver_step_missing_target(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/driver/step",
            data=json.dumps({"schema": "cli"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

    def test_validate_dispatch_driver_task(self):
        from harness.server import validate_dispatch
        args = validate_dispatch("driver_task", {"goal": "Inspect state", "max_steps": 3, "max_cost": 0.05})
        self.assertEqual(args["goal"], "Inspect state")
        self.assertEqual(args["max_steps"], 3)
        self.assertEqual(args["max_cost"], 0.05)
        self.assertTrue(args["auto_approve"])

    @patch.dict("harness.server.RUNNERS", {"driver_task": MagicMock(return_value={"status": "done"})})
    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_drive_dispatches_run(self, mock_get_adapter):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/driver/drive",
            data=json.dumps({"goal": "Test multi-step task", "max_steps": 2}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        res = urllib.request.urlopen(req)
        try:
            self.assertEqual(res.status, 201)
            data = json.loads(res.read().decode("utf-8"))
            self.assertIn("id", data)
            self.assertEqual(data["kind"], "driver_task")
            self.assertIn(data["status"], ("running", "done"))
        finally:
            res.close()

    @patch("harness.perception_client.PerceptionAdapter")
    def test_run_driver_task_execution(self, mock_adapter_cls):
        from harness.server import run_driver_task
        mock_adapter = MagicMock()
        mock_adapter.step.return_value = {
            "step_id": "drv-test-1",
            "ok": True,
            "stopped_at": "execute",
            "reason": None,
            "cost_usd": 0.0001,
        }
        mock_adapter.verify.return_value = {"ok": True, "audit": {"ok": True}}
        mock_adapter_cls.return_value = mock_adapter

        result = run_driver_task(
            "task-test-1",
            {"goal": "Check test environment", "max_steps": 2, "target": "cli"},
            lambda: False,
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["goal"], "Check test environment")
        self.assertTrue(len(result["steps"]) >= 1)
        self.assertIn("summary", result)


if __name__ == "__main__":
    unittest.main()

