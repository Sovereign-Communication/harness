"""HV-1 stage-specific Jev integrations: typed dimensions and honest signals.

The contract this file pins, in the order the canon states it:

- a JEV integration has a stable capability/site name, a typed input/output
  schema, operator-declared criteria, authority/fallback/confidence rules,
  budget/preflight, ledger evidence, and hermetic contract tests;
- no arbitrary plugin code and no model-created category can bypass the
  owner -- an unknown dimension is refused, not invented;
- JEV recommends a restart target; CODE validates the transition, preserves
  completed work, and forces consent renewal when the assignment changed;
- unkeyed / transport-failed / malformed results are never presented as
  native signals, and never a completion or readiness claim.
"""
import unittest
from unittest import mock

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_packs import (
    HOURGLASS_STAGE_DIMENSIONS,
    HOURGLASS_STAGE_PACK_ID,
    HOURGLASS_STAGE_PACK_VERSION,
    HOURGLASS_STAGE_SITE,
    declared_restart_targets,
    hourglass_stage_question_pack,
    normalize_restart_target,
    stage_judgment_requirement,
    HOURGLASS_STAGE_REQUIREMENTS,
    validate_restart_request,
)
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger


class RecordingGovernor:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    def reserve(self, amount, label):
        token = object()
        self.reservations.append((token, amount, label))
        return token

    def reconcile(self, token, amount):
        self.settlements.append((token, amount))

    def record_actual(self, amount, model):
        self.settlements.append((None, amount))


class FakeEvaluator:
    """A keyed evaluator whose answers the test declares per question id."""

    def __init__(self, answers=None, *, fallback=False, discard=False,
                 error=None, cost=0.0001, input_tokens=500, api_key="test-key"):
        self.answers = answers or {}
        self.fallback = fallback
        self.discard = discard
        self.error = error
        self.cost = cost
        self.input_tokens = input_tokens
        self.api_key = api_key
        self.min_confidence = 0.70
        self.model = "jev-1.13.0"
        self.endpoint = "https://api.typesafe.ai/v1/systemone"
        self.transport = None
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return JevEvaluationResult(
            "pass", 0.9, 0.9, dict(self.answers), [],
            cost=self.cost, input_tokens=self.input_tokens, output_tokens=10,
            is_fallback=self.fallback, model=self.model,
            discarded=self.discard)


def noul(value):
    return {"type": "noul", "noul": value}


def choice(target, confidence=0.8, unmatched=None):
    answer = {"type": "choice", "choice": target, "confidence": confidence,
              "probabilities": {target: 1.0}}
    if unmatched is not None:
        answer["unmatched_options"] = unmatched
    return answer


class StagePackDeclarationTests(unittest.TestCase):
    def test_exactly_the_five_declared_dimensions_exist(self):
        self.assertEqual(
            set(HOURGLASS_STAGE_DIMENSIONS),
            {"context_intake", "plan_soundness", "execution", "consent",
             "restart_target"})

    def test_every_dimension_declares_its_signals_and_matching_questions(self):
        for name, spec in HOURGLASS_STAGE_DIMENSIONS.items():
            self.assertTrue(spec["description"], name)
            self.assertTrue(spec["signals"], name)
            questions = hourglass_stage_question_pack(name)
            self.assertEqual(set(questions), set(spec["signals"]), name)
            for key, question in questions.items():
                self.assertIn(question["type"], ("noul", "choice"), name)
                self.assertTrue(question["instructions"].strip(), key)
                self.assertTrue(question["criteria"], key)

    def test_an_unknown_dimension_is_refused_not_invented(self):
        for bad in (None, "", "graph_neural", 7, ["execution"]):
            with self.assertRaises(ValueError):
                hourglass_stage_question_pack(bad)

    def test_restart_vocabulary_is_the_three_declared_stages(self):
        self.assertEqual(declared_restart_targets(),
                         ["context", "planning", "execution"])
        for good in ("context", "planning", "execution", " Planning ", "EXECUTION"):
            self.assertIn(normalize_restart_target(good),
                          declared_restart_targets())
        for bad in ("frontier", "verification", "", None, 3, "contexts"):
            self.assertIsNone(normalize_restart_target(bad))


