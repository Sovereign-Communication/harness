"""Guards for the suite's ledger isolation and the ledger's stray-file warning.

Two failure modes are pinned here.

1. **Test runs writing to a real ledger.** ``AutonomyLedger`` is append-only
   evidence. The suite exercises ledger-writing paths constantly, so without a
   pinned path every unit run appends into whatever ``HARNESS_LEDGER`` the
   environment resolves to -- on an operator machine, the live ledger. The fix
   lives in ``tests/__init__.py``; these tests assert it is actually in force,
   so it cannot be silently removed and leave the suite polluting again.

2. **The ledger warning about its own lock.** ``_file_lock`` opens
   ``self.path + ".lock"``. That name shares the ledger's prefix, so the
   stray-file guard used to report it on every run -- and a warning that always
   fires is one operators learn to ignore, which defeats the guard that exists
   to surface genuine strays (``DF-LEDGER-1``).
"""

import contextlib
import io
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.ledger import AutonomyLedger
from tests import LEDGER_ISOLATION_DIR, LEDGER_ISOLATION_PATH

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _stderr_of(action):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        action()
    return buf.getvalue()


class SuiteLedgerIsolationTests(unittest.TestCase):
    def test_suite_pins_a_temporary_ledger(self):
        self.assertEqual(os.environ.get("HARNESS_LEDGER"), LEDGER_ISOLATION_PATH)
        self.assertTrue(os.path.isdir(LEDGER_ISOLATION_DIR))

    def test_isolated_ledger_is_outside_the_operator_config_dir(self):
        # The whole point is that this is NOT ~/.config/harness/ledger.jsonl.
        self.assertNotIn(os.path.join(os.sep, ".config", "harness"),
                         LEDGER_ISOLATION_PATH)
        self.assertIn(tempfile.gettempdir(), LEDGER_ISOLATION_PATH)

    def test_settings_resolve_the_isolated_ledger(self):
        self.assertEqual(load_settings().ledger_path, LEDGER_ISOLATION_PATH)

    def test_a_suite_ledger_write_lands_in_the_isolated_file(self):
        ledger = AutonomyLedger(load_settings().ledger_path)
        ledger.append("test_isolation_probe", detail="guard")
        self.assertTrue(os.path.exists(LEDGER_ISOLATION_PATH))
        with open(LEDGER_ISOLATION_PATH, encoding="utf-8") as fh:
            self.assertIn("test_isolation_probe", fh.read())


class LedgerOwnLockTests(unittest.TestCase):
    def _make_ledger(self, name="ledger.jsonl"):
        td = tempfile.mkdtemp(prefix="harness-lock-test-")
        path = os.path.join(td, name)
        with open(path, "a", encoding="utf-8"):
            pass
        return path

    def test_own_lock_file_is_not_reported_as_a_stray(self):
        path = self._make_ledger()
        with open(path + ".lock", "a", encoding="utf-8"):
            pass
        stderr = _stderr_of(lambda: AutonomyLedger(path))
        self.assertNotIn("non-segment", stderr)

    def test_a_genuine_stray_is_still_reported(self):
        # The guard must keep working: the self-lock exemption is one exact
        # name, not a loosening of the prefix rule.
        path = self._make_ledger()
        stray = path + ".bak-20260101-human-backup"
        with open(stray, "a", encoding="utf-8"):
            pass
        stderr = _stderr_of(lambda: AutonomyLedger(path))
        self.assertIn("non-segment", stderr)
        self.assertIn(os.path.basename(stray), stderr)

    def test_lock_exemption_does_not_hide_a_rotation_segment(self):
        path = self._make_ledger()
        segment = path + ".20260101T000000.000000"
        with open(segment, "a", encoding="utf-8"):
            pass
        stderr = _stderr_of(lambda: AutonomyLedger(path))
        self.assertNotIn("non-segment", stderr)


class LedgerEnvNameTests(unittest.TestCase):
    #: This guard's own source names the bad key in order to describe it.
    _SELF = os.path.basename(__file__)

    def test_no_test_module_uses_the_unrecognised_ledger_env_name(self):
        # The wrong key is not mapped by harness.config, so "setting" it is a
        # silent no-op that leaves the write pointed at the ambient ledger.
        # Match the assignment form (not a mention) and skip this guard file.
        bad_key = "HARNESS_LEDGER" + "_PATH"
        needle = bad_key + '"]='
        offenders = []
        for name in sorted(os.listdir(_TESTS_DIR)):
            if not name.endswith(".py") or name == self._SELF:
                continue
            with open(os.path.join(_TESTS_DIR, name), encoding="utf-8") as fh:
                if needle in fh.read().replace(" ", ""):
                    offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
