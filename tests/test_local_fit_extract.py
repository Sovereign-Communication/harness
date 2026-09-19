"""Tests for the read-only seat extractor and label rules."""

import os
import sys
from typing import Any, Dict
from collections import Counter

import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestLabelRules(unittest.TestCase):
    def setUp(self):
        from harness.local_fit.schema import (
            LABEL_TRUNCATED,
            LABEL_UNUSABLE,
            LABEL_USABLE_STOP,
            is_unusable,
            is_truncated,
            is_usable_stop,
            resolve_label,
        )
        self.L = {
            "unusable": LABEL_UNUSABLE,
            "truncated": LABEL_TRUNCATED,
            "usable_stop": LABEL_USABLE_STOP,
        }
        self.is_unusable = is_unusable
        self.is_truncated = is_truncated
        self.is_usable_stop = is_usable_stop
        self.resolve_label = resolve_label

    def row(self, **overrides):
        row: Dict[str, Any] = {
            "finish_reason": None,
            "status": None,
            "content_present": True,
            "parseable": True,
            "resp_chars": 100,
            "prompt_chars": 100,
            "structured_required": False,
        }
        row.update(overrides)
        return row

    def test_usable_stop_basic(self):
        r = self.row(finish_reason="stop", content_present=True)
        self.assertTrue(self.is_usable_stop(r, False, True))
        self.assertEqual(self.resolve_label(r, False, True), self.L["usable_stop"])

    def test_truncated(self):
        r = self.row(finish_reason="length", content_present=True)
        self.assertTrue(self.is_truncated(r))
        self.assertFalse(self.is_usable_stop(r, False, True))
        self.assertEqual(self.resolve_label(r, False, True), self.L["truncated"])

    def test_unusable_finish_error(self):
        r = self.row(finish_reason="error", content_present=True)
        self.assertTrue(self.is_unusable(r))
        self.assertEqual(self.resolve_label(r, False, True), self.L["unusable"])

    def test_unusable_status_invalid_output(self):
        r = self.row(finish_reason="stop", status="invalid_output", content_present=True)
        self.assertTrue(self.is_unusable(r))

    def test_unusable_missing_content(self):
        r = self.row(finish_reason="stop", content_present=False)
        self.assertTrue(self.is_unusable(r))

    def test_structured_required_parseable_gate(self):
        r = self.row(finish_reason="stop", content_present=True, structured_required=True, parseable=False)
        self.assertFalse(self.is_usable_stop(r, True, False))
        self.assertEqual(self.resolve_label(r, True, False), self.L["unusable"])

    def test_severity_order_unusable_over_truncated(self):
        # Both conditions at once (error finish AND truncated flag): the
        # unusable verdict must win, not merely "not usable_stop".
        r = self.row(finish_reason="error", content_present=True)
        self.assertEqual(
            self.resolve_label(r, False, True, truncated_flag=True),
            self.L["unusable"])

    def test_severity_order_truncated_over_usable(self):
        # A length finish with content present is truncated, exactly --
        # and the truncated flag alone (stop finish) is sufficient too.
        r = self.row(finish_reason="length", content_present=True)
        self.assertEqual(self.resolve_label(r, False, True),
                         self.L["truncated"])
        flagged = self.row(finish_reason="stop", content_present=True)
        self.assertEqual(
            self.resolve_label(flagged, False, True, truncated_flag=True),
            self.L["truncated"])


@unittest.skipUnless(
    os.path.isdir(os.path.join("audits", "scmessenger", "_runs", "v4")),
    "optional deps/audit corpus fixture absent (environment)")
