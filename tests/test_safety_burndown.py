"""Regression pins for library roots, backup O_EXCL, and ledger load integrity."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.errors import HarnessError
from harness.filesafety import backup_file
from harness.ledger import AutonomyLedger, _canon
import hashlib
import json


class BackupOExclTests(unittest.TestCase):
    def test_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write("x = 1\n")
            backup_root = os.path.join(tmp, "harness-backups")
            os.makedirs(backup_root)
            # Pre-create the EXACT dest the backup will use (pid+uuid pinned).
            dest = os.path.join(backup_root, "task-r1-1-deadbeef-src.py")
            with open(dest, "w", encoding="utf-8") as f:
                f.write("HONEST\n")
            with mock.patch("harness.filesafety.tempfile.gettempdir",
                            return_value=tmp), \
                 mock.patch("harness.filesafety.os.getpid", return_value=1), \
                 mock.patch("harness.filesafety.uuid.uuid4") as u:
                u.return_value.hex = "deadbeefdeadbeef"
                out = backup_file(src, "task", 1)
            self.assertIsNone(out)
            with open(dest, encoding="utf-8") as f:
                self.assertEqual(f.read(), "HONEST\n")

    def test_creates_backup_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write("y = 2\n")
            with mock.patch("harness.filesafety.tempfile.gettempdir",
                            return_value=tmp):
                out = backup_file(src, "task", 2)
            self.assertIsNotNone(out)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "y = 2\n")


class _Gov:
    spent = 0.0
    max_cost = 1.0

    def preflight(self, *a, **k):
        return None


class _LedgerStub:
    def append(self, *a, **k):
        return None

    def participation_report(self, *a, **k):
        return {}


class AllowedRootsTests(unittest.TestCase):
    def test_engine_refuses_outside_roots(self):
        from harness.apply import ApplyEngine
        from harness.router import Router

        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as outside:
            inside = os.path.join(root, "ok.py")
            with open(inside, "w", encoding="utf-8") as f:
                f.write("a = 1\n")
            outside_f = os.path.join(outside, "bad.py")
            with open(outside_f, "w", encoding="utf-8") as f:
                f.write("b = 2\n")
            engine = ApplyEngine(
                transport=None, api_key="k", governor=_Gov(),
                ledger=_LedgerStub(),
                router=Router(["m"], "j", "m"),
                default_require_consent=False,
                allowed_roots=[root],
            )
            with self.assertRaises(HarnessError) as ctx:
                engine._prepare({
                    "file_path": outside_f,
                    "instruction": "x",
                    "verify_cmd": None,
                })
            self.assertIn("allowed_roots", str(ctx.exception))
            try:
                engine._prepare({
                    "file_path": inside,
                    "instruction": "x",
                    "verify_cmd": None,
                })
            except HarnessError as e:
                self.assertNotIn("allowed_roots", str(e))

    def test_empty_roots_is_unrestricted(self):
        from harness.apply import ApplyEngine
        from harness.router import Router
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "a.py")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("a=1\n")
            engine = ApplyEngine(
                transport=None, api_key="k", governor=_Gov(),
                ledger=_LedgerStub(),
                router=Router(["m"], "j", "m"),
                default_require_consent=False,
            )
            try:
                engine._prepare({
                    "file_path": f, "instruction": "x", "verify_cmd": None,
                })
            except HarnessError as e:
                self.assertNotIn("allowed_roots", str(e))


class LedgerLoadIntegrityTests(unittest.TestCase):
    def _write_line(self, path, entry):
        body = dict(entry)
        body["hash"] = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")
        return body

    def test_clean_chain_loads_unbroken(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            a = self._write_line(path, {
                "seq": 1, "ts": "t", "event": "offer", "task_id": "t1",
                "prev_hash": None,
            })
            self._write_line(path, {
                "seq": 2, "ts": "t", "event": "complete", "task_id": "t1",
                "prev_hash": a["hash"],
            })
            led = AutonomyLedger(path)
            self.assertFalse(led.chain_broken)
            self.assertEqual(led.quarantined, 0)
            self.assertEqual(len(led.entries()), 2)

    def test_tampered_hash_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            a = self._write_line(path, {
                "seq": 1, "ts": "t", "event": "offer", "task_id": "t1",
                "prev_hash": None,
            })
            self._write_line(path, {
                "seq": 2, "ts": "t", "event": "complete", "task_id": "t1",
                "prev_hash": a["hash"],
            })
            # Flip a byte in the first line's event name after the fact.
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            lines[0] = lines[0].replace("offer", "OFFER")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            led = AutonomyLedger(path)
            self.assertTrue(led.chain_broken)
            self.assertGreaterEqual(led.quarantined, 1)


if __name__ == "__main__":
    unittest.main()
