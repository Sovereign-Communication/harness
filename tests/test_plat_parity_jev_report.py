"""PLAT-parity-tests: the Jev report writer's bytes, identical everywhere.

``harness jev-phase --out <file>`` is evidence: it is what a reviewer, a CI
artifact and a cross-machine audit all read. The writer used the platform's
default newline, so a Windows run produced CRLF and a Linux run LF for the
same payload. This test pins the digest, drives the real writer through the
real CLI entry point, and (the part that is easy to fake) proves the path
values inside the report display identically whichever separator the
machine's filesystem uses.
"""
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import osal
from harness.cli import _write_jev_phase_out
from harness.cli_parser import build_parser

# A fixed, platform-neutral payload: every path is repo-relative, which is
# the only scope a shared evidence file can honestly promise (a machine's
# drive letter or home directory is machine-specific by design).
PAYLOAD = {
    "phase": "PLAT-cmd-data",
    "score": 100,
    "min_score": 90,
    "can_mark_complete": True,
    "hard_gates": {"gates_runnable": True, "audit_bar_met": True},
    "blockers": [],
    "files": [
        {"path": osal.display_path("harness/osal.py"), "role": "os owner"},
        {"path": osal.display_path("harness/gate_runner.py"), "role": "gate data"},
        {"path": osal.display_path("harness/ledger.py"), "role": "evidence"},
    ],
    "semantic": {"score": 1.0, "is_fallback": True, "model": None,
                 "note": "hermetic local-only pass"},
}

EXPECTED_SHA256 = "c26a5db21069c6ac2fe4b92ecd131e7a8b6ab45746c41e59f009824578a00f8b"


def _write_report(path, payload=None):
    """Drive the real writer through the real opts object."""
    parser = build_parser()
    opts = parser.parse_args(["jev-phase", "--phase", "PLAT-cmd-data",
                              "--out", path])
    _write_jev_phase_out(opts, PAYLOAD if payload is None else payload)
    with open(path, "rb") as handle:
        return handle.read()


class JevReportParityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "jev-phase.json")

    def test_report_bytes_are_byte_identical_across_platforms(self):
        raw = _write_report(self.path)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), EXPECTED_SHA256,
            "the Jev report's bytes changed; re-pin here only if the payload "
            "change is intended and all three CI platforms agree")

    def test_report_is_lf_only(self):
        raw = _write_report(self.path)
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"}"))

    def test_report_is_utf8_json(self):
        import json
        raw = _write_report(self.path)
        parsed = json.loads(raw.decode("utf-8"))
        self.assertEqual(parsed["phase"], "PLAT-cmd-data")
        self.assertEqual(parsed["files"][0]["path"], "harness/osal.py")

    def test_path_display_is_separator_independent(self):
        """The same report built from Windows-style paths hashes the same."""
        windows_style = {
            "phase": "PLAT-cmd-data",
            "files": [
                {"path": osal.display_path("harness\\osal.py"), "role": "os owner"},
                {"path": osal.display_path("harness\\gate_runner.py"),
                 "role": "gate data"},
                {"path": osal.display_path("harness\\ledger.py"), "role": "evidence"},
            ],
        }
        posix_style = {
            "phase": "PLAT-cmd-data",
            "files": [
                {"path": "harness/osal.py", "role": "os owner"},
                {"path": "harness/gate_runner.py", "role": "gate data"},
                {"path": "harness/ledger.py", "role": "evidence"},
            ],
        }
        self.assertEqual(windows_style, posix_style)
        a = _write_report(os.path.join(self.dir.name, "win.json"), windows_style)
        b = _write_report(os.path.join(self.dir.name, "posix.json"), posix_style)
        self.assertEqual(hashlib.sha256(a).hexdigest(),
                         hashlib.sha256(b).hexdigest())

    def test_missing_out_flag_writes_nothing(self):
        parser = build_parser()
        opts = parser.parse_args(["jev-phase", "--phase", "PLAT-cmd-data"])
        _write_jev_phase_out(opts, PAYLOAD)  # must not raise
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_nested_out_directory_is_created(self):
        nested = os.path.join(self.dir.name, "reports", "runs", "jev.json")
        _write_report(nested)
        self.assertTrue(os.path.isfile(nested))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
