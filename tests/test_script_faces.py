"""SCR-2: the chat box and MCP faces over the one governed runner.

Hermetic. Every executed script is a local program in a temp workspace; the
Jev seam is never reached (``score=False``), so no provider call is possible.
"""
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import harness.server as server_mod
from harness import script
from harness.errors import HarnessError
from harness.ledger import AutonomyLedger
from harness.mcp import McpServer
from harness.mcp_lanes import lane_for
from harness.mcp_schemas import TOOL_SCHEMAS


def _workspace(test):
    td = tempfile.TemporaryDirectory()
    test.addCleanup(td.cleanup)
    return Path(td.name)


def _server(test, root, *, enabled=True):
    ledger_path = _workspace(test) / "ledger.jsonl"
    return McpServer(
        transport=None, api_key=None, governor=None,
        ledger=AutonomyLedger(str(ledger_path)), router=None, engine=None,
        allowed_roots=[str(root)],
        settings=SimpleNamespace(script_enabled=enabled))


def _serve(server, feed):
    """Drive frames through the real stdio loop, like a client would."""
    server.stdin = io.StringIO(feed)
    out = io.StringIO()
    server.stdout = out
    server.serve_forever()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _prompt(goal="print one", source="print(1)"):
    return f"/script {goal}\n```python\n{source}\n```"


class ChatTriggerTests(unittest.TestCase):
    def setUp(self):
        self.root = _workspace(self)

    def test_ordinary_chat_is_never_scanned(self):
        self.assertIsNone(script.parse_script_request("write me a script"))
        self.assertIsNone(script.parse_script_request("/scripts are useful"))

    def test_the_command_reads_goal_and_source(self):
        parsed = script.parse_script_request(_prompt("count", "print(2)"))
        self.assertEqual(parsed["goal"], "count")
        self.assertEqual(parsed["source"], "print(2)")

    def test_prose_after_the_block_is_allowed(self):
        parsed = script.parse_script_request(
            _prompt() + "\nplease keep it short")
        self.assertEqual(parsed["source"], "print(1)")

    def test_no_block_or_two_blocks_are_refused(self):
        for prompt in ("/script do something", _prompt() + "\n```python\nprint(2)\n```"):
            with self.assertRaises(script.ScriptRefused) as ctx:
                script.parse_script_request(prompt)
            self.assertEqual(ctx.exception.reason, "empty")

    def test_proposal_compiles_but_writes_nothing(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}):
            proposal = script.propose_script(_prompt(), root=self.root)
        self.assertEqual(proposal["status"], "awaiting_consent")
        self.assertEqual(proposal["kind"], "script")
        info = proposal["script"]
        self.assertEqual(info["sha256"], hashlib.sha256(b"print(1)").hexdigest())
        self.assertEqual(info["chars"], len("print(1)"))
        self.assertEqual(info["root"], str(self.root.resolve()))
        self.assertFalse((self.root / ".harness").exists())

    def test_a_disabled_proposal_is_refused(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "0"}):
            with self.assertRaises(script.ScriptRefused) as ctx:
                script.propose_script(_prompt(), root=self.root)
        self.assertEqual(ctx.exception.reason, "disabled")

    def test_a_missing_workspace_is_an_actionable_refusal(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}):
            with self.assertRaises(script.ScriptRefused) as ctx:
                script.propose_script(_prompt(), root=None)
        self.assertEqual(ctx.exception.reason, "root")
        self.assertIn("select a workspace folder", str(ctx.exception))

    def test_a_syntax_error_is_caught_at_proposal_time(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}):
            with self.assertRaises(script.ScriptRefused) as ctx:
                script.propose_script(_prompt(source="def broken("), root=self.root)
        self.assertEqual(ctx.exception.reason, "syntax")
        self.assertIn("line 1", str(ctx.exception))

    def test_a_non_script_prompt_proposes_nothing(self):
        self.assertIsNone(script.propose_script("hello", root=self.root))

    def test_evidence_row_uses_the_shared_kind_vocabulary(self):
        row = script.script_evidence({
            "status": "ok", "exit_code": 0, "duration_s": 0.5,
            "script_sha256": "a" * 64, "script_path": "/tmp/x.py",
            "stdout": "hello"})
        self.assertEqual(row["kind"], "script")
        self.assertTrue(row["ok"])
        self.assertEqual(row["chars"], 5)
        self.assertEqual(row["path"], "/tmp/x.py")


