"""Hermetic tests for harness.jev_calibration (issue #168).

No network, no Jev key, no ledger — pure analysis over seeded records.
Seed values mirror the observed data points in docs/jev-calibration.md.
"""
import unittest

from harness.jev_calibration import (
    DESTRUCTIVE_SUPPORT_FLOOR,
    analyze_judgments,
    format_report,
    recommend_threshold,
)


SEED = [
    {"verdict": "needs_improvement", "confidence": 0.27, "destructive": 0.41,
     "disposition": "needs_improvement"},  # SCM #451
    {"verdict": "escalate", "confidence": 0.25, "destructive": 0.0,
     "disposition": "escalate"},           # SCM #426
    {"verdict": "escalate", "confidence": 0.42, "destructive": 0.0,
     "disposition": "escalate"},           # SCM #427
    {"verdict": "needs_improvement", "confidence": 0.14, "destructive": 0.0,
     "disposition": "needs_improvement"},  # SCM #428
    {"verdict": "proceed", "confidence": 0.90, "destructive": 0.0,
     "disposition": "proceed"},            # SCM #425 (escalated anyway)
    {"verdict": "escalate", "confidence": 0.81, "destructive": 0.55,
     "disposition": "escalate"},           # SCM #439
    {"verdict": "escalate", "confidence": 0.89, "destructive": 0.55,
     "disposition": "escalate"},           # harness #159
    {"verdict": "needs_improvement", "confidence": 0.70, "destructive": 0.0,
     "disposition": "needs_improvement"},  # harness #167
    {"verdict": "needs_improvement", "confidence": 0.37, "destructive": 0.0,
     "disposition": "needs_improvement"},  # harness #167 (re-gate)
    {"verdict": "needs_improvement", "confidence": 0.83, "destructive": 0.0,
     "disposition": "needs_improvement"},  # BigEnergyCo #173
    {"verdict": "needs_improvement", "confidence": 0.26, "destructive": 0.0,
     "disposition": "needs_improvement"},  # BigEnergyCo #173 (re-gate)
    {"verdict": "escalate", "confidence": 0.54, "destructive": 0.0,
     "disposition": "escalate"},           # audit batch ceiling
]


class AnalyzeJudgmentsTests(unittest.TestCase):
    def test_empty_batch_is_advisory_only(self):
        report = analyze_judgments([])
        self.assertEqual(report["n"], 0)
        self.assertIsNone(report["confidence"])
        self.assertIsNone(report["recommendation"])
        self.assertEqual(report["destructive_flags"], 0)
        self.assertTrue(any("advisory" in n for n in report["notes"]))

    def test_single_record_degenerates(self):
        report = analyze_judgments([SEED[0]])
        self.assertEqual(report["n"], 1)
        conf = report["confidence"]
        for key in ("min", "max", "mean", "p50", "p95"):
            self.assertAlmostEqual(conf[key], 0.27)

    def test_seed_distribution(self):
        report = analyze_judgments(SEED)
        self.assertEqual(report["n"], 12)
        conf = report["confidence"]
        self.assertAlmostEqual(conf["min"], 0.14)
        self.assertAlmostEqual(conf["max"], 0.90)
        self.assertAlmostEqual(conf["mean"], 6.38 / 12)
        self.assertAlmostEqual(conf["p50"], 0.48)
        self.assertAlmostEqual(conf["p95"], 0.8945)

    def test_per_disposition_breakdown(self):
        report = analyze_judgments(SEED)
        by_disp = report["by_disposition"]
        self.assertEqual(by_disp["escalate"]["n"], 5)
        self.assertEqual(by_disp["needs_improvement"]["n"], 6)
        self.assertEqual(by_disp["proceed"]["n"], 1)
        self.assertAlmostEqual(by_disp["proceed"]["confidence"]["max"], 0.90)
        self.assertAlmostEqual(by_disp["escalate"]["confidence"]["min"], 0.25)

    def test_destructive_flags_counted(self):
        report = analyze_judgments(SEED)
        self.assertEqual(report["destructive_flags"], 2)
        self.assertEqual(DESTRUCTIVE_SUPPORT_FLOOR, 0.5)

    def test_095_note_when_max_below_threshold(self):
        report = analyze_judgments(SEED)
        self.assertTrue(any("0.95" in n for n in report["notes"]))

    def test_missing_confidence_raises(self):
        with self.assertRaises(ValueError):
            analyze_judgments([{"verdict": "escalate"}])

    def test_non_numeric_confidence_raises(self):
        with self.assertRaises(ValueError):
            analyze_judgments([{"confidence": "high"}])

    def test_bool_confidence_raises(self):
        with self.assertRaises(ValueError):
            analyze_judgments([{"confidence": True}])

    def test_out_of_range_confidence_raises(self):
        for bad in (-0.1, 1.5):
            with self.assertRaises(ValueError):
                analyze_judgments([{"confidence": bad}])

    def test_non_mapping_record_raises(self):
        with self.assertRaises(ValueError):
            analyze_judgments(["not-a-record"])

    def test_missing_disposition_defaults_to_unknown(self):
        report = analyze_judgments([{"confidence": 0.5}])
        self.assertIn("unknown", report["by_disposition"])


class RecommendThresholdTests(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(recommend_threshold([]))

    def test_band_within_observed_range(self):
        rec = recommend_threshold(SEED)
        lo, hi = rec["discriminating_band"]
        self.assertLessEqual(0.14, lo)
        self.assertLessEqual(hi, 0.90)
        self.assertLess(lo, hi)
        self.assertAlmostEqual(rec["observed_max"], 0.90)

    def test_method_documents_max_insufficiency(self):
        rec = recommend_threshold(SEED)
        self.assertIn("freeze_jev_settings", rec["method"])
        self.assertIn("not sufficient", rec["method"])

    def test_warning_requires_labeled_review(self):
        rec = recommend_threshold(SEED)
        self.assertIn("freeze_jev_settings", rec["warning"])


class FormatReportTests(unittest.TestCase):
    def test_renders_seed_report(self):
        text = format_report(analyze_judgments(SEED))
        self.assertIn("n=12", text)
        self.assertIn("min=0.14", text)
        self.assertIn("max=0.90", text)
        self.assertIn("escalate: n=5", text)
        self.assertIn("destructive flags", text)

    def test_renders_empty_advisory(self):
        text = format_report(analyze_judgments([]))
        self.assertIn("advisory only", text)


if __name__ == "__main__":
    unittest.main()
