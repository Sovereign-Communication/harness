import atexit
import io
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from harness.apply import ApplyEngine
from harness.spend import SpendGovernor
from harness.ledger import AutonomyLedger
from harness.mcp import McpServer
from harness.mcp_lanes import LANES, lane_for
from harness.mcp_schemas import TOOL_SCHEMAS
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


def _serve(server, feed):
    """One owner for driving frames through the real stdio loop: ``feed``
    is stdin text (str or sequence of frames) or a reader object with
    ``readline``; returns the parsed reply frames."""
    if not hasattr(feed, "readline"):
        feed = io.StringIO(feed if isinstance(feed, str) else "".join(feed))
    out = io.StringIO()
    server.stdout = out
    server.stdin = feed
    server.serve_forever()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def run(feed, posts=None):
    transport, server = make_server(posts)
    return transport, _serve(server, feed)


# Reusable frames for the cancellation e2e scenarios. Post-trip liveness is
# proven through the REAL frame loop: these two requests ride the same stdin
# after the trip and their replies must come back.
CANCELLATION_41 = ('{"jsonrpc":"2.0","method":"notifications/cancelled",'
                   '"params":{"requestId":41}}\n')
PANEL_REQUEST_41 = ('{"jsonrpc":"2.0","id":41,"method":"tools/call",'
                    '"params":{"name":"panel_verify","arguments":'
                    '{"prompt":"sound?"}}}\n')
PANEL_REQUEST_42 = ('{"jsonrpc":"2.0","id":42,"method":"tools/call",'
                    '"params":{"name":"panel_verify","arguments":'
                    '{"prompt":"sound?"}}}\n')


def apply_request_43(target):
    quoted = target.replace("\\", "\\\\")
    return ('{"jsonrpc":"2.0","id":43,"method":"tools/call",'
            '"params":{"name":"apply_edit","arguments":'
            '{"file": ["' + quoted + '"],'
            '"instruction": "change it to return a + b + 0",'
            '"require_consent": false, "renew_consent": false}}}\n')


LIVENESS_FRAMES = (
    '{"jsonrpc":"2.0","id":99,"method":"ping"}\n',
    '{"jsonrpc":"2.0","id":100,"method":"tools/call",'
    '"params":{"name":"ledger_status","arguments":{}}}\n',
)


def _write_target(name):
    """A fresh apply target holding the canonical ORIGINAL bytes."""
    path = os.path.join(_TMP.name, name)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(ORIGINAL)
    return path


