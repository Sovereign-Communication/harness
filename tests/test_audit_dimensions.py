"""Hermetic tests for the JEV 4-dimension self-audit gate.

Covers:
- validate_audit_pack: None → default; str JSON; bad type → HarnessError;
  missing keys → HarnessError.
- audit_dimensions_question_pack: has dim_A/R/SM/SD keys; criteria == levels.
- heuristic_audit_dimensions: correct level indices for score bands; bar_met
  transitions; non-dict evidence; empty evidence; a dim absent from a real
  evidence mapping reports not_evaluated.
- JevPolicy._audit_dimension_text: header present; all four dim lines present;
  optional checks bullets.
- JevPolicy.evaluate_audit_dimensions unkeyed → is_fallback=True, zero cost;
  governor=None → is_fallback=True; correct scores from heuristic.
- JevPolicy.evaluate_audit_dimensions KEYED live path (fake transport + fake
  governor + fake ledger): preflight reservation, exactly ONE ledger
  jev_eval per call (site=audit_dimensions), declared-levels-only resolution
  via legend/probabilities, invalid/unmatched answers → is_fallback, a
  partial dimension_evidence mapping reports the missing dimension(s)
  not_evaluated and never lets bar_95_pass become a false PASS, and a
  transport HarnessError falls back with the reservation settled.
"""
import json
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_packs import (
    AUDIT_DIMENSIONS_SITE,
    DEFAULT_AUDIT_DIMENSIONS_PACK,
    audit_dimensions_question_pack,
    heuristic_audit_dimensions,
    validate_audit_pack,
)
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger


class ValidateAuditPackTests(unittest.TestCase):
    def test_none_returns_default(self):
        result = validate_audit_pack(None)
        self.assertIs(result, DEFAULT_AUDIT_DIMENSIONS_PACK)

    def test_dict_passthrough(self):
        pack = {
            "id": "custom",
            "dimensions": {"A": {"name": "Sec", "instructions": "x"}},
            "sentiment": {"levels": ["l0"], "ordinals": [5.0], "bar_met_index": 0},
        }
        self.assertIs(validate_audit_pack(pack), pack)

    def test_json_string_decoded(self):
        pack = {
            "id": "str-pack",
            "dimensions": {"A": {"name": "Sec", "instructions": "y"}},
            "sentiment": {"levels": ["l0"], "ordinals": [5.0], "bar_met_index": 0},
        }
        result = validate_audit_pack(json.dumps(pack))
        self.assertEqual(result["id"], "str-pack")

    def test_non_dict_raises(self):
        with self.assertRaises(HarnessError):
            validate_audit_pack([1, 2, 3])

    def test_missing_dimensions_raises(self):
        with self.assertRaises(HarnessError):
            validate_audit_pack({"sentiment": {}})

    def test_missing_sentiment_raises(self):
        with self.assertRaises(HarnessError):
            validate_audit_pack({"dimensions": {}})


class AuditDimensionsQuestionPackTests(unittest.TestCase):
    def test_has_all_four_dim_keys(self):
        pack = audit_dimensions_question_pack()
        for dim in ("A", "R", "SM", "SD"):
            self.assertIn(f"dim_{dim}", pack)

    def test_each_question_is_score_type(self):
        pack = audit_dimensions_question_pack()
        for key, q in pack.items():
            self.assertEqual(q["type"], "score", f"{key} should be type score")

    def test_criteria_match_sentiment_levels(self):
        pack = audit_dimensions_question_pack()
        levels = DEFAULT_AUDIT_DIMENSIONS_PACK["sentiment"]["levels"]
        for key, q in pack.items():
            self.assertEqual(q["criteria"], levels, f"{key} criteria mismatch")

    def test_instructions_non_empty(self):
        pack = audit_dimensions_question_pack()
        for key, q in pack.items():
            self.assertGreater(len(q["instructions"]), 10, f"{key} instructions too short")


