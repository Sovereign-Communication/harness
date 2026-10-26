"""Hermetic tests for the typed pre-escalation decision gate (issue #106)."""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult, _validate_questions
from harness.jev_packs import (
    DECISION_CALIBRATION_CASES,
    DECISION_CONFIDENCE_THRESHOLD,
    DECISION_DISPOSITION_MARGIN,
    DECISION_DISPOSITIONS,
    DECISION_MAX_REVISIONS,
    DECISION_PACK_VERSION,
    DECISION_SITE,
    DECISION_VERDICTS,
    compose_decision_verdict,
    decision_calibration_report,
    decision_question_pack,
)
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


def _decision_answers(*, destructive=0.05, disposition="proceed",
                      confidence=0.97, advances=0.96, probabilities=None):
    if probabilities is None:
        distribution = {name: 0.0 for name in DECISION_DISPOSITIONS}
        distribution[disposition] = 1.0
    else:
        distribution = {name: float(probabilities.get(name, 0.0))
                        for name in DECISION_DISPOSITIONS}
    return {
        "is_destructive": {"type": "noul", "noul": destructive},
        "disposition": {"type": "choice", "choice": disposition,
                        "confidence": confidence,
                        "probabilities": distribution,
                        "unmatched_options": []},
        "advances_goal": {"type": "noul", "noul": advances},
    }


def _decision_result(answers, *, is_fallback=False):
    return JevEvaluationResult(
        "pass", 0.97, 0.96, answers, ["test"], cost=0.0,
        input_tokens=10, output_tokens=3, is_fallback=is_fallback,
        model="jev-test")


def _decision_wire_response(*, destructive=0.05, disposition="proceed",
                            confidence=0.97, advances=0.96, tokens=100):
    return {
        "model": "jev-test",
        "answers": {
            "is_destructive": {"type": "noul", "noul": destructive},
            "disposition": {
                "type": "choice", "choice": disposition,
                "confidence": confidence,
                "probabilities": {
                    "proceed": 0.9 if disposition == "proceed" else 0.05,
                    "needs_improvement": (
                        0.9 if disposition == "needs_improvement" else 0.05),
                    "escalate": 0.9 if disposition == "escalate" else 0.05,
                },
            },
            "advances_goal": {"type": "noul", "noul": advances},
        },
        "usage": {"input_tokens": tokens, "output_tokens": 3},
    }


class DecisionPackTests(unittest.TestCase):
    def test_pack_declares_exactly_three_typed_questions(self):
        pack = decision_question_pack()
        self.assertEqual(set(pack), {"is_destructive", "disposition",
                                    "advances_goal"})
        self.assertEqual(pack["is_destructive"]["type"], "noul")
        self.assertEqual(pack["disposition"]["type"], "choice")
        self.assertEqual(pack["advances_goal"]["type"], "noul")

    def test_pack_uses_only_supported_primitives_and_validates(self):
        cleaned = _validate_questions(decision_question_pack())
        self.assertEqual(set(cleaned), {"is_destructive", "disposition",
                                       "advances_goal"})
        criteria = cleaned["disposition"]["criteria"]
        self.assertEqual(set(criteria), set(DECISION_DISPOSITIONS))


