"""Hermetic tests for the seven HV-1 Hourglass JEV capabilities."""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult, jev_cost
from harness.jev_packs import (
    HOURGLASS_JEV_INTEGRATION_MATRIX,
    HOURGLASS_JEV_RESTART_CHOICES,
    hourglass_jev_blocking_facts,
    hourglass_jev_decision_evidence,
    hourglass_jev_evidence_refs,
    hourglass_jev_integration_matrix,
    hourglass_jev_legal_restart_targets,
    hourglass_jev_preflight,
    hourglass_jev_question_pack,
    prepare_hourglass_restart,
    validate_hourglass_jev_answers,
    validate_hourglass_jev_state,
)
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.tokens import estimate_prompt_tokens


_CONTEXT_FACTS = {
    "source_identity_verified": True,
    "coverage_scope_declared": True,
}
_PLANNING_FACTS = {
    "target_paths_valid": True,
    "dependency_graph_valid": True,
    "limits_within_bounds": True,
    "requirements_enumerated": True,
}
_EXECUTION_FACTS = {
    "target_paths_valid": True,
    "limits_within_bounds": True,
    "consent_valid": True,
    "checkpoint_evidence_verified": True,
}
_CONSENT_FACTS = {
    "signature_valid": True,
    "not_expired": True,
    "assignment_matches": True,
    "limits_match": True,
}
_FINAL_FACTS = {
    "independent_verification_passed": True,
    "named_artifacts_present": True,
    "requirements_enumerated": True,
    "evidence_references_verified": True,
}
_RESTART_FACTS = {
    "completed_work_verified": True,
    "evidence_provenance_verified": True,
    "budget_history_verified": True,
}
_CLAIM_FACTS = {"evidence_references_verified": True}


def _states():
    return {
        "context_intake": {
            "goal": "Explain the feature",
            "source_candidates": [
                {"id": "src-a", "source_ref": "docs/a.md", "summary": "Primary source"},
                {"id": "src-b", "source_ref": "docs/b.md", "summary": "Related source"},
            ],
            "coverage_scope": [{"id": "scope-main", "description": "Feature behavior"}],
            "conflict_candidates": [{
                "id": "conflict-a", "description": "Different stated behavior",
                "source_a_ref": "docs/a.md", "source_b_ref": "docs/b.md",
            }],
            "code_facts": dict(_CONTEXT_FACTS),
        },
        "planning_waist": {
            "goal": "Ship the bounded change",
            "requirements": [{"id": "req-a", "description": "Preserve compatibility"}],
            "plan_steps": [
                {"id": "inspect", "description": "Inspect behavior",
                 "depends_on": [], "target_paths": ["harness/example.py"],
                 "evidence_refs": ["docs/design.md"]},
                {"id": "test", "description": "Add focused tests",
                 "depends_on": ["inspect"], "target_paths": ["tests/test_example.py"]},
            ],
            "evidence_candidates": [{
                "id": "design", "source_ref": "docs/design.md", "description": "Design source",
            }],
            "code_facts": dict(_PLANNING_FACTS),
        },
        "execution_checkpoint": {
            "work_package": {
                "id": "pkg-a", "goal": "Implement one bounded change",
                "steps": [
                    {"id": "step-a", "description": "Change code"},
                    {"id": "step-b", "description": "Run tests"},
                ],
            },
            "checkpoint": {
                "id": "checkpoint-a", "summary": "Code changed and tests started",
                "completed_step_ids": ["step-a"], "evidence_refs": ["git:change-a"],
            },
            "code_facts": dict(_EXECUTION_FACTS),
        },
        "consent_signals": {
            "assignment": {
                "id": "assign-a", "summary": "Bounded code change",
                "context_ref": "context:abc", "limits_ref": "limits:abc",
            },
            "consent": {
                "assignment_id": "assign-a", "summary": "Approved bounded change",
                "context_ref": "context:abc", "limits_ref": "limits:abc",
            },
            "code_facts": dict(_CONSENT_FACTS),
        },
        "final_alignment": {
            "original_request": "Implement the requested feature without changing its contract.",
            "retained_source_context": [{
                "id": "src-a", "source_ref": "docs/spec.md",
                "content": "The uncondensed relevant source content.",
            }],
            "requirements": [{
                "id": "req-a", "description": "Preserve the original contract.",
                "evidence_refs": ["verification:test-a"],
            }],
            "completed_work": [{
                "id": "work-a", "description": "Implemented and tested the feature.",
                "evidence_refs": ["git:commit-a"],
            }],
            "evidence_refs": ["docs/spec.md", "verification:test-a", "git:commit-a"],
            "code_facts": dict(_FINAL_FACTS),
        },
        "restart_target": {
            "current_stage": "verification",
            "original_request": "Complete the original feature.",
            "unmet_requirements": [],
            "retained_source_context": [{
                "id": "src-a", "source_ref": "docs/spec.md", "content": "Source content retained intact.",
            }],
            "completed_work": [{
                "id": "work-a", "description": "Completed and independently verified.",
                "evidence_refs": ["verification:work-a"],
            }],
            "remaining_work": [],
            "evidence_refs": ["docs/spec.md", "verification:work-a"],
            "provenance_refs": ["git:commit-a"],
            "budget_history": [{
                "stage": "execution", "usage_source": "actual",
                "input_tokens": 100, "output_tokens": 30, "cost_usd": 0.0001,
            }],
            "current_limits": {"max_input_tokens": 2048, "max_cost_usd": 0.05},
            "requested_limits": {"max_input_tokens": 1024, "max_cost_usd": 0.05},
            "current_assignment_id": "assign-a",
            "requested_assignment_id": "assign-a",
            "code_facts": dict(_RESTART_FACTS),
        },
        "verification_claim_support": {
            "claims": [{
                "id": "claim-a", "text": "The fix closes the regression.",
                "evidence_refs": ["test:regression"],
            }],
            "evidence_context": "The regression test passes after the fix.",
            "code_facts": dict(_CLAIM_FACTS),
        },
    }


