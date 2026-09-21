"""JEV-P3-calibration: ledger_analytics jev confidence vs verify outcomes."""
import os
import tempfile
import unittest

from harness.ledger import AutonomyLedger


class JevCalibrationReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))

    def test_empty_ledger_returns_advisory_empty_report(self):
        report = self.ledger.jev_calibration_report()
        self.assertEqual(report["jev_evals"], 0)
        self.assertEqual(report["tasks_joined_with_verify"], 0)
        self.assertIn("advisory only", report["notes"][0])

    def test_joins_jev_confidence_with_verify_outcomes(self):
        led = self.ledger
        # Overconfident: high supported, verify fails.
        led.append("jev_eval", task_id="t1", site="apply", verdict="pass",
                   confidence=0.9, supported=0.95, is_fallback=False,
                   input_tokens=10, cost=0.00042)
        led.append("verify_round", task_id="t1", round=1, passed=False,
                   model="coder")
        # Well-calibrated: high supported, verify passes.
        led.append("jev_eval", task_id="t2", site="route", verdict="pass",
                   confidence=0.88, supported=0.9, is_fallback=False,
                   input_tokens=10, cost=0.00042)
        led.append("verify_round", task_id="t2", round=1, passed=True,
                   model="coder")
        # Fallback evals must not pollute keyed confidence buckets.
        led.append("jev_eval", task_id="t3", site="triage", verdict="pass",
                   confidence=0.0, supported=1.0, is_fallback=True,
                   input_tokens=0, cost=0.0)
        led.append("verify_round", task_id="t3", round=1, passed=True,
                   model="coder")
        # jev_eval without task_id counts in site totals only.
        led.append("jev_eval", task_id=None, site="waist", verdict="pass",
                   confidence=0.8, supported=0.8, is_fallback=False,
                   input_tokens=5, cost=0.00021)

        report = led.jev_calibration_report()
        self.assertEqual(report["jev_evals"], 4)
        self.assertEqual(report["jev_fallback_evals"], 1)
        self.assertEqual(report["jev_keyed_evals"], 3)
        self.assertEqual(report["tasks_with_jev"], 3)
        self.assertEqual(report["tasks_joined_with_verify"], 3)
        self.assertIn("apply", report["by_site"])
        self.assertIn("waist", report["by_site"])
        high = report["confidence_buckets"]["high_supported_ge_0.8"]
        self.assertEqual(high["evals"], 2)
        self.assertEqual(high["verify_pass"], 1)
        self.assertEqual(high["verify_fail"], 1)
        self.assertEqual(high["verify_pass_rate"], 0.5)
        self.assertTrue(report["review_notes"])
        self.assertTrue(any("overconfidence" in n for n in report["review_notes"]))

    def test_participation_report_embeds_jev_calibration(self):
        self.ledger.append("jev_eval", task_id="t9", site="apply",
                           verdict="pass", confidence=0.9, supported=0.9,
                           is_fallback=False, input_tokens=8, cost=0.000336)
        self.ledger.append("verify_round", task_id="t9", round=1,
                           passed=True, model="coder")
        report = self.ledger.participation_report()
        self.assertIn("jev_calibration", report)
        self.assertEqual(report["jev_calibration"]["tasks_joined_with_verify"], 1)
        self.assertEqual(report["jev_calibration"]["jev_evals"], 1)

    def test_no_silent_threshold_magic(self):
        """Advisory report never mutates min_confidence or settings."""
        from harness.config import load_settings
        settings = load_settings()
        before = settings.min_confidence
        self.ledger.append("jev_eval", task_id="t1", site="apply",
                           verdict="pass", confidence=0.4, supported=0.4,
                           is_fallback=False, input_tokens=10, cost=0.00042)
        self.ledger.append("verify_round", task_id="t1", round=1,
                           passed=True, model="coder")
        report = self.ledger.jev_calibration_report()
        after = load_settings().min_confidence
        self.assertEqual(before, after)
        self.assertTrue(any("never mutates settings" in n
                            for n in report["notes"]))
        self.assertTrue(any("do not silently lower" in n
                            for n in report["review_notes"]))


if __name__ == "__main__":
    unittest.main()