class HeuristicAuditDimensionsTests(unittest.TestCase):
    def _evidence(self, **scores):
        return {dim: {"score": s} for dim, s in scores.items()}

    def test_exemplary_index_at_10(self):
        ev = self._evidence(A=10.0, R=10.0, SM=10.0, SD=10.0)
        result = heuristic_audit_dimensions(ev)
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level_index"], 4, f"{dim} idx should be 4 at 10.0")
            self.assertTrue(result[dim]["bar_met"])

    def test_bar_met_index_at_9_5(self):
        ev = self._evidence(A=9.5, R=9.6, SM=9.7, SD=9.5)
        result = heuristic_audit_dimensions(ev)
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level_index"], 3, f"{dim} should be at bar_met index 3")
            self.assertTrue(result[dim]["bar_met"])

    def test_approaching_index_at_8_5(self):
        ev = self._evidence(A=8.5, R=9.0, SM=8.7, SD=9.1)
        result = heuristic_audit_dimensions(ev)
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level_index"], 2)
            self.assertFalse(result[dim]["bar_met"])

    def test_at_risk_index_at_7_5(self):
        ev = self._evidence(A=7.5, R=8.0, SM=7.0, SD=8.4)
        result = heuristic_audit_dimensions(ev)
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level_index"], 1)
            self.assertFalse(result[dim]["bar_met"])

    def test_failing_index_below_7(self):
        ev = self._evidence(A=0.0, R=5.0, SM=6.9, SD=4.0)
        result = heuristic_audit_dimensions(ev)
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level_index"], 0)
            self.assertFalse(result[dim]["bar_met"])

    def test_non_dict_evidence_value_treated_as_zero(self):
        ev = {"A": "bad-value", "R": {"score": 10.0}, "SM": None, "SD": {"score": 9.5}}
        result = heuristic_audit_dimensions(ev)
        self.assertEqual(result["A"]["level_index"], 0)
        self.assertEqual(result["SM"]["level_index"], 0)
        self.assertEqual(result["R"]["level_index"], 4)
        self.assertEqual(result["SD"]["level_index"], 3)

    def test_none_evidence_treated_as_empty(self):
        result = heuristic_audit_dimensions(None)
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level_index"], 0)
            self.assertFalse(result[dim]["bar_met"])

    def test_result_has_required_keys(self):
        ev = self._evidence(A=10.0, R=10.0, SM=10.0, SD=10.0)
        result = heuristic_audit_dimensions(ev)
        for dim in ("A", "R", "SM", "SD"):
            self.assertIn("name", result[dim])
            self.assertIn("level_index", result[dim])
            self.assertIn("level", result[dim])
            self.assertIn("score", result[dim])
            self.assertIn("bar_met", result[dim])

    def test_partial_evidence_marks_absent_dim_not_evaluated(self):
        """A dim missing from a real evidence dict is honestly not_evaluated,
        never a false 0.0/failing score (JEV-AUDIT-GATE)."""
        ev = self._evidence(A=10.0)
        result = heuristic_audit_dimensions(ev)
        self.assertEqual(result["A"]["level_index"], 4)
        self.assertTrue(result["A"]["evaluated"])
        for dim in ("R", "SM", "SD"):
            self.assertIsNone(result[dim]["level_index"])
            self.assertIsNone(result[dim]["score"])
            self.assertEqual(result[dim]["level"], "not_evaluated")
            self.assertFalse(result[dim]["bar_met"])
            self.assertFalse(result[dim]["evaluated"])

    def test_empty_dict_evidence_marks_all_dims_not_evaluated(self):
        result = heuristic_audit_dimensions({})
        for dim in ("A", "R", "SM", "SD"):
            self.assertEqual(result[dim]["level"], "not_evaluated")
            self.assertFalse(result[dim]["evaluated"])

    def test_none_evidence_keeps_all_zero_fallback_not_not_evaluated(self):
        """Whole-call None (no mapping shape at all) keeps the historical
        all-zero behavior -- only a real partial dict distinguishes
        not_evaluated from a genuine 0.0 score."""
        result = heuristic_audit_dimensions(None)
        for dim in ("A", "R", "SM", "SD"):
            self.assertTrue(result[dim]["evaluated"])
            self.assertEqual(result[dim]["level_index"], 0)
            self.assertNotEqual(result[dim]["level"], "not_evaluated")


