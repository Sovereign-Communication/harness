import json
import os
import tempfile
import unittest

from harness.consent import probe_consent, consent_renew
from harness.core import SpendGovernor
from harness.ledger import AutonomyLedger
from tests._fake import FakeTransport, m, comp, consent

JUDGE = "inclusionai/ling-2.6-flash"
APPLY = "deepseek/deepseek-chat"
FALLBACK = "meta-llama/llama-3.1-8b-instruct"


class ConsentRotationTests(unittest.TestCase):
    """An unusable consent answer rotates down the pool; a parsed decision
    (including defer/decline) is sovereign and never re-asked."""

    def setUp(self):
        self.models = [m(JUDGE), m(FALLBACK)]

    def probe(self, posts, model=JUDGE):
        fake = FakeTransport(models=self.models, posts=list(posts))
        gov = SpendGovernor(fake, "sk-test")
        result = probe_consent(transport=fake, api_key="k", governor=gov,
                               task_id="rot", task="Do the work", model=model,
                               ledger=None, fallback_pool=[FALLBACK])
        return fake, gov, result

    def test_reasoning_only_rotates_to_fallback(self):
        """Regression: north-mini-style reasoning-only output must not dead-end
        consent; the probe rotates and the fallback's accept is honored."""
        fake, gov, r = self.probe([comp(None, reasoning="let me think"),
                                   consent("accept", "fits")])
        self.assertEqual(r["decision"], "accept")
        self.assertEqual(r["model"], FALLBACK)
        self.assertEqual([a["status"] for a in r["attempts"]], ["error"])
        self.assertIn("reasoning-only", r["attempts"][0]["error"])

    def test_http_error_rotates_then_fails_closed(self):
        fake, gov, r = self.probe([(500, {"error": {"message": "down"}}),
                                   (500, {"error": {"message": "down"}})])
        self.assertEqual(r["decision"], "defer")
        self.assertEqual(len(r["attempts"]), 2)
        self.assertIn("fail-closed", r["reason"])

    def test_unparseable_rotates_and_parsed_defer_is_sovereign(self):
        """Prose from the first candidate rotates; an explicit defer from the
        second is reported as the model's own decision (no further rotation)."""
        fake, gov, r = self.probe([comp("Sounds great, I am in!"),
                                   consent("defer", "out of my depth")])
        self.assertEqual(r["decision"], "defer")
        self.assertEqual(r["model"], FALLBACK)
        self.assertEqual(r["reason"], "out of my depth")
        self.assertEqual(len(r["attempts"]), 1, "parsed decisions must not rotate further")

    def test_rotation_preflights_whole_pool(self):
        """Every candidate must be priced before the first call so the ceiling
        stays exact when rotation happens."""
        fake = FakeTransport(models=self.models,
                             posts=[comp(None, reasoning="hmm"), consent("accept")])
        gov = SpendGovernor(fake, "sk-test", max_cost=0.01)
        preflight_calls = []
        original = gov.preflight
        def spy(prompt, calls):
            preflight_calls.extend(calls)
            return original(prompt, calls)
        gov.preflight = spy
        probe_consent(transport=fake, api_key="k", governor=gov, task_id="pf",
                      task="Do the work", model=JUDGE, ledger=None,
                      fallback_pool=[FALLBACK])
        self.assertEqual([c[1] for c in preflight_calls], [JUDGE, FALLBACK])

    def test_renew_passes_fallback_pool(self):
        fake = FakeTransport(models=self.models,
                             posts=[comp(None, reasoning="hmm"), consent("accept")])
        gov = SpendGovernor(fake, "sk-test")
        r = consent_renew(transport=fake, api_key="k", governor=gov, task_id="rn",
                          task="continue", model=JUDGE, ledger=None,
                          fallback_pool=[FALLBACK])
        self.assertEqual(r["decision"], "accept")
        self.assertEqual(r["model"], FALLBACK)


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

    def test_consent_cost_is_in_governor_and_ledger(self):
        fake = FakeTransport(models=self.models, posts=[consent("accept")])
        # Use a non-default amount so an accidental zero-cost path is visible.
        fake.posts[0]["usage"]["cost"] = 0.000321
        gov = SpendGovernor(fake, "sk-test")
        result = probe_consent(transport=fake, api_key="k", governor=gov,
                               task_id="cost-task", task="Do the work",
                               model=JUDGE, ledger=self.ledger)
        report = self.ledger.participation_report()
        self.assertAlmostEqual(result["cost"], 0.000321, places=9)
        self.assertAlmostEqual(gov.spent, 0.000321, places=9)
        self.assertAlmostEqual(report["tracked_cost"], gov.spent, places=9)
        self.assertEqual(report["billable_event_count"], 1)

    def test_billable_error_cost_is_counted_and_fails_closed(self):
        fake = FakeTransport(
            models=self.models,
            posts=[(500, {"error": {"message": "provider rejected"},
                          "usage": {"cost": 0.000123}})])
        gov = SpendGovernor(fake, "sk-test")
        result = probe_consent(transport=fake, api_key="k", governor=gov,
                               task_id="error-cost", task="Do the work",
                               model=JUDGE, ledger=self.ledger)
        report = self.ledger.participation_report()
        self.assertEqual(result["decision"], "defer")
        self.assertAlmostEqual(gov.spent, 0.000123, places=9)
        self.assertAlmostEqual(report["tracked_cost"], gov.spent, places=9)


if __name__ == "__main__":
    unittest.main()
