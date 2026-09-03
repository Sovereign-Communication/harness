import json
import os
import tempfile
import unittest

from harness.consent import probe_consent, consent_renew, DECISIONS
from harness.core import SpendGovernor
from harness.ledger import AutonomyLedger
from tests._fake import FakeTransport, m, comp, consent

JUDGE = "inclusionai/ling-2.6-flash"
APPLY = "deepseek/deepseek-chat"


class ConsentProbeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = AutonomyLedger(os.path.join(self.dir.name, "ledger.jsonl"))
        self.models = [m(JUDGE), m(APPLY)]

    def tearDown(self):
        self.dir.cleanup()

    def probe(self, response, model=JUDGE):
        fake = FakeTransport(models=self.models, posts=[response])
        gov = SpendGovernor(fake, "sk-test")
        result = probe_consent(transport=fake, api_key="k", governor=gov,
                               task_id="t1", task="Refactor the flush path",
                               model=model, ledger=self.ledger)
        return fake, gov, result

    def test_accept(self):
        fake, gov, r = self.probe(consent("accept", "fits my context"))
        self.assertEqual(r["decision"], "accept")
        events = [e["event"] for e in self.ledger.entries()]
        self.assertEqual(events, ["offer", "consent_accept"])
        self.assertTrue(self.ledger.entries()[0]["required"])

    def test_decline(self):
        _, _, r = self.probe(consent("decline", "lacks context"))
        self.assertEqual(r["decision"], "decline")

    def test_unparseable_fails_closed_to_defer(self):
        _, _, r = self.probe(comp("I would love to help with that task! Sure!"))
        self.assertEqual(r["decision"], "defer")
        self.assertIn("unparseable", r["reason"])

    def test_http_failure_fails_closed(self):
        _, _, r = self.probe((500, {"error": {"message": "down"}}))
        self.assertEqual(r["decision"], "defer")

    def test_redirect_surfaces_signal(self):
        body = {"decision": "redirect", "reason": "wrong model size",
                "redirect_model": "bigger/model", "scope_suggestion": "split into two PRs"}
        _, _, r = self.probe(comp(json.dumps(body)))
        self.assertEqual(r["decision"], "redirect")
        self.assertEqual(r["redirect_model"], "bigger/model")
        self.assertEqual(r["scope_suggestion"], "split into two PRs")

    def test_renew_records_renew_event(self):
        fake = FakeTransport(models=self.models, posts=[consent("accept")])
        gov = SpendGovernor(fake, "sk-test")
        r = consent_renew(transport=fake, api_key="k", governor=gov, task_id="t1",
                          task="continue work", model=JUDGE, ledger=self.ledger)
        self.assertEqual(r["decision"], "accept")
        events = [e["event"] for e in self.ledger.entries()]
        self.assertEqual(events, ["consent_renew_accept"])


if __name__ == "__main__":
    unittest.main()