class RestartTransitionGuardTests(unittest.TestCase):
    """Code owns the transition; a Jev recommendation is never an action."""

    def test_a_backward_restart_into_incomplete_work_is_allowed(self):
        decision = validate_restart_request(
            "execution", "planning", completed_stages=["context"],
            consent_fresh=True)
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["target"], "planning")
        self.assertEqual(decision["reasons"], [])
        self.assertFalse(decision["consent_renewal_required"])

    def test_completed_work_is_preserved_and_never_re_entered(self):
        decision = validate_restart_request(
            "execution", "context", completed_stages=["context", "planning"],
            consent_fresh=True)
        self.assertFalse(decision["allowed"])
        self.assertIn("already recorded complete", " ".join(decision["reasons"]))
        # The preserved work is reported so a caller can resume, not repeat.
        self.assertEqual(sorted(decision["preserved_stages"]),
                         ["context", "planning"])

    def test_a_forward_or_same_stage_move_is_not_a_restart(self):
        for target in ("execution",):
            decision = validate_restart_request("execution", target)
            self.assertFalse(decision["allowed"])
            self.assertIn("not earlier than the current stage",
                          " ".join(decision["reasons"]))

    def test_out_of_vocabulary_target_is_refused(self):
        decision = validate_restart_request("execution", "frontier")
        self.assertFalse(decision["allowed"])
        self.assertIsNone(decision["target"])
        self.assertIn("not a declared stage", decision["reasons"][0])

    def test_stale_consent_forces_renewal_and_blocks_the_restart(self):
        decision = validate_restart_request(
            "execution", "planning", completed_stages=["context"],
            consent_fresh=False)
        self.assertFalse(decision["allowed"])
        self.assertTrue(decision["consent_renewal_required"])
        self.assertIn("must be renewed", " ".join(decision["reasons"]))

    def test_unknown_consent_state_does_not_invent_a_renewal(self):
        decision = validate_restart_request(
            "execution", "planning", completed_stages=[], consent_fresh=None)
        self.assertTrue(decision["allowed"])
        self.assertFalse(decision["consent_renewal_required"])

    def test_guard_never_raises_on_hostile_input(self):
        for current, target, done in (
                (None, None, None), (7, {"a": 1}, ["bogus", None]),
                ("execution", "", ["context"])):
            decision = validate_restart_request(
                current, target, completed_stages=done)
            self.assertIn("allowed", decision)
            self.assertIsInstance(decision["reasons"], list)


class StageJudgmentPolicyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(self.tmp.name + "/ledger.jsonl")
        self.governor = RecordingGovernor()

    def _policy(self, evaluator):
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({
                "jev_api_key": "test-key", "jev_model": "jev-test"})
        return policy_for(settings, transport=None, governor=self.governor,
                          ledger=self.ledger, evaluator=evaluator)

    def test_context_intake_keys_signals_and_settles_exactly_once(self):
        evaluator = FakeEvaluator({
            "context_relevant": noul(0.91),
            "context_coverage_sufficient": noul(0.62),
            "context_conflict_present": noul(0.05)})
        result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "audit the ledger"})
        self.assertTrue(structural["native"])
        self.assertEqual(structural["dimension"], "context_intake")
        self.assertEqual(structural["capability"], HOURGLASS_STAGE_SITE)
        self.assertEqual(structural["pack_id"], HOURGLASS_STAGE_PACK_ID)
        self.assertEqual(structural["pack_version"], HOURGLASS_STAGE_PACK_VERSION)
        self.assertAlmostEqual(structural["context_relevant"], 0.91)
        self.assertAlmostEqual(structural["context_conflict_present"], 0.05)
        self.assertEqual(structural["declared_signals"],
                         list(HOURGLASS_STAGE_DIMENSIONS["context_intake"]["signals"]))
        # One dispatch, one reservation, one settlement, one event.
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(len(self.governor.reservations), 1)
        self.assertEqual(len(self.governor.settlements), 1)
        events = self.ledger.entries()
        self.assertEqual([e["event"] for e in events], ["jev_eval"])
        self.assertEqual(events[0]["site"], HOURGLASS_STAGE_SITE)
        self.assertEqual(events[0]["result_state"], "judged")
        self.assertEqual(events[0]["dimension"], "context_intake")
        self.assertEqual(events[0]["capability"], HOURGLASS_STAGE_SITE)
        self.assertEqual(result.cost, 0.0001)
        # Metadata only: no payload, no key.
        self.assertNotIn("payload", events[0])
        self.assertNotIn("api_key", events[0])

    def test_restart_target_yields_only_a_declared_key(self):
        evaluator = FakeEvaluator({"restart_target": choice("planning")})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "restart_target", {"request": "finish the migration"})
        self.assertTrue(structural["native"])
        self.assertEqual(structural["restart_target"]["target"], "planning")
        # The recommendation is not an action: the caller must still validate.
        self.assertNotIn("allowed", structural)

    def test_out_of_vocabulary_choice_is_reported_not_snapped(self):
        evaluator = FakeEvaluator({"restart_target": choice("frontier")})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "restart_target", {"request": "x"})
        self.assertFalse(structural["native"])
        self.assertIsNone(structural["restart_target"])
        self.assertEqual(self.ledger.entries()[0]["result_state"], "unavailable")

    def test_partial_answers_are_all_or_nothing(self):
        evaluator = FakeEvaluator({"context_relevant": noul(0.9)})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "x"})
        self.assertFalse(structural["native"])
        # A half-answered dimension is unavailable, not partially believed.
        for key in HOURGLASS_STAGE_DIMENSIONS["context_intake"]["signals"]:
            self.assertIsNone(structural[key])

    def test_out_of_range_and_non_numeric_signals_are_refused(self):
        for bad in ({"context_relevant": noul(1.4)},
                    {"context_relevant": noul(-0.2)},
                    {"context_relevant": noul("yes")},
                    {"context_relevant": noul(True)},
                    {"context_relevant": {"type": "noul"}}):
            evaluator = FakeEvaluator(dict(bad))
            _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
                "context_intake", {"request": "x"})
            self.assertFalse(structural["native"], bad)
            self.assertIsNone(structural["context_relevant"], bad)

    def test_malformed_live_response_is_never_native_but_still_settled(self):
        # A billed response whose answer cannot be used: the choice carries no
        # usable confidence, so the dimension is unavailable -- but the call
        # was still made and its real cost is settled, not hidden.
        evaluator = FakeEvaluator(
            {"restart_target": {"type": "choice", "choice": "planning"}},
            discard=True, cost=0.0002, input_tokens=900)
        result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "restart_target", {"request": "x"})
        self.assertFalse(structural["native"])
        self.assertIsNone(structural["restart_target"])
        self.assertEqual(len(self.governor.settlements), 1)
        self.assertAlmostEqual(self.governor.settlements[0][1], 0.0002)
        self.assertEqual(result.input_tokens, 900)
        event = self.ledger.entries()[0]
        self.assertEqual(event["result_state"], "unavailable")
        self.assertEqual(event["fallback_state"], "invalid")

    def test_a_discarded_call_is_marked_on_the_structural_envelope(self):
        evaluator = FakeEvaluator(
            {"execution_suitable": noul(0.9), "checkpoint_required": noul(0.0)},
            discard=True)
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "execution", {"package": "edit"})
        self.assertTrue(structural["discarded"])

    def test_unkeyed_is_all_none_and_still_ledgered(self):
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({})
        policy = policy_for(settings, transport=None,
                            governor=self.governor, ledger=self.ledger,
                            evaluator=FakeEvaluator({}, api_key=None))
        _result, structural = policy.evaluate_hourglass_stage(
            "consent", {"assignment": "apply the patch"})
        self.assertFalse(structural["native"])
        for key in HOURGLASS_STAGE_DIMENSIONS["consent"]["signals"]:
            self.assertIsNone(structural[key])
        self.assertEqual(self.governor.reservations, [])
        self.assertEqual(self.ledger.entries()[0]["result_state"], "unavailable")

    def test_transport_failure_is_all_none_and_ledgered_once(self):
        evaluator = FakeEvaluator({}, error=HarnessError("transport refused"))
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "execution", {"package": "edit 3 files"})
        self.assertFalse(structural["native"])
        self.assertIsNone(structural["execution_suitable"])
        self.assertEqual(len(self.ledger.entries()), 1)
        # The failed attempt is settled at zero, never left outstanding.
        self.assertEqual(self.governor.settlements[-1][1], 0.0)

    def test_no_signal_is_ever_a_completion_or_readiness_claim(self):
        evaluator = FakeEvaluator({
            "plan_sound": noul(1.0), "plan_evidence_requested": noul(0.0)})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "plan_soundness", {"plan": "step 1, step 2"})
        import json
        serialized = json.dumps(structural, default=str)
        for forbidden in ("can_mark_complete", "readiness", "phase_status",
                          "complete"):
            self.assertNotIn(forbidden, serialized)

    def test_consent_dimension_exposes_freshness_defer_and_escalation(self):
        evaluator = FakeEvaluator({
            "consent_fresh": noul(0.2),
            "consent_defer_required": noul(0.8),
            "escalation_justified": noul(0.6)})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "consent", {"assignment": "rewrite the ledger"})
        self.assertTrue(structural["native"])
        self.assertAlmostEqual(structural["consent_fresh"], 0.2)
        self.assertAlmostEqual(structural["consent_defer_required"], 0.8)
        self.assertAlmostEqual(structural["escalation_justified"], 0.6)


