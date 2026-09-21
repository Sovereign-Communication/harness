"""SITE-9: hermetic coverage for the `route` CLI face and MCP `route_query`.

The policy-level router (`JevPolicy.evaluate_model_route`) has its own
contract tests (test_route_pack.py). This module covers the two interface
envelopes the site/hosts consume — the same fail-open composition shape as
issue-sort: unkeyed settings answer via the code-owned heuristic with
``is_fallback=true``, and the 0-hallucination guarantees hold at the faces.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.config import load_settings
from harness.errors import HarnessError


def _route_pack():
    return {
        "id": "route-test-v1",
        "rungs": [
            {"rung_id": "scout", "tier": "T0", "model": "helper-lite",
             "cost_class": "free",
             "guidance": ["typo", "rename", "docstring", "format"]},
            {"rung_id": "worker", "tier": "T1", "model": "worker-1",
             "cost_class": "cheap", "guidance": ["algorithm", "parse"]},
        ],
    }


def _unkeyed_settings(tmp, ledger_name):
    settings = load_settings()
    settings.jev_api_key = None
    settings.ledger_path = os.path.join(tmp, ledger_name)
    return settings


class RouteCliEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pack_path = os.path.join(self.tmp.name, "pack.json")
        with open(self.pack_path, "w", encoding="utf-8") as handle:
            json.dump(_route_pack(), handle)
        self.out_path = os.path.join(self.tmp.name, "out.json")

    def test_cli_envelope_carries_route_and_structural(self):
        from harness import cli
        settings = _unkeyed_settings(self.tmp.name, "cli-ledger.jsonl")
        with mock.patch("harness.cli.load_settings", return_value=settings):
            cli.main([
                "route",
                "--goal", "fix a typo in the docstring",
                "--pack", self.pack_path,
                "--out", self.out_path,
            ])
        with open(self.out_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["status"], "ok")
        self.assertIn("route", data)
        self.assertIn("structural", data)
        self.assertEqual(data["rung_id"], "scout")
        self.assertEqual(data["tier"], "T0")
        self.assertEqual(data["model"], "helper-lite")
        self.assertEqual(data["pack_id"], "route-test-v1")
        self.assertTrue(data["is_fallback"],
                        "unkeyed CLI route must ride the deterministic heuristic")
        self.assertIn("site", data["structural"])

    def test_cli_route_requires_pack_and_valid_pack(self):
        from harness import cli
        settings = _unkeyed_settings(self.tmp.name, "cli-ledger2.jsonl")
        with mock.patch("harness.cli.load_settings", return_value=settings):
            # missing pack file -> clean HarnessError -> documented exit 1
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["route", "--goal", "x",
                          "--pack", os.path.join(self.tmp.name, "nope.json")])
            self.assertEqual(ctx.exception.code, 1)
            bad = os.path.join(self.tmp.name, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump({"id": "x"}, handle)  # no rungs
            with self.assertRaises(SystemExit) as ctx2:
                cli.main(["route", "--goal", "x", "--pack", bad])
            self.assertEqual(ctx2.exception.code, 1)


class RouteMcpEnvelopeTests(unittest.TestCase):
    def _server(self, engine):
        from harness.mcp import McpServer
        return McpServer(transport=mock.MagicMock(), api_key="key",
                         governor=mock.MagicMock(),
                         ledger=mock.MagicMock(),
                         router=mock.MagicMock(), engine=engine)

    def test_mcp_route_query_envelope(self):
        settings = _unkeyed_settings(tempfile.gettempdir(),
                                     f"mcp-route-{os.urandom(4).hex()}.jsonl")
        engine = SimpleNamespace(jev_policy=None, settings=settings)
        result = self._server(engine)._invoke("route_query", {
            "goal": "fix a typo in the docstring",
            "pack": _route_pack(),
        })
        self.assertEqual(result["status"], "ok")
        self.assertIn("route", result)
        self.assertIn("structural", result)
        self.assertEqual(result["rung_id"], "scout")
        self.assertEqual(result["tier"], "T0")
        self.assertEqual(result["structural"]["site"], "model_route")
        self.assertTrue(result["is_fallback"])

    def test_mcp_route_query_requires_pack_and_policy(self):
        engine = SimpleNamespace(jev_policy=None, settings=None)
        server = self._server(engine)
        with self.assertRaises(HarnessError):
            server._invoke("route_query", {"goal": "x"})  # missing pack
        with self.assertRaises(HarnessError):
            server._invoke("route_query",
                           {"goal": "x", "pack": {"id": "x"}})  # invalid pack
        # engine with neither policy nor settings must refuse honestly
        with self.assertRaises(HarnessError):
            server._invoke("route_query",
                           {"goal": "x", "pack": _route_pack()})

    def test_mcp_tool_schema_declares_route_query(self):
        from harness.mcp_schemas import TOOL_SCHEMAS
        names = [t["name"] for t in TOOL_SCHEMAS]
        self.assertIn("route_query", names)


if __name__ == "__main__":
    unittest.main()