class RefusalTypingTests(unittest.TestCase):
    def setUp(self):
        self.root = _workspace(self)
        self.source = "print(1)"
        self.sha = hashlib.sha256(self.source.encode()).hexdigest()
        self.policy = script.ScriptPolicy(enabled=True)

    def _run(self, **over):
        kwargs = {"root": self.root, "allow_exec": True,
                  "consent_sha": self.sha, "policy": self.policy}
        kwargs.update(over)
        return script.run_script(self.source, **kwargs)

    def test_no_consent_is_typed(self):
        with self.assertRaises(script.ScriptRefused) as ctx:
            self._run(allow_exec=False)
        self.assertEqual(ctx.exception.reason, "no_consent")
        self.assertEqual(ctx.exception.kind, "script_refused")
        self.assertTrue(str(ctx.exception).startswith("script refused:"))

    def test_a_mismatched_sha_is_typed(self):
        with self.assertRaises(script.ScriptRefused) as ctx:
            self._run(consent_sha="0" * 64)
        self.assertEqual(ctx.exception.reason, "sha_mismatch")

    def test_a_malformed_sha_is_typed(self):
        with self.assertRaises(script.ScriptRefused) as ctx:
            self._run(consent_sha="not-a-digest")
        self.assertEqual(ctx.exception.reason, "sha_invalid")

    def test_a_cancelled_run_returns_a_receipt_not_an_exception(self):
        flag = {"stop": False}
        timer = threading.Timer(0.4, lambda: flag.update(stop=True))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        receipt = script.run_script(
            "import time; time.sleep(20)", root=self.root, allow_exec=True,
            policy=self.policy, timeout_s=30, cancel_check=lambda: flag["stop"])
        self.assertEqual(receipt["status"], "cancelled")
        self.assertTrue(receipt["cancelled"])
        self.assertEqual(receipt["exit_code"], 125)
        self.assertLess(time.monotonic() - started, 10)