class ComposeDecisionVerdictTests(unittest.TestCase):
    def _verdict(self, **kwargs):
        return compose_decision_verdict(
            _decision_result(_decision_answers(**kwargs)))

    def test_safe_high_confidence_action_proceeds(self):
        verdict = self._verdict()
        self.assertEqual(verdict["verdict"], "proceed")
        self.assertEqual(verdict["threshold"], DECISION_CONFIDENCE_THRESHOLD)
        self.assertEqual(verdict["margin"], DECISION_DISPOSITION_MARGIN)

    def test_destructive_escalates_at_any_confidence(self):
        verdict = self._verdict(destructive=0.9, disposition="proceed",
                                confidence=0.99, advances=0.99)
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("destructive", verdict["reasons"][0])

    def test_destructive_boundary_is_half(self):
        verdict = self._verdict(destructive=0.5, disposition="proceed",
                                confidence=0.99, advances=0.99)
        self.assertEqual(verdict["verdict"], "escalate")
        verdict = self._verdict(destructive=0.49, disposition="proceed",
                                confidence=0.99, advances=0.99)
        self.assertEqual(verdict["verdict"], "proceed")

    def test_jev_escalate_disposition_escalates(self):
        verdict = self._verdict(disposition="escalate", confidence=0.9,
                                advances=0.96)
        self.assertEqual(verdict["verdict"], "escalate")

    def test_ambiguous_disposition_escalates(self):
        """No clear front-runner is the failure this gate exists to catch.

        This replaces a test that asserted a *low confidence scalar* escalated
        while its own fixture claimed a one-hot distribution. It could not
        express a real ambiguity, so it never tested the thing its name
        implied.
        """
        verdict = self._verdict(
            disposition="proceed", confidence=0.45, advances=0.96,
            probabilities={"proceed": 0.45, "escalate": 0.40,
                           "needs_improvement": 0.15})
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("no clear recommendation", verdict["reasons"][0])

    def test_lead_below_margin_escalates(self):
        verdict = self._verdict(
            disposition="proceed", confidence=0.55, advances=0.96,
            probabilities={"proceed": 0.55, "escalate": 0.40,
                           "needs_improvement": 0.05})
        self.assertEqual(verdict["verdict"], "escalate")

    def test_lead_exactly_at_the_margin_does_not_escalate(self):
        """#326: the rule is `lead < margin`, so equality must proceed.

        (0.60, 0.40) is a lead of exactly the 0.20 margin, but IEEE
        subtraction yields 0.19999999999999996, so the gate escalated and
        printed "leads ... by only 0.20 < margin 0.20" -- a sentence that
        contradicts itself.
        """
        verdict = self._verdict(
            disposition="proceed", confidence=0.60, advances=0.96,
            probabilities={"proceed": 0.60, "escalate": 0.40,
                           "needs_improvement": 0.0})
        self.assertAlmostEqual(verdict["disposition_lead"], 0.20, places=12)
        self.assertEqual(verdict["verdict"], "proceed", verdict["reasons"])

    def test_lead_just_below_the_margin_still_escalates(self):
        """The boundary fix must not swallow a real shortfall."""
        verdict = self._verdict(
            disposition="proceed", confidence=0.59, advances=0.96,
            probabilities={"proceed": 0.59, "escalate": 0.40,
                           "needs_improvement": 0.01})
        self.assertEqual(verdict["verdict"], "escalate")

    def test_decisive_lead_without_unanimity_proceeds(self):
        """A clear recommendation is actionable; residual uncertainty is not danger."""
        verdict = self._verdict(
            disposition="proceed", confidence=0.78, advances=0.95,
            probabilities={"proceed": 0.78, "escalate": 0.15,
                           "needs_improvement": 0.07})
        self.assertEqual(verdict["verdict"], "proceed")
        self.assertAlmostEqual(verdict["disposition_lead"], 0.63)

    def test_confidence_alone_no_longer_gates_the_verdict(self):
        """Regression pin for the fix itself.

        Holding a Choice's concentration to the absolute 0.95 Noul bar is the
        conflation this change removes. A single coherent distribution must
        produce the same verdict whatever concentration is reported alongside
        it, because the reported concentration is telemetry.
        """
        coherent = {"proceed": 0.78, "escalate": 0.15,
                    "needs_improvement": 0.07}
        verdicts = {
            self._verdict(disposition="proceed", confidence=value,
                          advances=0.95, probabilities=coherent)["verdict"]
            for value in (0.5, 0.78, 0.95, 0.99)
        }
        self.assertEqual(verdicts, {"proceed"})

    def test_missing_probabilities_fail_closed(self):
        bad = _decision_answers()
        bad["disposition"] = {"type": "choice", "choice": "proceed",
                              "confidence": 0.99}
        verdict = compose_decision_verdict(_decision_result(bad))
        self.assertEqual(verdict["verdict"], "escalate")

    def test_probabilities_not_summing_to_one_fail_closed(self):
        bad = _decision_answers()
        bad["disposition"]["probabilities"] = {"proceed": 0.9,
                                               "escalate": 0.9,
                                               "needs_improvement": 0.9}
        verdict = compose_decision_verdict(_decision_result(bad))
        self.assertEqual(verdict["verdict"], "escalate")

    def test_destructive_still_escalates_on_a_decisive_proceed_lead(self):
        """The margin change must not soften the destructive guard."""
        verdict = self._verdict(
            destructive=0.9, disposition="proceed", confidence=0.99,
            advances=0.99,
            probabilities={"proceed": 0.99, "escalate": 0.005,
                           "needs_improvement": 0.005})
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("destructive", verdict["reasons"][0])

    def test_advances_goal_below_bar_investigates(self):
        """v1 rule 6 (#324): a low advances_goal investigates, never escalates.

        The Noul is still a real probability, but a low one is a reason to
        gather evidence rather than proof of risk. Escalating here is what
        made the gate reproduce the #312 artifact in Noul form.
        """
        verdict = self._verdict(
            destructive=0.02, disposition="proceed", confidence=0.97,
            advances=0.30)
        self.assertEqual(verdict["verdict"], "investigate", verdict["reasons"])
        self.assertIn("advances_goal", verdict["reasons"][0])
        self.assertIn("investigate", verdict["reasons"][0])

    def test_investigate_is_a_distinct_verdict_not_a_soft_proceed(self):
        # A caller must be able to tell "cleared" from "look closer": the two
        # outcomes are different strings, so a naive `verdict == "proceed"`
        # check cannot silently treat an unresolved investigation as approval.
        self.assertIn("investigate", DECISION_VERDICTS)
        self.assertNotEqual("investigate", "proceed")
        verdict = self._verdict(disposition="proceed", confidence=0.97,
                                advances=0.30)
        self.assertEqual(verdict["verdict"], "investigate")

    def test_v1_worked_example_from_issue_312_investigates(self):
        """The doc's own worked example, which v1 says proceeds via investigate.

        destructive 0.19, a one-hot proceed, advances_goal 0.87. Under v1 the
        gate investigates; the documented resolution of that investigation
        (claim-level checks found no risk) is what makes it proceed. The
        composer's job is to stop at ``investigate`` and say so.
        """
        verdict = self._verdict(
            destructive=0.19, disposition="proceed", confidence=0.98,
            advances=0.87)
        self.assertEqual(verdict["verdict"], "investigate", verdict["reasons"])

    def test_advances_goal_at_the_bar_proceeds(self):
        # The bar is `advances_goal < threshold`; equality is not below it.
        verdict = self._verdict(disposition="proceed", confidence=0.97,
                                advances=0.95)
        self.assertEqual(verdict["verdict"], "proceed", verdict["reasons"])

    def test_revision_bound_escalates_instead_of_revising_forever(self):
        """DECISION_MAX_REVISIONS is enforced by the composer, not just named.

        It was previously exported and referenced nowhere, so a caller had to
        re-implement the bound (or forget to).
        """
        answers = _decision_answers(disposition="needs_improvement",
                                    confidence=0.96, advances=0.96)
        for used in range(DECISION_MAX_REVISIONS):
            verdict = compose_decision_verdict(_decision_result(answers),
                                               revisions=used)
            self.assertEqual(verdict["verdict"], "revise", verdict["reasons"])
        verdict = compose_decision_verdict(
            _decision_result(answers), revisions=DECISION_MAX_REVISIONS)
        self.assertEqual(verdict["verdict"], "escalate", verdict["reasons"])
        self.assertIn("revisions", verdict["reasons"][0])

    def test_needs_improvement_returns_revise(self):
        verdict = self._verdict(disposition="needs_improvement",
                                confidence=0.96, advances=0.96)
        self.assertEqual(verdict["verdict"], "revise")

    def test_fallback_fails_closed(self):
        verdict = compose_decision_verdict(_decision_result({}, is_fallback=True))
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIsNone(verdict["is_destructive"])

    def test_missing_answers_fail_closed(self):
        verdict = compose_decision_verdict(
            _decision_result({"is_destructive": {"type": "noul", "noul": 0.1}}))
        self.assertEqual(verdict["verdict"], "escalate")

    def test_malformed_answers_fail_closed(self):
        bad = _decision_answers()
        bad["disposition"] = {"type": "choice", "choice": "invented",
                              "confidence": 0.99}
        verdict = compose_decision_verdict(_decision_result(bad))
        self.assertEqual(verdict["verdict"], "escalate")


