import json
import os
import tempfile
import unittest

from harness.consent import probe_consent
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, comp, m


class ConsentConfidenceTests(unittest.TestCase):
    def test_accept_records_confidence(self):
        body = {"decision": "accept", "confidence": 0.91,
                "reason": "fits", "redirect_model": None,
                "scope_suggestion": None}
        with tempfile.TemporaryDirectory() as td:
            ledger = AutonomyLedger(os.path.join(td, "ledger.jsonl"))
            transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
            governor = SpendGovernor(transport, "sk-test")
            result = probe_consent(
                transport=transport, api_key="k", governor=governor,
                task_id="t", task="work", model="judge", ledger=ledger)
            self.assertEqual(result["decision"], "accept")
            self.assertEqual(result["confidence"], 0.91)
            self.assertEqual(ledger.entries()[-1]["confidence"], 0.91)

    def test_low_confidence_accept_defers_before_dispatch(self):
        body = {"decision": "accept", "confidence": 0.40,
                "reason": "uncertain", "redirect_model": None,
                "scope_suggestion": None}
        with tempfile.TemporaryDirectory() as td:
            ledger = AutonomyLedger(os.path.join(td, "ledger.jsonl"))
            transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
            governor = SpendGovernor(transport, "sk-test")
            result = probe_consent(
                transport=transport, api_key="k", governor=governor,
                task_id="t", task="work", model="judge", ledger=ledger,
                min_confidence=0.70)
            self.assertEqual(result["decision"], "defer")
            self.assertFalse(result["dispatched"])
            self.assertIn("below", result["reason"])
            self.assertEqual(ledger.entries()[-1]["event"], "consent_defer")
            self.assertEqual(ledger.entries()[-1]["confidence"], 0.40)

    def test_invalid_confidence_does_not_authorize_acceptance(self):
        body = {"decision": "accept", "confidence": "certain",
                "reason": "fits", "redirect_model": None,
                "scope_suggestion": None}
        transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
        governor = SpendGovernor(transport, "sk-test")
        result = probe_consent(
            transport=transport, api_key="k", governor=governor,
            task_id="t", task="work", model="judge")
        self.assertEqual(result["decision"], "accept")
        self.assertIsNone(result["confidence"])


if __name__ == "__main__":
    unittest.main()