class AuditDimensionTextTests(unittest.TestCase):
    def _make_evidence(self, score=10.0, satisfied=12, total=12, checks=None):
        ev = {
            dim: {
                "score": score,
                "checks_satisfied": satisfied,
                "checks_count": total,
                "checks": checks or [],
            }
            for dim in ("A", "R", "SM", "SD")
        }
        return ev

    def test_header_present(self):
        text = JevPolicy._audit_dimension_text(self._make_evidence())
        self.assertIn("Harness 4-Dimensional Self-Audit Evidence:", text)

    def test_all_four_dims_appear(self):
        text = JevPolicy._audit_dimension_text(self._make_evidence())
        for dim in ("A", "R", "SM", "SD"):
            self.assertIn(f"Dimension {dim}:", text)

    def test_score_format_in_output(self):
        text = JevPolicy._audit_dimension_text(self._make_evidence(score=9.29))
        self.assertIn("9.29/10", text)

    def test_checks_satisfied_in_output(self):
        text = JevPolicy._audit_dimension_text(self._make_evidence(satisfied=11, total=12))
        self.assertIn("11/12", text)

    def test_check_bullets_included(self):
        checks = [
            {"id": "A1", "label": "Auth guard", "score": 1},
            {"id": "A2", "label": "Partial check", "score": 0},
        ]
        ev = {dim: {"score": 10.0, "checks_satisfied": 1, "checks_count": 2, "checks": checks}
              for dim in ("A", "R", "SM", "SD")}
        text = JevPolicy._audit_dimension_text(ev)
        self.assertIn("[A1] pass Auth guard", text)
        self.assertIn("[A2] part Partial check", text)

    def test_none_evidence_does_not_raise(self):
        text = JevPolicy._audit_dimension_text(None)
        self.assertIn("Harness 4-Dimensional Self-Audit Evidence:", text)
        for dim in ("A", "R", "SM", "SD"):
            self.assertIn(f"Dimension {dim}:", text)

    def test_at_most_five_check_bullets_per_dim(self):
        checks = [{"id": f"A{i}", "label": f"check {i}", "score": 1} for i in range(10)]
        ev = {"A": {"score": 10.0, "checks_satisfied": 10, "checks_count": 10, "checks": checks},
              "R": {}, "SM": {}, "SD": {}}
        text = JevPolicy._audit_dimension_text(ev)
        # only first 5 bullets for dim A
        self.assertIn("[A4] pass check 4", text)
        self.assertNotIn("[A5] pass check 5", text)


