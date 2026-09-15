import atexit
import io
import json
import os
import tempfile
import threading
import time
import unittest

from harness.apply import ApplyEngine
from harness.spend import SpendGovernor
from harness.ledger import AutonomyLedger
from harness.mcp import McpServer
from harness.router import Router
from tests._fake import FakeTransport, m, comp, consent

JUDGE = "inclusionai/ling-2.6-flash"
P1 = "meta-llama/llama-3.1-8b-instruct"
P2 = "ibm-granite/granite-4.1-8b"
APPLY = "deepseek/deepseek-chat"
ORIGINAL = "def add(a, b):\n    return a + b\n"
CHANGED = "def add(a, b):\n    return a + b + 0\n"


_TMP = tempfile.TemporaryDirectory()
# The temp dir lives at module scope so every test's ledger is isolated by an
# unpredictable name; without an atexit hook it is GC'd at interpreter
# shutdown, tripping -W error::ResourceWarning runs.
atexit.register(_TMP.cleanup)


def make_server(posts=None):
    transport = FakeTransport(models=[m(JUDGE), m(P1), m(P2), m(APPLY)], posts=posts)
    governor = SpendGovernor(transport, "sk-test")
    ledger = AutonomyLedger(os.path.join(_TMP.name, f"ledger-{os.urandom(4).hex()}.jsonl"))
    router = Router([P1, P2], JUDGE, APPLY)
    engine = ApplyEngine(transport, "sk-test", governor, ledger, router,
                         default_require_consent=True)
    return transport, McpServer(transport=transport, api_key="sk-test", governor=governor,
                                ledger=ledger, router=router, engine=engine,
                                allow_write=True, allowed_roots=[_TMP.name])


def consent_json(decision, reason="ok"):
    return json.dumps({"decision": decision, "reason": reason,
                       "redirect_model": None, "scope_suggestion": None})


def run(feed, posts=None):
    transport, server = make_server(posts)
    out = io.StringIO()
    server.stdout = out
    server.stdin = io.StringIO(feed)
    server.serve_forever()
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return transport, lines


