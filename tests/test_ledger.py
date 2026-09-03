import json
import os
import tempfile
import unittest

from harness.ledger import AutonomyLedger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ledger.jsonl")
        self.ledger = AutonomyLedger(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_chain_builds_and_verifies(self):
        for i in range(5):
            self.ledger.append("offer", task_id=f"t{i}", model="m1", required=True)
        self.assertEqual(len(self.ledger.entries()), 5)
        ok, bad = self.ledger.verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_tamper_detected(self):
        self.ledger.append("offer", task_id="t1", model="m1")
        self.ledger.append("consent_accept", task_id="t1", model="m1", reason="sure")
        # Tamper with the on-disk record: rewrite entry 2's reason.
        lines = open(self.path, encoding="utf-8").read().splitlines()
        entry = json.loads(lines[1])
        entry["reason"] = "FORGED"
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(e, sort_keys=True) for e in
                              [json.loads(lines[0]), entry]) + "\n")
        reloaded = AutonomyLedger(self.path)
        ok, bad = reloaded.verify()
        self.assertFalse(ok)
        self.assertEqual(bad, 2)

    def test_reload_resumes_chain(self):
        self.ledger.append("offer", task_id="t1", model="m1")
        self.ledger.append("consent_accept", task_id="t1", model="m1", reason="ok")
        again = AutonomyLedger(self.path)
        self.assertEqual(again._seq, 2)
        ok, bad = again.verify()
        self.assertTrue(ok)
        # appending on the reloaded handle stays chained
        again.append("complete", task_id="t1", model="m1", status="ok")
        ok, bad = AutonomyLedger(self.path).verify()
        self.assertTrue(ok)

    def test_participation_report_counts(self):
        l = self.ledger
        l.append("offer", task_id="t1", model="judge", required=True)
        l.append("consent_accept", task_id="t1", model="judge", reason="ok")
        l.append("dispatch_start", task_id="t1", model="coder")
        l.append("verify_round", task_id="t1", round=1, passed=True, model="coder")
        l.append("complete", task_id="t1", model="coder", rounds=1, status="ok")
        l.append("offer", task_id="t2", model="judge", required=True)
        l.append("consent_decline", task_id="t2", model="judge", reason="no")
        l.append("offer", task_id="t3", model="judge", required=False)
        l.append("consent_defer", task_id="t3", model="judge", reason="later")
        r = l.participation_report()
        self.assertEqual(r["offers"], 3)
        self.assertEqual(r["accepts"], 1)
        self.assertEqual(r["declines"], 1)
        self.assertEqual(r["defers"], 1)
        self.assertEqual(r["completions"], 1)
        self.assertEqual(r["accept_rate"], round(1 / 3, 4))
        self.assertEqual(r["completion_rate"], 1.0)
        self.assertEqual(r["consent_required_offers"], 2)
        self.assertEqual(r["mean_verify_rounds"], 1.0)
        self.assertFalse(r["consent_looks_degenerate"])

    def test_calibration_tracks_readiness_vs_verify(self):
        """Join HARNESS_READY: confident verdicts with the same-round verify outcome."""
        l = self.ledger
        # coder_a: confident twice, both passed -> well-calibrated
        l.append("readiness", task_id="t1", model="coder_a", round=1, decision="confident")
        l.append("verify_round", task_id="t1", round=1, passed=True, model="coder_a",
                 readiness="confident")
        l.append("readiness", task_id="t2", model="coder_a", round=1, decision="confident")
        l.append("verify_round", task_id="t2", round=1, passed=True, model="coder_a",
                 readiness="confident")
        # coder_b: confident twice, both failed -> overconfident
        l.append("readiness", task_id="t3", model="coder_b", round=1, decision="confident")
        l.append("verify_round", task_id="t3", round=1, passed=False, model="coder_b",
                 readiness="confident")
        l.append("readiness", task_id="t4", model="coder_b", round=1, decision="confident")
        l.append("verify_round", task_id="t4", round=1, passed=False, model="coder_b",
                 readiness="confident")
        # coder_c: deferred every time -> no verify join, but defer counted
        l.append("readiness", task_id="t5", model="coder_c", round=1, decision="defer")
        r = l.participation_report()
        cal = r["calibration"]
        self.assertEqual(cal["coder_a"]["confidence_precision"], 1.0)
        self.assertEqual(cal["coder_b"]["confidence_precision"], 0.0)
        self.assertEqual(cal["coder_c"]["defer"], 1)
        self.assertIsNone(cal["coder_c"]["confidence_precision"])
        self.assertEqual(r["confidence_precision"], round(2 / 4, 3))
        self.assertIn("coder_b", r["underconfident_or_overconfident"])
        self.assertNotIn("coder_a", r["underconfident_or_overconfident"])

    def test_calibration_ignores_unmatched_readiness(self):
        """A confident verdict with no same-round verify outcome does not count as pass."""
        l = self.ledger
        l.append("readiness", task_id="t1", model="coder", round=1, decision="confident")
        # verify_round for a different round -> no join
        l.append("verify_round", task_id="t1", round=2, passed=False, model="coder",
                 readiness="confident")
        r = l.participation_report()
        cal = r["calibration"]["coder"]
        self.assertEqual(cal["confident"], 1)
        self.assertEqual(cal["confident_verified"], 0)
        self.assertIsNone(cal["confidence_precision"])

    def test_success_rate_and_model_result_samples(self):
        """success_rate aggregates verify outcomes; samples counts model_result events."""
        l = self.ledger
        l.append("verify_round", task_id="t1", round=1, passed=True, model="coder")
        l.append("verify_round", task_id="t1", round=2, passed=True, model="coder")
        l.append("verify_round", task_id="t2", round=1, passed=False, model="coder")
        l.append("verify_round", task_id="t3", round=1, passed=True, model="coder")
        l.append("model_result", task_id="t1", model="coder", task_type="code",
                 json_expected=False, json_ok=None, status="ok")
        l.append("model_result", task_id="t2", model="coder", task_type="code",
                 json_expected=False, json_ok=None, status="ok")
        l.append("model_result", task_id="t3", model="coder", task_type="code",
                 json_expected=False, json_ok=None, status="ok")
        r = l.participation_report()
        cal = r["calibration"]["coder"]
        self.assertEqual(cal["success_rate"], round(3 / 4, 3))
        self.assertEqual(cal["samples"], 3)

    def test_degenerate_consent_flagged(self):
        l = self.ledger
        for i in range(10):
            l.append("offer", task_id=f"t{i}", model="judge", required=True)
            l.append("consent_accept", task_id=f"t{i}", model="judge", reason="ok")
        r = l.participation_report()
        self.assertEqual(r["accept_rate"], 1.0)
        self.assertTrue(r["consent_looks_degenerate"])
        self.assertIn("theater", r["degenerate_note"])


if __name__ == "__main__":
    unittest.main()