class McpScriptToolTests(unittest.TestCase):
    def setUp(self):
        self.root = _workspace(self)
        self.source = "print('mcp ok')"
        self.sha = hashlib.sha256(self.source.encode()).hexdigest()
        self.server = _server(self, self.root)

    def _call(self, **over):
        args = {"source": self.source, "root": str(self.root),
                "allow_exec": True, "consent_sha": self.sha, "score": False}
        args.update(over)
        return self.server._invoke("script_run", args)

    def test_the_tool_is_listed_after_continue_work(self):
        names = [tool["name"] for tool in TOOL_SCHEMAS]
        self.assertIn("script_run", names)
        self.assertEqual(names[names.index("script_run") - 1], "continue_work")
        tool = TOOL_SCHEMAS[names.index("script_run")]
        self.assertEqual(sorted(tool["inputSchema"]["required"]),
                         ["allow_exec", "consent_sha", "root", "source"])
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["destructiveHint"])
        self.assertEqual(lane_for("script_run"), "mutation")

    def test_a_listed_call_runs_over_the_protocol_loop(self):
        frames = _serve(self.server, "".join([
            '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "script_run", "arguments": {
                            "source": self.source, "root": str(self.root),
                            "allow_exec": True, "consent_sha": self.sha,
                            "score": False}}}) + "\n",
        ]))
        self.assertIn("script_run", [t["name"] for t in frames[0]["result"]["tools"]])
        result = frames[1]["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["stdout"].strip(), "mcp ok")

    def test_disabled_without_consent_and_without_a_sha_are_typed_refusals(self):
        cases = [
            ("disabled", {"source": self.source}, "disabled"),
            ("no_exec", {"allow_exec": False}, "no_consent"),
            ("no_sha", {"consent_sha": None}, "sha_missing"),
        ]
        for label, over, reason in cases:
            server = _server(self, self.root, enabled=label != "disabled")
            server.settings = SimpleNamespace(script_enabled=label != "disabled")
            args = {"source": self.source, "root": str(self.root),
                    "allow_exec": True, "consent_sha": self.sha, "score": False}
            args.update(over)
            with self.subTest(label):
                with self.assertRaises(script.ScriptRefused) as ctx:
                    server._invoke("script_run", args)
                self.assertEqual(ctx.exception.reason, reason)

    def test_a_sha_mismatch_is_hostile_trust_evidence_and_writes_nothing(self):
        with self.assertRaises(script.ScriptRefused) as ctx:
            self._call(consent_sha="0" * 64)
        self.assertEqual(ctx.exception.reason, "sha_mismatch")
        entries = [e for e in self.server.ledger.entries()
                   if e.get("event") == "trust_gate"]
        self.assertTrue(any(e.get("severity") == "hostile" for e in entries))
        self.assertFalse((self.root / ".harness").exists())

    def test_a_root_outside_the_allowed_roots_is_refused(self):
        outside = _workspace(self)
        with self.assertRaises(script.ScriptRefused) as ctx:
            self._call(root=str(outside))
        self.assertEqual(ctx.exception.reason, "root")

    def test_an_empty_source_is_a_validation_error(self):
        with self.assertRaises(HarnessError) as ctx:
            self._call(source="   ")
        self.assertIn("source", str(ctx.exception))


class ServerScriptTests(unittest.TestCase):
    def setUp(self):
        self.root = _workspace(self)
        self.source = "print('server ok')"
        self.sha = hashlib.sha256(self.source.encode()).hexdigest()
        self.settings = mock.patch.object(
            server_mod, "load_settings", return_value=SimpleNamespace())
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def _args(self, **over):
        args = {"root_dir": str(self.root), "source": self.source,
                "goal": "print something", "consent_sha": self.sha,
                "allow_exec": True, "score": False}
        args.update(over)
        return args

    def test_dispatch_normalizes_a_valid_request(self):
        args = server_mod.validate_dispatch("script", self._args())
        self.assertEqual(args["consent_sha"], self.sha)
        self.assertTrue(args["allow_exec"])
        self.assertEqual(args["goal"], "print something")
        self.assertIsNone(args["timeout_s"])

    def test_dispatch_refuses_missing_root_sha_and_consent(self):
        bad = [
            {"source": self.source, "consent_sha": self.sha, "allow_exec": True},
            self._args(consent_sha="xyz"),
            self._args(consent_sha=None),
            self._args(allow_exec=False),
            self._args(allow_exec="yes"),
            self._args(source="x" * (script.MAX_SOURCE_CHARS + 1)),
        ]
        for args in bad:
            with self.subTest(args=list(args)):
                with self.assertRaises(HarnessError):
                    server_mod.validate_dispatch("script", args)

    def test_run_script_task_returns_the_receipt_envelope(self):
        args = server_mod.validate_dispatch("script", self._args())
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}):
            result = server_mod.run_script_task("ui/test", args, lambda: False)
        self.assertEqual(result["kind"], "script")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["script"]["stdout"].strip(), "server ok")
        self.assertEqual(result["script_evidence"][0]["kind"], "script")
        self.assertEqual(result["cost"], 0.0)
        self.assertIn("Script ok", result["response"])

    def test_a_wrong_sha_propagates_as_script_refused(self):
        args = server_mod.validate_dispatch(
            "script", self._args(consent_sha="0" * 64))
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}):
            with self.assertRaises(script.ScriptRefused) as ctx:
                server_mod.run_script_task("ui/test", args, lambda: False)
        self.assertEqual(ctx.exception.kind, "script_refused")

    def test_a_script_prompt_returns_a_card_without_calling_a_model(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}), \
                mock.patch.object(server_mod, "AutonomousAgent") as agent_cls:
            result = server_mod.run_chat_task(
                "ui/test", {"prompt": _prompt("serve", self.source),
                            "root_dir": str(self.root)}, lambda: False)
        agent_cls.assert_not_called()
        self.assertEqual(result["status"], "awaiting_consent")
        self.assertEqual(result["script"]["sha256"], self.sha)
        self.assertFalse((self.root / ".harness").exists())

    def test_a_script_prompt_without_a_workspace_is_refused(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}), \
                mock.patch.object(server_mod, "AutonomousAgent") as agent_cls:
            with self.assertRaises(script.ScriptRefused) as ctx:
                server_mod.run_chat_task(
                    "ui/test", {"prompt": _prompt()}, lambda: False)
        agent_cls.assert_not_called()
        self.assertEqual(ctx.exception.reason, "root")

    def test_a_disabled_script_prompt_is_refused(self):
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "0"}), \
                mock.patch.object(server_mod, "AutonomousAgent") as agent_cls:
            with self.assertRaises(script.ScriptRefused) as ctx:
                server_mod.run_chat_task(
                    "ui/test", {"prompt": _prompt(), "root_dir": str(self.root)},
                    lambda: False)
        agent_cls.assert_not_called()
        self.assertEqual(ctx.exception.reason, "disabled")


if __name__ == "__main__":
    unittest.main()
