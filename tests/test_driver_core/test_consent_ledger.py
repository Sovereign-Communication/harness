"""Bounds and failure behavior for durable REST consent replay state."""
import os
import shutil
import tempfile
import unittest

from driver_core.consent_ledger import (
    ConsentLedgerFull, ConsentLedgerUnavailable, RestConsentLedger,
)


class RestConsentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="driver-consent-ledger-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "consents.sqlite3")
        self.grant = {
            "granted": True,
            "action": "submit_irreversible",
            "params": {"target": "Publish"},
            "by": "test",
        }

    def test_capacity_refuses_new_ids_without_evicting_spent_ids(self):
        ledger = RestConsentLedger(self.path, max_records=1)
        first = ledger.bind("first", self.grant)
        self.assertTrue(first.claim())
        first.finish(succeeded=True)

        with self.assertRaises(ConsentLedgerFull):
            ledger.bind("second", self.grant)

        replay = RestConsentLedger(
            self.path, max_records=1).bind("first", self.grant)
        self.assertFalse(replay.available())

    def test_unfinished_claim_remains_blocked_after_reopen(self):
        first = RestConsentLedger(self.path).bind(
            "interrupted", self.grant)
        self.assertTrue(first.claim())

        reopened = RestConsentLedger(self.path).bind(
            "interrupted", self.grant)
        self.assertFalse(reopened.available())
        self.assertFalse(reopened.claim())

    def test_storage_failure_refuses_binding(self):
        blocked_path = os.path.join(self.directory, "is-a-directory")
        os.mkdir(blocked_path)
        ledger = RestConsentLedger(blocked_path)

        with self.assertRaises(ConsentLedgerUnavailable):
            ledger.bind("cannot-store", self.grant)


if __name__ == "__main__":
    unittest.main()
