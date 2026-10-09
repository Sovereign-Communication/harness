"""Cancellable urllib subprocess tests use only a loopback HTTP fixture."""
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from pathlib import Path

from harness._http import HttpTransport
from harness.chat import chat
from harness.errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from harness import osal
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


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
        spawn_options = []
        spawn = osal.spawn_piped_process

        def track_child(argv, **kwargs):
            child = spawn(argv, **kwargs)
            children.append(child)
            commands.append(argv)
            spawn_options.append(kwargs)
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
            except ToolCancelled as exc:
                result["cancelled"] = True
                result["cancel_error"] = exc
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
        self.assertTrue(result["cancel_error"].usage_unknown)
        self.assertLess(time.monotonic() - cancel_at, 4.0)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll(), "HTTP child process was not reaped")
        command = " ".join(commands[0])
        self.assertNotIn("secret-api-key", command)
        self.assertNotIn("secret-prompt", command)
        self.assertIn("-I", commands[0])
        self.assertTrue(commands[0][-1].endswith("_http_worker.py"))
        self.assertTrue(Path(commands[0][-1]).is_absolute())
        self.assertEqual(Path(spawn_options[0]["cwd"]).resolve(),
                         Path(__file__).resolve().parents[1])
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
        with self.assertRaises(ToolCancelled) as cancelled:
            HttpTransport(cancel_check=cancel.is_set).post(
                f"http://127.0.0.1:{server.server_port}/", "test", {})
        self.assertFalse(cancelled.exception.usage_unknown)
        self.assertEqual(server.requests, 0)

    def test_uncancelled_child_request_preserves_success_response(self):
        server = self._server("success")
        status, response = HttpTransport(cancel_check=lambda: False).post(
            f"http://127.0.0.1:{server.server_port}/", "test", {"ok": True})
        self.assertEqual(status, 200)
        self.assertEqual(response["usage"]["cost"], 0.001)
        self.assertEqual(server.requests, 1)

    def test_with_cancel_preserves_stateful_subclass_without_mutation(self):
        from harness.events import current_cancel_check

        class StatefulTransport(HttpTransport):
            def __init__(self, marker):
                super().__init__()
                self.marker = marker
                self.observed = []

            def post(self, *_args, **_kwargs):
                self.observed.append((self.marker, current_cancel_check()))
                return 200, {"ok": True}

        cancel = threading.Event()
        cancel_check = cancel.is_set
        transport = StatefulTransport(marker=object())
        bound = transport.with_cancel(cancel_check)

        status, response = bound.post("https://fixture.invalid", "key", {})

        self.assertEqual((status, response), (200, {"ok": True}))
        self.assertIs(bound.marker, transport.marker)
        self.assertIsNone(transport.cancel_check)
        self.assertEqual(len(transport.observed), 1)
        self.assertIs(transport.observed[0][1], cancel_check)

    def test_run_cancel_check_overrides_transport_default(self):
        from harness.events import cancellation_context

        configured = lambda: False
        run_cancel = lambda: True
        transport = HttpTransport(cancel_check=configured).with_cancel(run_cancel)

        with self.assertRaises(ToolCancelled):
            transport.post("https://fixture.invalid", "key", {})

        self.assertIs(transport.cancel_check, configured)
        with cancellation_context(run_cancel):
            self.assertIs(transport._active_cancel_check(), run_cancel)

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
        self.assertFalse(cancelled.exception.usage_unknown)

    def test_transient_attempt_cost_and_wire_progress_survive_success(self):
        from harness import events
        from harness.jev import JevEvaluator, jev_cost
        from harness.events import provider_request_context, task_context

        events_seen = []
        events.add_sink(events_seen.append)
        self.addCleanup(events.remove_sink, events_seen.append)
        transport = HttpTransport(cancel_check=lambda: False)
        response = {"answers": {"supported": {"type": "noul", "noul": 0.99}},
                    "usage": {"input_tokens": 100, "output_tokens": 0}}
        bodies = iter((
            (429, json.dumps({"usage": {"input_tokens": 20}}), None),
            (200, json.dumps(response), None),
        ))
        with mock.patch.object(transport, "_request_once_cancellable",
                               side_effect=lambda *_a, **_k: next(bodies)), \
                mock.patch.object(transport, "_retry_wait"):
            with task_context("ui/retry-run"):
                with provider_request_context("req-123", "jev-test", "jev"):
                    status, parsed = transport.post(
                        "https://fixture.invalid", "secret-api-key",
                        {"model": "jev-test", "prompt": "secret-prompt"})

        self.assertEqual(status, 200)
        self.assertAlmostEqual(parsed["usage"]["retry_cost"], jev_cost(20))
        evaluator = JevEvaluator(api_key="key")
        evaluator.model = "jev-test"
        result = evaluator._parse_jev_response(
            parsed, {"supported": {"type": "noul", "instructions": "support"}})
        self.assertAlmostEqual(result.cost, jev_cost(100) + jev_cost(20))
        phases = [ev["phase"] for ev in events_seen]
        self.assertEqual(phases, ["start", "response", "retry_wait", "start", "response"])
        self.assertTrue(all(ev["task_id"] == "ui/retry-run" for ev in events_seen))
        self.assertTrue(all(ev["request_id"] == "req-123" for ev in events_seen))
        serialized = json.dumps(events_seen)
        self.assertNotIn("secret-api-key", serialized)
        self.assertNotIn("secret-prompt", serialized)
        self.assertEqual(events_seen[2]["retry_reason"], "rate_limited")

    def test_network_failure_is_not_retransmitted_and_marks_unknown_usage(self):
        from harness import events
        from harness.events import provider_request_context, task_context

        events_seen = []
        sink = events_seen.append
        events.add_sink(sink)
        self.addCleanup(events.remove_sink, sink)
        transport = HttpTransport(cancel_check=lambda: False)
        with mock.patch.object(
                transport, "_request_once_cancellable",
                side_effect=OSError("offline")) as request, \
                mock.patch.object(transport, "_retry_wait") as retry_wait:
            with task_context("ui/network-run"), \
                    provider_request_context("req-network", "test/model", "primary"):
                with self.assertRaises(ProviderUsageUnknown):
                    transport.post(
                        "https://fixture.invalid", "key", {"model": "test/model"})
        self.assertEqual(request.call_count, 1)
        retry_wait.assert_not_called()
        self.assertEqual([e["phase"] for e in events_seen], ["start", "error"])
        self.assertTrue(events_seen[-1]["usage_unknown"])

    def test_chat_holds_unknown_provider_liability_after_network_failure(self):
        from harness import events
        governor = SpendGovernor(
            FakeTransport(models=[m("test/model", prompt="0.000001",
                                    completion="0.000002")]),
            "sk-test", max_cost=0.10)
        transport = HttpTransport(cancel_check=lambda: False)
        events_seen = []
        sink = events_seen.append
        events.add_sink(sink)
        self.addCleanup(events.remove_sink, sink)
        with mock.patch.object(transport, "_request_once_cancellable",
                               side_effect=OSError("socket timed out")) as request:
            with self.assertRaises(ProviderUsageUnknown) as raised:
                chat(transport, "key", "test/model", [
                    {"role": "user", "content": "simple"}], 64,
                    reasoning_effort="none", governor=governor)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(raised.exception.usage_unknown)
        self.assertTrue(raised.exception.cost_accounted)
        self.assertEqual(governor.spent, 0.0)
        self.assertGreater(governor.outstanding, 0.0)
        self.assertGreater(governor.snapshot()["unknown_liability"], 0.0)
        self.assertLess(governor.remaining(), 0.10)
        self.assertEqual(sum(e["type"] == "provider_usage_unknown"
                             for e in events_seen), 1)

    def test_chat_cancellation_holds_unknown_provider_liability(self):
        governor = SpendGovernor(
            FakeTransport(models=[m("test/model", prompt="0.000001",
                                    completion="0.000002")]),
            "sk-test", max_cost=0.10)
        transport = HttpTransport(cancel_check=lambda: False)
        with mock.patch.object(
                transport, "_request_once_cancellable",
                side_effect=ToolCancelled(usage_unknown=True)):
            with self.assertRaises(ToolCancelled) as raised:
                chat(transport, "key", "test/model", [
                    {"role": "user", "content": "simple"}], 64,
                    reasoning_effort="none", governor=governor)
        self.assertTrue(raised.exception.cost_accounted)
        self.assertGreater(governor.outstanding, 0.0)
        self.assertGreater(governor.snapshot()["unknown_liability"], 0.0)

    def test_terminal_jev_http_failure_keeps_known_retry_spend(self):
        from harness.jev import JevEvaluator, jev_cost

        transport = HttpTransport(cancel_check=lambda: False)
        bodies = iter((
            (429, json.dumps({"usage": {"input_tokens": 20}}), None),
            (500, json.dumps({"usage": {"input_tokens": 30}}), None),
        ))
        with mock.patch.object(HttpTransport, "MAX_RETRIES", 1), \
                mock.patch.object(
                    transport, "_request_once_cancellable",
                    side_effect=lambda *_a, **_k: next(bodies)), \
                mock.patch.object(transport, "_retry_wait"):
            evaluator = JevEvaluator(api_key="key", transport=transport)
            result = evaluator.evaluate(
                {"content": "test"},
                {"supported": {"type": "noul", "instructions": "support"}})
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.fallback_reason, "http_fallback")
        self.assertAlmostEqual(result.cost, jev_cost(20) + jev_cost(30))

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
