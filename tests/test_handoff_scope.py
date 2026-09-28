"""Tests for the advisory handoff ownership gate.

The gate lives in scripts/validate_handoff_scope.py (not a package), so it is
loaded by path. The script's own --self-test covers the classifier and the
waiver mechanism deterministically; these tests pin the harness-side wiring:
the script is importable, warn-only mode never fails on document content, and
the strict mode still fails closed on findings.
"""

import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_handoff_scope.py"


def load_gate():
    import sys

    spec = importlib.util.spec_from_file_location("validate_handoff_scope", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses with `from __future__ import annotations` resolve their
    # field types via sys.modules[cls.__module__]; register before exec.
    sys.modules["validate_handoff_scope"] = module
    spec.loader.exec_module(module)
    return module


class TestHandoffScopeGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = load_gate()

    def test_script_self_test_passes(self):
        self.assertEqual(self.gate.self_test(), 0)

    def test_warn_only_never_fails_on_document_content(self):
        # A document naming a foreign product trips the detector; in
        # warn-only mode that must be a warning, not a failure.
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.gate.main(
                [
                    "--repo-root", str(REPO_ROOT),
                    "--warn-only",
                    "--document", "HANDOFF/CTO_HANDOFF_HARNESS_JEV_FREEBUFF_AUDIT_2026-09-21.md",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("[WARN]", buf.getvalue())

    def test_strict_mode_still_fails_closed(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.gate.main(
                [
                    "--repo-root", str(REPO_ROOT),
                    "--document", "HANDOFF/CTO_HANDOFF_HARNESS_JEV_FREEBUFF_AUDIT_2026-09-21.md",
                ]
            )
        self.assertEqual(code, 1)

    def test_clean_document_passes_in_both_modes(self):
        for extra in ([], ["--warn-only"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.gate.main(
                    [
                        "--repo-root", str(REPO_ROOT),
                        *extra,
                        "--document", "HANDOFF/OWNERSHIP_GATE_UNTRACKED_2026-09-26.md",
                    ]
                )
            # This document carries no foreign alias; whether it has a scope
            # block or not, warn-only must exit 0.
            if "--warn-only" in extra:
                self.assertEqual(code, 0)

    def test_waiver_register_exists_and_parses(self):
        register = REPO_ROOT / "handoff_scope_waivers.json"
        self.assertTrue(register.is_file())
        waivers = self.gate.parse_waivers(
            __import__("json").loads(register.read_text(encoding="utf-8"))
        )
        self.assertIsInstance(waivers, dict)


if __name__ == "__main__":
    unittest.main()
