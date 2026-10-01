"""Hermetic unit tests for Phase 4: harness brief CLI & MCP tool (generate_brief).

Validates that `harness brief` supports positional or --goal flags, multiple files / CSV,
budget limits, freshness reports, markdown rendering, and that `generate_brief` in MCP
exposes the same capabilities on the observe lane.
"""
import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace

from harness.cli import _cmd_brief
from harness.cli_parser import build_parser
from harness.errors import HarnessError
from harness.mcp_lanes import lane_for
from harness.mcp_schemas import TOOL_SCHEMAS


class TestBriefCli(unittest.TestCase):
    """Test CLI parsing and execution of `harness brief`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file1 = os.path.join(self.tmp.name, "mod1.py")
        with open(self.file1, "w", encoding="utf-8") as f:
            f.write("def foo(): return 42\n")
        self.file2 = os.path.join(self.tmp.name, "mod2.py")
        with open(self.file2, "w", encoding="utf-8") as f:
            f.write("def bar(): return 99\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parser_accepts_goal_and_files(self):
        parser = build_parser()
        args = parser.parse_args(["brief", "Fix bug in calculation", "--files", f"{self.file1},{self.file2}"])
        self.assertEqual(args.goal_pos, "Fix bug in calculation")
        self.assertEqual(args.files_csv, f"{self.file1},{self.file2}")

    def test_parser_accepts_flag_goal(self):
        parser = build_parser()
        args = parser.parse_args(["brief", "--goal", "Refactor module", "--file", self.file1, "--file", self.file2])
        self.assertEqual(args.goal_opt, "Refactor module")
        self.assertEqual(args.files, [self.file1, self.file2])

    def test_cmd_brief_with_flag_goal_and_files_csv(self):
        opts = SimpleNamespace(
            goal_opt="Refactor module",
            goal_pos=None,
            files=[],
            files_csv=f"{self.file1},{self.file2}",
            budget=20000,
            freshness=True,
            render=False,
            out=None,
            validate=True,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _cmd_brief(opts)

        import json
        out = json.loads(buf.getvalue())
        self.assertEqual(out["status"], "ok")
        self.assertIn("brief", out)
        self.assertEqual(out["brief"]["goal"], "Refactor module")
        self.assertIn("freshness_report", out["brief"])
        self.assertTrue(out["brief"]["freshness_report"]["fresh"])

    def test_cmd_brief_render_markdown(self):
        opts = SimpleNamespace(
            goal_opt="render goal",
            goal_pos=None,
            files=[self.file1],
            files_csv=None,
            budget=10000,
            freshness=False,
            render=True,
            out=None,
            validate=False,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _cmd_brief(opts)

        rendered = buf.getvalue()
        self.assertIn("# Brief — render goal", rendered)
        self.assertIn("def foo():", rendered)

    def test_cmd_brief_missing_goal_raises(self):
        opts = SimpleNamespace(
            goal_opt=None,
            goal_pos=None,
            goal=None,
            files=[],
            files_csv=None,
            budget=48000,
            freshness=False,
            render=False,
            out=None,
            validate=False,
        )
        with self.assertRaises(HarnessError) as ctx:
            _cmd_brief(opts)
        self.assertIn("requires a goal", str(ctx.exception))

    def test_cmd_brief_render_to_out_file(self):
        out_path = os.path.join(self.tmp.name, "rendered.md")
        opts = SimpleNamespace(
            goal_opt="render out goal",
            goal_pos=None,
            files=[self.file1],
            files_csv=None,
            budget=10000,
            freshness=False,
            render=True,
            out=out_path,
            validate=False,
        )
        _cmd_brief(opts)
        self.assertTrue(os.path.exists(out_path))
        with open(out_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("# Brief — render out goal", content)


class TestBriefMcp(unittest.TestCase):
    """Test MCP tool exposure and invocation of generate_brief."""

    def test_schema_and_lane_registration(self):
        schemas = {s["name"]: s for s in TOOL_SCHEMAS}
        self.assertIn("generate_brief", schemas)
        schema = schemas["generate_brief"]
        self.assertEqual(schema["inputSchema"]["required"], ["goal"])
        self.assertEqual(lane_for("generate_brief"), "observe")

    def test_mcp_invoke_generate_brief(self):
        from tests.test_mcp import make_server

        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "main.py")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("print('hello world')\n")

            _, server = make_server()
            res = server._invoke("generate_brief", {
                "goal": "Explain main",
                "files": [fpath],
                "freshness": True,
                "render": True,
            })

            self.assertEqual(res["status"], "ok")
            self.assertIn("brief", res)
            self.assertIn("freshness_report", res)
            self.assertTrue(res["freshness_report"]["fresh"])
            self.assertIn("rendered", res)
            self.assertIn("# Brief — Explain main", res["rendered"])

    def test_mcp_invoke_generate_brief_string_files(self):
        from tests.test_mcp import make_server
        with tempfile.TemporaryDirectory() as d:
            fpath = os.path.join(d, "single.py")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("val = 123\n")
            _, server = make_server()
            res = server._invoke("generate_brief", {
                "goal": "Test string file",
                "files": fpath,
            })
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["brief"]["scope"]["included"], [fpath])

    def test_mcp_invoke_generate_brief_invalid_files_type(self):
        from tests.test_mcp import make_server
        _, server = make_server()
        with self.assertRaises(HarnessError) as ctx:
            server._invoke("generate_brief", {
                "goal": "Test invalid files",
                "files": 12345,
            })
        self.assertIn("files must be a list of strings", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