class McpProtocolTests(unittest.TestCase):
    def test_handshake_and_tools(self):
        feed = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize",'
            '"params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{}}}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
            '{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["id"], 1)
        result = lines[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "harness")
        self.assertIn("tools", result["capabilities"])
        # tools/list
        self.assertEqual(lines[1]["id"], 2)
        tools = {t["name"]: t for t in lines[1]["result"]["tools"]}
        self.assertIn("panel_verify", tools)
        self.assertIn("apply_edit", tools)
        self.assertIn("offer_work", tools)
        self.assertIn("defer_work", tools)
        self.assertIn("ledger_status", tools)
        self.assertIn("participation_report", tools)
        self.assertIn("spend_status", tools)
        self.assertTrue(tools["panel_verify"]["inputSchema"]["properties"]["prompt"])
        # ping
        self.assertEqual(lines[2]["id"], 3)
        self.assertEqual(lines[2]["result"], {})

    def test_initialize_captures_client_caller(self):
        """The stdio peer names itself in clientInfo: the server tags its
        session authorship so ledger evidence attributes to the caller."""
        feed = ('{"jsonrpc":"2.0","id":1,"method":"initialize",'
                '"params":{"protocolVersion":"2025-06-18",'
                '"clientInfo":{"name":"probe-harness","version":"1.0"}}}\n')
        transport, server = make_server()
        out = io.StringIO()
        server.stdout = out
        server.stdin = io.StringIO(feed)
        server.serve_forever()
        self.assertEqual(server.caller, "mcp:probe-harness/1.0")
        self.assertEqual(server.ledger.caller, "mcp:probe-harness/1.0")

    def test_initialize_without_client_info_stays_generic(self):
        feed = ('{"jsonrpc":"2.0","id":1,"method":"initialize",'
                '"params":{"protocolVersion":"2025-06-18"}}\n')
        _, server = make_server()
        server.stdin = io.StringIO(feed)
        server.stdout = io.StringIO()
        server.serve_forever()
        self.assertEqual(server.caller, "mcp")

    def test_initialize_notification_has_no_response(self):
        feed = ('{"jsonrpc":"2.0","method":"initialize",'
                '"params":{"protocolVersion":"2025-06-18"}}\n'
                '{"jsonrpc":"2.0","id":10,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual([line["id"] for line in lines], [10])

    def test_panel_verify_tool(self):
        feed = ('{"jsonrpc":"2.0","id":10,"method":"tools/call",'
                '"params":{"name":"panel_verify","arguments":{"prompt":"Is this design sound?"}}}\n')
        _, lines = run(feed, posts=[comp("yes"), comp("mostly"), comp("verdict: sound")])
        self.assertEqual(lines[0]["id"], 10)
        result = lines[0]["result"]
        self.assertFalse(result["isError"])
        sc = result["structuredContent"]
        self.assertEqual(len(sc["panel_results"]), 2)
        self.assertIn("verdict", sc)
        text = result["content"][0]["text"]
        self.assertIsInstance(json.loads(text), dict)

    def test_panel_verify_delegates_to_service_layer(self):
        """MCP's verify lane must delegate to the canonical service seam
        (the same owner the CLI and web server consume): the protocol layer
        keeps boundary validation and its historical result shape, but lane
        assembly, cancellation envelope, and cost/meta have ONE owner."""
        import harness.mcp as mcp_module
        captured = {}

        def fake_run_verify(settings=None, **kwargs):
            captured.update(kwargs)
            captured["settings"] = settings
            return {"status": "ok", "verdict": "sound", "actual_cost": 0.0}

        transport, server = make_server()
        original = mcp_module._service_run_verify
        mcp_module._service_run_verify = fake_run_verify
        try:
            feed = ('{"jsonrpc":"2.0","id":40,"method":"tools/call",'
                    '"params":{"name":"panel_verify","arguments":'
                    '{"prompt":"sound?","judge":"' + P2 + '"}}}\n')
            out = io.StringIO()
            server.stdout = out
            server.stdin = io.StringIO(feed)
            server.serve_forever()
        finally:
            mcp_module._service_run_verify = original
        # The tool result shape is unchanged: the service return becomes the
        # structuredContent verbatim (no meta/cost attachment in MCP).
        response = json.loads(out.getvalue())
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"],
                         {"status": "ok", "verdict": "sound",
                          "actual_cost": 0.0})
        # Session-injected dependencies flow to the service; the service is
        # told MCP owns the task-id and meta contracts.
        self.assertIs(captured["governor"], server.governor)
        self.assertIs(captured["ledger"], server.ledger)
        self.assertIs(captured["transport"], transport)
        self.assertIs(captured["router"], server.router)
        self.assertFalse(captured["generate_task_id"])
        self.assertFalse(captured["attach_meta"])
        self.assertIsNone(captured["settings"])
        self.assertEqual(captured["prompt"], "sound?")
        self.assertEqual(captured["judge"], P2)  # boundary validation passed through
        self.assertIsNotNone(captured["cancel_check"])  # cooperative cancel threaded
        # No verify assembly left behind in the protocol module.
        self.assertFalse(hasattr(mcp_module, "panel_judge"))

    def test_panel_verify_cancelled_run_maps_to_protocol_error(self):
        """End-to-end cancellation through the real stdio frame loop: the
        first panel seat bills normally, a notifications/cancelled arrives
        while the second seat's call is in flight, the cooperative
        cancel_check trips, the service builds the honest cancelled envelope
        (in-flight billed spend included), and MCP answers its established
        -32800 error without hiding the spend."""
        class GatedTransport(FakeTransport):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.second_started = threading.Event()
                self.release = threading.Event()
                self._count = 0

            def post(self, *args, **kwargs):
                self._count += 1
                if self._count == 2:
                    self.second_started.set()
                    self.release.wait(10)
                return super().post(*args, **kwargs)

        transport = GatedTransport(models=[m(JUDGE), m(P1), m(P2), m(APPLY)],
                                   posts=[comp("yes"), comp("yes")])
        governor = SpendGovernor(transport, "sk-test")
        ledger = AutonomyLedger(os.path.join(_TMP.name,
                                             f"ledger-{os.urandom(4).hex()}.jsonl"))
        router = Router([P1, P2], JUDGE, APPLY)
        engine = ApplyEngine(transport, "sk-test", governor, ledger, router,
                             default_require_consent=True)
        server = McpServer(transport=transport, api_key="sk-test",
                           governor=governor, ledger=ledger, router=router,
                           engine=engine, allow_write=True,
                           allowed_roots=[_TMP.name])

        class Feed:
            calls = 0

            def readline(self):
                if Feed.calls == 0:
                    Feed.calls += 1
                    return ('{"jsonrpc":"2.0","id":41,"method":"tools/call",'
                            '"params":{"name":"panel_verify","arguments":'
                            '{"prompt":"sound?"}}}\n')
                if Feed.calls == 1:
                    Feed.calls += 1
                    # Cancel only after the second seat's network call is
                    # genuinely in flight (deterministic, no sleeps) and the
                    # first seat has billed.
                    if not transport.second_started.wait(10):
                        transport.release.set()
                        raise AssertionError("panel never made two calls")
                    return ('{"jsonrpc":"2.0","method":"notifications/cancelled",'
                            '"params":{"requestId":41}}\n')
                transport.release.set()
                return ""

        out = io.StringIO()
        server.stdout = out
        server.stdin = Feed()
        server.serve_forever()
        response = json.loads(out.getvalue())
        # The established protocol-level cancellation error is unchanged.
        self.assertEqual(response["error"]["code"], -32800)
        self.assertEqual(response["error"]["message"], "Request cancelled")
        # The first seat billed and completed; the second seat's POST was
        # already in flight (in-flight POSTs run to their own timeout -- the
        # documented residual) and its real spend is preserved too.
        self.assertEqual(len(transport.chat_posts()), 2)
        self.assertGreater(governor.spent, 0.0)
        self.assertEqual(server._inflight, set())

    def test_tool_deadline_stops_delegated_run_at_protocol_error(self):
        """End-to-end tool deadline through the real stdio frame loop: no
        notifications/cancelled is ever sent -- the only trip mechanism is
        the per-tool deadline stamped at submit time, polled through the
        service-delegated cancel_check. The first seat completes and bills;
        the second seat's POST (held by the gate) runs to completion after
        release -- the documented in-flight residual -- and its post-POST
        poll trips the expired deadline, so the service builds the honest
        cancelled envelope and MCP answers -32800 without hiding spend.
        Deterministic: the gate releases only once the deadline is provably
        expired, never on a hope-and-a-sleep."""
        class GatedTransport(FakeTransport):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.second_started = threading.Event()
                self.release = threading.Event()
                self._count = 0

            def post(self, *args, **kwargs):
                self._count += 1
                if self._count == 2:
                    self.second_started.set()
                    self.release.wait(10)
                return super().post(*args, **kwargs)

        transport = GatedTransport(models=[m(JUDGE), m(P1), m(P2), m(APPLY)],
                                   posts=[comp("yes"), comp("yes")])
        governor = SpendGovernor(transport, "sk-test")
        ledger = AutonomyLedger(os.path.join(_TMP.name,
                                             f"ledger-{os.urandom(4).hex()}.jsonl"))
        router = Router([P1, P2], JUDGE, APPLY)
        engine = ApplyEngine(transport, "sk-test", governor, ledger, router,
                             default_require_consent=True)
        server = McpServer(transport=transport, api_key="sk-test",
                           governor=governor, ledger=ledger, router=router,
                           engine=engine, allow_write=True,
                           allowed_roots=[_TMP.name])
        server.tool_timeout = 0.05  # seat 1's in-memory call is far faster

        class Feed:
            calls = 0

            def readline(self):
                if Feed.calls == 0:
                    Feed.calls += 1
                    return ('{"jsonrpc":"2.0","id":42,"method":"tools/call",'
                            '"params":{"name":"panel_verify","arguments":'
                            '{"prompt":"sound?"}}}\n')
                if Feed.calls == 1:
                    Feed.calls += 1
                    # Hold until the second seat is genuinely in flight AND
                    # the submit-time deadline clock has provably expired
                    # (bounded backstop: 5s, then fail instead of hanging).
                    if not transport.second_started.wait(10):
                        transport.release.set()
                        raise AssertionError("panel never made two calls")
                    for _ in range(2500):
                        if server._is_expired(42):
                            break
                        time.sleep(0.002)
                    else:
                        transport.release.set()
                        raise AssertionError("tool deadline never expired")
                    transport.release.set()
                    return ""
                return ""

        out = io.StringIO()
        server.stdout = out
        server.stdin = Feed()
        server.serve_forever()
        response = json.loads(out.getvalue())
        # The deadline path answers the same established protocol error as
        # an explicit cancellation -- one cooperative stop, one message.
        self.assertEqual(response["error"]["code"], -32800)
        self.assertEqual(response["error"]["message"], "Request cancelled")
        # Seat 1 billed; seat 2's POST was in flight past its poll point and
        # completed (documented residual) -- spend preserved, never hidden.
        self.assertEqual(len(transport.chat_posts()), 2)
        self.assertGreater(governor.spent, 0.0)
        self.assertEqual(server._inflight, set())

    def test_offer_and_defer_tools(self):
        feed = (
            '{"jsonrpc":"2.0","id":11,"method":"tools/call",'
            '"params":{"name":"offer_work","arguments":{"task":"Rewrite the parser"}}}\n'
            '{"jsonrpc":"2.0","id":12,"method":"tools/call",'
            '"params":{"name":"defer_work","arguments":{"task_id":"t9","reason":"changed my mind"}}}\n'
            '{"jsonrpc":"2.0","id":13,"method":"tools/call",'
            '"params":{"name":"participation_report","arguments":{}}}\n')
        transport, server = make_server(
            posts=[consent("defer", "I would rather not")])
        out = io.StringIO()
        server.stdout = out
        server.stdin = io.StringIO(feed)
        server.serve_forever()
        lines = [json.loads(line) for line in out.getvalue().splitlines()
                 if line.strip()]
        # Lanes run concurrently, so frames correlate by id, never by
        # position (JSON-RPC permits any response order).
        by_id = {line["id"]: line for line in lines}
        offer = by_id[11]["result"]
        self.assertEqual(offer["structuredContent"]["decision"], "defer")
        defer = by_id[12]["result"]["structuredContent"]
        self.assertEqual(defer["status"], "deferred")
        # serve_forever drains every lane before returning, so a report
        # taken afterwards sees all three tools' evidence deterministically
        # (an in-feed report would race the spendy lane by design).
        report = server._invoke("participation_report", {})
        self.assertEqual(report["offers"], 1)
        self.assertEqual(report["defers"], 1)

    def test_invalid_jsonrpc_envelope_is_rejected_and_server_continues(self):
        feed = ('{"jsonrpc":"1.0","id":19,"method":"ping"}\n'
                '{"jsonrpc":"2.0","id":20,"method":"ping"}\n'
                '{"jsonrpc":"2.0","id":21,"method":"ping","params":{"x":NaN}}\n'
                '{"jsonrpc":"2.0","id":22,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual([line["error"]["code"] for line in lines[:1]], [-32600])
        self.assertEqual(lines[1]["id"], 20)
        self.assertEqual(lines[2]["error"]["code"], -32700)
        self.assertEqual(lines[3]["id"], 22)

    def test_non_object_params_are_protocol_errors(self):
        feed = ('{"jsonrpc":"2.0","id":19,"method":"tools/call",'
                '"params":[]}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32602)
        self.assertIn("params must be an object", lines[0]["error"]["message"])

    def test_non_object_arguments_are_protocol_errors(self):
        feed = ('{"jsonrpc":"2.0","id":19,"method":"tools/call",'
                '"params":{"name":"ledger_status","arguments":[]}}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32602)
        self.assertIn("arguments must be an object", lines[0]["error"]["message"])

    def test_non_object_cancellation_notification_is_ignored(self):
        feed = ('{"jsonrpc":"2.0","method":"notifications/cancelled",'
                '"params":[]}\n'
                '{"jsonrpc":"2.0","id":25,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual([line["id"] for line in lines], [25])


    def test_malformed_optional_arguments_return_error_results(self):
        cases = [
            ("reasoning_effort", [], "reasoning_effort"),
            ("converge", "yes", "converge"),
            ("task_id", [], "task_id"),
        ]
        for key, value, message in cases:
            with self.subTest(key=key):
                arguments = {"prompt": "x", key: value}
                feed = json.dumps({"jsonrpc": "2.0", "id": 18,
                                   "method": "tools/call",
                                   "params": {"name": "panel_verify",
                                              "arguments": arguments}}) + "\n"
                _, lines = run(feed)
                self.assertEqual(len(lines), 1)
                response = lines[0]
                if "error" in response:
                    self.assertEqual(response["error"]["code"], -32602)
                else:
                    self.assertTrue(response["result"]["isError"])
                    self.assertIn(message, response["result"]["content"][0]["text"])

    def test_missing_offer_task_is_error_result(self):
        feed = ('{"jsonrpc":"2.0","id":18,"method":"tools/call",'
                '"params":{"name":"offer_work","arguments":{}}}\n')
        _, lines = run(feed)
        self.assertTrue(lines[0]["result"]["isError"])
        self.assertIn("task is required", lines[0]["result"]["content"][0]["text"])

    def test_missing_defer_task_id_is_error_result(self):
        feed = ('{"jsonrpc":"2.0","id":18,"method":"tools/call",'
                '"params":{"name":"defer_work","arguments":{}}}\n')
        _, lines = run(feed)
        self.assertTrue(lines[0]["result"]["isError"])
        self.assertIn("task_id is required", lines[0]["result"]["content"][0]["text"])

        feed = ('{"jsonrpc":"2.0","id":20,"method":"tools/call",'
                '"params":{"name":"nope","arguments":{}}}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32602)

    def test_unexpected_tool_exception_still_returns_protocol_error(self):
        server = make_server()[1]
        server._invoke = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        out = io.StringIO()
        server.stdout = out
        server.stdin = io.StringIO('{"jsonrpc":"2.0","id":22,"method":"tools/call",'
                                    '"params":{"name":"ledger_status","arguments":{}}}\n')
        server.serve_forever()
        response = json.loads(out.getvalue())
        self.assertEqual(response["error"]["code"], -32603)

    def test_duplicate_request_id_is_rejected_while_in_flight(self):
        server = make_server()[1]
        out = io.StringIO()
        server.stdout = out
        server.stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":7,"method":"tools/call",'
            '"params":{"name":"ledger_status","arguments":{}}}\n')
        server._inflight.add(7)
        server.serve_forever()
        response = json.loads(out.getvalue())
        self.assertEqual(response["error"]["code"], -32600)
        self.assertIn("duplicate request id", response["error"]["message"])

    def test_duplicate_id_stays_reserved_until_response_is_written(self):
        server = make_server()[1]
        out = io.StringIO()
        server.stdout = out
        ready = threading.Event()
        release = threading.Event()
        original_call = server._call_tool

        def delayed_call(message):
            response = original_call(message)
            ready.set()
            release.wait(2)
            return response

        class Feed:
            def __init__(self):
                self.calls = 0

            def readline(self):
                if self.calls == 0:
                    self.calls += 1
                    return ('{"jsonrpc":"2.0","id":7,"method":"tools/call",'
                            '"params":{"name":"ledger_status","arguments":{}}}\n')
                if self.calls == 1:
                    self.calls += 1
                    ready.wait(2)
                    return ('{"jsonrpc":"2.0","id":7,"method":"tools/call",'
                            '"params":{"name":"ledger_status","arguments":{}}}\n')
                release.set()
                return ""

        server._call_tool = delayed_call
        server.stdin = Feed()
        server.serve_forever()
        responses = [json.loads(line) for line in out.getvalue().splitlines()
                     if line.strip()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(sum("error" in response for response in responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(sum("result" in response for response in responses), 1)
        self.assertEqual(server._inflight, set())

    def test_null_request_id_is_still_an_identified_request(self):
        feed = ('{"jsonrpc":"2.0","id":null,"method":"ping"}\n'
                '{"jsonrpc":"2.0","id":null,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual(len(lines), 2)
        self.assertIsNone(lines[0]["id"])
        self.assertIsNone(lines[1]["id"])
        self.assertNotIn("error", lines[0])
        self.assertNotIn("error", lines[1])

    def test_non_scalar_ids_are_protocol_errors(self):
        feed = (json.dumps({"jsonrpc": "2.0", "id": [],
                            "method": "tools/call",
                            "params": {"name": "ledger_status", "arguments": {}}}) + "\n"
                '{"jsonrpc":"2.0","id":26,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32600)
        self.assertIsNone(lines[0]["id"])
        self.assertEqual(lines[1]["id"], 26)

    def test_non_scalar_cancellation_id_notification_is_ignored(self):
        feed = ('{"jsonrpc":"2.0","method":"notifications/cancelled",'
                '"params":{"requestId":[]}}\n'
                '{"jsonrpc":"2.0","id":27,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual([line["id"] for line in lines], [27])

    def test_exception_cleanup_clears_late_cancellation(self):
        server = make_server()[1]
        server._inflight.add(2)
        server._cancelled.add(2)
        server._call_tool_impl = lambda msg: (_ for _ in ()).throw(RuntimeError("boom"))
        response = server._write_tool_response({"id": 2})
        self.assertEqual(response, None)
        self.assertNotIn(2, server._inflight)
        self.assertNotIn(2, server._cancelled)

    def test_queued_cancellation_completes_and_cleans_up(self):
        server = make_server()[1]
        out = io.StringIO()
        server.stdout = out
        server._inflight.add(2)
        server._cancelled.add(2)
        server._write_tool_response({"id": 2, "params": {}})
        response = json.loads(out.getvalue())
        self.assertEqual(response["error"]["code"], -32800)
        self.assertNotIn(2, server._inflight)
        self.assertNotIn(2, server._cancelled)

    def test_tool_notification_has_no_response(self):
        """A tools/call notification must not emit an unsolicited result."""
        feed = ('{"jsonrpc":"2.0","method":"tools/call",'
                '"params":{"name":"ledger_status","arguments":{}}}\n'
                '{"jsonrpc":"2.0","id":26,"method":"ping"}\n')
        _, lines = run(feed)
        self.assertEqual([line["id"] for line in lines], [26])

    def test_tool_execution_error_is_error_result(self):
        feed = ('{"jsonrpc":"2.0","id":21,"method":"tools/call",'
                '"params":{"name":"apply_edit","arguments":'
                '{"file":"C:/does/not/exist.py","instruction":"x"}}}\n')
        _, lines = run(feed)
        self.assertNotIn("error", lines[0])
        self.assertTrue(lines[0]["result"]["isError"])

    def test_apply_rejects_ungated_continuation_before_capability_lookup(self):
        """MCP must share the CLI/library authority boundary. Validation now
        runs structurally before any governor/capability access, so the guard
        is the empty chat log itself."""
        transport, server = make_server()
        state = {"file_path": "missing.py", "verify_only": False,
                 "verification_required": True}
        with self.assertRaisesRegex(Exception, "missing its authoritative verify_cmd"):
            server._invoke("apply_edit", {"instruction": "x", "continuation": state})
        self.assertEqual(transport.chat_posts(), [])

    def test_apply_tool_schema_allows_continuation_without_file(self):
        transport, server = make_server()
        tool = next(t for t in server._tools() if t["name"] == "apply_edit")
        self.assertEqual(tool["inputSchema"]["anyOf"], [
            {"required": ["instruction"]},
            {"required": ["continuation"]},
        ])

    def test_apply_tool_accepts_file_list_for_batch(self):
        """Batch parity with the CLI (#12): the tool's 'file' argument accepts
        an array and reaches the engine's apply_batch."""
        tool = next(t for t in McpServer(transport=None, api_key="k", governor=None,
                                         ledger=None, router=None, engine=None)._tools()
                    if t["name"] == "apply_edit")
        file_schema = tool["inputSchema"]["properties"]["file"]
        self.assertEqual(file_schema["anyOf"][0]["type"], "string")
        self.assertEqual(file_schema["anyOf"][1]["type"], "array")
        self.assertIn("max_tokens", tool["inputSchema"]["properties"])
        self.assertIn("allow_escalation", tool["inputSchema"]["properties"])

    def test_apply_edit_never_mutates_the_router(self):
        """The server keeps one router for its whole lifetime; per-request
        capability ordering must never write routing state into it."""
        transport, server = make_server(posts=[comp(consent_json("accept")),
                                               comp("HARNESS_READY: confident\n" + CHANGED)])
        target = os.path.join(_TMP.name, "mcp_target.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        before_pool = list(server.router.apply_pool)
        before_model = server.router.apply_model
        # Unknown trust refuses gateless writes: exercise routing through a
        # preview (no write, no gate run), which still builds the request
        # pool exactly like a mutating call.
        resp = server._invoke("apply_edit", {"file": [target], "instruction": "change",
                                             "require_consent": False,
                                             "verify_only": True})
        self.assertEqual(resp["status"], "preview")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)
        self.assertEqual(server.router.apply_pool, before_pool)
        self.assertEqual(server.router.apply_model, before_model)

    def test_verify_only_does_not_require_remote_write_authorization(self):
        transport, server = make_server(posts=[comp(CHANGED)])
        target = os.path.join(_TMP.name, "preview_target.py")
        with open(target, "w", encoding="utf-8") as stream:
            stream.write(ORIGINAL)
        result = server._invoke("apply_edit", {
            "file": [target], "instruction": "change", "verify_only": True,
            "require_consent": False, "renew_consent": False,
        })
        self.assertEqual(result["status"], "preview")
        self.assertEqual(transport.chat_posts()[0][2]["model"], APPLY)

    def test_symlink_target_is_rejected_before_dispatch(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        target = os.path.join(_TMP.name, "symlink_target.py")
        real = os.path.join(_TMP.name, "real_target.py")
        with open(real, "w", encoding="utf-8") as stream:
            stream.write(ORIGINAL)
        try:
            os.symlink(real, target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        transport, server = make_server()
        with self.assertRaisesRegex(Exception, "symlink targets"):
            server._invoke("apply_edit", {
                "file": [target], "instruction": "change",
            })
        self.assertEqual(transport.chat_posts(), [])

    def test_mcp_rejects_truthy_non_boolean_authorization(self):
        transport, server = make_server()
        target = os.path.join(_TMP.name, "auth_target.py")
        with open(target, "w", encoding="utf-8") as stream:
            stream.write(ORIGINAL)
        with self.assertRaisesRegex(Exception, "allow_write must be a boolean"):
            server._invoke("apply_edit", {
                "file": [target], "instruction": "change", "allow_write": "false",
            })
        self.assertEqual(transport.chat_posts(), [])

    def test_mcp_validates_required_prompt(self):
        feed = ('{"jsonrpc":"2.0","id":23,"method":"tools/call",'
                '"params":{"name":"panel_verify","arguments":{}}}\n')
        _, lines = run(feed)
        result = lines[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["errorKind"], "harness_error")
        self.assertIn("prompt is required", result["content"][0]["text"])

    def test_mcp_validates_ledger_limit(self):
        feed = ('{"jsonrpc":"2.0","id":24,"method":"tools/call",'
                '"params":{"name":"ledger_status","arguments":{"limit":0}}}\n')
        _, lines = run(feed)
        result = lines[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("limit must be between 1 and 1000", result["content"][0]["text"])

    def test_observe_lane_answers_during_long_apply(self):
        """A long apply_edit must not head-of-line-block read-only tools:
        ledger_status answers on its own lane while the mutation lane is
        still busy."""
        transport, server = make_server()
        target = os.path.join(_TMP.name, "lane_target.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        started = threading.Event()
        release = threading.Event()

        def blocking_batch(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=15), "test stalled")
            return {"status": "ok", "task_id": "t", "changed": False,
                    "rounds": [], "cost": 0.0}

        server.engine.apply_batch = blocking_batch
        quoted = target.replace("\\", "\\\\")
        feed = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"apply_edit","arguments":{"file": ["' + quoted + '"],'
            '"instruction": "change", "verify_only": true}}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
            '"params":{"name":"ledger_status","arguments":{}}}\n'
        )
        out = io.StringIO()
        server.stdout = out
        server.stdin = io.StringIO(feed)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            self.assertTrue(started.wait(timeout=15), "apply never started")
            answered = False
            for _ in range(100):
                lines = [json.loads(line) for line in
                         out.getvalue().splitlines() if line.strip()]
                if any(line.get("id") == 2 for line in lines):
                    answered = True
                    break
                threading.Event().wait(0.05)
            self.assertTrue(answered,
                            "ledger_status must answer while apply is blocked")
        finally:
            release.set()
            worker.join(timeout=15)
        self.assertFalse(worker.is_alive())

    def test_tool_deadline_fires_through_cancel_check(self):
        """An expired per-tool deadline trips the same cooperative path as
        cancellation, so an uncancelled-but-overdue run still stops."""
        _, server = make_server()
        server.tool_timeout = 0.01
        request_id = "deadline-probe"
        server._note_start(request_id)
        threading.Event().wait(0.05)
        self.assertTrue(server._cancel_check(request_id, with_deadline=True))
        fresh = "fresh-probe"
        server._note_start(fresh)
        self.assertFalse(server._cancel_check(fresh, with_deadline=True))

    def test_tool_timeout_config_range(self):
        """HARNESS_MCP_TOOL_TIMEOUT outside 60..7200 refuses; default 1800."""
        import os as _os
        import tempfile as _tempfile
        from unittest import mock as _mock
        import harness.config as _cfg
        from harness.errors import HarnessError
        with _tempfile.TemporaryDirectory() as cfgdir, \
             _mock.patch.object(_cfg, "CONFIG_DIR", cfgdir):
            with _mock.patch.dict(_os.environ, {"HARNESS_MCP_TOOL_TIMEOUT": "30"}):
                with self.assertRaises(HarnessError):
                    _cfg.load_settings()
            with _mock.patch.dict(_os.environ, {"HARNESS_MCP_TOOL_TIMEOUT": "60"}):
                self.assertEqual(_cfg.load_settings().mcp_tool_timeout, 60)
            env = {k: v for k, v in _os.environ.items()
                   if k != "HARNESS_MCP_TOOL_TIMEOUT"}
            with _mock.patch.dict(_os.environ, env, clear=True):
                self.assertEqual(_cfg.load_settings().mcp_tool_timeout, 1800)

        feed = (
            'not json at all\n'
            '{"jsonrpc":"2.0","id":22,"method":"bogus_method"}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