def _typed_answers(questions, *, confidence=0.97, noul=0.91, choice=None):
    answers = {}
    for question_id, question in questions.items():
        kind = question["type"]
        if kind == "noul":
            answers[question_id] = {"type": "noul", "noul": noul}
        elif kind == "choice":
            selected = choice if choice in question["criteria"] else next(iter(question["criteria"]))
            probabilities = {
                key: (1.0 if key == selected else 0.0)
                for key in question["criteria"]
            }
            answers[question_id] = {
                "type": "choice", "choice": selected,
                "probabilities": probabilities, "confidence": confidence,
            }
        else:
            criteria = question["criteria"]
            probabilities = {str(i): (1.0 if i == len(criteria) - 1 else 0.0)
                             for i in range(len(criteria))}
            answers[question_id] = {
                "type": "score", "score": float(len(criteria) - 1),
                "legend": {str(i): value for i, value in enumerate(criteria)},
                "probabilities": probabilities, "confidence": confidence,
            }
    return answers


def _result(questions, *, answers=None, model="jev-test", input_tokens=123,
            output_tokens=17, model_observed=True, input_observed=True,
            output_observed=True, fallback=False, verdict="pass"):
    return JevEvaluationResult(
        verdict, 0.0, 0.0,
        _typed_answers(questions) if answers is None else answers,
        [], cost=jev_cost(input_tokens), input_tokens=input_tokens,
        output_tokens=output_tokens, is_fallback=fallback, model=model,
        usage_observed=input_observed and output_observed,
        model_observed=model_observed,
        input_tokens_observed=input_observed,
        output_tokens_observed=output_observed)


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def post_once(self, url, key, payload, timeout=120):
        self.calls.append((url, key, copy.deepcopy(payload)))
        return 200, {"ok": True}

    def post(self, *args, **kwargs):
        raise AssertionError("HV-1 must use the one-attempt evaluator")


class RecordingEvaluator:
    def __init__(self, result=None, error=None, *, api_key="test-key", transport=None):
        self.api_key = api_key
        self.model = "jev-test"
        self.transport = transport or RecordingTransport()
        self.result = result
        self.error = error
        self.calls = []

    def evaluate_once(self, state, questions):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        if self.error:
            raise self.error
        return self.result


