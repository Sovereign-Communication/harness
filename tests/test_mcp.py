import io
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.apply import ApplyEngine
from harness.core import SpendGovernor
from harness.ledger import AutonomyLedger
from harness.mcp import McpServer
from harness.router import Router
from tests._fake import FakeTransport, m, comp, consent

JUDGE = "inclusionai/ling-2.6-flash"
P1 = "meta-llama/llama-3.1-8b-instruct"
P2 = "ibm-granite/granite-4.1-8b"
APPLY = "deepseek/deepseek-chat"


_TMP = tempfile.TemporaryDirectory()


def make_server(posts=None):
    transport = FakeTransport(models=[m(JUDGE), m(P1), m(P2), m(APPLY)], posts=posts)
    governor = SpendGovernor(transport, "sk-test")
    ledger = AutonomyLedger(os.path.join(_TMP.name, f"ledger-{os.urandom(4).hex()}.jsonl"))
    router = Router([P1, P2], JUDGE, APPLY)
    engine = ApplyEngine(transport, "sk-test", governor, ledger, router,
                         default_require_consent=True)
    return transport, McpServer(transport=transport, api_key="sk-test", governor=governor,
                                ledger=ledger, router=router, engine=engine)


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

    def test_offer_and_defer_tools(self):
        feed = (
            '{"jsonrpc":"2.0","id":11,"method":"tools/call",'
            '"params":{"name":"offer_work","arguments":{"task":"Rewrite the parser"}}}\n'
            '{"jsonrpc":"2.0","id":12,"method":"tools/call",'
            '"params":{"name":"defer_work","arguments":{"task_id":"t9","reason":"changed my mind"}}}\n'
            '{"jsonrpc":"2.0","id":13,"method":"tools/call",'
            '"params":{"name":"participation_report","arguments":{}}}\n')
        _, lines = run(feed, posts=[consent("defer", "I would rather not")])
        offer = lines[0]["result"]
        self.assertEqual(offer["structuredContent"]["decision"], "defer")
        defer = lines[1]["result"]["structuredContent"]
        self.assertEqual(defer["status"], "deferred")
        report = lines[2]["result"]["structuredContent"]
        self.assertEqual(report["offers"], 1)
        self.assertEqual(report["defers"], 1)

    def test_unknown_tool_is_protocol_error(self):
        feed = ('{"jsonrpc":"2.0","id":20,"method":"tools/call",'
                '"params":{"name":"nope","arguments":{}}}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32602)

    def test_tool_execution_error_is_error_result(self):
        feed = ('{"jsonrpc":"2.0","id":21,"method":"tools/call",'
                '"params":{"name":"apply_edit","arguments":'
                '{"file":"C:/does/not/exist.py","instruction":"x"}}}\n')
        _, lines = run(feed)
        self.assertNotIn("error", lines[0])
        self.assertTrue(lines[0]["result"]["isError"])

    def test_apply_rejects_ungated_continuation_before_capability_lookup(self):
        """MCP must share the CLI/library authority boundary."""
        transport, server = make_server()
        state = {"file_path": "missing.py", "verify_only": False,
                 "verification_required": True}
        with mock.patch.object(server, "_capability_context",
                               side_effect=AssertionError("capability lookup must not run")):
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

    def test_unknown_method_and_parse_error(self):
        feed = (
            'not json at all\n'
            '{"jsonrpc":"2.0","id":22,"method":"bogus_method"}\n')
        _, lines = run(feed)
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