def gated_cancellation_server(*, gate_post, posts, tool_timeout=None,
                             apply_pool=None):
    """One fixture for the three cancellation e2e scenarios (explicit
    notification, spendy-lane deadline, mutation-lane deadline).

    GatedTransport holds post number ``gate_post`` (1-based) behind an
    event so each scenario can make its trip deterministic: release only
    after the cancel/deadline condition provably holds, never on a
    sleep. ``tool_timeout`` stamps the per-tool deadline; ``apply_pool``
    overrides the router's apply pool (the mutation lane needs two models
    so rotation reaches the next attempt poll).

    Returns a handle with ``wait_gated`` (block until the gated POST is
    in flight), ``deadline_expired`` (bounded-poll the submit-time
    deadline, then release -- fail, never hang) and ``drive`` (feed the
    request, run ``gate_wait`` after it, then any extra frames, through
    the real serve_forever/stdin path; returns parsed reply frames).
    The gate is always released at EOF so a failing test errors instead
    of hanging.
    """
    class GatedTransport(FakeTransport):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.gated = threading.Event()
            self.release = threading.Event()
            self._count = 0

        def post(self, *args, **kwargs):
            self._count += 1
            if self._count == gate_post:
                self.gated.set()
                self.release.wait(10)
            return super().post(*args, **kwargs)

    transport = GatedTransport(models=[m(JUDGE), m(P1), m(P2), m(APPLY)],
                               posts=list(posts))
    governor = SpendGovernor(transport, "sk-test")
    ledger = AutonomyLedger(os.path.join(_TMP.name,
                                         f"ledger-{os.urandom(4).hex()}.jsonl"))
    router = Router([P1, P2], JUDGE, APPLY)
    if apply_pool is not None:
        router.apply_pool = list(apply_pool)
    engine = ApplyEngine(transport, "sk-test", governor, ledger, router,
                         default_require_consent=True)
    server = McpServer(transport=transport, api_key="sk-test",
                       governor=governor, ledger=ledger, router=router,
                       engine=engine, allow_write=True,
                       allowed_roots=[_TMP.name])
    if tool_timeout is not None:
        server.tool_timeout = tool_timeout

    def wait_gated():
        # 60s: the bound is CI-runner tolerance for the worker's lazy
        # apply-chain construction, not a behavior pin (the main thread
        # must release the gate regardless, so a longer wait cannot mask
        # a real stall -- it only stops punishing slow interpreters).
        if not transport.gated.wait(60):
            transport.release.set()
            raise AssertionError(
                f"gated POST never started (posts seen: {transport._count})")

    def deadline_expired(request_id):
        wait_gated()
        for _ in range(2500):
            if server._is_expired(request_id):
                break
            time.sleep(0.002)
        else:
            transport.release.set()
            raise AssertionError("tool deadline never expired")
        transport.release.set()

    def drive(request, gate_wait=None, extra_frames=(), prove_liveness=True):
        # Liveness is default-on for every scenario: the ping +
        # ledger_status frames ride the same stdin after the trip and their
        # replies must come back. prove_liveness=False exists for scenarios
        # with no post-trip answer to prove; it feeds only the scenario's
        # own frames and asserts nothing live (the knob test pins these
        # mechanics).
        frames = [request] + list(extra_frames)
        if prove_liveness:
            frames += LIVENESS_FRAMES

        class Feed:
            idx = 0

            def readline(self):
                # The gate_wait runs on the call AFTER the request frame was
                # returned -- i.e. once serve_forever has submitted it --
                # never before, or the request would never reach a worker.
                if gate_wait is not None and Feed.idx == 1:
                    gate_wait()
                if Feed.idx < len(frames):
                    line = frames[Feed.idx]
                    Feed.idx += 1
                    return line
                transport.release.set()  # EOF: never leave the gate shut
                return ""

        lines = _serve(server, Feed())
        if prove_liveness:
            replies = {line.get("id"): line for line in lines}
            if replies.get(99, {}).get("result") != {}:
                raise AssertionError(
                    "frame loop wedged: ping after the trip got no reply")
            sc = replies.get(100, {}).get("result", {}).get("structuredContent", {})
            if not sc.get("verified", {}).get("ok"):
                raise AssertionError(
                    "frame loop wedged: ledger_status after the trip got no reply")
        return lines

    return SimpleNamespace(transport=transport, governor=governor,
                           ledger=ledger, server=server,
                           wait_gated=wait_gated,
                           deadline_expired=deadline_expired, drive=drive)


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
        _serve(server, feed)
        self.assertEqual(server.caller, "mcp:probe-harness/1.0")
        self.assertEqual(server.ledger.caller, "mcp:probe-harness/1.0")

    def test_initialize_without_client_info_stays_generic(self):
        feed = ('{"jsonrpc":"2.0","id":1,"method":"initialize",'
                '"params":{"protocolVersion":"2025-06-18"}}\n')
        _, server = make_server()
        _serve(server, feed)
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
            response = _serve(server, feed)[0]
        finally:
            mcp_module._service_run_verify = original
        # The tool result shape is unchanged: the service return becomes the
        # structuredContent verbatim (no meta/cost attachment in MCP).
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

    def assert_cancelled_trip(self, lines, request_id, h, posts):
        """The trip contract shared by every cancelled/deadline scenario.
        Lanes run concurrently, so frames correlate by id, never by
        position (JSON-RPC permits any response order). Asserts the
        established -32800 shape and honest spend (in-flight POSTs are the
        documented residual, never hidden); liveness after the trip is
        proven by the shared drive, on by default."""
        by_id = {line.get("id"): line for line in lines}
        response = by_id[request_id]
        self.assertEqual(response["error"]["code"], -32800)
        self.assertEqual(response["error"]["message"], "Request cancelled")
        self.assertEqual(len(h.transport.chat_posts()), posts)
        self.assertGreater(h.governor.spent, 0.0)
        self.assertEqual(h.server._inflight, set())

    def test_panel_verify_cancelled_run_maps_to_protocol_error(self):
        """Explicit notifications/cancelled, end to end: the first panel
        seat bills normally, the cancel notification arrives while the
        second seat's call is in flight, the cooperative cancel_check
        trips, the service builds the honest cancelled envelope (in-flight
        billed spend included), and MCP answers its established -32800
        error without hiding the spend."""
        h = gated_cancellation_server(gate_post=2, posts=[comp("yes"), comp("yes")])
        lines = h.drive(PANEL_REQUEST_41, gate_wait=h.wait_gated,
                        extra_frames=(CANCELLATION_41,))
        # The first seat billed and completed; the second seat's POST was
        # already in flight (in-flight POSTs run to their own timeout -- the
        # documented residual) and its real spend is preserved too.
        self.assert_cancelled_trip(lines, 41, h, posts=2)

    def test_tool_deadline_stops_delegated_run_at_protocol_error(self):
        """Tool deadline on the spendy lane, end to end: no
        notifications/cancelled is ever sent -- the only trip mechanism is
        the per-tool deadline stamped at submit time, polled through the
        service-delegated cancel_check. The first seat completes and bills;
        the second seat's POST (held by the gate) runs to completion after
        release -- the documented in-flight residual -- and its post-POST
        poll trips the expired deadline, so the service builds the honest
        cancelled envelope and MCP answers -32800 without hiding spend."""
        h = gated_cancellation_server(gate_post=2, posts=[comp("yes"), comp("yes")],
                                      tool_timeout=1.5)  # seat 1 is far faster
        lines = h.drive(PANEL_REQUEST_42,
                        gate_wait=lambda: h.deadline_expired(42))
        # Seat 1 billed; seat 2's POST was in flight past its poll point and
        # completed (documented residual) -- spend preserved, never hidden.
        self.assert_cancelled_trip(lines, 42, h, posts=2)

    def test_tool_deadline_stops_mutation_lane_and_frame_loop_stays_live(self):
        """Tool deadline on the mutation lane, end to end: an overdue
        deadline stops an apply_edit run at ApplyEngine's next attempt/round
        poll (in-flight POSTs have no poll -- the documented residual),
        answering the same -32800 frame as the spendy lane. Mutation
        honesty: the stop lands before any file mutation, so the target's
        bytes are untouched; the ledger carries only honest attempt
        evidence (the billable failed attempt, no readiness/escalation/
        deferral overclaims); dispatch_start is honest pre-poll evidence
        (the run began before any poll could trip); the completed attempt's
        real cost is preserved, not hidden. Liveness rides the real frame
        loop, proven by default in the shared drive."""
        h = gated_cancellation_server(
            gate_post=1, posts=[comp("")],  # empty content -> no-usable-output rotation
            tool_timeout=1.5, apply_pool=[APPLY, P1])
        # Two-model apply pool; the gated attempt returns empty content -- the
        # rotating, billable no-usable-output error path -- so rotation
        # must reach the NEXT attempt-top poll (a single-model pool would
        # terminate the run without another poll and never trip; a 200
        # with garbage content is NOT rotated -- "treating as confident"
        # hits the consent gate instead).
        target = _write_target("deadline_target.py")
        lines = h.drive(apply_request_43(target),
                        gate_wait=lambda: h.deadline_expired(43))
        # The one completed in-flight attempt's cost is real spend
        # (documented residual) -- preserved, never hidden.
        self.assert_cancelled_trip(lines, 43, h, posts=1)
        # Mutation honesty: the stop landed before any file mutation.
        with open(target, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), ORIGINAL)
        # The ledger carries only honest attempt evidence -- the billable
        # failed attempt -- and no post-content progress (readiness,
        # escalation, deferral) that would overclaim a cancelled run.
        model_results = [entry for entry in h.ledger.tail(100)
                         if entry.get("event") == "model_result"]
        self.assertTrue(model_results)
        self.assertTrue(all(entry.get("status") == "error"
                            for entry in model_results))
        events = {entry.get("event") for entry in h.ledger.tail(100)}
        self.assertNotIn("readiness", events)
        self.assertNotIn("escalate", events)
        self.assertNotIn("defer_midtask", events)

    def test_liveness_opt_out_appends_no_liveness_frames(self):
        """The prove_liveness knob's contract, driven: with the opt-out the
        drive feeds only the scenario's own frames -- no ping/ledger_status
        appended (their reply ids 99/100 are absent) -- and runs no liveness
        assertion (the drive returns without raising)."""
        h = gated_cancellation_server(gate_post=2, posts=[comp("yes"), comp("yes")])
        lines = h.drive(PANEL_REQUEST_41, gate_wait=h.wait_gated,
                        extra_frames=(CANCELLATION_41,), prove_liveness=False)
        self.assert_cancelled_trip(lines, 41, h, posts=2)
        reply_ids = {line.get("id") for line in lines}
        self.assertNotIn(99, reply_ids)
        self.assertNotIn(100, reply_ids)

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
        lines = _serve(server, feed)
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
        response = _serve(
            server,
            '{"jsonrpc":"2.0","id":22,"method":"tools/call",'
            '"params":{"name":"ledger_status","arguments":{}}}\n')[0]
        self.assertEqual(response["error"]["code"], -32603)

    def test_duplicate_request_id_is_rejected_while_in_flight(self):
        server = make_server()[1]
        server._inflight.add(7)
        response = _serve(
            server,
            '{"jsonrpc":"2.0","id":7,"method":"tools/call",'
            '"params":{"name":"ledger_status","arguments":{}}}\n')[0]
        self.assertEqual(response["error"]["code"], -32600)
        self.assertIn("duplicate request id", response["error"]["message"])

    def test_duplicate_id_stays_reserved_until_response_is_written(self):
        server = make_server()[1]
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
        responses = _serve(server, Feed())
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
        self.assertEqual(transport.chat_posts()[0][2]["model"].replace(":floor", ""), APPLY)

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


    def test_progress_token_streams_typed_events_as_progress_frames(self):
        """Opt-in MCP progress (2025-06-18 utilities/progress), end to end
        through the real frame loop: a client that includes
        params._meta.progressToken on a long panel_verify receives one
        notifications/progress frame per typed run event before the final
        result frame; each frame carries that token and a monotonically
        increasing progress; notifications carry no id. A client that
        sends no token gets zero progress frames (the historical
        behavior). The delegated verify runs to completion, so no spend
        assertion belongs here beyond the run's own result."""
        import harness.events as events_module
        baseline_sinks = events_module.sink_count()
        posts = [comp("yes"), comp("mostly"), comp("verdict: sound")]
        _, lines = run(
            ('{"jsonrpc":"2.0","id":50,"method":"tools/call",'
             '"params":{"name":"panel_verify","arguments":'
             '{"prompt":"sound?"},"_meta":{"progressToken":"tok-50"}}}\n'),
            posts=posts)
        progress = [ln for ln in lines
                    if ln.get("method") == "notifications/progress"]
        self.assertTrue(progress, "no progress frames for a tokened request")
        # Correlation + monotonicity + notification shape (no id).
        for i, ln in enumerate(progress, 1):
            self.assertEqual(ln["params"]["progressToken"], "tok-50")
            self.assertEqual(ln["params"]["progress"], i)
            self.assertNotIn("id", ln)
            self.assertTrue(ln["params"].get("message"))
        # Every frame predates the result: the final frame is id 50's reply.
        self.assertEqual(lines[-1]["id"], 50)
        self.assertFalse(lines[-1]["result"]["isError"])
        self.assertEqual(len(lines[-1]["result"]["structuredContent"]
                             ["panel_results"]), 2)
        # The sink lifecycle: progress sinks are removed with the request,
        # so this connection leaves the events bus with no net gain.
        self.assertEqual(events_module.sink_count(), baseline_sinks)

    def test_no_progress_token_means_no_progress_frames(self):
        """The historical default: without params._meta.progressToken, zero
        notifications/progress frames reach the stream, tokened or not."""
        feed = (
            '{"jsonrpc":"2.0","id":11,"method":"tools/call",'
            '"params":{"name":"panel_verify","arguments":'
            '{"prompt":"sound?"}}}\n'
            '{"jsonrpc":"2.0","id":12,"method":"tools/call",'
            '"params":{"name":"panel_verify","arguments":'
            '{"prompt":"sound?"},"_meta":{"progressToken":true}}}\n')
        _, lines = run(feed, posts=[comp("yes"), comp("mostly"),
                                    comp("verdict: sound"),
                                    comp("yes"), comp("mostly"),
                                    comp("verdict: sound")])
        self.assertFalse([ln for ln in lines
                          if ln.get("method") == "notifications/progress"])
        by_id = {ln.get("id"): ln for ln in lines}
        self.assertFalse(by_id[11]["result"]["isError"])
        # A non-string/non-int token (a client bug) is ignored -- _meta is
        # advisory, so a malformed telemetry preference never fails the
        # run: same result, still zero progress frames.
        self.assertFalse(by_id[12]["result"]["isError"])