class StageJudgmentRequirementTests(unittest.TestCase):
    """HV-1: "avoid redundant calls when no decision is needed".

    The guard is code-owned and declared as data, so the rule lives with the
    pack instead of being re-implemented per caller. Two properties matter and
    are pinned separately below: a suppressed call must cost nothing at all,
    and a suppression must never be mistakable for a judgment.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(self.tmp.name + "/ledger.jsonl")
        self.governor = RecordingGovernor()

    def _policy(self, evaluator):
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({
                "jev_api_key": "test-key", "jev_model": "jev-test"})
        return policy_for(settings, transport=None, governor=self.governor,
                          ledger=self.ledger, evaluator=evaluator)

    def test_default_requires_a_judgment_for_every_declared_dimension(self):
        # Permissive by default: a caller that knows nothing about its own
        # state must still be judged, or the guard would silently disable
        # every integration.
        for name in HOURGLASS_STAGE_DIMENSIONS:
            with self.subTest(dimension=name):
                need = stage_judgment_requirement(name)
                self.assertTrue(need["required"], name)
                self.assertEqual(need["disposition"], "call", name)
                self.assertIsNone(need["code_owned_fact"], name)

    def test_each_code_owned_fact_suppresses_and_names_itself(self):
        for name in HOURGLASS_STAGE_DIMENSIONS:
            for kwargs in ({"subject_supplied": False}, {"superseded": True}):
                with self.subTest(dimension=name, **kwargs):
                    need = stage_judgment_requirement(name, **kwargs)
                    self.assertFalse(need["required"])
                    self.assertEqual(need["disposition"], "skipped")
                    self.assertEqual(need["dimension"], name)
                    # The responsible fact is named, so a skip is never silent.
                    self.assertTrue(need["reason"].strip())
                    self.assertTrue(need["code_owned_fact"])

    def test_requirements_are_declared_for_exactly_the_five_dimensions(self):
        # The declared set and the dimension set must not drift; a name in one
        # and not the other would make a dimension unaskable or undeclared.
        self.assertEqual(set(HOURGLASS_STAGE_REQUIREMENTS),
                         set(HOURGLASS_STAGE_DIMENSIONS))
        for name, spec in HOURGLASS_STAGE_REQUIREMENTS.items():
            self.assertEqual(spec["dimension"], name)
            self.assertTrue(spec["description"], name)
            self.assertTrue(spec["subject"], name)

    def test_an_unknown_dimension_is_refused_not_guarded(self):
        # The guard must not become a softer door around the pack's own
        # refusal: an undeclared dimension is still a hard error.
        for bad in (None, "", "graph_neural", 7, ["execution"]):
            with self.assertRaises(ValueError):
                stage_judgment_requirement(bad)

    def test_a_suppressed_call_dispatches_nothing_and_costs_nothing(self):
        # The whole point: no preflight reservation, no dispatch, no
        # settlement. A guard that still reserved would not have saved a call.
        evaluator = FakeEvaluator({"context_relevant": noul(0.9)})
        result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "audit the ledger"},
            superseded=True)
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(self.governor.reservations, [])
        self.assertEqual(self.governor.settlements, [])
        self.assertEqual(self.ledger.entries(), [])
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(structural["cost"], 0.0)
        self.assertFalse(structural["dispatched"])

    def test_a_suppressed_call_is_never_a_judgment(self):
        # A skip reports the skip. It must not be readable as a pass, a fail,
        # or a native signal, and it must not be silently absent either.
        evaluator = FakeEvaluator({"context_relevant": noul(0.9)})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "x"}, subject_supplied=False)
        self.assertFalse(structural["judgment_required"])
        self.assertEqual(structural["result_state"], "not_required")
        self.assertFalse(structural["native"])
        self.assertTrue(structural["skip_reason"].strip())
        for key in HOURGLASS_STAGE_DIMENSIONS["context_intake"]["signals"]:
            self.assertIsNone(structural[key], key)

    def test_a_suppressed_call_makes_no_completion_or_readiness_claim(self):
        # The skip shares the "never a completion claim" rule with every other
        # outcome: a dimension that was not asked says nothing at all.
        import json
        evaluator = FakeEvaluator({"context_relevant": noul(0.9)})
        _result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "x"}, superseded=True)
        serialized = json.dumps(structural, default=str)
        for forbidden in ("can_mark_complete", "readiness", "phase_status",
                          "complete"):
            self.assertNotIn(forbidden, serialized)

    def test_a_required_judgment_is_dispatched_and_ledgered_normally(self):
        # The other half of the contract: suppressing is opt-in, and the
        # ordinary path is unchanged -- one call, one event, real cost.
        evaluator = FakeEvaluator({
            "context_relevant": noul(0.91),
            "context_coverage_sufficient": noul(0.62),
            "context_conflict_present": noul(0.05)})
        result, structural = self._policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "audit the ledger"},
            subject_supplied=True, superseded=False)
        self.assertTrue(structural["judgment_required"])
        self.assertTrue(structural["dispatched"])
        self.assertTrue(structural["native"])
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual([e["event"] for e in self.ledger.entries()],
                         ["jev_eval"])
        self.assertEqual(result.cost, 0.0001)

    def test_suppression_cannot_mask_an_unkeyed_run(self):
        # Unkeyed already fails closed. The guard must not let a caller turn
        # an unjudgeable run into a clean-looking "not required": a judgment
        # WAS required here, it simply could not be made. That is a different
        # fact from a suppressed call and must stay distinguishable.
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({"jev_api_key": None})
        policy = policy_for(settings, transport=None,
                            governor=self.governor, ledger=self.ledger,
                            evaluator=FakeEvaluator({}))
        result, structural = policy.evaluate_hourglass_stage(
            "context_intake", {"request": "x"})
        self.assertTrue(structural["judgment_required"])
        self.assertFalse(structural["dispatched"])
        self.assertTrue(result.is_fallback)
        self.assertNotEqual(structural.get("result_state"), "not_required")
        # Unavailable, not suppressed: the three outcomes never alias.
        self.assertNotIn("skip_reason", structural)


if __name__ == "__main__":
    unittest.main()