class RecordingGovernor:
    def __init__(self, error=None, settlement_error=None):
        self.error = error
        self.settlement_error = settlement_error
        self.reservations = []
        self.settlements = []

    def reserve(self, amount, label):
        if self.error:
            raise self.error
        token = object()
        self.reservations.append((token, amount, label))
        return token

    def reconcile(self, token, amount):
        self.settlements.append((token, amount))
        if self.settlement_error:
            raise self.settlement_error


class HourglassJevIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_number = 0

    def _policy(self, *, evaluator=None, governor=None, ledger=True):
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({
                "jev_api_key": getattr(evaluator, "api_key", "test-key"),
                "jev_model": "jev-test",
            })
        if ledger:
            path = os.path.join(self.tmp.name, "ledger-{}.jsonl".format(self.ledger_number))
            self.ledger_number += 1
            ledger_obj = AutonomyLedger(path)
        else:
            ledger_obj = None
        evaluator = evaluator or RecordingEvaluator(transport=RecordingTransport())
        governor = governor or RecordingGovernor()
        return policy_for(
            settings, transport=evaluator.transport, governor=governor,
            ledger=ledger_obj, evaluator=evaluator), governor, ledger_obj

    def _run(self, capability, state, *, confidence=0.97, answers=None,
             evaluator=None, governor=None, ledger=True, max_input_tokens=8192,
             min_confidence=0.70):
        questions = hourglass_jev_question_pack(capability, state)
        if evaluator is None:
            evaluator = RecordingEvaluator(_result(
                questions, answers=answers, model="jev-observed"))
        policy, governor, ledger_obj = self._policy(
            evaluator=evaluator, governor=governor, ledger=ledger)
        result, decision = policy.evaluate_hourglass(
            capability, state, max_input_tokens=max_input_tokens,
            min_confidence=min_confidence)
        return result, decision, evaluator, governor, ledger_obj

    def test_matrix_declares_exactly_seven_selectable_typed_capabilities(self):
        matrix = hourglass_jev_integration_matrix()
        self.assertEqual(set(matrix), set(_states()))
        self.assertEqual(set(matrix), set(HOURGLASS_JEV_INTEGRATION_MATRIX))
        self.assertEqual(len(matrix), 7)
        for capability, state in _states().items():
            with self.subTest(capability=capability):
                questions = hourglass_jev_question_pack(capability, state)
                evidence = hourglass_jev_decision_evidence(capability, state)
                self.assertTrue(questions)
                self.assertEqual(set(questions), set(evidence))
                self.assertTrue(all(q["type"] in ("noul", "choice", "score")
                                    for q in questions.values()))
                self.assertEqual(matrix[capability]["pack_version"], "1.0.0")

    def test_every_capability_dispatches_once_settles_once_and_records_metadata_only(self):
        for capability, state in _states().items():
            with self.subTest(capability=capability):
                _, decision, evaluator, governor, ledger = self._run(capability, state)
                self.assertEqual(decision["result_state"], "judged")
                self.assertFalse(decision["dispatch_authorized"])
                self.assertEqual(len(evaluator.calls), 1)
                self.assertEqual(len(governor.reservations), 1)
                self.assertEqual(len(governor.settlements), 1)
                token, reserve, label = governor.reservations[0]
                self.assertIs(governor.settlements[0][0], token)
                self.assertEqual(label, "jev:" + HOURGLASS_JEV_INTEGRATION_MATRIX[capability]["site"])
                self.assertEqual(reserve, jev_cost(8192))
                self.assertEqual(evaluator.transport.calls, [])
                rows = ledger.entries()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["event"], "jev_eval")
                self.assertEqual(rows[0]["capability"], capability)
                self.assertEqual(rows[0]["result_state"], "judged")
                self.assertNotIn("payload", rows[0])
                self.assertNotIn("api_key", json.dumps(rows[0]))
                self.assertNotIn("state", rows[0])
                self.assertEqual(decision["answers"], _typed_answers(evaluator.calls[0][1]))
                self.assertEqual(set(decision["decision_evidence"]),
                                 set(evaluator.calls[0][1]))

    def test_preflight_measures_same_payload_and_refuses_before_reservation(self):
        state = _states()["context_intake"]
        measured = hourglass_jev_preflight("context_intake", state, "jev-test", 8192)
        serialized = json.dumps({
            "model": "jev-test", "state": measured["state"],
            "questions": measured["questions"],
        })
        self.assertEqual(measured["payload_utf8_bytes"], len(serialized.encode("utf-8")))
        self.assertEqual(measured["estimated_input_tokens"], estimate_prompt_tokens(serialized))
        self.assertTrue(measured["fits_context"])
        evaluator = RecordingEvaluator(transport=RecordingTransport())
        _, decision, evaluator, governor, ledger = self._run(
            "context_intake", state, evaluator=evaluator, max_input_tokens=1)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "preflight_refused")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(governor.reservations, [])
        self.assertEqual([row["event"] for row in ledger.entries()], ["jev_refusal"])

    def test_failing_code_facts_block_without_dispatch_and_have_distinct_state(self):
        state = _states()["context_intake"]
        state["code_facts"]["source_identity_verified"] = False
        _, decision, evaluator, governor, ledger = self._run("context_intake", state)
        self.assertEqual(decision["result_state"], "blocked_by_code_fact")
        self.assertEqual(decision["fallback_state"], "code_fact_blocked")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(governor.reservations, [])
        self.assertEqual(ledger.entries()[0]["event"], "jev_refusal")
        self.assertEqual(ledger.entries()[0]["result_state"], "blocked_by_code_fact")

    def test_unkeyed_transport_and_preflight_failures_are_unassessed(self):
        state = _states()["context_intake"]
        cases = (
            ("unkeyed", RecordingEvaluator(api_key=None, transport=RecordingTransport()),
             RecordingGovernor(), True),
            ("budget", RecordingEvaluator(transport=RecordingTransport()),
             RecordingGovernor(error=HarnessError("budget denied")), True),
            ("no ledger", RecordingEvaluator(transport=RecordingTransport()),
             RecordingGovernor(), False),
        )
        for label, evaluator, governor, ledger in cases:
            with self.subTest(label=label):
                _, decision, evaluator, governor, ledger_obj = self._run(
                    "context_intake", state, evaluator=evaluator,
                    governor=governor, ledger=ledger)
                self.assertEqual(decision["result_state"], "unassessed")
                self.assertEqual(evaluator.calls, [])
                self.assertEqual(governor.reservations, [])
                if ledger_obj:
                    self.assertEqual(ledger_obj.entries()[0]["event"], "jev_refusal")

    def test_evaluator_error_settles_once_with_estimate_and_never_approves(self):
        transport = RecordingTransport()
        evaluator = RecordingEvaluator(error=OSError("provider failed"), transport=transport)
        _, decision, evaluator, governor, ledger = self._run(
            "context_intake", _states()["context_intake"], evaluator=evaluator)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["usage_source"], "estimated")
        self.assertEqual(decision["cost_source"], "estimated_input")
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(len(governor.reservations), 1)
        self.assertEqual(len(governor.settlements), 1)
        self.assertAlmostEqual(
            governor.settlements[0][1],
            jev_cost(decision["estimated_input_tokens"]))
        self.assertEqual(ledger.entries()[0]["event"], "jev_eval")
        self.assertEqual(ledger.entries()[0]["result_state"], "unassessed")

    def test_malformed_answers_and_unobserved_usage_or_model_are_atomic(self):
        state = _states()["context_intake"]
        questions = hourglass_jev_question_pack("context_intake", state)
        valid = _typed_answers(questions)
        malformed = copy.deepcopy(valid)
        malformed.pop("source_0_relevant")
        cases = (
            ("malformed", _result(questions, answers=malformed)),
            ("model", _result(questions, model_observed=False)),
            ("input usage", _result(questions, input_observed=False)),
            ("output usage", _result(questions, output_observed=False)),
            ("fallback", _result(questions, fallback=True)),
        )
        for label, raw in cases:
            with self.subTest(label=label):
                evaluator = RecordingEvaluator(raw, transport=RecordingTransport())
                result, decision, evaluator, governor, ledger = self._run(
                    "context_intake", state, evaluator=evaluator)
                self.assertEqual(decision["result_state"], "unassessed")
                self.assertEqual(decision["answers"], {})
                self.assertEqual(result.answers, {})
                self.assertEqual(len(evaluator.calls), 1)
                self.assertEqual(len(governor.reservations), 1)
                self.assertEqual(len(governor.settlements), 1)
                self.assertEqual(len(ledger.entries()), 1)
                self.assertEqual(ledger.entries()[0]["event"], "jev_eval")

    def test_partial_usage_is_settled_once_and_usage_source_is_explicit(self):
        state = _states()["context_intake"]
        questions = hourglass_jev_question_pack("context_intake", state)
        for label, input_observed, output_observed in (
                ("input only", True, False), ("output only", False, True)):
            with self.subTest(label=label):
                raw = _result(
                    questions, input_observed=input_observed,
                    output_observed=output_observed)
                evaluator = RecordingEvaluator(raw, transport=RecordingTransport())
                _, decision, _, governor, ledger = self._run(
                    "context_intake", state, evaluator=evaluator)
                self.assertEqual(decision["result_state"], "unassessed")
                self.assertEqual(decision["usage_source"], "actual_partial")
                self.assertEqual(decision["cost_source"],
                                 "actual_input" if input_observed else "estimated_input")
                self.assertEqual(decision["input_tokens"], 123 if input_observed else None)
                self.assertEqual(decision["output_tokens"], 17 if output_observed else None)
                self.assertEqual(len(governor.settlements), 1)
                self.assertEqual(
                    governor.settlements[0][1],
                    jev_cost(123 if input_observed else decision["estimated_input_tokens"]))
                self.assertEqual(ledger.entries()[0]["usage_source"], "actual_partial")

    def test_false_code_facts_block_all_semantic_calls_not_schema_refuse(self):
        for capability, state in _states().items():
            facts = state["code_facts"]
            fact = next(iter(facts))
            state["code_facts"][fact] = False
            with self.subTest(capability=capability, fact=fact):
                normalized = validate_hourglass_jev_state(capability, state)
                self.assertFalse(normalized["code_facts"][fact])
                _, decision, evaluator, governor, ledger = self._run(capability, state)
                self.assertEqual(decision["result_state"], "blocked_by_code_fact")
                self.assertEqual(evaluator.calls, [])
                self.assertEqual(governor.reservations, [])
                self.assertEqual(ledger.entries()[0]["result_state"],
                                 "blocked_by_code_fact")

    def test_consent_mismatch_is_a_code_blocker_and_false_match_facts_are_preserved(self):
        state = _states()["consent_signals"]
        state["consent"]["limits_ref"] = "limits:other"
        state["code_facts"]["assignment_matches"] = False
        state["code_facts"]["limits_match"] = False
        normalized = validate_hourglass_jev_state("consent_signals", state)
        self.assertEqual(
            hourglass_jev_blocking_facts("consent_signals", normalized["code_facts"]),
            ["assignment_matches", "limits_match"])
        _, decision, evaluator, governor, ledger = self._run("consent_signals", state)
        self.assertEqual(decision["result_state"], "blocked_by_code_fact")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(governor.reservations, [])
        self.assertEqual(ledger.entries()[0]["result_state"], "blocked_by_code_fact")

    def test_invalid_restart_transition_is_atomic_and_no_legal_target_is_skipped(self):
        state = _states()["restart_target"]
        legal = hourglass_jev_legal_restart_targets(state)
        self.assertEqual(legal, ["no_iteration"])
        questions = hourglass_jev_question_pack("restart_target", state)
        valid_answers = _typed_answers(questions, choice="no_iteration")
        evaluator = RecordingEvaluator(
            _result(questions, answers=valid_answers), transport=RecordingTransport())
        with mock.patch("harness.jev_policy.prepare_hourglass_restart",
                        side_effect=ValueError("transition became invalid")):
            result, decision, _, _, ledger = self._run(
                "restart_target", state, evaluator=evaluator)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "invalid_restart_transition")
        self.assertTrue(decision["is_fallback"])
        self.assertFalse(decision["confidence_review_required"])
        self.assertIsNone(decision["confidence"])
        self.assertEqual(decision["supported"], 0.0)
        self.assertEqual(result.answers, {})
        self.assertEqual(ledger.entries()[0]["result_state"], "unassessed")
        self.assertTrue(ledger.entries()[0]["is_fallback"])

        state["current_stage"] = "execution"
        policy, governor, ledger = self._policy(
            evaluator=RecordingEvaluator(transport=RecordingTransport()))
        result, decision = policy.evaluate_hourglass("restart_target", state)
        self.assertEqual(decision["result_state"], "skipped")
        self.assertEqual(decision["fallback_state"], "no_legal_restart_transition")
        self.assertEqual(result.answers, {})
        self.assertEqual(governor.reservations, [])
        self.assertEqual(ledger.entries()[0]["result_state"], "skipped")

    def test_invalid_choice_distribution_and_duplicate_answer_ids_are_rejected(self):
        questions = hourglass_jev_question_pack(
            "execution_checkpoint", _states()["execution_checkpoint"])
        answers = _typed_answers(questions)
        answers["checkpoint_recommendation"]["probabilities"] = {
            key: (1.0 if key == "defer" else 0.0)
            for key in questions["checkpoint_recommendation"]["criteria"]
        }
        with self.assertRaisesRegex(ValueError, "probability distribution"):
            validate_hourglass_jev_answers(answers, questions)
        answers = _typed_answers(questions)
        answers["unexpected"] = answers.pop("package_suitable")
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_hourglass_jev_answers(answers, questions)

    def test_observed_input_over_bound_is_settled_but_not_trusted_as_a_judgment(self):
        state = _states()["context_intake"]
        questions = hourglass_jev_question_pack("context_intake", state)
        raw = _result(questions, input_tokens=8193)
        evaluator = RecordingEvaluator(raw, transport=RecordingTransport())
        result, decision, _, governor, ledger = self._run(
            "context_intake", state, evaluator=evaluator, max_input_tokens=8192)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "observed_input_exceeded_limit")
        self.assertEqual(decision["answers"], {})
        self.assertEqual(result.answers, {})
        self.assertEqual(governor.settlements[0][1], jev_cost(8193))
        self.assertEqual(ledger.entries()[0]["result_state"], "unassessed")

    def test_low_choice_score_confidence_requires_review_but_noul_is_not_confidence(self):
        state = _states()["execution_checkpoint"]
        questions = hourglass_jev_question_pack("execution_checkpoint", state)
        answers = _typed_answers(questions, confidence=0.2)
        raw = _result(questions, answers=answers)
        evaluator = RecordingEvaluator(raw, transport=RecordingTransport())
        result, decision, *_ = self._run(
            "execution_checkpoint", state, evaluator=evaluator, min_confidence=0.8)
        self.assertEqual(decision["result_state"], "review_required")
        self.assertTrue(decision["confidence_review_required"])
        self.assertEqual(decision["confidence"], 0.2)
        self.assertEqual(decision["supported"], 0.91)
        self.assertEqual(result.supported, 0.91)
        self.assertEqual(result.verdict, "fail")

        noul_state = _states()["consent_signals"]
        noul_questions = hourglass_jev_question_pack("consent_signals", noul_state)
        noul_answers = _typed_answers(noul_questions, noul=0.1, confidence=0.2)
        # Keep the declared pack complete; Noul probabilities remain separate
        # from the Choice confidence supplied by the escalation question.
        raw = _result(noul_questions, answers=noul_answers)
        evaluator = RecordingEvaluator(raw, transport=RecordingTransport())
        _, decision, *_ = self._run("consent_signals", noul_state, evaluator=evaluator)
        self.assertEqual(decision["confidence"], 0.2)
        self.assertEqual(decision["noul_min_probability"], 0.1)
        self.assertEqual(decision["supported"], 0.1)
        self.assertEqual(decision["result_state"], "review_required")

        # A pure-Noul capability leaves action confidence absent even for
        # a low yes-probability.
        claim_state = _states()["verification_claim_support"]
        claim_questions = hourglass_jev_question_pack(
            "verification_claim_support", claim_state)
        claim_answers = _typed_answers(claim_questions, noul=0.1)
        evaluator = RecordingEvaluator(
            _result(claim_questions, answers=claim_answers),
            transport=RecordingTransport())
        _, decision, *_ = self._run(
            "verification_claim_support", claim_state, evaluator=evaluator)
        self.assertIsNone(decision["confidence"])
        self.assertEqual(decision["noul_min_probability"], 0.1)
        self.assertEqual(decision["result_state"], "judged")

    def test_ledger_failure_and_settlement_failure_clear_typed_answers(self):
        state = _states()["context_intake"]
        evaluator = RecordingEvaluator(transport=RecordingTransport())
        governor = RecordingGovernor(settlement_error=HarnessError("settle failed"))
        result, decision, _, governor, ledger = self._run(
            "context_intake", state, evaluator=evaluator, governor=governor)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "settlement_failed")
        self.assertEqual(result.answers, {})
        self.assertEqual(len(governor.settlements), 1)
        self.assertEqual(ledger.entries()[0]["settlement_error"], "HarnessError")

        class BrokenLedger:
            def append(self, *args, **kwargs):
                raise OSError("ledger unavailable")

        evaluator = RecordingEvaluator(transport=RecordingTransport())
        policy, governor, _ = self._policy(evaluator=evaluator, ledger=False)
        policy.ledger = BrokenLedger()
        result, decision = policy.evaluate_hourglass("context_intake", state)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "ledger_append_failed")
        self.assertEqual(result.answers, {})
        self.assertEqual(len(governor.settlements), 1)

    def test_ledger_failure_clears_restart_handoff_from_unassessed_result(self):
        state = _states()["restart_target"]
        state["unmet_requirements"] = [{
            "id": "req-missing", "description": "Needs another evidence check",
            "evidence_refs": ["evidence:missing"],
        }]
        state["evidence_refs"].append("evidence:missing")
        questions = hourglass_jev_question_pack("restart_target", state)
        answers = _typed_answers(questions, choice="planning")
        evaluator = RecordingEvaluator(
            _result(questions, answers=answers), transport=RecordingTransport())
        policy, governor, _ = self._policy(evaluator=evaluator, ledger=False)

        class BrokenLedger:
            def append(self, *args, **kwargs):
                raise OSError("ledger unavailable")

        policy.ledger = BrokenLedger()
        result, decision = policy.evaluate_hourglass("restart_target", state)

        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "ledger_append_failed")
        self.assertTrue(decision["is_fallback"])
        self.assertEqual(decision["answers"], {})
        self.assertIsNone(decision["restart"])
        self.assertFalse(decision["restart_requested"])
        self.assertEqual(result.answers, {})
        self.assertTrue(result.is_fallback)
        self.assertEqual(len(governor.settlements), 1)

    def test_restart_choices_are_state_legal_and_preserve_completed_work(self):
        state = _states()["restart_target"]
        self.assertEqual(HOURGLASS_JEV_RESTART_CHOICES,
                         ("context", "planning", "execution", "no_iteration"))
        self.assertEqual(
            hourglass_jev_legal_restart_targets(state), ["no_iteration"])
        handoff = prepare_hourglass_restart("no_iteration", "verification", state)
        self.assertFalse(handoff["restart_requested"])
        self.assertFalse(handoff["requires_budget_repreflight"])
        self.assertFalse(handoff["requires_consent_recheck"])
        self.assertFalse(handoff["dispatch_authorized"])
        self.assertEqual(handoff["completed_work"], state["completed_work"])
        self.assertEqual(handoff["budget_history"], state["budget_history"])
        self.assertEqual(handoff["limits"]["max_input_tokens"], 2048)
        with self.assertRaisesRegex(ValueError, "current_stage disagrees"):
            prepare_hourglass_restart("planning", "planning", state)

    def test_restart_blocks_replay_and_only_offers_no_iteration_when_finished(self):
        state = _states()["restart_target"]
        state["unmet_requirements"] = [{
            "id": "req-missing", "description": "Needs another evidence check",
            "evidence_refs": ["evidence:missing"],
        }]
        state["evidence_refs"].append("evidence:missing")
        targets = hourglass_jev_legal_restart_targets(state)
        self.assertIn("planning", targets)
        self.assertNotIn("no_iteration", targets)
        with self.assertRaisesRegex(ValueError, "no_iteration requires"):
            prepare_hourglass_restart("no_iteration", "verification", state)

        state = _states()["restart_target"]
        state["current_stage"] = "planning"
        state["unmet_requirements"] = [{
            "id": "req-a", "description": "Evidence remains",
            "evidence_refs": ["evidence:req-a"],
        }]
        state["remaining_work"] = [{
            "id": "work-b", "description": "Finish pending step",
            "evidence_refs": ["evidence:work-b"],
        }]
        state["evidence_refs"].extend(["evidence:req-a", "evidence:work-b"])
        state["requested_assignment_id"] = "assign-changed"
        self.assertNotIn("execution", hourglass_jev_legal_restart_targets(state))
        with self.assertRaisesRegex(ValueError, "changed execution assignment"):
            prepare_hourglass_restart("execution", "planning", state)

    def test_restart_rejects_loosened_limits_and_unverified_work(self):
        state = _states()["restart_target"]
        state["requested_limits"]["max_input_tokens"] = 4096
        with self.assertRaisesRegex(ValueError, "only be tightened"):
            validate_hourglass_jev_state("restart_target", state)
        state = _states()["restart_target"]
        state["code_facts"]["budget_history_verified"] = False
        self.assertEqual(hourglass_jev_blocking_facts("restart_target", state["code_facts"]),
                         ["budget_history_verified"])
        self.assertEqual(hourglass_jev_legal_restart_targets(state), [])

    def test_final_alignment_requires_verified_evidence_and_original_context(self):
        state = _states()["final_alignment"]
        normalized = validate_hourglass_jev_state("final_alignment", state)
        self.assertEqual(normalized["original_request"], state["original_request"])
        self.assertEqual(normalized["retained_source_context"][0]["content"],
                         state["retained_source_context"][0]["content"])
        self.assertIn("docs/spec.md", hourglass_jev_evidence_refs(
            "final_alignment", normalized))
        missing = copy.deepcopy(state)
        missing["code_facts"]["evidence_references_verified"] = False
        _, decision, evaluator, governor, ledger = self._run(
            "final_alignment", missing)
        self.assertEqual(decision["result_state"], "blocked_by_code_fact")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(governor.reservations, [])
        self.assertEqual(ledger.entries()[0]["event"], "jev_refusal")

    def test_paths_claim_refs_and_answer_vocabulary_are_strict(self):
        state = _states()["planning_waist"]
        state["plan_steps"][0]["target_paths"] = ["../outside.py"]
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            validate_hourglass_jev_state("planning_waist", state)

        state = _states()["verification_claim_support"]
        state["claims"][0]["evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "requires cited evidence"):
            validate_hourglass_jev_state("verification_claim_support", state)

        questions = hourglass_jev_question_pack(
            "execution_checkpoint", _states()["execution_checkpoint"])
        answers = _typed_answers(questions)
        answers["checkpoint_recommendation"]["choice"] = "dispatch"
        with self.assertRaisesRegex(ValueError, "declared vocabulary"):
            validate_hourglass_jev_answers(answers, questions)

    def test_hard_cost_cap_and_invalid_capability_refuse_before_dispatch(self):
        state = _states()["context_intake"]
        evaluator = RecordingEvaluator(transport=RecordingTransport())
        policy, governor, ledger = self._policy(evaluator=evaluator)
        with mock.patch("harness.jev_policy.HARD_MAX_COST", 0.0):
            _, decision = policy.evaluate_hourglass(
                "context_intake", state, max_input_tokens=8192)
        self.assertEqual(decision["result_state"], "unassessed")
        self.assertEqual(decision["fallback_state"], "budget_refused")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(governor.reservations, [])
        with self.assertRaisesRegex(HarnessError, "unknown Hourglass"):
            policy.evaluate_hourglass("not_a_capability", state)

    def test_confidence_and_input_limits_reject_bool_and_nonfinite_values(self):
        state = _states()["context_intake"]
        for threshold in (True, float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(threshold=threshold):
                _, decision, evaluator, governor, _ = self._run(
                    "context_intake", state, min_confidence=threshold)
                self.assertEqual(decision["result_state"], "unassessed")
                self.assertEqual(evaluator.calls, [])
                self.assertEqual(governor.reservations, [])
        with self.assertRaisesRegex(ValueError, "outside the declared"):
            hourglass_jev_preflight("context_intake", state, "jev-test", True)


if __name__ == "__main__":
    unittest.main()