class EvaluateDecisionPolicyTests(unittest.TestCase):
    @staticmethod
    def _unkeyed_settings():
        settings = load_settings()
        settings.jev_api_key = None
        return settings

    def _ledger(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))

    def test_unkeyed_gate_fails_closed_with_zero_cost(self):
        ledger = self._ledger()
        ledger.ensure_offered_task_id("t1", source="test_setup")
        policy = policy_for(self._unkeyed_settings(), ledger=ledger)
        verdict, structural = policy.evaluate_decision(
            "merge the PR", "land the fix", "green CI, reviewed", task_id="t1")
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(structural["cost"], 0.0)
        self.assertEqual(structural["site"], DECISION_SITE)
        events = [e for e in ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual([e["event"] for e in events], ["jev_eval"])
        self.assertEqual(events[0]["site"], DECISION_SITE)
        self.assertEqual(events[0]["decision_verdict"], "escalate")
        self.assertEqual(events[0]["cost"], 0.0)

    def test_keyed_gate_composes_verdict_and_ledgers_once(self):
        transport = FakeTransport(
            jev_posts=[_decision_wire_response(tokens=100)])
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.10)
        ledger = self._ledger()
        ledger.ensure_offered_task_id("t1", source="test_setup")
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=governor,
                            ledger=ledger)
        verdict, structural = policy.evaluate_decision(
            "open a tracking issue", "record follow-up work", "verified defect",
            task_id="t1")
        expected = 100 * 0.042 / 1_000_000
        self.assertEqual(verdict["verdict"], "proceed")
        self.assertEqual(verdict["disposition"], "proceed")
        self.assertFalse(structural["is_fallback"])
        self.assertAlmostEqual(structural["cost"], expected)
        self.assertAlmostEqual(governor.spent, expected)
        events = [e for e in ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual([e["event"] for e in events], ["jev_eval"])
        self.assertEqual(events[0]["decision_verdict"], "proceed")
        self.assertEqual(events[0]["decision_disposition"], "proceed")
        self.assertAlmostEqual(events[0]["decision_confidence"], 0.97)
        self.assertAlmostEqual(events[0]["decision_lead"], 0.85)
        self.assertAlmostEqual(events[0]["decision_advances_goal"], 0.96)
        self.assertAlmostEqual(events[0]["decision_destructive"], 0.05)
        self.assertEqual(events[0]["decision_threshold"],
                         DECISION_CONFIDENCE_THRESHOLD)
        self.assertEqual(events[0]["decision_margin"],
                         DECISION_DISPOSITION_MARGIN)
        self.assertEqual(events[0]["decision_pack"], DECISION_PACK_VERSION)

    def test_keyed_destructive_action_escalates_but_still_ledgers(self):
        transport = FakeTransport(
            jev_posts=[_decision_wire_response(destructive=0.9,
                                               disposition="proceed",
                                               confidence=0.99, advances=0.99,
                                               tokens=50)])
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.10)
        ledger = self._ledger()
        ledger.ensure_offered_task_id("t9", source="test_setup")
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=governor,
                            ledger=ledger)
        verdict, _structural = policy.evaluate_decision(
            "push directly to main", "ship fast", "no review")
        self.assertEqual(verdict["verdict"], "escalate")
        decision = next(e for e in ledger.entries()
                        if e["event"] == "jev_eval")
        self.assertEqual(decision["decision_verdict"], "escalate")


