"""Issue 325: the v1 five-aspect assessment gate must exist in code.

The policy (docs/JEV_GATE_POLICY_V1.md) was prose only, so two lanes could
read the same judgement and reach different dispositions. These tests pin the
declared aggregation rule -- a band holds when it has the majority of the
declared mass, and a spread with no majority is UNCERTAIN and investigated --
and every branch of the flexibility and single-point rules.

The distribution is never converted into a point score, and the composer
verdict a caller supplies is carried through untouched: the canon forbids
presenting an investigate-cleared v1 disposition as the composer's proceed.
"""
import io
import json
import os
import tempfile
import unittest
from typing import Any, Dict, cast
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from harness import cli
from harness.jev_assessment import (
    ASPECTS, BLOCK, INVESTIGATE, PASS, PASS_WITH_NOTE, STOP,
    AssessmentError, AspectJudgment, assess, from_payload, run_cli,
    verdict_for_payload,
)


def _judgment(name, green=0.0, flex=0.0, below=0.0, **kwargs):
    return AspectJudgment(
        name=name,
        probabilities={">=90": green, "85-89": flex, "<85": below},
        **kwargs)


def _all_green(exceptions=None):
    exceptions = exceptions or {}
    return [
        _judgment(name, **exceptions.get(name, {"green": 0.95, "below": 0.05}))
        for name in ASPECTS
    ]


class AggregationRuleTests(unittest.TestCase):

    def test_majority_band_is_the_aspect_band(self):
        self.assertEqual(_judgment("correctness", green=0.6, below=0.4).band,
                         ">=90")
        self.assertEqual(_judgment("correctness", flex=0.7, green=0.3).band,
                         "85-89")
        self.assertEqual(_judgment("correctness", below=0.8, flex=0.2).band,
                         "<85")

    def test_a_spread_with_no_majority_is_uncertain_not_a_score(self):
        judgment = _judgment("confidence", green=0.45, flex=0.35, below=0.20)
        self.assertEqual(judgment.band, "UNCERTAIN")
        self.assertEqual(judgment.to_dict()["probabilities"]["85-89"], 0.35)

    def test_a_distribution_that_does_not_sum_to_one_is_refused(self):
        with self.assertRaises(AssessmentError):
            _judgment("completeness", green=0.9, flex=0.2)

    def test_an_unknown_or_missing_band_is_refused(self):
        with self.assertRaises(AssessmentError):
            AspectJudgment("completeness",
                           probabilities={">=90": 1.0, "great": 0.0})
        with self.assertRaises(AssessmentError):
            AspectJudgment("completeness", probabilities={">=90": 1.0})

    def test_an_irrelevant_aspect_needs_a_reason(self):
        with self.assertRaises(AssessmentError):
            AspectJudgment("confidence", relevant=False)
        irrelevant = AspectJudgment("confidence", relevant=False,
                                    reason="static document, no runtime claim")
        self.assertEqual(irrelevant.band, "IRRELEVANT")
        self.assertIsNone(irrelevant.probabilities)


