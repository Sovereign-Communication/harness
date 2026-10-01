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
import copy
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
    HOURGLASS_STAGE_REQUIREMENTS,
    HOURGLASS_SUPPRESSION_FACTS,
    normalize_restart_target,
    stage_judgment_requirement,
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


class StageRequirementDeclarationTests(unittest.TestCase):
    """HV-1: "avoid redundant calls when no decision is needed".

    The guard is CODE-OWNED and declared as data, so the rule lives with the
    pack instead of being re-implemented per caller. Two properties matter and
    are pinned separately below: a suppressed call must cost nothing at all,
    and a suppression must never be mistakable for a judgment.
    """

    def test_requirements_table_is_derived_from_the_declared_dimensions(self):
        """One source of truth: a caller may not add a dimension to the
        requirement table that the pack does not declare, or vice versa."""
        self.assertEqual(set(HOURGLASS_STAGE_REQUIREMENTS),
                         set(HOURGLASS_STAGE_DIMENSIONS))
        for name, spec in HOURGLASS_STAGE_REQUIREMENTS.items():
            self.assertEqual(spec["dimension"], name)
            self.assertEqual(spec["signals"],
                             tuple(HOURGLASS_STAGE_DIMENSIONS[name]["signals"]))
            self.assertTrue(spec["description"].strip(), name)
            self.assertTrue(spec["subject"].strip(), name)

    def test_the_default_is_required(self):
        """Conservative by construction: a caller that knows nothing about its
        own state still gets judged. A guard that defaulted to skipping would
        make every integration silently inert."""
        for name in HOURGLASS_STAGE_DIMENSIONS:
            decision = stage_judgment_requirement(name)
            self.assertTrue(decision["required"], name)
            self.assertEqual(decision["disposition"], "call", name)
            self.assertIsNone(decision["code_owned_fact"], name)
            self.assertTrue(decision["reason"].strip(), name)

    def test_each_code_owned_fact_suppresses_and_explains_itself(self):
        """A suppressed call must name the fact responsible, so it is never a
        silent no-op."""
        for name in HOURGLASS_STAGE_DIMENSIONS:
            for kwargs, fact in (({"subject_supplied": False},
                                  "subject_supplied"),
                                 ({"superseded": True}, "superseded")):
                decision = stage_judgment_requirement(name, **kwargs)
                self.assertFalse(decision["required"], (name, kwargs))
                self.assertEqual(decision["disposition"], "skipped", name)
                self.assertEqual(decision["fact"], fact, name)
                self.assertTrue(decision["code_owned_fact"].strip(), name)
                # The reported prose must BE the declared prose for that fact,
                # so the table cannot drift from the behaviour.
                self.assertIn(decision["fact"], HOURGLASS_SUPPRESSION_FACTS)
                self.assertEqual(decision["reason"],
                                 HOURGLASS_SUPPRESSION_FACTS[decision["fact"]])

    def test_an_unknown_dimension_is_refused_not_invented(self):
        """Same refusal as the question pack: no caller may invent an
        integration just to reach the skip path."""
        for bad in (None, "", "graph_neural", 7, ["execution"]):
            with self.assertRaises(ValueError):
                stage_judgment_requirement(bad)
            with self.assertRaises(ValueError):
                stage_judgment_requirement(bad, subject_supplied=False)

    def test_the_guard_is_pure(self):
        """Same inputs, same decision, and it never mutates the declared
        tables -- it is data the policy reads, not a state machine."""
        first = stage_judgment_requirement("execution", superseded=True)
        second = stage_judgment_requirement("execution", superseded=True)
        self.assertEqual(first, second)
        before = copy.deepcopy(HOURGLASS_STAGE_REQUIREMENTS)
        stage_judgment_requirement("consent", subject_supplied=False)
        self.assertEqual(HOURGLASS_STAGE_REQUIREMENTS, before)