class TestExtractionBasics(unittest.TestCase):
    def test_extract_v4_produces_nonzero_rows(self):
        from harness.local_fit.extract import extract
        path = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith(".json")]
        rows = extract(files)
        self.assertGreater(len(rows), 0, "expected v4 runs to yield seat rows")

    def test_extracted_rows_have_all_labels(self):
        from harness.local_fit.extract import extract
        path = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith(".json")]
        rows = extract(files)
        labels = Counter(r.label for r in rows)
        self.assertIn("usable_stop", labels)
        self.assertIn("unusable", labels)

    def test_extracted_rows_have_features(self):
        from harness.local_fit.extract import extract
        path = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith(".json")]
        rows = extract(files)
        self.assertGreater(len(rows[0].features), 0)
        self.assertTrue(all(isinstance(v, (int, float, str, bool)) for v in rows[0].features.values()))

    def test_specialist_seat_extracted_when_convergence_present(self):
        from harness.local_fit.extract import extract
        path = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith(".json")]
        rows = extract(files)
        specialist_rows = [r for r in rows if r.seat_role == "specialist"]
        self.assertGreater(len(specialist_rows), 0, "expected structured_claims runs to include specialist seat")


class TestFeatureSchema(unittest.TestCase):
    def test_feature_order_matches_schema(self):
        from harness.local_fit.schema import FEATURE_ORDER, TASK_FEATURES, MODEL_FEATURES, INTERACTION_FEATURES
        expected = TASK_FEATURES + MODEL_FEATURES + INTERACTION_FEATURES
        self.assertEqual(FEATURE_ORDER, expected)

    def test_label_order_severity(self):
        from harness.local_fit.schema import LABEL_ORDER
        self.assertEqual(LABEL_ORDER, ["unusable", "truncated", "usable_stop"])


@unittest.skipUnless(
    os.path.isdir(os.path.join("audits", "scmessenger", "_runs", "v4")),
    "optional deps/audit corpus fixture absent (environment)")
class TestAllAuditsExtraction(unittest.TestCase):
    def test_all_run_files_finds_v4_only(self):
        """Currently the clone only has scmessenger/v4 *_runs/ data."""
        from harness.local_fit.extract import all_run_files
        files = all_run_files("audits")
        self.assertGreater(len(files), 0)
        for f in files:
            self.assertTrue(f.endswith(".json"), f"non-json in all_run_files: {f}")
            # all should be under a _runs/ directory
            rel = os.path.relpath(f, "audits")
            parts = rel.split(os.sep)
            self.assertIn("_runs", parts, f"file not under _runs: {rel}")
        # round2_scores.json is a ledger, not a run, so it must be excluded
        self.assertTrue(all("round2_scores.json" not in f for f in files),
                        "ledger file must not be treated as a run")

    def test_all_audits_extraction_produces_expected_union(self):
        """Extract over all available *_runs/ data and check the union shape."""
        from harness.local_fit.extract import all_run_files, extract
        files = all_run_files("audits")
        rows = extract(files)
        self.assertGreater(len(rows), 0)
        # Only structured_claims task type exists in this clone today
        task_types = {r.task_type for r in rows}
        self.assertIn("structured_claims", task_types)
        labels = {r.label for r in rows}
        self.assertIn("usable_stop", labels)
        self.assertIn("unusable", labels)
        # Confirm panel + panel_failure + specialist roles exist
        roles = {r.seat_role for r in rows}
        self.assertIn("panel", roles)
        self.assertIn("panel_failure", roles)
        self.assertIn("specialist", roles)
        # Union must include every v4 row. Extra live-run dirs (e.g. a new
        # dogfood round) may add rows; do not pin the union to v4-only.
        v4_path = os.path.join("audits", "scmessenger", "_runs", "v4")
        v4_files = [os.path.join(v4_path, f) for f in sorted(os.listdir(v4_path)) if f.endswith(".json")]
        v4_rows = extract(v4_files)
        self.assertGreaterEqual(len(rows), len(v4_rows),
                                "all-audits union must include every v4 row")

    def test_all_audits_union_label_distribution(self):
        from harness.local_fit.extract import all_run_files, extract
        rows = extract(all_run_files("audits"))
        from collections import Counter
        c = Counter(r.label for r in rows)
        self.assertGreater(c["usable_stop"], 0)
        self.assertGreater(c["unusable"], 0)
