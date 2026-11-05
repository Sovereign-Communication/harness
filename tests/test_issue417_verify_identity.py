"""Issue 417: verify names the store and chain head it actually checked.

The GUI audit ran from an empty cwd and printed a green audit verdict from
the per-user driver-core store, which was easy to misread as Harness evidence.
These tests pin the additive report fields and the CLI wording, including the
empty-cwd case.
"""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

from driver_core.audit import (
    GENESIS, AuditLog, MemoryAuditLog,
)
from driver_core.cli import cmd_verify
from driver_core.config import load_settings
from driver_core.driver import driver_from_settings


class VerifyIdentityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "audit.jsonl")
        self.log = AuditLog(self.path)
        self.log.append("capture", step_id="s1")
        self.log.append("decision", step_id="s1", action="observe")
        self.head = self.log.head()

    def _cli(self, json_mode, path=None):
        settings = load_settings(env={"DRIVER_AUDIT_PATH": path or self.path},
                                 dry_run=True)
        driver = driver_from_settings(settings)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_verify(argparse.Namespace(json=json_mode), driver)
        return code, out.getvalue()

    def test_a_verdict_names_the_store_and_head_hash(self):
        verdict = self.log.verify()
        payload = verdict.to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["records"], 2)
        self.assertEqual(payload["store"], os.path.abspath(self.path))
        self.assertEqual(payload["store_scope"], "driver-core")
        self.assertEqual(payload["head"], self.head)
        self.assertNotEqual(payload["head"], GENESIS)

    def test_an_in_memory_store_is_named_explicitly(self):
        memory = MemoryAuditLog()
        memory.append("capture", step_id="s1")
        payload = memory.verify().to_dict()
        self.assertEqual(payload["store"], "<memory>")
        self.assertEqual(payload["head"], memory.head())

    def test_a_broken_chain_has_no_trusted_head(self):
        verifier = AuditLog(self.path)
        with open(self.path, "r+", encoding="utf-8") as handle:
            body = handle.read().replace('"action":"observe"',
                                         '"action":"delete_file"')
            handle.seek(0)
            handle.write(body)
            handle.truncate()
        payload = verifier.verify().to_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["store"], os.path.abspath(self.path))
        self.assertIsNone(payload["head"])

    def test_the_cli_prints_store_and_chain_head(self):
        code, out = self._cli(json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn("VERIFIED", out)
        self.assertIn(os.path.abspath(self.path), out)
        self.assertIn("driver-core local audit store", out)
        self.assertIn("chain head", out)
        self.assertIn(self.head, out)

    def test_json_verify_carries_the_same_identity(self):
        code, out = self._cli(json_mode=True)
        self.assertEqual(code, 0)
        audit = json.loads(out)["audit"]
        self.assertEqual(audit["store"], os.path.abspath(self.path))
        self.assertEqual(audit["store_scope"], "driver-core")
        self.assertEqual(audit["head"], self.head)

    def test_an_empty_cwd_run_names_the_store_it_checked(self):
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        with tempfile.TemporaryDirectory() as empty:
            os.chdir(empty)
            try:
                local = AuditLog("audit.jsonl")
                local.append("capture", step_id="s1")
                expected = os.path.abspath("audit.jsonl")
                code, out = self._cli(json_mode=False, path="audit.jsonl")
                self.assertEqual(code, 0)
                self.assertIn(expected, out)
                self.assertIn("driver-core local audit store", out)
                self.assertEqual(AuditLog("audit.jsonl").verify().store,
                                 expected)
            finally:
                # Windows cannot remove a directory that is still the cwd.
                os.chdir(previous)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
