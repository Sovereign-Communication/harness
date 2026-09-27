"""PLAT-parity-tests: the ledger's bytes must be identical on every OS.

A parity hash is only worth pinning if what it hashes is deterministic.
This test freezes the clock (the ledger stamps ``ts`` from the wall clock),
drives the real :class:`harness.ledger.AutonomyLedger`, and hashes the bytes
on disk. The expected digest was produced on Windows; before the newline
fix, the same appends wrote CRLF there and LF on Linux, so this test would
have passed on exactly one platform. Now it passes on all three, or fails on
one of them -- which is the whole point of a parity test.
"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.ledger import AutonomyLedger

# 2026-09-26T12:00:00Z -- fixed so the chain (and therefore the hash) is a
# function of the appends alone.
FROZEN_ISO = "2026-09-26T12:00:00+00:00"
FROZEN_STRFTIME = "20260926T120000.000000"

# sha256 of the exact JSONL produced by the appends in _build_ledger below,
# as produced on Linux/macOS and (after the newline fix) on Windows.
EXPECTED_SHA256 = "ed904b3852b4f4a45215f57c212106fb2fcda939ede45f4c8f580cc244b1adc7"


class _FrozenDatetime(object):
    """A datetime stand-in that always says the same second."""

    @staticmethod
    def now(tz=None):
        import datetime as _dt
        return _dt.datetime(2026, 9, 26, 12, 0, 0,
                            tzinfo=_dt.timezone.utc if tz is not None else None)

    @staticmethod
    def strftime(_fmt):
        return FROZEN_STRFTIME

    @staticmethod
    def fromtimestamp(_ts, tz=None):
        return _FrozenDatetime.now(tz)


def _build_ledger(path):
    """A fixed set of appends: the parity fixture, built by the real owner."""
    with mock.patch("harness.ledger.datetime", _FrozenDatetime):
        ledger = AutonomyLedger(path)
        ledger.append("run_start", task_id="parity-1", model="test/paid-flash",
                      surface="hourglass")
        ledger.append("jev_eval", task_id="parity-1", model="test/paid-flash",
                      cost=0.000042, tokens_in=1000, verdict="pass")
        ledger.append("complete", task_id="parity-1", model="test/paid-flash",
                      cost=0.000042, status="pass")
    return ledger


class LedgerParityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ledger.jsonl")

    def _digest(self):
        with open(self.path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def test_ledger_bytes_are_byte_identical_across_platforms(self):
        _build_ledger(self.path)
        self.assertEqual(
            self._digest(), EXPECTED_SHA256,
            "the ledger's canonical bytes changed; if the change is intended, "
            "re-pin the hash here AND check the chain still verifies on all "
            "three CI platforms")

    def test_ledger_is_lf_only(self):
        """The CRLF claim, asserted: a Windows run must not fork the bytes."""
        _build_ledger(self.path)
        with open(self.path, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"\n"), 3)
        self.assertTrue(raw.endswith(b"\n"))

    def test_chain_still_verifies_and_is_stable_under_reload(self):
        ledger = _build_ledger(self.path)
        ok, bad = ledger.verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)
        reloaded = AutonomyLedger(self.path)
        ok, bad = reloaded.verify()
        self.assertTrue(ok, f"reloaded chain broke at entry {bad}")
        self.assertEqual(len(reloaded.entries()), 3)
        # Reloading must not rewrite the file (a rewrite would change bytes).
        before = self._digest()
        AutonomyLedger(self.path)
        self.assertEqual(self._digest(), before)

    def test_timestamps_are_the_frozen_ones(self):
        _build_ledger(self.path)
        for entry in AutonomyLedger(self.path).entries():
            self.assertEqual(entry["ts"], FROZEN_ISO)

    def test_rewrite_repair_keeps_lf(self):
        """A rotation/rewrite path is a second writer; it must agree."""
        _build_ledger(self.path)
        ledger = AutonomyLedger(self.path)
        with mock.patch("harness.ledger.datetime", _FrozenDatetime):
            ledger._rewrite_file(self.path, ledger.entries())
        with open(self.path, "rb") as handle:
            self.assertNotIn(b"\r\n", handle.read())
        self.assertEqual(self._digest(), EXPECTED_SHA256)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