class EvaluateAuditDimensionsUnkeyedTests(unittest.TestCase):
    """Unkeyed policy must return heuristic fallback, zero cost, no network."""

    def _evidence(self):
        return {
            dim: {"score": 10.0, "checks_satisfied": 12, "checks_count": 12, "checks": []}
            for dim in ("A", "R", "SM", "SD")
        }

    def test_unkeyed_returns_is_fallback_true(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        result = policy.evaluate_audit_dimensions(self._evidence())
        self.assertTrue(result["is_fallback"])

    def test_unkeyed_zero_cost(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        result = policy.evaluate_audit_dimensions(self._evidence())
        self.assertEqual(result["cost"], 0.0)
        self.assertEqual(result["input_tokens"], 0)

    def test_unkeyed_all_dims_present(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        result = policy.evaluate_audit_dimensions(self._evidence())
        for dim in ("A", "R", "SM", "SD"):
            self.assertIn(dim, result["dimensions"])

    def test_unkeyed_high_scores_bar_passed(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        result = policy.evaluate_audit_dimensions(self._evidence())
        self.assertTrue(result["bar_95_pass"])
        self.assertGreaterEqual(result["min_score"], 9.5)

    def test_unkeyed_low_scores_bar_fails(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        low_ev = {dim: {"score": 5.0} for dim in ("A", "R", "SM", "SD")}
        result = policy.evaluate_audit_dimensions(low_ev)
        self.assertFalse(result["bar_95_pass"])

    def test_governor_none_also_fallback(self):
        """policy_for with keyed settings but no governor → fallback."""
        settings = load_settings({"jev_api_key": "fake-key"})
        # policy_for without governor keeps governor=None
        policy = policy_for(settings, evaluator=None)
        result = policy.evaluate_audit_dimensions(self._evidence())
        self.assertTrue(result["is_fallback"])

    def test_reasons_list_present(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        result = policy.evaluate_audit_dimensions(self._evidence())
        self.assertIsInstance(result["reasons"], list)
        self.assertTrue(len(result["reasons"]) > 0)

    def test_audit_dimensions_site_constant(self):
        self.assertEqual(AUDIT_DIMENSIONS_SITE, "audit_dimensions")

    def test_unkeyed_exactly_one_ledger_jev_eval(self):
        """Even the unkeyed path must settle through the ONE shared
        accounting helper: exactly one jev_eval, never a jev_refusal
        standing in for it."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            settings = load_settings()
            settings.jev_api_key = None
            policy = policy_for(settings, evaluator=None, ledger=ledger)
            policy.evaluate_audit_dimensions(self._evidence())
            events = [e for e in ledger.entries() if e["event"] == "jev_eval"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["site"], AUDIT_DIMENSIONS_SITE)
            self.assertTrue(events[0]["is_fallback"])
            self.assertEqual(
                len([e for e in ledger.entries() if e["event"] == "jev_refusal"]), 0)

    def test_partial_run_unkeyed_never_false_passes(self):
        """A partial dimension_evidence (only A supplied) must never report
        bar_95_pass True even when A itself scores perfectly."""
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=None)
        partial = {"A": {"score": 10.0}}
        result = policy.evaluate_audit_dimensions(partial)
        self.assertFalse(result["bar_95_pass"])
        self.assertEqual(result["dimensions"]["A"]["level_index"], 4)
        for dim in ("R", "SM", "SD"):
            self.assertIsNone(result["dimensions"][dim]["score"])
            self.assertEqual(result["dimensions"][dim]["level"], "not_evaluated")
            self.assertIsNone(result["scores"][dim])


def _legend_answer(levels, chosen_idx, *, confidence=0.92):
    """Build one official score answer: legend maps numeric-string anchors
    to the pack's declared level strings; the anchor with the highest
    probability is the chosen index -- the same shape every other
    score-typed site in jev_policy.py parses (never a raw index guess)."""
    anchors = [str(i) for i in range(len(levels))]
    legend = {str(i): levels[i] for i in range(len(levels))}
    n = len(levels)
    probs = {a: (0.8 if int(a) == chosen_idx else round(0.2 / max(1, n - 1), 6))
             for a in anchors}
    total = sum(probs.values())
    probs = {k: round(v / total, 6) for k, v in probs.items()}
    drift = round(1.0 - sum(probs.values()), 6)
    probs[anchors[0]] = round(probs[anchors[0]] + drift, 6)
    return {"type": "score", "score": float(chosen_idx), "legend": legend,
            "probabilities": probs, "confidence": confidence}


def _build_live_dim_answers(pack_doc, dim_indices):
    """dim_indices: {dim_id: chosen_level_index}."""
    levels = pack_doc["sentiment"]["levels"]
    return {f"dim_{dim}": _legend_answer(levels, idx)
            for dim, idx in dim_indices.items()}


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


class _CountingGovernor:
    def __init__(self, max_cost=1.0):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.max_cost = max_cost
        self.spent = 0.0

    def reserve(self, worst, label):
        self.reserved.append((worst, label))
        return {"label": label, "worst": worst}

    def reconcile(self, reservation, cost):
        self.reconciled.append((reservation, cost))
        self.spent += float(cost or 0.0)

    def record_actual(self, cost, model):
        self.actual.append((cost, model))
        self.spent += float(cost or 0.0)


def _keyed_settings():
    return load_settings({"jev_api_key": "test-key"})


class EvaluateAuditDimensionsKeyedTests(unittest.TestCase):
    """Hermetic keyed/live path: fake transport + fake governor + fake
    ledger only -- never a real key, never network (JEV-AUDIT-GATE)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        self.pack = DEFAULT_AUDIT_DIMENSIONS_PACK
        self.levels = self.pack["sentiment"]["levels"]
        self.bar_idx = self.pack["sentiment"]["bar_met_index"]

    def _full_evidence(self, score=9.6):
        return {dim: {"score": score, "checks_satisfied": 12, "checks_count": 12,
                      "checks": []} for dim in ("A", "R", "SM", "SD")}

    def test_keyed_live_valid_answers_use_declared_levels_only(self):
        answers = _build_live_dim_answers(
            self.pack, {"A": self.bar_idx, "R": self.bar_idx,
                       "SM": self.bar_idx, "SD": self.bar_idx})
        transport = _JevTransport({
            "model": "jev-test", "answers": answers,
            "usage": {"input_tokens": 200, "output_tokens": 40},
        })
        gov = _CountingGovernor()
        policy = policy_for(_keyed_settings(), transport=transport, governor=gov,
                            ledger=self.ledger)
        result = policy.evaluate_audit_dimensions(self._full_evidence())

        self.assertFalse(result["is_fallback"])
        for dim in ("A", "R", "SM", "SD"):
            d = result["dimensions"][dim]
            self.assertEqual(d["level_index"], self.bar_idx)
            self.assertEqual(d["level"], self.levels[self.bar_idx])
            self.assertIn(d["level"], self.levels)  # declared levels only
            self.assertTrue(d["bar_met"])
        self.assertTrue(result["bar_95_pass"])

        # preflight reservation before dispatch
        self.assertEqual(len(gov.reserved), 1)
        self.assertEqual(gov.reserved[0][1], "jev:" + AUDIT_DIMENSIONS_SITE)
        # settled exactly once with the live cost
        self.assertEqual(len(gov.reconciled), 1)

        # exactly ONE ledger jev_eval per call
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], AUDIT_DIMENSIONS_SITE)
        self.assertFalse(events[0]["is_fallback"])
        self.assertEqual(len(transport.calls), 1)

    def test_partial_run_keyed_marks_missing_dim_not_evaluated_and_fails_closed(self):
        # Live transport still answers all 4 (the operator's own audit
        # always asks); only "A" is a declared/evaluated dimension this run.
        answers = _build_live_dim_answers(
            self.pack, {"A": 4, "R": 4, "SM": 4, "SD": 4})
        transport = _JevTransport({
            "model": "jev-test", "answers": answers,
            "usage": {"input_tokens": 200, "output_tokens": 40},
        })
        gov = _CountingGovernor()
        policy = policy_for(_keyed_settings(), transport=transport, governor=gov,
                            ledger=self.ledger)
        partial_evidence = {"A": {"score": 10.0}}
        result = policy.evaluate_audit_dimensions(partial_evidence)

        self.assertFalse(result["is_fallback"])
        self.assertEqual(result["dimensions"]["A"]["level_index"], 4)
        for dim in ("R", "SM", "SD"):
            self.assertEqual(result["dimensions"][dim]["level"], "not_evaluated")
            self.assertIsNone(result["dimensions"][dim]["score"])
            self.assertFalse(result["dimensions"][dim]["evaluated"])
        # fail closed: never a false PASS on a partial run, even though the
        # one evaluated dimension is exemplary.
        self.assertFalse(result["bar_95_pass"])
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)

    def test_unmatched_dim_falls_back_to_evidence_calibrated_index_only(self):
        """One dimension's legend never matches a declared level: that
        dimension alone falls back to its own evidence-calibrated index --
        never an invented level -- while the others stay live."""
        answers = _build_live_dim_answers(
            self.pack, {"A": self.bar_idx, "SM": self.bar_idx, "SD": self.bar_idx})
        answers["dim_R"] = {"type": "score", "score": 0.5,
                            "legend": {"0": "invented-level-not-in-pack"},
                            "probabilities": {"0": 1.0}, "confidence": 0.9}
        transport = _JevTransport({
            "model": "jev-test", "answers": answers,
            "usage": {"input_tokens": 180, "output_tokens": 30},
        })
        gov = _CountingGovernor()
        policy = policy_for(_keyed_settings(), transport=transport, governor=gov,
                            ledger=self.ledger)
        evidence = self._full_evidence()
        evidence["R"] = {"score": 6.0}  # evidence-calibrated -> index 0
        result = policy.evaluate_audit_dimensions(evidence)

        self.assertFalse(result["is_fallback"])
        self.assertNotIn("invented-level-not-in-pack", self.levels)
        self.assertEqual(result["dimensions"]["R"]["level"], self.levels[0])
        self.assertEqual(result["dimensions"]["R"]["level_index"], 0)
        self.assertIn(result["dimensions"]["A"]["level"], self.levels)
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["is_fallback"])

    def test_all_dims_unmatched_yields_whole_call_fallback(self):
        answers = {
            f"dim_{dim}": {"type": "score", "score": 0.5,
                          "legend": {"0": "invented"}, "probabilities": {"0": 1.0},
                          "confidence": 0.9}
            for dim in ("A", "R", "SM", "SD")
        }
        transport = _JevTransport({
            "model": "jev-test", "answers": answers,
            "usage": {"input_tokens": 150, "output_tokens": 20},
        })
        gov = _CountingGovernor()
        policy = policy_for(_keyed_settings(), transport=transport, governor=gov,
                            ledger=self.ledger)
        result = policy.evaluate_audit_dimensions(self._full_evidence(score=10.0))

        self.assertTrue(result["is_fallback"])
        # governor reservation still settled exactly once
        self.assertEqual(len(gov.reserved), 1)
        self.assertEqual(len(gov.reconciled), 1)
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_fallback"])

    def test_transport_harness_error_falls_back_and_reservation_settled(self):
        class _BoomEvaluator:
            api_key = "test-key"
            model = "jev-test"

            def evaluate(self, state, questions=None):
                raise HarnessError("network down")

        gov = _CountingGovernor()
        policy = JevPolicy(_keyed_settings(), evaluator=_BoomEvaluator(),
                           governor=gov, ledger=self.ledger)
        result = policy.evaluate_audit_dimensions(self._full_evidence(score=10.0))

        self.assertTrue(result["is_fallback"])
        self.assertTrue(any("transport_error" in r for r in result["reasons"]))
        self.assertEqual(len(gov.reserved), 1)
        self.assertEqual(len(gov.reconciled), 1)
        self.assertEqual(gov.reconciled[0][1], 0.0)  # settled at zero, never charged
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_fallback"])
        self.assertEqual(
            len([e for e in self.ledger.entries() if e["event"] == "jev_refusal"]), 0)

    def test_invalid_typesafe_response_falls_back_honestly(self):
        """A live response missing usage/answers shape -> evaluator itself
        returns is_fallback=True; the policy must not present it as live."""

        class _MalformedEvaluator:
            api_key = "test-key"
            model = "jev-test"

            def evaluate(self, state, questions=None):
                return JevEvaluationResult(
                    "fail", 0.0, 0.0, {}, ["invalid TypeSafe response: boom"],
                    is_fallback=True, model="jev-test")

        gov = _CountingGovernor()
        policy = JevPolicy(_keyed_settings(), evaluator=_MalformedEvaluator(),
                           governor=gov, ledger=self.ledger)
        result = policy.evaluate_audit_dimensions(self._full_evidence(score=10.0))

        self.assertTrue(result["is_fallback"])
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_fallback"])

    def test_no_second_jev_client(self):
        import inspect

        import harness.jev_packs as jev_packs
        self.assertNotIn("JevEvaluator(", inspect.getsource(jev_packs))
        self.assertNotIn(
            "JevEvaluator(",
            inspect.getsource(JevPolicy.evaluate_audit_dimensions))


if __name__ == "__main__":
    unittest.main()