class DispositionTests(unittest.TestCase):

    def test_five_green_aspects_pass(self):
        verdict = assess(_all_green())
        self.assertEqual(verdict.decision, PASS)
        self.assertTrue(verdict.ok)

    def test_four_green_and_one_flex_investigates_before_clearing(self):
        aspects = _all_green({"evidence_quality": {"flex": 0.85,
                                                  "green": 0.15}})
        first = assess(aspects)
        self.assertEqual(first.decision, INVESTIGATE)
        self.assertEqual(first.investigate, ["evidence_quality"])
        self.assertFalse(first.ok)

        aspects[4].investigation = "read the diff and the CI run; two claims"
        cleared = assess(aspects)
        self.assertEqual(cleared.decision, PASS_WITH_NOTE)
        self.assertTrue(cleared.ok)
        self.assertIn("evidence_quality", " ".join(cleared.notes))

    def test_a_named_risk_blocks_even_a_cleared_flex_aspect(self):
        aspects = _all_green({"correctness": {"flex": 0.9, "green": 0.1}})
        aspects[2].investigation = "looked"
        aspects[2].risk = "the new path drops the audit record"
        verdict = assess(aspects)
        self.assertEqual(verdict.decision, BLOCK)
        self.assertIn("drops the audit record", " ".join(verdict.risks))

    def test_three_green_and_two_flex_investigate_both(self):
        aspects = _all_green({
            "confidence": {"flex": 0.9, "green": 0.1},
            "evidence_quality": {"flex": 0.9, "green": 0.1},
        })
        verdict = assess(aspects)
        self.assertEqual(verdict.decision, INVESTIGATE)
        self.assertEqual(sorted(verdict.investigate),
                         ["confidence", "evidence_quality"])
        aspects[1].investigation = "checked the claim against the source"
        aspects[4].investigation = "CI output is attached"
        self.assertEqual(assess(aspects).decision, PASS_WITH_NOTE)

    def test_below_bar_blocks_even_with_a_clearing_investigation(self):
        """The policy is explicit: below 85 the work is fixed, not excused."""
        aspects = _all_green({"completeness": {"below": 0.8, "green": 0.2}})
        aspects[0].investigation = "no actual risk found"
        verdict = assess(aspects)
        self.assertEqual(verdict.decision, BLOCK)
        self.assertIn("completeness", " ".join(verdict.notes))

    def test_an_uncertain_aspect_investigates_then_clears_with_a_note(self):
        aspects = _all_green()
        aspects[1] = _judgment("confidence", green=0.45, flex=0.35, below=0.2)
        self.assertEqual(assess(aspects).decision, INVESTIGATE)
        aspects[1].investigation = "re-ran the probe; the spread was provider"
        self.assertEqual(assess(aspects).decision, PASS_WITH_NOTE)

    def test_three_stalled_rounds_stop_honestly(self):
        verdict = assess(_all_green(), stalled_rounds=3)
        self.assertEqual(verdict.decision, STOP)
        self.assertTrue(verdict.stalled)
        self.assertFalse(verdict.ok)
        self.assertEqual(len(verdict.aspects), len(ASPECTS))

    def test_a_missing_or_duplicate_aspect_is_refused(self):
        with self.assertRaises(AssessmentError):
            assess(_all_green()[:4])
        doubled = _all_green() + [_judgment("correctness", green=1.0)]
        with self.assertRaises(AssessmentError):
            assess(doubled)

    def test_all_irrelevant_is_not_a_pass(self):
        aspects = [AspectJudgment(name, relevant=False, reason="no claims")
                   for name in ASPECTS]
        with self.assertRaises(AssessmentError):
            assess(aspects)

    def test_the_verdict_records_its_narrow_authority(self):
        payload = assess(_all_green()).to_dict()
        self.assertIn("lane-workflow", payload["authority"])
        self.assertIn("unchanged", payload["production_gate"])
        self.assertEqual(payload["aspects"][0]["band"], ">=90")


class ComposerSeparationTests(unittest.TestCase):

    def test_a_composer_verdict_is_carried_and_never_merged(self):
        composer = {"verdict": "escalate", "advances_goal": 0.87,
                    "reason": "advances_goal_below_0.95"}
        verdict = assess(_all_green(), composer=composer)
        payload = verdict.to_dict()
        self.assertEqual(verdict.decision, PASS)
        self.assertEqual(payload["composer"], composer)
        # Nothing in the v1 decision claims the composer proceeded.
        self.assertNotIn("proceed", json.dumps(verdict.decision).lower())
        self.assertEqual(composer["verdict"], "escalate")