class StageRequirementPolicyTests(unittest.TestCase):
    """The policy's side: a suppressed call spends nothing, and every
    non-dispatched exit is honestly labelled."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(self.tmp.name + "/ledger.jsonl")
        self.governor = RecordingGovernor()

    def _keyed_policy(self, evaluator):
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({
                "jev_api_key": "test-key", "jev_model": "jev-test"})
        return policy_for(settings, transport=None, governor=self.governor,
                          ledger=self.ledger, evaluator=evaluator)

    def _unkeyed_policy(self, evaluator):
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({})
        return policy_for(settings, transport=None,
                          governor=self.governor, ledger=self.ledger,
                          evaluator=evaluator)

    def _good_answers(self, dimension):
        return {key: noul(0.9) for key in
                HOURGLASS_STAGE_DIMENSIONS[dimension]["signals"]}

    def test_a_suppressed_call_reserves_nothing_and_spends_nothing(self):
        """The whole point of the guard: no reservation, no dispatch, no
        settlement, and no ledger event, because nothing was evaluated."""
        evaluator = FakeEvaluator(self._good_answers("execution"))
        result, structural = self._keyed_policy(evaluator).evaluate_hourglass_stage(
            "execution", {"package": "edit"}, subject_supplied=False)
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(self.governor.reservations, [])
        self.assertEqual(self.governor.settlements, [])
        self.assertEqual(self.ledger.entries(), [])
        self.assertFalse(structural["dispatched"])
        self.assertEqual(structural["cost"], 0.0)
        self.assertEqual(result.cost, 0.0)
        # A skip is not a fallback and not a judgment.
        self.assertFalse(structural["judgment_required"])
        self.assertFalse(structural["native"])
        self.assertFalse(result.is_fallback)

    def test_a_suppressed_call_reports_every_signal_as_none(self):
        """It must not manufacture a passing or failing judgment."""
        for name, spec in HOURGLASS_STAGE_DIMENSIONS.items():
            _result, structural = self._keyed_policy(
                FakeEvaluator({})).evaluate_hourglass_stage(
                    name, {"request": "x"}, superseded=True)
            self.assertEqual(structural["result_state"], "not_required", name)
            for key in spec["signals"]:
                self.assertIsNone(structural[key], (name, key))
            self.assertTrue(structural["skip_reason"].strip(), name)
            self.assertTrue(structural["code_owned_fact"], name)
            # Identity is still the declared one, so a skip is traceable.
            self.assertEqual(structural["dimension"], name)
            self.assertEqual(structural["pack_id"], HOURGLASS_STAGE_PACK_ID)
            self.assertEqual(structural["capability"], HOURGLASS_STAGE_SITE)

    def test_every_dimension_can_be_suppressed(self):
        """The guard is declared for all five, not just the one exercised."""
        for name in HOURGLASS_STAGE_DIMENSIONS:
            for kwargs in ({"subject_supplied": False}, {"superseded": True}):
                evaluator = FakeEvaluator({})
                _result, structural = self._keyed_policy(evaluator).evaluate_hourglass_stage(
                    name, {"request": "x"}, **kwargs)
                self.assertEqual(evaluator.calls, [], (name, kwargs))
                self.assertFalse(structural["dispatched"], (name, kwargs))

    def test_the_default_path_still_dispatches(self):
        """Regression floor: the guard is additive. With no suppression fact
        the behaviour is exactly what it was before, including one dispatch
        and one settlement."""
        evaluator = FakeEvaluator(self._good_answers("context_intake"))
        _result, structural = self._keyed_policy(evaluator).evaluate_hourglass_stage(
            "context_intake", {"request": "audit"})
        self.assertEqual(len(evaluator.calls), 1)
        self.assertTrue(structural["dispatched"])
        self.assertTrue(structural["judgment_required"])
        self.assertTrue(structural["native"])
        self.assertAlmostEqual(structural["context_relevant"], 0.9)
        self.assertNotIn("skip_reason", structural)
        self.assertEqual(
            [e["event"] for e in self.ledger.entries()], ["jev_eval"])

    def test_unkeyed_required_a_judgment_it_could_not_make(self):
        """The honest envelope: a judgment WAS required and could not be
        made. That is a different fact from "not required", and the two must
        never alias -- otherwise a caller could read an unavailable run as a
        clean skip."""
        _result, structural = self._unkeyed_policy(
            FakeEvaluator({}, api_key=None)).evaluate_hourglass_stage(
                "consent", {"assignment": "apply the patch"})
        self.assertTrue(structural["judgment_required"])
        self.assertFalse(structural["dispatched"])
        # result_state is what distinguishes the three outcomes, and the skip
        # path is the only one allowed to claim "not_required".
        self.assertEqual(structural["result_state"], "unavailable")
        self.assertNotIn("skip_reason", structural)
        self.assertEqual(self.governor.reservations, [])
        # The unavailable state is still recorded, as it always was, and the
        # envelope agrees with the ledger rather than restating it.
        self.assertEqual(self.ledger.entries()[0]["result_state"],
                         structural["result_state"])

    def test_a_refused_call_is_also_honestly_labelled(self):
        """The pre-dispatch refusal path must not claim it dispatched, and the
        reconciliation it does perform must charge nothing."""
        evaluator = FakeEvaluator({}, error=HarnessError("budget refused"))
        _result, structural = self._keyed_policy(evaluator).evaluate_hourglass_stage(
            "plan_soundness", {"plan": "step"})
        self.assertTrue(structural["judgment_required"])
        self.assertFalse(structural["dispatched"])
        self.assertTrue(self.governor.reservations)
        self.assertTrue(self.governor.settlements)
        for _token, amount in self.governor.settlements:
            self.assertEqual(amount, 0.0)

    def test_the_three_outcomes_never_alias(self):
        """skip / required-but-unavailable / judged are three distinct facts,
        distinguishable on the envelope alone."""
        seen = {}
        _r, seen["skip"] = self._keyed_policy(FakeEvaluator({})).evaluate_hourglass_stage(
            "execution", {"p": 1}, superseded=True)
        _r, seen["unavailable"] = self._unkeyed_policy(
            FakeEvaluator({}, api_key=None)).evaluate_hourglass_stage(
                "execution", {"p": 1})
        _r, seen["judged"] = self._keyed_policy(
            FakeEvaluator(self._good_answers("execution"))).evaluate_hourglass_stage(
                "execution", {"p": 1})
        self.assertEqual(seen["skip"]["result_state"], "not_required")
        self.assertEqual(seen["unavailable"]["result_state"], "unavailable")
        self.assertEqual(seen["judged"]["result_state"], "judged")
        # The three states are pairwise distinct, so a caller can branch on
        # the envelope alone without consulting the ledger.
        self.assertEqual(
            len({seen[k]["result_state"]
                 for k in ("skip", "unavailable", "judged")}), 3)
        self.assertFalse(seen["skip"]["judgment_required"])
        self.assertTrue(seen["unavailable"]["judgment_required"])
        self.assertTrue(seen["judged"]["judgment_required"])
        self.assertEqual(
            [seen[k]["dispatched"] for k in ("skip", "unavailable", "judged")],
            [False, False, True])
        # Only the skip may claim a suppression reason.
        self.assertIn("skip_reason", seen["skip"])
        self.assertNotIn("skip_reason", seen["unavailable"])
        self.assertNotIn("skip_reason", seen["judged"])


class StageCallingLanesIntegrationTests(unittest.TestCase):
    """Pin that the 5 declared HV-1 stage dimensions are genuinely reached
    from the production calling lanes (waist context intake, composition,
    and agent stage execution/restart/consent), with honest degradation."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(str(Path(self.tmp.name) / "ledger.jsonl"))
        self.governor = RecordingGovernor()

    def _policy(self, evaluator):
        settings = load_settings({
            "jev_api_key": "test-key",
            "jev_model": "jev-test",
        })
        return policy_for(settings, transport=None, governor=self.governor,
                          ledger=self.ledger, evaluator=evaluator)

    def test_intake_brief_invokes_context_intake_dimension(self):
        from harness.waist import intake_brief
        from pathlib import Path
        f = Path(self.tmp.name) / "sample.py"
        f.write_text("x = 1\n", encoding="utf-8")
        evaluator = FakeEvaluator({
            "context_relevant": noul(0.95),
            "context_coverage_sufficient": noul(0.85),
            "context_conflict_present": noul(0.02),
        })
        policy = self._policy(evaluator)
        intake = intake_brief("refactor sample.py", [str(f)], jev_policy=policy)
        self.assertIsNotNone(intake.get("judgment"))
        self.assertEqual(intake["judgment"]["dimension"], "context_intake")
        self.assertTrue(intake["judgment"]["native"])
        self.assertAlmostEqual(intake["judgment"]["signals"]["context_relevant"], 0.95)
        self.assertAlmostEqual(intake["judgment"]["signals"]["context_coverage_sufficient"], 0.85)

    def test_intake_brief_degrades_gracefully_when_unkeyed(self):
        from harness.waist import intake_brief
        from pathlib import Path
        f = Path(self.tmp.name) / "sample.py"
        f.write_text("x = 1\n", encoding="utf-8")
        evaluator = FakeEvaluator({}, api_key=None)
        settings = load_settings({"jev_api_key": None})
        policy = policy_for(settings, transport=None, governor=self.governor,
                            ledger=self.ledger, evaluator=evaluator)
        intake = intake_brief("refactor sample.py", [str(f)], jev_policy=policy)
        self.assertIsNotNone(intake.get("judgment"))
        self.assertFalse(intake["judgment"]["native"])
        self.assertEqual(intake["judgment"]["signals"], {})

    def test_compose_run_stages_evaluates_selected_context_and_execution(self):
        from harness.agent import AutonomousAgent
        from pathlib import Path
        evaluator = FakeEvaluator({
            "context_relevant": noul(0.95),
            "context_coverage_sufficient": noul(0.85),
            "context_conflict_present": noul(0.02),
            "execution_suitable": noul(0.90),
            "checkpoint_required": noul(0.10),
            "restart_target": choice("execution"),
            "consent_fresh": noul(0.92),
            "consent_defer_required": noul(0.05),
            "escalation_justified": noul(0.10),
        })
        policy = self._policy(evaluator)
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path(self.tmp.name))
        plan = {
            "composition": {
                "stages": [
                    {"stage": "context", "state": "pending"},
                    {"stage": "execution", "state": "pending"},
                ]
            },
            "nodes": [{"node_id": "n1", "instruction": "edit sample.py", "target_files": ["sample.py"]}],
        }
        envelope, _, judgments = agent._compose_run_stages(
            "refactor sample.py", ["sample.py"], jev_policy=policy, plan=plan)
        self.assertIn("context", judgments)
        self.assertIn("execution", judgments)
        self.assertTrue(judgments["context"]["native"])
        self.assertTrue(judgments["execution"]["native"])
        self.assertIn("consent", judgments["execution"])
        self.assertTrue(judgments["execution"]["consent"]["native"])
        self.assertAlmostEqual(judgments["execution"]["consent"]["signals"]["consent_fresh"], 0.92)

    def test_restart_decision_forces_renewal_when_consent_stale(self):
        from harness.agent import AutonomousAgent
        from pathlib import Path
        evaluator = FakeEvaluator({
            "execution_suitable": noul(0.90),
            "checkpoint_required": noul(0.10),
            "restart_target": choice("planning"),
            "consent_fresh": noul(0.30),  # Stale!
            "consent_defer_required": noul(0.80),
            "escalation_justified": noul(0.50),
        })
        policy = self._policy(evaluator)
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path(self.tmp.name))
        decision = agent._restart_decision("refactor sample.py", policy, {}, completed_stages=())
        self.assertTrue(decision["consent_renewal_required"])
        self.assertIn("consent no longer covers this assignment", " ".join(decision["reasons"]))

    def test_intake_brief_handles_non_tuple_and_exception(self):
        from harness.waist import intake_brief
        from unittest.mock import MagicMock
        from pathlib import Path
        f = Path(self.tmp.name) / "sample.py"
        f.write_text("x = 1\n", encoding="utf-8")

        # Non-tuple return with structural attribute
        mock_policy = MagicMock()
        class NonTupleRes:
            structural = {
                "context_relevant": 0.88,
                "context_coverage_sufficient": 0.77,
                "native": True,
            }
        mock_policy.evaluate_hourglass_stage.return_value = NonTupleRes()
        intake = intake_brief("test", [str(f)], jev_policy=mock_policy)
        self.assertTrue(intake["judgment"]["native"])
        self.assertAlmostEqual(intake["judgment"]["signals"]["context_relevant"], 0.88)

        # Exception raised
        mock_policy.evaluate_hourglass_stage.side_effect = HarnessError("eval error")
        intake_exc = intake_brief("test", [str(f)], jev_policy=mock_policy)
        self.assertFalse(intake_exc["judgment"]["native"])
        self.assertEqual(intake_exc["judgment"]["signals"], {})

    def test_compose_plan_context_stage_exception_handling(self):
        from harness.waist import compose_plan
        from harness.token_budget import TokenBudget
        from unittest.mock import MagicMock
        from pathlib import Path
        f = Path(self.tmp.name) / "sample.py"
        f.write_text("x = 1\n", encoding="utf-8")
        mock_policy = MagicMock()
        mock_policy.evaluate_hourglass_stage.side_effect = HarnessError("plan eval error")
        budget = TokenBudget(max_input_tokens=10000, max_output_tokens=1000)
        plan = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="refactor", candidate_files=[str(f)],
            token_budget=budget, stages=["context"],
            jev_policy=mock_policy)
        self.assertIn("stage_judgments", plan)
        self.assertIn("context", plan["stage_judgments"])
        self.assertFalse(plan["stage_judgments"]["context"]["native"])
        self.assertEqual(plan["stage_judgments"]["context"]["signals"], {})

    def test_agent_ask_stage_dimension_non_tuple_return(self):
        from harness.agent import AutonomousAgent
        from unittest.mock import MagicMock
        from pathlib import Path
        agent = AutonomousAgent(settings=load_settings(), root_dir=Path(self.tmp.name))
        mock_policy = MagicMock()
        class NonTupleRes:
            structural = {
                "execution_suitable": 0.95,
                "checkpoint_required": 0.05,
                "native": True,
            }
        mock_policy.evaluate_hourglass_stage.return_value = NonTupleRes()
        res = agent._ask_stage_dimension("execution", {}, jev_policy=mock_policy)
        self.assertTrue(res["native"])
        self.assertAlmostEqual(res["signals"]["execution_suitable"], 0.95)


if __name__ == "__main__":
    unittest.main()
