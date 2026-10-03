"""Hermetic strict-scoring contract for the dogfood matrix (no network, no spend).

The live matrix (``tests/dogfood_matrix_suite.py``) used to pass planted
failures: a false claim read as "supported", a driver loop with no ok step, a
failing phase floored to 95. These tests plant exactly those failures behind
model doubles and prove the strict scorer fails them while the original
lenient formulas (replicated below) would have passed them.
"""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m
from tests.dogfood_matrix_suite import (
    PASS_SCORE,
    DogfoodMatrixRunner,
    score_cases,
    score_claim_verification,
    score_driver_loop,
    score_phase_bar,
    strict_score,
)

class _ScriptedJev:
    """TypeSafe double: answers every typed question from a script.

    ``claim_support(text)`` gives the noul for a claim; ``pick(key, text,
    criteria)`` gives the chosen option. Never touches the network.
    """

    def __init__(self, claim_support=None, pick=None):
        self.claim_support = claim_support or (lambda text: 0.5)
        self.pick = pick or (lambda key, text, criteria: sorted(criteria)[0])
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        state = payload["state"]
        text = str(state).lower()
        answers = {}
        for qkey, question in payload["questions"].items():
            if question["type"] == "noul":
                if qkey.startswith("claim_"):
                    claim = state["claims"][int(qkey.split("_")[1])]["text"]
                    noul = self.claim_support(claim.lower())
                else:
                    noul = 0.5
                answers[qkey] = {"type": "noul", "noul": noul}
            elif question["type"] == "choice":
                choice = self.pick(qkey, text, question["criteria"])
                rest = [c for c in question["criteria"] if c != choice]
                probs = {choice: 0.8 if rest else 1.0}
                for c in rest:
                    probs[c] = 0.2 / len(rest)
                answers[qkey] = {"type": "choice", "choice": choice,
                                 "probabilities": probs, "confidence": 0.9}
        return 200, {"model": "jev-test", "answers": answers,
                     "usage": {"input_tokens": 60, "output_tokens": 2}}


def _honest_claims(text):
    return 0.05 if "completely false" in text else 0.95


def _honest_pick(key, text, criteria):
    for needle, choice in (("typo", "r0"), ("json parser", "r1"),
                           ("deadlock", "r2"), ("401", "auth"),
                           ("latency", "perf"), ("driver perception", "driver")):
        if needle in text and choice in criteria:
            return choice
    return sorted(criteria)[0]


# What the matrix scored before this change -- kept verbatim in spirit so the
# planted failures are shown to slip past it.
def _legacy_cat06(true_verdict, true_supported, false_verdict, false_supported):
    pass_true = (true_verdict == "pass" or true_supported >= 0.70)
    fail_false = (false_verdict == "fail" or false_supported <= 0.30)
    return 100.0 if (pass_true and fail_false) else (95.0 if pass_true else 50.0)


def _legacy_cat08(total_steps):
    return 95.0 if total_steps >= 1 else 50.0


def _legacy_cat10(score_val):
    return max(score_val, 95.0)


class MatrixRigMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def runner(self, transport, keyed=True):
        settings = load_settings({"jev_api_key": "jev-key" if keyed else None})
        if not keyed:
            settings.jev_api_key = None
        gov = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.10)
        ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        policy = policy_for(settings, transport=transport, governor=gov, ledger=ledger)
        return DogfoodMatrixRunner(settings=settings, gov=gov, ledger=ledger,
                                   policy=policy)


class StrictScoreTests(unittest.TestCase):
    def test_any_miss_stays_below_pass(self):
        self.assertEqual(strict_score({"a": True, "b": True}), 100.0)
        self.assertLess(strict_score({"a": True, "b": True, "c": False}), PASS_SCORE)
        self.assertEqual(strict_score({"a": False}), 0.0)
        self.assertEqual(strict_score({}), 0.0)

    def test_score_cases_counts_only_matches(self):
        score, details = score_cases([{"match": True}, {"match": False}])
        self.assertEqual(score, 50.0)
        self.assertEqual(details["accuracy"], "1/2")
        self.assertEqual(score_cases([])[0], 0.0)


class Cat06ClaimVerificationTests(MatrixRigMixin, unittest.TestCase):
    def test_honest_evaluator_passes(self):
        runner = self.runner(_ScriptedJev(claim_support=_honest_claims))
        score, details = runner.test_cat06_consensus_claim_verification()
        self.assertEqual(score, 100.0, details)
        self.assertFalse(details["false_claim_passed_as_supported"])

    def test_planted_false_claim_judged_supported_hard_fails(self):
        # A lenient evaluator says everything is supported.
        runner = self.runner(_ScriptedJev(claim_support=lambda text: 0.95))
        score, details = runner.test_cat06_consensus_claim_verification()
        self.assertEqual(score, 0.0)
        self.assertTrue(details["false_claim_passed_as_supported"])
        # The original matrix would have PASSED this exact outcome.
        self.assertGreaterEqual(_legacy_cat06("pass", 1.0, "pass", 1.0), PASS_SCORE)

    def test_unkeyed_policy_cannot_pass_claim_verification(self):
        # Unkeyed Jev yields no verdict either way; the old call also passed a
        # bare string, which normalized to zero claims and skipped silently.
        runner = self.runner(_ScriptedJev(), keyed=False)
        score, details = runner.test_cat06_consensus_claim_verification()
        self.assertLess(score, PASS_SCORE)
        self.assertTrue(details["false_claim_is_fallback"])
        self.assertGreaterEqual(_legacy_cat06("pass", 1.0, "pass", 1.0), PASS_SCORE)

    def test_borderline_false_claim_is_not_rejected_enough(self):
        score, _ = score_claim_verification(
            {"noul": 0.9, "fallback": False}, {"noul": 0.4, "fallback": False})
        self.assertLess(score, PASS_SCORE)

    def test_missing_flags_score_zero_checks(self):
        score, details = score_claim_verification(None, None)
        self.assertEqual(score, 0.0)
        self.assertFalse(any(details["checks"].values()))


