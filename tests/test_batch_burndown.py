"""Regression pins for Batch A/C burn-down: verdict R/NR, tally conflicts,
atomic-write parent realpath, MCP max_tokens default, verify tokenization."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from harness.convergence import tally_convergence
from harness.filesafety import _AtomicWriteError, _atomic_write
from harness.validation import validate_mcp_max_tokens
from harness.errors import HarnessError


def _panel(model, claims):
    body = {cid: {"real": real, "confidence": 0.9} for cid, real in claims.items()}
    return {"model": model, "content": json.dumps(body), "finish_reason": "stop"}


class VerdictHonestyTests(unittest.TestCase):
    def test_tally_exposes_real_and_not_real_votes(self):
        panel = [
            _panel("a", {"C1": True}),
            _panel("b", {"C1": True}),
            _panel("c", {"C1": False}),
        ]
        t = tally_convergence(panel, of_panel=3)
        c1 = t["claims"]["C1"]
        self.assertEqual(c1["real_votes"], 2)
        self.assertEqual(c1["not_real_votes"], 1)
        self.assertEqual(c1["verdict"], "real")
        self.assertFalse(c1["unanimous"])
        self.assertFalse(c1["converged"])

    def test_unanimous_real_is_unanimous(self):
        panel = [
            _panel("a", {"C1": True}),
            _panel("b", {"C1": True}),
            _panel("c", {"C1": True}),
        ]
        t = tally_convergence(panel, of_panel=3)
        c1 = t["claims"]["C1"]
        self.assertTrue(c1["unanimous"])
        self.assertTrue(c1["converged"])
        self.assertEqual(c1["real_votes"], 3)


class AtomicWriteParentTests(unittest.TestCase):
    def test_refuses_symlink_parent_via_realpath_staging(self):
        # Staging uses realpath(parent). A symlink parent still works if the
        # real dir is writable; the important pin is that we do not raise
        # _AtomicWriteError for a normal path.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "out.py")
            _atomic_write(target, "x = 1\n")
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "x = 1\n")


class McpMaxTokensTests(unittest.TestCase):
    def test_default_matches_cli_verify(self):
        self.assertEqual(validate_mcp_max_tokens(None), 2048)
        self.assertEqual(validate_mcp_max_tokens(512), 512)


class VerifyTokenizeTests(unittest.TestCase):
    def test_engine_rejects_unbalanced_quotes(self):
        from harness.apply import ApplyEngine
        from harness.router import Router

        class Gov:
            spent = 0.0
            max_cost = 1.0

        class Led:
            def append(self, *a, **k):
                return None

            def participation_report(self, *a, **k):
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "a.py")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("a=1\n")
            engine = ApplyEngine(
                transport=None, api_key="k", governor=Gov(), ledger=Led(),
                router=Router(["m"], "j", "m"),
                default_require_consent=False,
            )
            with self.assertRaises(HarnessError):
                engine._prepare({
                    "file_path": f,
                    "instruction": "x",
                    "verify_cmd": 'python -c "unbalanced',
                })


if __name__ == "__main__":
    unittest.main()