class PlanWaistTests(unittest.TestCase):
    """plan_and_execute's waist params: confirm rides the canned frontier
    verdict; a refusal is terminal and never reaches execution."""

    def _call(self, arguments, posts=None):
        feed = ('{"jsonrpc":"2.0","id":77,"method":"tools/call","params":'
                '{"name":"plan_and_execute","arguments":' +
                json.dumps(arguments) + '}}\n')
        return run(feed, posts=posts)

    def test_confirm_preview_rides_approval(self):
        verdict = json.dumps({"verdict": "approve"})
        _, frames = self._call(
            {"goal": "Split the work", "confirm": True, "frontier_model": JUDGE,
             "decompose_llm": False},
            posts=[{"choices": [{"message": {"content": verdict},
                                 "finish_reason": "stop"}],
                    "usage": {"cost": 0.0001, "is_byok": False}}])
        result = frames[0]["result"]["content"][0]["text"]
        plan = json.loads(result) if result.startswith("{") else result
        self.assertEqual(plan["confirmation"]["verdict"], "approved")
        self.assertEqual(plan["status"], "planned")

    def test_refusal_is_terminal(self):
        verdict = json.dumps({"verdict": "refuse", "reason": "tier mismatch",
                              "evidence": "brief: task_2 is concurrency"})
        _, frames = self._call(
            {"goal": "Split the work", "confirm": True, "frontier_model": JUDGE,
             "decompose_llm": False},
            posts=[{"choices": [{"message": {"content": verdict},
                                 "finish_reason": "stop"}],
                    "usage": {"cost": 0.0001, "is_byok": False}}])
        result = frames[0]["result"]["content"][0]["text"]
        plan = json.loads(result) if result.startswith("{") else result
        self.assertEqual(plan["status"], "refused")
        self.assertEqual(plan["confirmation"]["evidence"], "brief: task_2 is concurrency")


