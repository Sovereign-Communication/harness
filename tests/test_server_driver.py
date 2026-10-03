"""Tests for /api/driver endpoints on harness.server.UiRequestHandler."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from unittest.mock import MagicMock, patch

from harness import server as ui_server
from harness.perception_client import PerceptionAdapter, PerceptionUnavailable
from harness.server import make_server, run_driver_task
from tests.test_server import ServerHarness, _request

#: The literal the harness used to fall back to. It must never come back.
_OLD_FIXED_TOKEN = "harness-driver-session-token-1234"


def tearDownModule():
    # Belt and braces: every case below stops the daemon it starts, but a
    # leaked listening socket would surface as a ResourceWarning at GC and
    # fail the audit's hermetic run (R13).
    ui_server.shutdown_driver_daemon()


class DriverEnvMixin:
    """A private driver: ephemeral port, temp audit log, no ambient config.

    Every ``DRIVER_*`` variable is dropped (an operator's own driver or token
    must not leak into a hermetic test), the audit path is pinned to a temp
    dir, and the daemon this process started is stopped on cleanup.
    """

    def setUp(self):
        super().setUp()
        self._drv_tmp = tempfile.mkdtemp(prefix="harness-driver-test-")
        self.addCleanup(shutil.rmtree, self._drv_tmp, True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("DRIVER_")}
        env["DRIVER_AUDIT_PATH"] = os.path.join(self._drv_tmp, "audit.jsonl")
        env["DRIVER_PORT"] = "0"
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # LIFO: the daemon is stopped before the environment is restored.
        self.addCleanup(ui_server.shutdown_driver_daemon)


class ServerDriverEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.httpd = make_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=60)

    def _call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            return _request(conn, method, path, body=body)
        finally:
            conn.close()

    def _get(self, path):
        return self._call("GET", path)

    def _post(self, path, body):
        return self._call("POST", path, body)

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

        status, data = self._get("/api/driver/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], "3.4.0")
        self.assertIn("cli", data["sources"])

    @patch("harness.server.UiRequestHandler._get_driver_adapter")
    def test_api_driver_health_down_returns_graceful_status(self, mock_get_adapter):
        adapter = MagicMock()
        adapter.health.side_effect = PerceptionUnavailable("connection refused")
        mock_get_adapter.return_value = adapter

        status, data = self._get("/api/driver/health")
        self.assertEqual(status, 200)
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

        status, data = self._get("/api/driver/vocabulary")
        self.assertEqual(status, 200)
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

        status, data = self._get("/api/driver/schemas")
        self.assertEqual(status, 200)
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

        status, data = self._get("/api/driver/verify")
        self.assertEqual(status, 200)
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

        status, data = self._post("/api/driver/step",
                                  {"target": "cli", "schema": "cli"})
        self.assertEqual(status, 200)
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

        status, data = self._post("/api/driver/step",
                                  {"target": "unknown", "schema": "screen"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no_capture")

    def test_api_driver_step_missing_target(self):
        status, _ = self._post("/api/driver/step", {"schema": "cli"})
        self.assertEqual(status, 400)

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
        status, data = self._post("/api/driver/drive",
                                  {"goal": "Test multi-step task", "max_steps": 2})
        self.assertEqual(status, 201)
        self.assertIn("id", data)
        self.assertEqual(data["kind"], "driver_task")
        self.assertIn(data["status"], ("running", "done"))

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



class _FakeAdapter:
    """Scripted stand-in for PerceptionAdapter at the run_driver_task seam."""

    def __init__(self, envelopes, audit=None):
        self._envelopes = list(envelopes)
        self._audit = audit
        self.calls = []

    def step(self, target, **kw):
        self.calls.append((target, kw))
        env = self._envelopes.pop(0)
        if isinstance(env, Exception):
            raise env
        return env

    def verify(self):
        if isinstance(self._audit, Exception):
            raise self._audit
        return self._audit


_REFUSED = {"ok": False, "stopped_at": "capture", "reason": "no_capture",
            "cost_usd": 0.0}
_OK = {"ok": True, "stopped_at": "execute", "reason": None, "cost_usd": 0.001}


class RunDriverTaskSummaryTest(unittest.TestCase):
    """The task summary must say what happened, not what was hoped for."""

    def _run(self, adapter, args=None, cancel=None):
        a = {"goal": "g", "max_steps": 2, "target": "cli"}
        a.update(args or {})
        with patch.object(ui_server, "_driver_adapter", return_value=adapter):
            return run_driver_task("t-1", a, cancel)

    def test_all_refused_is_not_reported_as_verified(self):
        adapter = _FakeAdapter([_REFUSED, _REFUSED], audit={"ok": True})
        res = self._run(adapter)
        self.assertEqual(res["status"], "max_steps_reached")
        self.assertEqual(res["ok_steps"], 0)
        self.assertEqual(res["total_steps"], 2)
        self.assertIn("nothing verified", res["summary"])
        self.assertNotIn("all aspects verified", res["summary"])
        self.assertEqual([c[1]["schema"] for c in adapter.calls], ["cli", "mcp"])

    def test_ok_step_without_verify_command_is_unconfirmed(self):
        res = self._run(_FakeAdapter([_OK], audit={"ok": True}))
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["ok_steps"], 1)
        self.assertIn("no verify command confirmed", res["summary"])
        self.assertAlmostEqual(res["total_cost_usd"], 0.001)

    def test_verify_command_pass_is_goal_met_even_with_zero_ok_steps(self):
        with patch("harness.gate_runner.run_gate", return_value=(0, "fine")):
            res = self._run(_FakeAdapter([_REFUSED]), {"verify": "true"})
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["ok_steps"], 0)
        self.assertIn("Driver goal met", res["summary"])
        self.assertIn("0 driver step(s) ok", res["summary"])
        self.assertTrue(res["steps"][0]["verification"]["ok"])

    def test_verify_command_failure_and_error_do_not_complete(self):
        with patch("harness.gate_runner.run_gate",
                   side_effect=[(1, "no"), RuntimeError("boom")]):
            res = self._run(_FakeAdapter([_REFUSED, _REFUSED]),
                            {"verify": "false"})
        self.assertEqual(res["status"], "max_steps_reached")
        self.assertFalse(res["steps"][0]["verification"]["ok"])
        self.assertEqual(res["steps"][0]["verification"]["returncode"], 1)
        self.assertIn("boom", res["steps"][1]["verification"]["error"])

    def test_transport_failure_becomes_a_no_capture_step(self):
        adapter = _FakeAdapter([PerceptionUnavailable("down")],
                               audit=RuntimeError("no audit"))
        res = self._run(adapter, {"max_steps": 1})
        env = res["steps"][0]["envelope"]
        self.assertFalse(env["ok"])
        self.assertEqual(env["reason"], "no_capture")
        self.assertIn("down", env["detail"])
        self.assertIsNone(res["audit"])

    def test_cancel_is_reported_as_cancelled(self):
        res = self._run(_FakeAdapter([]), cancel=lambda: True)
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(res["total_steps"], 0)
        self.assertIn("cancelled", res["summary"])

    def test_schema_walks_the_tiers_to_screen(self):
        adapter = _FakeAdapter([_REFUSED] * 4)
        self._run(adapter, {"max_steps": 4})
        self.assertEqual([c[1]["schema"] for c in adapter.calls],
                         ["cli", "mcp", "dom", "screen"])

    def test_auto_approve_false_sends_no_consent(self):
        adapter = _FakeAdapter([_REFUSED])
        self._run(adapter, {"max_steps": 1, "auto_approve": False})
        self.assertIsNone(adapter.calls[0][1]["consent"])


class DriverDaemonTest(DriverEnvMixin, unittest.TestCase):
    """The in-process driver: random per-start token, real serve thread."""

    def test_token_is_random_and_not_the_old_fixed_literal(self):
        adapter = ui_server.ensure_driver_daemon()
        self.assertNotEqual(adapter.token, _OLD_FIXED_TOKEN)
        self.assertGreaterEqual(len(adapter.token), 32)
        # The credential lives in the adapter only: not in the environment.
        self.assertNotIn(adapter.token, os.environ.values())
        self.assertNotIn("DRIVER_TOKEN", os.environ)

    def test_two_starts_get_two_tokens(self):
        first = ui_server.ensure_driver_daemon().token
        ui_server.shutdown_driver_daemon()
        second = ui_server.ensure_driver_daemon().token
        self.assertNotEqual(first, second)

    def test_second_call_reuses_the_running_daemon(self):
        a = ui_server.ensure_driver_daemon()
        b = ui_server.ensure_driver_daemon()
        self.assertEqual((a.base, a.token), (b.base, b.token))

    def test_daemon_actually_serves_and_enforces_its_token(self):
        adapter = ui_server.ensure_driver_daemon()
        health = adapter.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["status"], "up")
        # health must never leak the credential.
        self.assertNotIn(adapter.token, json.dumps(health))
        wrong = PerceptionAdapter(base_url=adapter.base, token="not-the-token")
        with self.assertRaises(PerceptionUnavailable) as ctx:
            wrong.health()
        self.assertIn("401", str(ctx.exception))
        anon = PerceptionAdapter(base_url=adapter.base)
        anon.token = None
        with self.assertRaises(PerceptionUnavailable):
            anon.health()

    def test_operator_declared_token_is_honoured(self):
        declared = "operator-declared-token-0123456789abcdef"
        with patch.dict(os.environ, {"DRIVER_TOKEN": declared}):
            adapter = ui_server.ensure_driver_daemon()
        self.assertEqual(adapter.token, declared)
        self.assertTrue(adapter.health()["ok"])

    def test_shutdown_is_idempotent_and_frees_the_socket(self):
        ui_server.shutdown_driver_daemon()  # nothing started: a no-op
        adapter = ui_server.ensure_driver_daemon()
        ui_server.shutdown_driver_daemon()
        ui_server.shutdown_driver_daemon()
        with self.assertRaises(PerceptionUnavailable):
            adapter.health()

    def test_adapter_without_autostart_does_not_start_anything(self):
        adapter = ui_server._driver_adapter(autostart=False)
        self.assertIsInstance(adapter, PerceptionAdapter)
        self.assertEqual(ui_server._DRIVER_DAEMON, {})

    def test_adapter_prefers_a_reachable_driver(self):
        with patch.object(PerceptionAdapter, "health", return_value={"ok": True}):
            adapter = ui_server._driver_adapter()
        self.assertEqual(ui_server._DRIVER_DAEMON, {})
        self.assertIsInstance(adapter, PerceptionAdapter)

    def test_unreachable_driver_autostarts_ours(self):
        with patch.object(PerceptionAdapter, "health",
                          side_effect=PerceptionUnavailable("down")):
            adapter = ui_server._driver_adapter()
        self.assertIn("httpd", ui_server._DRIVER_DAEMON)
        self.assertEqual(adapter.token, ui_server._DRIVER_DAEMON["token"])
        # And a later call reuses it instead of starting a second one.
        again = ui_server._driver_adapter()
        self.assertEqual(again.base, adapter.base)

    def test_a_listener_that_never_answers_does_not_stall_the_probe(self):
        import socket
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        self.addCleanup(silent.close)
        url = "http://127.0.0.1:{}".format(silent.getsockname()[1])
        env = {"DRIVER_BASE_URL": url}
        with patch.dict(os.environ, env), \
                patch.object(ui_server, "_DRIVER_PROBE_TIMEOUT", 0.3):
            started = time.time()
            adapter = ui_server._driver_adapter()
        self.assertLess(time.time() - started, 10)
        # It gave up on the silent endpoint and started its own driver.
        self.assertIn("httpd", ui_server._DRIVER_DAEMON)
        self.assertEqual(adapter.token, ui_server._DRIVER_DAEMON["token"])

    def test_a_driver_that_cannot_start_degrades_to_the_plain_adapter(self):
        with patch.object(PerceptionAdapter, "health",
                          side_effect=PerceptionUnavailable("down")), \
             patch.object(ui_server, "ensure_driver_daemon",
                          side_effect=OSError("bind failed")):
            adapter = ui_server._driver_adapter()
        self.assertIsInstance(adapter, PerceptionAdapter)
        self.assertEqual(ui_server._DRIVER_DAEMON, {})


class DriverApiAuthTest(DriverEnvMixin, ServerHarness):
    """/api/driver/* behind a real UI token, against a real in-process driver."""

    token = "s3cret-ui-token"

    def _call(self, method, path, body=None, headers=None):
        conn = self._conn()
        try:
            return _request(conn, method, path, body=body, headers=headers)
        finally:
            conn.close()

    def _authed(self, method, path, body=None):
        return self._call(method, path, body,
                          headers={"X-Harness-Auth": self.token})

    GET_ROUTES = ("/api/driver/health", "/api/driver/vocabulary",
                  "/api/driver/schemas", "/api/driver/verify")
    POST_ROUTES = ("/api/driver/step", "/api/driver/start",
                   "/api/driver/drive")

    def test_every_route_is_401_without_a_token(self):
        for path in self.GET_ROUTES:
            self.assertEqual(self._call("GET", path)[0], 401, path)
        for path in self.POST_ROUTES:
            self.assertEqual(self._call("POST", path, {"target": "cli"})[0],
                             401, path)

    def test_every_route_is_401_with_a_wrong_token(self):
        bad = {"X-Harness-Auth": "wrong"}
        for path in self.GET_ROUTES:
            self.assertEqual(self._call("GET", path, headers=bad)[0], 401, path)
        bearer = {"Authorization": "Bearer wrong"}
        for path in self.POST_ROUTES:
            status, _ = self._call("POST", path, {"target": "cli"},
                                   headers=bearer)
            self.assertEqual(status, 401, path)
        self.assertEqual(self._call("GET", "/api/driver/health?token=wrong")[0],
                         401)

    def test_unauthorised_request_never_starts_a_driver(self):
        self._call("GET", "/api/driver/health")
        self.assertEqual(ui_server._DRIVER_DAEMON, {})

    def test_health_vocabulary_schemas_verify(self):
        status, health = self._authed("GET", "/api/driver/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["status"], "up")
        status, vocab = self._authed("GET", "/api/driver/vocabulary")
        self.assertEqual(status, 200)
        self.assertTrue(vocab["ok"])
        self.assertIn("vocabulary", vocab)
        status, schemas = self._authed("GET", "/api/driver/schemas")
        self.assertEqual(status, 200)
        self.assertEqual(len(schemas["schemas"]), 3)
        status, verify = self._authed("GET", "/api/driver/verify")
        self.assertEqual(status, 200)
        self.assertTrue(verify["ok"])
        self.assertIn("audit", verify)
        self.assertIn("budget", verify)

    def test_health_does_not_leak_the_driver_token(self):
        _, health = self._authed("GET", "/api/driver/health")
        self.assertNotIn(ui_server._DRIVER_DAEMON["token"], json.dumps(health))

    def test_step_with_no_source_is_a_refusal_not_an_error(self):
        status, data = self._authed("POST", "/api/driver/step",
                                    {"target": "cli", "schema": "cli"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no_capture")

    def test_step_input_errors_are_400(self):
        status, _ = self._authed("POST", "/api/driver/step", {"schema": "cli"})
        self.assertEqual(status, 400)
        status, data = self._authed("POST", "/api/driver/step",
                                    {"target": "cli"})
        self.assertEqual(status, 400)
        self.assertIn("schema", data["error"])

    def test_start_reports_running(self):
        status, data = self._authed("POST", "/api/driver/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["health"]["status"], "up")

    def test_drive_runs_to_an_honest_summary(self):
        status, run = self._authed("POST", "/api/driver/drive",
                                   {"goal": "look around", "max_steps": 2})
        self.assertEqual(status, 201)
        deadline = time.time() + 60
        result = {}
        while time.time() < deadline:
            _, result = self._authed("GET", f"/api/runs/{run['id']}/result")
            if result.get("status") != "running":
                break
            time.sleep(0.05)
        self.assertEqual(result["status"], "max_steps_reached")
        body = result["result"]
        self.assertEqual(body["ok_steps"], 0)
        self.assertEqual(body["total_steps"], 2)
        self.assertIn("nothing verified", body["summary"])
        self.assertTrue(body["audit"]["ok"])

    def test_drive_validation_rejects_a_missing_goal(self):
        status, _ = self._authed("POST", "/api/driver/drive", {})
        self.assertEqual(status, 400)

    def test_step_transport_and_unexpected_errors_map_to_503_and_500(self):
        adapter = MagicMock()
        with patch.object(ui_server, "_driver_adapter", return_value=adapter):
            adapter.step.side_effect = PerceptionUnavailable("service down")
            status, _ = self._authed("POST", "/api/driver/step",
                                     {"target": "cli", "schema": "cli"})
            self.assertEqual(status, 503)
            adapter.step.side_effect = RuntimeError("kaboom")
            status, _ = self._authed("POST", "/api/driver/step",
                                     {"target": "cli", "schema": "cli"})
            self.assertEqual(status, 500)
            adapter.health.side_effect = RuntimeError("down")
            status, _ = self._authed("POST", "/api/driver/start", {})
            self.assertEqual(status, 500)
            for path, attr in (("/api/driver/vocabulary", "vocabulary"),
                               ("/api/driver/schemas", "schemas"),
                               ("/api/driver/verify", "verify")):
                getattr(adapter, attr).side_effect = PerceptionUnavailable("x")
                self.assertEqual(self._authed("GET", path)[0], 503, path)


class EphemeralAuthTest(unittest.TestCase):
    """``ephemeral_auth`` marks a start-generated token. It is never replaced
    by whatever a caller presents first."""

    def setUp(self):
        self.httpd = make_server("127.0.0.1", 0, auth_token="start-token",
                                 ephemeral_auth=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        from harness import events as _events
        _events.remove_sink(self.httpd.ui._on_event)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=60)

    def _status(self, headers=None, path="/api/status"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            return _request(conn, "GET", path, headers=headers)[0]
        finally:
            conn.close()

    def test_the_flag_is_recorded(self):
        self.assertTrue(self.httpd.ui.ephemeral_auth)

    def test_no_token_is_401(self):
        self.assertEqual(self._status(), 401)

    def test_a_first_presenter_cannot_take_over_the_token(self):
        for headers in ({"X-Harness-Auth": "attacker"},
                        {"Authorization": "Bearer attacker"}):
            self.assertEqual(self._status(headers), 401)
        self.assertEqual(self._status(path="/api/status?token=attacker"), 401)
        # Still the start token, and the start token still works.
        self.assertEqual(self.httpd.ui.auth_token, "start-token")
        self.assertEqual(self._status({"X-Harness-Auth": "start-token"}), 200)
        # ...and the attacker is still locked out afterwards.
        self.assertEqual(self._status({"X-Harness-Auth": "attacker"}), 401)

    def test_the_start_token_is_accepted_in_every_supported_form(self):
        self.assertEqual(self._status({"X-Harness-Auth": "start-token"}), 200)
        self.assertEqual(self._status({"Authorization": "Bearer start-token"}), 200)
        self.assertEqual(self._status(path="/api/status?token=start-token"), 200)

    def test_no_token_file_is_written_by_a_presenter(self):
        tmp = tempfile.mkdtemp(prefix="harness-ephemeral-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with patch("harness.config.CONFIG_DIR", tmp):
            self._status({"X-Harness-Auth": "attacker"})
        self.assertFalse(os.path.exists(os.path.join(tmp, "desktop_token")))


if __name__ == "__main__":
    unittest.main()
