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
        labels = [label for _, label in release.interpreters()]
        joined = " | ".join(labels)
        for ver in ("3.9", "3.11", "3.13"):
            self.assertIn(ver, joined,
                          "battery matrix must cover CI interpreter " + ver)

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
        self.assertIn('if "bar met" not in verdict.lower():', src,
                      "the BAR MET hard gate must stay")

    def test_leak_signatures_match_r13(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from audits.self import audit as audit_mod
        self.assertEqual(tuple(audit_mod._SUITE_LEAK_SIGNATURES),
                         release.LEAK_SIGNATURES,
                         "driver leak signatures must match R13's owner")


if __name__ == "__main__":
    unittest.main()
