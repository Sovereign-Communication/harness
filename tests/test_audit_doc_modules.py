"""D7 pin: docs may name a real sibling package, never an invented module.

``docs/driver.md`` records the vendored ``driver_core`` sync, including module
names that moved into packages (``extractors.py`` -> ``extractors/``). D7 must
resolve those against the sibling package while still failing closed on a name
that exists nowhere. Without the sibling set, the real migration note is
reported stale; without the fail-closed half, the check stops guarding.
"""
import importlib.util
import unittest
from pathlib import Path

_AUDIT = Path(__file__).resolve().parent.parent / "audits" / "self" / "audit.py"
_spec = importlib.util.spec_from_file_location("harness_self_audit", _AUDIT)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


class DocumentedModuleNamesTests(unittest.TestCase):
    def test_driver_core_modules_resolve(self):
        names = audit._documented_module_names()
        for name in ("consent_ledger", "guardrails", "jev_audit",
                     "jev_context", "locate", "verdicts", "extractors"):
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_invented_name_still_fails_closed(self):
        self.assertNotIn("definitely_not_a_module",
                         audit._documented_module_names())


if __name__ == "__main__":
    unittest.main()