class LaneSchedulingTests(unittest.TestCase):
    """The lane contract mcp.py's pool wiring depends on: LANES is the
    pool-creation order and lane_for routes every tool contract name.
    Pinned so a reorder or membership drift cannot pass silently --
    serve_forever builds one pool per LANES entry in tuple order."""

    def test_lanes_tuple_is_pool_creation_order(self):
        self.assertEqual(LANES, ("mutation", "spendy", "observe"))

    def test_lane_for_covers_every_tool_contract(self):
        # Deriving names from TOOL_SCHEMAS means a new or renamed tool
        # breaks this pin until its lane is stated explicitly.
        self.assertEqual(
            {name: lane_for(name) for name in
             (t["name"] for t in TOOL_SCHEMAS)},
            {"apply_edit": "mutation",
             "plan_and_execute": "mutation",
             "panel_verify": "spendy",
             "offer_work": "spendy",
             "log_judgment": "spendy",
             "ledger_status": "observe",
             "defer_work": "observe",
             "participation_report": "observe",
             "spend_status": "observe",
             "trust_status": "observe",
             "issue_sort": "observe"})

    def test_unknown_and_missing_names_ride_observe(self):
        self.assertEqual(lane_for("not_a_tool"), "observe")
        self.assertEqual(lane_for(None), "observe")


if __name__ == "__main__":
    unittest.main()