class RoutingAndTriageTests(MatrixRigMixin, unittest.TestCase):
    def test_honest_oracle_passes_cat03_and_cat04(self):
        runner = self.runner(_ScriptedJev(pick=_honest_pick))
        score3, d3 = runner.test_cat03_query_routing()
        score4, d4 = runner.test_cat04_issue_triage()
        self.assertEqual(score3, 100.0, d3)
        self.assertEqual(score4, 100.0, d4)

    def test_wrong_oracle_fails_both(self):
        runner = self.runner(_ScriptedJev(pick=lambda key, text, crit: sorted(crit)[-1]))
        self.assertLess(runner.test_cat03_query_routing()[0], PASS_SCORE)
        self.assertLess(runner.test_cat04_issue_triage()[0], PASS_SCORE)

    def test_unkeyed_heuristic_is_not_a_jev_match(self):
        runner = self.runner(_ScriptedJev(), keyed=False)
        for fn in (runner.test_cat03_query_routing, runner.test_cat04_issue_triage):
            score, details = fn()
            self.assertLess(score, PASS_SCORE)
            self.assertTrue(all(c["fallback"] for c in details["cases"]))


class Cat08DriverLoopTests(unittest.TestCase):
    @staticmethod
    def _result(ok_flags, audit_ok=True):
        return {
            "status": "max_steps_reached",
            "total_steps": len(ok_flags),
            "steps": [{"envelope": {"ok": ok}} for ok in ok_flags],
            "audit": {"ok": True, "audit": {"ok": audit_ok}},
            "summary": "ran",
        }

    def test_real_ok_step_with_clean_audit_passes(self):
        score, details = score_driver_loop(self._result([False, True]))
        self.assertEqual(score, 100.0)
        self.assertEqual(details["ok_steps"], 1)

    def test_steps_with_no_ok_step_fail_even_though_legacy_passed(self):
        res = self._result([False, False])
        score, details = score_driver_loop(res)
        self.assertLess(score, PASS_SCORE)
        self.assertEqual(details["ok_steps"], 0)
        self.assertGreaterEqual(_legacy_cat08(res["total_steps"]), PASS_SCORE)

    def test_missing_or_broken_audit_fails(self):
        for audit in (None, {"ok": True}, {"ok": True, "audit": {"ok": False}}):
            res = self._result([True])
            res["audit"] = audit
            self.assertLess(score_driver_loop(res)[0], PASS_SCORE)

    def test_status_string_is_not_an_ok_step(self):
        # The old scorer also counted ``status in ("ok", "complete")``; steps
        # carry no such field, so only the envelope verdict may count.
        res = self._result([False])
        res["steps"][0]["status"] = "ok"
        self.assertEqual(score_driver_loop(res)[1]["ok_steps"], 0)

    def test_no_steps_fails(self):
        self.assertLess(score_driver_loop({"steps": [], "audit": None})[0], PASS_SCORE)


class Cat10PhaseBarTests(unittest.TestCase):
    @staticmethod
    def _phase(score, bar_pass=True, blocking=(), complete=True, fallback=False):
        return {"score": score, "can_mark_complete": complete,
                "bar": {"pass": bar_pass, "blocking_axes": list(blocking)},
                "semantic": {"is_fallback": fallback}}

    def test_real_pass_keeps_the_real_score(self):
        score, _ = score_phase_bar(self._phase(97.5))
        self.assertEqual(score, 97.5)

    def test_low_real_score_is_not_floored_to_95(self):
        phase = self._phase(40.0, bar_pass=False, blocking=["tests"], complete=False)
        score, details = score_phase_bar(phase)
        self.assertLess(score, 41.0)
        self.assertEqual(details["blocking_axes"], ["tests"])
        self.assertGreaterEqual(_legacy_cat10(40.0), PASS_SCORE)

    def test_local_heuristic_cannot_reach_pass(self):
        score, details = score_phase_bar(self._phase(99.0, fallback=True))
        self.assertLess(score, PASS_SCORE)
        self.assertFalse(details["checks"]["live_jev"])

    def test_missing_semantic_block_counts_as_not_live(self):
        phase = self._phase(99.0)
        phase.pop("semantic")
        self.assertLess(score_phase_bar(phase)[0], PASS_SCORE)


if __name__ == "__main__":
    unittest.main()
