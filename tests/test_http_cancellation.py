"""Cancellable urllib subprocess tests use only a loopback HTTP fixture."""
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from harness._http import HttpTransport
from harness.chat import chat
from harness.errors import HarnessError, ToolCancelled
from harness import osal


class _StallHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _wait_for_disconnect(self):
        self.connection.settimeout(4)
        try:
            if not self.connection.recv(1):
                self.server.peer_closed.set()
        except socket.timeout:
            pass
        except OSError:
            self.server.peer_closed.set()

    def do_GET(self):
        self.server.requests += 1
        self.server.accepted.set()
        self._wait_for_disconnect()

    def do_POST(self):
        self.server.requests += 1
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.accepted.set()
        if self.server.mode == "success":
            body = json.dumps({"usage": {"cost": 0.001}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.server.mode == "body":
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"{")
            self.wfile.flush()
        self._wait_for_disconnect()


class HttpCancellationTests(unittest.TestCase):
    def _server(self, mode):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StallHandler)
        server.daemon_threads = True
        server.mode = mode
        server.accepted = threading.Event()
        server.peer_closed = threading.Event()
        server.requests = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _cancelled_request(self, mode, method="POST"):
        server = self._server(mode)
        cancel = threading.Event()
        children = []
        commands = []
        spawn = osal.spawn_piped_process

        def track_child(argv):
            child = spawn(argv)
            children.append(child)
            commands.append(argv)
            return child

        result = {}

        def request():
            transport = HttpTransport(cancel_check=cancel.is_set)
            try:
                if method == "GET":
                    transport.get(f"http://127.0.0.1:{server.server_port}/",
                                  "secret-api-key", timeout=30)
                elif method == "POST_ONCE":
                    transport.post_once(
                        f"http://127.0.0.1:{server.server_port}/",
                        "secret-api-key", {"prompt": "secret-prompt"},
                        timeout=30)
                else:
                    transport.post(
                        f"http://127.0.0.1:{server.server_port}/",
                        "secret-api-key", {"prompt": "secret-prompt"},
                        timeout=30)
            except ToolCancelled:
                result["cancelled"] = True
            except BaseException as exc:
                result["exception"] = exc

        with mock.patch("harness.osal.spawn_piped_process",
                        side_effect=track_child):
            caller = threading.Thread(target=request)
            caller.start()
            self.assertTrue(server.accepted.wait(5), "child never dispatched request")
            cancel_at = time.monotonic()
            cancel.set()
            caller.join(4)
        self.assertFalse(caller.is_alive(), "cancelled caller thread did not return")
        self.assertTrue(result.get("cancelled"), result.get("exception"))
        self.assertLess(time.monotonic() - cancel_at, 4.0)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll(), "HTTP child process was not reaped")
        command = " ".join(commands[0])
        self.assertNotIn("secret-api-key", command)
        self.assertNotIn("secret-prompt", command)
        self.assertTrue(server.peer_closed.wait(4), "provider peer did not observe disconnect")

    def test_cancel_while_waiting_for_response_headers(self):
        self._cancelled_request("headers")

    def test_cancel_while_reading_response_body(self):
        self._cancelled_request("body")

    def test_cancel_during_key_status_get(self):
        self._cancelled_request("headers", method="GET")

    def test_cancel_during_one_shot_jev_request(self):
        self._cancelled_request("headers", method="POST_ONCE")

    def test_cancel_before_dispatch_sends_no_request(self):
        server = self._server("headers")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ToolCancelled):
            HttpTransport(cancel_check=cancel.is_set).post(
                f"http://127.0.0.1:{server.server_port}/", "test", {})
        self.assertEqual(server.requests, 0)

    def test_uncancelled_child_request_preserves_success_response(self):
        server = self._server("success")
        status, response = HttpTransport(cancel_check=lambda: False).post(
            f"http://127.0.0.1:{server.server_port}/", "test", {"ok": True})
        self.assertEqual(status, 200)
        self.assertEqual(response["usage"]["cost"], 0.001)
        self.assertEqual(server.requests, 1)

    def test_cancel_during_retry_delay_prevents_another_attempt(self):
        cancel = threading.Event()
        transport = HttpTransport(cancel_check=cancel.is_set)

        def transient_then_cancel(*_args):
            cancel.set()
            return 429, json.dumps({"usage": {"cost": 0.001}}), None

        with mock.patch.object(transport, "_request_once_cancellable",
                               side_effect=transient_then_cancel) as request:
            with self.assertRaises(ToolCancelled) as cancelled:
                transport.post("http://127.0.0.1/", "test", {})
        request.assert_called_once()
        self.assertAlmostEqual(cancelled.exception.known_cost, 0.001)

    def test_cancelled_reasoning_retry_settles_first_attempt_cost_once(self):
        class Governor:
            def __init__(self):
                self.costs = []

            def check_byok(self, _model):
                pass

            def assert_no_tools(self, _payload, _model):
                pass

            def record_actual(self, cost, model):
                self.costs.append((cost, model))

        class RetryTransport:
            def __init__(self):
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return 400, {
                        "error": {"message": "unsupported parameter: reasoning"},
                        "usage": {"cost": 0.002},
                    }
                raise ToolCancelled()

        governor = Governor()
        transport = RetryTransport()
        with mock.patch("harness.chat._effort_to_send", return_value="low"), \
                mock.patch("harness.chat.reasoning_param_rejected", return_value=False):
            with self.assertRaises(ToolCancelled) as cancelled:
                chat(transport, "test-key", "test/model", [], 64,
                     reasoning_effort="low", governor=governor)
        self.assertEqual(transport.calls, 2)
        self.assertAlmostEqual(cancelled.exception.known_cost, 0.002)
        self.assertEqual(len(governor.costs), 1)
        self.assertAlmostEqual(governor.costs[0][0], 0.002)
        self.assertEqual(governor.costs[0][1], "test/model")

    def test_cancelled_known_cost_overrun_is_booked_without_masking_cancel(self):
        class Governor:
            def __init__(self):
                self.overruns = []

            def check_byok(self, _model):
                pass

            def assert_no_tools(self, _payload, _model):
                pass

            def record_actual(self, _cost, _model):
                raise HarnessError("over ceiling")

            def record_overrun(self, cost, model):
                self.overruns.append((cost, model))

        class CancelTransport:
            def post(self, *_args, **_kwargs):
                raise ToolCancelled(known_cost=0.003)

        governor = Governor()
        with self.assertRaises(ToolCancelled) as cancelled:
            chat(CancelTransport(), "test-key", "test/model", [], 64,
                 reasoning_effort="none", governor=governor)
        self.assertTrue(cancelled.exception.cost_accounted)
        self.assertEqual(governor.overruns, [(0.003, "test/model")])

    def test_web_fetch_uses_the_run_cancellation_context(self):
        from harness import web
        from harness.events import task_context

        server = self._server("headers")
        cancel = threading.Event()
        result = {}

        def request():
            with task_context("ui/web-run", cancel.is_set):
                try:
                    web._http_get(
                        f"http://127.0.0.1:{server.server_port}/", timeout=30)
                except ToolCancelled:
                    result["cancelled"] = True
                except BaseException as exc:
                    result["exception"] = exc

        caller = threading.Thread(target=request)
        caller.start()
        self.assertTrue(server.accepted.wait(5), "web worker never dispatched request")
        cancel_at = time.monotonic()
        cancel.set()
        caller.join(4)
        self.assertFalse(caller.is_alive(), "web fetch did not stop promptly")
        self.assertTrue(result.get("cancelled"), result.get("exception"))
        self.assertLess(time.monotonic() - cancel_at, 4.0)
        self.assertTrue(server.peer_closed.wait(4), "web peer did not observe disconnect")


if __name__ == "__main__":
    unittest.main()