class CliTests(unittest.TestCase):

    def _payload(self):
        payload: Dict[str, Any] = {
            "aspects": {
                name: {"probabilities": {">=90": 0.9, "85-89": 0.05,
                                         "<85": 0.05}}
                for name in ASPECTS
            }
        }
        return payload

    def test_a_passing_payload_exits_zero_and_prints_the_verdict(self):
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "judgement.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._payload(), handle)
            out = io.StringIO()
            with redirect_stdout(out):
                code = run_cli(["--input", path])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["decision"], PASS)

    def test_a_blocking_payload_exits_one(self):
        payload = self._payload()
        payload["aspects"]["correctness"]["probabilities"] = {
            ">=90": 0.1, "85-89": 0.1, "<85": 0.8}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with redirect_stdout(out):
                code = run_cli(["--input", "-"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["decision"], BLOCK)

    def test_malformed_json_exits_two_with_an_error(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not json")):
            with redirect_stdout(out):
                code = run_cli(["--input", "-"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_the_payload_reader_refuses_unknown_fields(self):
        payload = self._payload()
        payload["aspects"]["correctness"]["score"] = 91
        with self.assertRaises(AssessmentError):
            verdict_for_payload(payload)

    def test_probability_and_direct_aspect_inputs_reject_bad_types(self):
        with self.assertRaisesRegex(AssessmentError, "no probabilities"):
            AspectJudgment(name="correctness", probabilities={})
        with self.assertRaisesRegex(AssessmentError, "is not a number"):
            AspectJudgment(name="correctness", probabilities={
                ">=90": True, "85-89": 0.0, "<85": 0.0})
        with self.assertRaisesRegex(AssessmentError, r"outside \[0, 1\]"):
            AspectJudgment(name="correctness", probabilities={
                ">=90": 1.1, "85-89": 0.0, "<85": 0.0})
        with self.assertRaisesRegex(AssessmentError, "not one of the five aspects"):
            AspectJudgment(name="extra", probabilities={
                ">=90": 1.0, "85-89": 0.0, "<85": 0.0})
        with self.assertRaisesRegex(AssessmentError, "AspectJudgment instances"):
            assess([object()])

    def test_payload_parser_refuses_wrong_container_and_aspect_shapes(self):
        with self.assertRaisesRegex(AssessmentError, "payload must be an object"):
            from_payload(cast(Any, []))
        with self.assertRaisesRegex(AssessmentError, "aspects.*object"):
            from_payload({"aspects": []})
        payload = self._payload()
        payload["aspects"]["extra"] = {"probabilities": {}}
        with self.assertRaisesRegex(AssessmentError, "undeclared aspect"):
            from_payload(payload)
        payload = self._payload()
        payload["aspects"].pop("confidence")
        with self.assertRaisesRegex(AssessmentError, "missing from payload"):
            from_payload(payload)
        payload = self._payload()
        payload["aspects"]["confidence"] = "not-an-object"
        with self.assertRaisesRegex(AssessmentError, "must be an object"):
            from_payload(payload)
        with self.assertRaisesRegex(AssessmentError, "payload must be an object"):
            verdict_for_payload(cast(Any, []))
        with self.assertRaisesRegex(AssessmentError, "unknown payload field"):
            verdict_for_payload(dict(self._payload(), extra=True))

    def test_harness_cli_assessment_handler_reports_all_exit_classes(self):
        def invoke(payload_text, input_path="-"):
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(payload_text)):
                with redirect_stdout(out):
                    with self.assertRaises(SystemExit) as raised:
                        cli._cmd_assess(
                            SimpleNamespace(input=input_path), SimpleNamespace())
            return raised.exception.code, json.loads(out.getvalue())

        good = json.dumps(self._payload())
        code, verdict = invoke(good)
        self.assertEqual(code, 0)
        self.assertEqual(verdict["decision"], PASS)

        stopped = self._payload()
        stopped["stalled_rounds"] = 3
        code, verdict = invoke(json.dumps(stopped))
        self.assertEqual(code, 1)
        self.assertEqual(verdict["decision"], STOP)

        code, error = invoke("not-json")
        self.assertEqual(code, 2)
        self.assertFalse(error["ok"])

        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "assessment.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(good)
            code, verdict = invoke(good, path)
        self.assertEqual(code, 0)
        self.assertEqual(verdict["decision"], PASS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
