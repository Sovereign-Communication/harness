"""#286: MCP tool calls log structured records to a file, never to stdio."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.mcp import McpServer

SECRET = "sk-or-v1-" + "a" * 24


def make_logged_server(path):
    return McpServer(
        transport=None, api_key="sk-test", governor=None, ledger=None,
        router=None, engine=None, mcp_log_path=str(path),
    )


def call(server, name, arguments, request_id=1):
    return server._call_tool({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def records(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class McpToolCallLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mcp.jsonl"
        self.server = make_logged_server(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_one_structured_record_per_call(self):
        response = call(self.server, "host_provision_probe", {})
        self.assertNotIn("error", response)
        logged = records(self.path)
        self.assertEqual(len(logged), 1)
        record = logged[0]
        self.assertEqual(record["tool"], "host_provision_probe")
        self.assertTrue(record["ok"])
        self.assertIsInstance(record["duration_ms"], int)
        self.assertEqual(record["args"], {})
        self.assertIn("at", record)

    def test_planted_secret_does_not_reach_disk(self):
        call(self.server, "not_a_tool", {"api_key": SECRET, "prompt": SECRET})
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertIn("[REDACTED]", text)
        record = records(self.path)[0]
        self.assertFalse(record["ok"])
        self.assertEqual(record["args"]["api_key"], "[REDACTED]")
        self.assertEqual(record["args"]["prompt"], "[REDACTED]")

    def test_logging_path_writes_nothing_to_stdout_or_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            call(self.server, "host_provision_probe", {})
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_environment_selects_the_log_file(self):
        path = Path(self.temp.name) / "from-env.jsonl"
        server = McpServer(
            transport=None, api_key="sk-test", governor=None, ledger=None,
            router=None, engine=None,
        )
        with mock.patch.dict(os.environ, {"HARNESS_MCP_LOG": str(path)}):
            server = McpServer(
                transport=None, api_key="sk-test", governor=None,
                ledger=None, router=None, engine=None,
            )
        call(server, "host_provision_probe", {})
        self.assertEqual(records(path)[0]["tool"], "host_provision_probe")

    def test_no_path_means_no_log_file(self):
        server = McpServer(
            transport=None, api_key="sk-test", governor=None, ledger=None,
            router=None, engine=None,
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_MCP_LOG", None)
            server = McpServer(
                transport=None, api_key="sk-test", governor=None,
                ledger=None, router=None, engine=None,
            )
        call(server, "host_provision_probe", {})
        self.assertEqual(list(Path(self.temp.name).glob("*.jsonl")), [])


class McpVersioningPolicyTests(unittest.TestCase):
    def test_module_docstring_states_additive_only_policy(self):
        from harness import mcp
        text = (mcp.__doc__ or "").lower()
        self.assertIn("additive-only", text)
        self.assertIn("new tool name", text)


if __name__ == "__main__":
    unittest.main()