class DecisionCalibrationTests(unittest.TestCase):
    def test_calibration_set_has_at_least_twenty_labeled_cases(self):
        self.assertGreaterEqual(len(DECISION_CALIBRATION_CASES), 20)
        for case in DECISION_CALIBRATION_CASES:
            self.assertIn(case["expected"], DECISION_VERDICTS)

    def test_calibration_fixtures_are_internally_coherent(self):
        """A fixture may not claim a distribution its own answer contradicts.

        The old helper let ``confidence`` be set independently of a one-hot
        distribution, so cases could assert near-unanimity and low confidence
        simultaneously. The helper now derives one from the other; this pins
        that every case in the set agrees with itself.
        """
        for case in DECISION_CALIBRATION_CASES:
            answers = case.get("answers") or {}
            disposition = answers.get("disposition") or {}
            probabilities = disposition.get("probabilities")
            if not probabilities:
                continue
            self.assertAlmostEqual(sum(probabilities.values()), 1.0,
                                   msg=case.get("id"))
            self.assertEqual(set(probabilities), set(DECISION_DISPOSITIONS),
                             case.get("id"))
            self.assertEqual(disposition.get("choice"),
                             max(probabilities, key=probabilities.get),
                             "{}: the declared choice is not the top "
                             "probability".format(case.get("id")))

    def test_composer_matches_ground_truth_with_zero_false_proceeds(self):
        report = decision_calibration_report()
        self.assertEqual(report["case_count"],
                         len(DECISION_CALIBRATION_CASES))
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["false_proceed_rate"], 0.0)
        # Recorded for the calibration gate: unnecessary escalations are
        # reported, not asserted, so tightening the gate stays data-driven.
        print("\ncalibration: {} cases, threshold {}, margin {}, "
              "false_proceed_rate {}, unnecessary_escalation_rate {}".format(
                  report["case_count"], report["threshold"], report["margin"],
                  report["false_proceed_rate"],
                  report["unnecessary_escalation_rate"]))


