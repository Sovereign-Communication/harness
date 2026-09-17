"""S6 pin for the scripted release driver (audits/self/release.py).

The driver mechanizes docs/releasing.md's mechanical steps; this pin keeps
its contract honest so a future edit cannot silently bypass an audit gate
(the D1/D2/D8 lesson: pins follow the owner).
"""
import ast
import importlib.util
import unittest
from pathlib import Path

_RELEASE = Path(__file__).resolve().parent.parent / "audits" / "self" / "release.py"
_spec = importlib.util.spec_from_file_location("harness_release_driver", _RELEASE)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


class ReleaseDriverContractTests(unittest.TestCase):
    """The driver must CALL the repo's own checks, never reimplement them."""

    def test_interpreter_matrix_covers_ci_versions(self):
        """On dev machines with uv this proves 3.9/3.11/3.13 are found; on
        machines without uv it proves the driver degrades honestly (local
        default plus a note per missing CI interpreter) instead of
        crashing -- the exact failure class PR #24 fixes for CI runners."""
        import shutil
        has_uv = shutil.which("uv") is not None
        labels = [label for _, label in release.interpreters()]
        self.assertIn("local default", labels)
        if has_uv:
            joined = " | ".join(labels)
            for ver in ("3.9", "3.11", "3.13"):
                self.assertIn(ver, joined,
                              "battery matrix must cover CI interpreter " + ver)
        else:
            for _, label in release.interpreters(full=True):
                self.assertNotIn("uv-managed", label)

    def test_battery_step_invokes_audit_and_leak_scan(self):
        src = _RELEASE.read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        # ruff + the self-audit are invoked as subprocesses (subprocess.run
        # via run/capture); the leak scan is the R13 signature tuple.
        self.assertIn("run", names)
        self.assertIn("check=True", src)
        self.assertIn("AUDIT", src)
        self.assertEqual(release.LEAK_SIGNATURES,
                         ("ResourceWarning", "unclosed file"))
        self.assertIn('any(v < 9.5 for v in dims.values())', src,
                      "the BAR MET hard gate must stay (per-dimension >= 9.5)")

    def test_leak_signatures_match_r13(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from audits.self import audit as audit_mod
        self.assertEqual(tuple(audit_mod._SUITE_LEAK_SIGNATURES),
                         release.LEAK_SIGNATURES,
                         "driver leak signatures must match R13's owner")


class StepEditsFlattenTests(unittest.TestCase):
    """step_edits' real path on a temp-repo fixture: the flatten must
    preserve the changelog header above [Unreleased] (the first real
    run dropped it -- caught by the D6 gate) and splice the release
    section in place. pip/metadata subprocesses are stubbed; every
    file mutation is real."""

    def _make_repo(self, root):
        NL = release.NL
        root = Path(root)
        (root / "harness").mkdir()
        header = "# Changelog" + NL + NL + "Format is Keep a Changelog." + NL + NL
        unreleased = ("## [Unreleased]" + NL + NL + "- Added: the sixth" + NL
                      + "- Fixed: the seventh" + NL + NL)
        released = "## [0.3.2] - 2026-09-10" + NL + NL + "- old entry" + NL
        (root / "CHANGELOG.md").write_bytes(
            (header + unreleased + released).encode("utf-8"))
        (root / "pyproject.toml").write_bytes(
            ('name = "harness"' + NL + 'version = "0.3.2"' + NL).encode("utf-8"))
        (root / "harness" / "__init__.py").write_bytes(
            ('__version__ = "0.3.2"' + NL).encode("utf-8"))

    def test_flatten_preserves_header_and_bumps_versions(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            self._make_repo(Path(td))
            calls = []
            class _Out:
                stdout = "0.3.3" + release.NL
                returncode = 0
            ctx = [mock.patch.object(release, "ROOT", Path(td)),
                   mock.patch.object(release, "run", side_effect=calls.append),
                   mock.patch.object(release, "capture", return_value=_Out())]
            for c in ctx:
                c.start()
            try:
                release.step_edits(False, "0.3.3")
            finally:
                for c in ctx:
                    c.stop()
            out = (Path(td) / "CHANGELOG.md").read_bytes().decode("utf-8")
            self.assertTrue(out.startswith("# Changelog"),
                            "header above [Unreleased] must survive")
            self.assertIn("## [Unreleased]" + release.NL + release.NL
                          + "Nothing yet.", out)
            today = release.date.today().isoformat()
            self.assertIn("## [0.3.3] " + chr(0x2014) + " " + today, out)
            self.assertIn("- Added: the sixth", out)
            self.assertIn("## [0.3.2] - 2026-09-10", out)
            self.assertIn('version = "0.3.3"',
                          (Path(td) / "pyproject.toml")
                          .read_text(encoding="utf-8"))
            self.assertIn('__version__ = "0.3.3"',
                          (Path(td) / "harness" / "__init__.py")
                          .read_text(encoding="utf-8"))
            self.assertTrue(any("pip" in " ".join(c) for c in calls),
                            "editable reinstall must be invoked")




class VenvLayoutTests(unittest.TestCase):
    """Cross-platform smoke-venv resolution: the audit named the Windows-only
    Scripts/python hardcode as a correctness residual; POSIX validation is
    by this unit exercise (the real venv creation is exercised on Windows).
    """

    def _fake_venv(self, root, layout):
        import os
        d = os.path.join(str(root), layout)
        os.makedirs(d)
        exe = os.path.join(d, "python.exe" if layout == "Scripts" else "python")
        open(exe, "wb").close()
        return str(root)

    def test_windows_layout_resolves(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            venv = self._fake_venv(td, "Scripts")
            self.assertIn("Scripts", release.venv_python(venv))

    def test_posix_layout_resolves(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            venv = self._fake_venv(td, "bin")
            self.assertIn("bin", release.venv_python(venv))

    def test_missing_layout_fails_loudly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                release.venv_python(td)


if __name__ == "__main__":
    unittest.main()
