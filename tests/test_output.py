"""The stderr/stdout contract: progress chatter through eprint (quiet-aware),
machine JSON alone on stdout, warnings always audible.

Mirrors harness/output.py plus the call sites that must honor it.
All hermetic: no network, no key.
"""
import contextlib
import io
import os
import tempfile
import unittest

from harness import output


class QuietTests(unittest.TestCase):
    def setUp(self):
        self._old = output.QUIET

    def tearDown(self):
        output.QUIET = self._old

    def _capture(self, func, *args, **kwargs):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            func(*args, **kwargs)
        return err.getvalue(), out.getvalue()

    def test_chatter_suppressed_under_quiet(self):
        output.QUIET = True
        err, _ = self._capture(output.eprint, "[panel] calling x ...")
        self.assertEqual(err, "")

    def test_warn_always_audible(self):
        output.QUIET = True
        err, _ = self._capture(output.eprint, "[warn] key file is readable")
        self.assertIn("[warn]", err)

    def test_ledger_prefix_audible(self):
        output.QUIET = True
        err, _ = self._capture(output.eprint, "[ledger] corrupt line quarantined")
        self.assertIn("[ledger]", err)

    def test_fatal_always_audible(self):
        output.QUIET = True
        err, _ = self._capture(output.eprint, "[FATAL] no key")
        self.assertIn("[FATAL]", err)


class CapabilitiesTableTests(unittest.TestCase):
    def test_table_never_touches_stdout(self):
        """Stdout stays pure JSON for piping; the human table lives on
        stderr and vanishes under --quiet while the JSON still flows."""
        from harness.cli_report import _print_capabilities_table
        out = {"models": [{
            "model": "m", "context": 8192, "reasoning": False,
            "json_declared": 0.0, "json_reliable": 0.0, "capability": 0.1,
            "fitness_structured": 0.1, "reliability_structured": 0.1,
        }], "count": 1}
        err, stdout = io.StringIO(), io.StringIO()
        old = output.QUIET
        output.QUIET = False
        try:
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(stdout):
                _print_capabilities_table(out)
        finally:
            output.QUIET = old
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("m", err.getvalue())

    def test_table_silent_under_quiet(self):
        from harness.cli_report import _print_capabilities_table
        old = output.QUIET
        output.QUIET = True
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _print_capabilities_table({"models": [], "count": 0})
        finally:
            output.QUIET = old
        self.assertEqual(err.getvalue(), "")


class KeyWarningTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode bits are meaningless on Windows")
    def test_insecure_keyfile_warns_on_stderr(self):
        from harness import config
        old = output.QUIET
        output.QUIET = False
        try:
            with tempfile.NamedTemporaryFile(delete=False) as f:
                path = f.name
            try:
                os.chmod(path, 0o644)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    config._warn_insecure_keyfile(path)
                self.assertIn("[warn]", err.getvalue())
                self.assertIn("chmod 600", err.getvalue())
            finally:
                os.unlink(path)
        finally:
            output.QUIET = old


if __name__ == "__main__":
    unittest.main()