if __name__ == "__main__":
    unittest.main()


class DecisionGateMalformedSignalTests(unittest.TestCase):
    """Fail-closed coverage for malformed Jev signals (D12)."""

    def _verdict(self, answers):
        return compose_decision_verdict(_decision_result(answers))

    def test_non_numeric_destructive_noul_escalates(self):
        bad = _decision_answers()
        bad["is_destructive"] = {"type": "noul", "noul": "high"}
        verdict = self._verdict(bad)
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("is_destructive", verdict["reasons"][0])

    def test_bool_advances_goal_noul_escalates(self):
        bad = _decision_answers()
        bad["advances_goal"] = {"type": "noul", "noul": True}
        verdict = self._verdict(bad)
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("advances_goal", verdict["reasons"][0])

    def test_out_of_range_noul_escalates(self):
        bad = _decision_answers()
        bad["is_destructive"] = {"type": "noul", "noul": 1.5}
        verdict = self._verdict(bad)
        self.assertEqual(verdict["verdict"], "escalate")

    def test_non_finite_noul_escalates(self):
        bad = _decision_answers()
        bad["advances_goal"] = {"type": "noul", "noul": float("nan")}
        verdict = self._verdict(bad)
        self.assertEqual(verdict["verdict"], "escalate")

    def test_wrong_type_disposition_answer_escalates(self):
        bad = _decision_answers()
        bad["disposition"] = {"type": "noul", "noul": 0.9}
        verdict = self._verdict(bad)
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("disposition", verdict["reasons"][0])

    def test_malformed_disposition_confidence_escalates(self):
        bad = _decision_answers()
        bad["disposition"] = {"type": "choice", "choice": "proceed",
                              "confidence": "high"}
        verdict = self._verdict(bad)
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertIn("disposition", verdict["reasons"][0])


class EvaluateDecisionRefusalTests(unittest.TestCase):
    """The HarnessError path: settle the reservation, record the refusal."""

    def _ledger(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))

    def test_evaluator_error_settles_reservation_and_records_refusal(self):
        class _ExplodingEvaluator:
            model = "stub"
            api_key = "jev-key"

            def evaluate(self, state, questions):
                raise HarnessError("TypeSafe transport exploded")

        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.10)
        ledger = self._ledger()
        ledger.ensure_offered_task_id("t9", source="test_setup")
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=governor, ledger=ledger,
                            evaluator=_ExplodingEvaluator())
        verdict, structural = policy.evaluate_decision(
            "merge the PR", "land the fix", "green CI", task_id="t9")
        self.assertEqual(verdict["verdict"], "escalate")
        self.assertTrue(
            verdict["reasons"][0].startswith("evaluation refused: "))
        self.assertIn("TypeSafe transport exploded", verdict["reasons"][0])
        self.assertEqual(governor.spent, 0.0)
        self.assertEqual(structural["cost"], 0.0)
        events = ledger.entries()
        refusals = [e for e in events if e["event"] == "jev_refusal"]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["site"], DECISION_SITE)
        self.assertEqual(refusals[0]["reason"], "TypeSafe transport exploded")
