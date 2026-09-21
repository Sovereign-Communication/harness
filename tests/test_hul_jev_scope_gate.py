"""HUL-C mission scope gate tests (tests/test_hul_jev_scope_gate.py).

Covers: scope question pack shape via jev_packs; JevPolicy.evaluate_scope
determination (missing scope / low success → complete false); unkeyed
fallback cannot alone mark complete; false-done blocked; ledger jev_eval +
mission jev_evals.jsonl storage; site=hul_scope structural envelope.
"""
import os
import tempfile
import unittest
from pathlib import Path

from harness import mission_record as mr
from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_packs import (
    HUL_SCOPE_SITE,
    hul_scope_question_pack,
    normalize_complexity_class,
    scope_in_scope_holds,
)
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger


class _CountingGovernor:
    def __init__(self):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.max_cost = 1.0
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


class _FixedEvaluator:
    """Hermetic evaluator that returns a prepared typed answers map."""

    def __init__(self, answers=None, *, keyed=True, fallback=False,
                 raise_error=None):
        self.api_key = "jev-key" if keyed else None
        self.model = "jev-test"
        self.answers = answers or {}
        self.fallback = fallback
        self.raise_error = raise_error
        self.calls = 0

    def evaluate(self, state, questions=None):
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        if self.fallback:
            return JevEvaluationResult(
                "fail", 0.0, 0.0, {}, ["fallback"],
                is_fallback=True, model=self.model)
        return JevEvaluationResult(
            "pass", 0.8, 0.9, dict(self.answers), ["ok"],
            is_fallback=False, model=self.model)


def _unkeyed_settings():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


def _ledger(tmpdir):
    return AutonomyLedger(os.path.join(tmpdir, "ledger.jsonl"))


def _jev_evals(ledger):
    return [e for e in ledger.entries() if e["event"] == "jev_eval"]


def _good_answers():
    return {
        "scope_coverage": {
            "type": "score", "score": 0.9,
            "legend": {"0": 0.1, "1": 0.9},
            "probabilities": {"0": 0.1, "1": 0.9},
            "confidence": 0.85,
        },
        "success_definition_met": {"type": "noul", "noul": 0.95},
        "claims_supported": {"type": "noul", "noul": 0.9},
        "needs_human": {"type": "noul", "noul": 0.1},
        "complexity_class": {
            "type": "choice", "choice": "bounded",
            "probabilities": {"bounded": 0.8, "iterative": 0.1,
                              "architectural": 0.1},
            "confidence": 0.8,
        },
    }


def _mission_state(**overrides):
    state = {
        "mission_id": "m-scope-1",
        "request": "Implement the scoped feature",
        "success_definition": "Gate tests green and STATUS honest",
        "scope": {
            "in_scope": ["harness/jev_policy.py", "tests/test_hul_jev_scope_gate.py"],
            "out_of_scope": ["live dogfood"],
        },
        "evidence_summary": "tests green; STATUS updated on branch",
        "verifier_holds": True,
    }
    state.update(overrides)
    return state


def _init_pack(root, mid="m-scope-pack", *, in_scope=None):
    spec = mr.build_mission_spec(
        mission_id=mid,
        request="Implement the scoped feature",
        success_definition="Gate tests green and STATUS honest",
        max_cost_usd=0.40,
        terminal_reserve_cost_usd=0.05,
        in_scope=in_scope if in_scope is not None else [
            "harness/jev_policy.py", "tests/test_hul_jev_scope_gate.py"],
        out_of_scope=["live dogfood"],
        persistence_root=str(root),
        verifier_kind="hermetic-local",
    )
    return mr.init_mission_pack(root, spec)


class ScopeQuestionPackTests(unittest.TestCase):
    def test_pack_declares_required_typed_questions(self):
        pack = hul_scope_question_pack()
        self.assertEqual(
            set(pack),
            {"scope_coverage", "success_definition_met", "claims_supported",
             "needs_human", "complexity_class"})
        self.assertEqual(pack["scope_coverage"]["type"], "score")
        self.assertIsInstance(pack["scope_coverage"]["criteria"], list)
        self.assertGreaterEqual(len(pack["scope_coverage"]["criteria"]), 2)
        for key in ("success_definition_met", "claims_supported", "needs_human"):
            self.assertEqual(pack[key]["type"], "noul")
        self.assertEqual(pack["complexity_class"]["type"], "choice")
        self.assertEqual(
            set(pack["complexity_class"]["criteria"]),
            {"bounded", "iterative", "architectural"})
        self.assertEqual(HUL_SCOPE_SITE, "hul_scope")

    def test_code_owned_scope_helpers(self):
        self.assertTrue(scope_in_scope_holds({"in_scope": ["a.py"]}))
        self.assertFalse(scope_in_scope_holds({"in_scope": []}))
        self.assertFalse(scope_in_scope_holds({"in_scope": ["  "]}))
        self.assertFalse(scope_in_scope_holds(None))
        self.assertFalse(scope_in_scope_holds("nope"))
        self.assertFalse(scope_in_scope_holds({"in_scope": "not-a-list"}))
        self.assertFalse(scope_in_scope_holds({"in_scope": 3}))
        self.assertEqual(normalize_complexity_class("Bounded"), "bounded")
        self.assertEqual(normalize_complexity_class("iterative"), "iterative")
        self.assertIsNone(normalize_complexity_class("invented"))
        self.assertIsNone(normalize_complexity_class(3))

    def test_scope_answer_helpers_edge_shapes(self):
        # Bad noul/score payloads → None; raw numbers accepted.
        self.assertIsNone(JevPolicy._scope_noul(
            {"x": {"noul": "not-a-number"}}, "x"))
        self.assertIsNone(JevPolicy._scope_noul({"x": True}, "x"))
        self.assertIsNone(JevPolicy._scope_noul({"x": "nope"}, "x"))
        self.assertEqual(JevPolicy._scope_noul({"x": 0.7}, "x"), 0.7)
        self.assertEqual(
            JevPolicy._scope_noul({"x": {"noul": 0.4}}, "x"), 0.4)
        self.assertIsNone(JevPolicy._scope_score(
            {"y": {"score": "bad"}}, "y"))
        self.assertIsNone(JevPolicy._scope_score({"y": True}, "y"))
        self.assertEqual(JevPolicy._scope_score({"y": 0.3}, "y"), 0.3)
        self.assertEqual(
            JevPolicy._scope_score({"y": {"score": 0.8}}, "y"), 0.8)
        self.assertEqual(JevPolicy._scope_choice({"z": "bounded"}, "z"),
                         "bounded")
        self.assertIsNone(JevPolicy._scope_choice({"z": {"choice": "x"}}, "z"))
        self.assertEqual(JevPolicy._scope_text("plain string state"),
                         "plain string state")
        facts = JevPolicy._scope_state_facts(
            {"mission_id": "m", "scope": "not-a-dict"})
        self.assertEqual(facts["scope"], {"in_scope": [], "out_of_scope": []})
        self.assertFalse(facts["verifier_holds"])


class EvaluateScopeDeterminationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = _ledger(self.tmp.name)

    def test_unkeyed_cannot_alone_mark_complete(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, determination = policy.evaluate_scope(
            _mission_state(verifier_holds=True))
        self.assertTrue(result.is_fallback)
        self.assertFalse(determination["complete"])
        self.assertFalse(determination["success_definition_met"])
        self.assertTrue(determination["is_fallback"])
        self.assertEqual(structural["site"], "hul_scope")
        self.assertEqual(structural["determination"]["complete"], False)
        events = _jev_evals(self.ledger)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], "hul_scope")
        self.assertTrue(events[0]["is_fallback"])

    def test_missing_scope_blocks_complete_even_when_keyed_green(self):
        evaluator = _FixedEvaluator(_good_answers())
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        result, structural, determination = policy.evaluate_scope(
            _mission_state(scope={"in_scope": [], "out_of_scope": []}))
        self.assertFalse(result.is_fallback)
        self.assertFalse(determination["complete"])
        self.assertFalse(determination["scope_holds"])
        self.assertTrue(
            any("missing scope.in_scope" in r for r in determination["reasons"]))
        self.assertEqual(structural["site"], "hul_scope")

    def test_low_success_definition_met_blocks_complete(self):
        answers = _good_answers()
        answers["success_definition_met"] = {"type": "noul", "noul": 0.2}
        evaluator = _FixedEvaluator(answers)
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _result, _structural, determination = policy.evaluate_scope(
            _mission_state())
        self.assertFalse(determination["complete"])
        self.assertFalse(determination["success_definition_met"])
        self.assertTrue(
            any("success_definition_met is low" in r
                for r in determination["reasons"]))

    def test_low_scope_coverage_blocks_complete(self):
        answers = _good_answers()
        answers["scope_coverage"] = {
            "type": "score", "score": 0.1,
            "legend": {"0": 0.9, "1": 0.1},
            "probabilities": {"0": 0.9, "1": 0.1},
            "confidence": 0.5,
        }
        evaluator = _FixedEvaluator(answers)
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = policy.evaluate_scope(_mission_state())
        self.assertFalse(determination["complete"])
        self.assertFalse(determination["scope_holds"])

    def test_needs_human_blocks_complete(self):
        answers = _good_answers()
        answers["needs_human"] = {"type": "noul", "noul": 0.9}
        evaluator = _FixedEvaluator(answers)
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = policy.evaluate_scope(_mission_state())
        self.assertFalse(determination["complete"])
        self.assertTrue(determination["needs_human"])

    def test_verifier_false_blocks_complete(self):
        evaluator = _FixedEvaluator(_good_answers())
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = policy.evaluate_scope(
            _mission_state(verifier_holds=False))
        self.assertFalse(determination["complete"])
        self.assertFalse(determination["verifier_holds"])

    def test_keyed_all_holds_marks_complete(self):
        evaluator = _FixedEvaluator(_good_answers())
        gov = _CountingGovernor()
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=gov, ledger=self.ledger)
        result, structural, determination = policy.evaluate_scope(
            _mission_state())
        self.assertTrue(determination["complete"])
        self.assertTrue(determination["verifier_holds"])
        self.assertTrue(determination["success_definition_met"])
        self.assertTrue(determination["scope_holds"])
        self.assertEqual(determination["complexity_class"], "bounded")
        self.assertFalse(determination["is_fallback"])
        self.assertEqual(structural["site"], "hul_scope")
        self.assertEqual(structural["determination"]["complete"], True)
        self.assertEqual(len(gov.reserved), 1)
        events = _jev_evals(self.ledger)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["is_fallback"])

    def test_false_done_blocked_when_fallback_despite_claim(self):
        """Attempt claims verifier_holds=True + 'done' — unkeyed still false."""
        evaluator = _FixedEvaluator(fallback=True)
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = policy.evaluate_scope(_mission_state())
        self.assertTrue(determination["is_fallback"])
        self.assertFalse(determination["complete"])
        self.assertTrue(
            any("cannot alone mark mission complete" in r
                for r in determination["reasons"]))

    def test_transport_error_fails_closed(self):
        from harness.errors import HarnessError
        evaluator = _FixedEvaluator(raise_error=HarnessError("transport down"))
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        result, structural, determination = policy.evaluate_scope(_mission_state())
        self.assertTrue(result.is_fallback)
        self.assertFalse(determination["complete"])
        self.assertIn("reason", structural)

    def test_transport_error_reconcile_failure_still_fails_closed(self):
        from harness.errors import HarnessError

        class _BadGovernor(_CountingGovernor):
            def reconcile(self, reservation, cost):
                raise HarnessError("reconcile exploded")

        evaluator = _FixedEvaluator(raise_error=HarnessError("transport down"))
        # Force a reservation path first via a successful preflight shape.
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_BadGovernor(), ledger=self.ledger)
        result, structural, determination = policy.evaluate_scope(_mission_state())
        self.assertTrue(result.is_fallback)
        self.assertFalse(determination["complete"])

    def test_raw_number_answers_normalize_in_determination(self):
        """Scope helpers accept raw numbers (not only official typed dicts)."""
        class _RawNumberEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions=None):
                return JevEvaluationResult(
                    "pass", 0.8, 0.9,
                    {
                        "scope_coverage": 0.9,
                        "success_definition_met": 0.9,
                        "claims_supported": 0.85,
                        "needs_human": 0.05,
                        "complexity_class": "iterative",
                    },
                    ["raw"], is_fallback=False, model=self.model)

        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_RawNumberEvaluator(), governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = policy.evaluate_scope(_mission_state())
        self.assertTrue(determination["complete"])
        self.assertEqual(determination["complexity_class"], "iterative")

    def test_state_text_edge_shapes(self):
        self.assertEqual(JevPolicy._scope_text({"evidence_summary": "hi"}), "hi")
        self.assertEqual(JevPolicy._scope_text({"empty": 1}), "")
        self.assertEqual(JevPolicy._scope_text(None), "")
        facts = JevPolicy._scope_state_facts(None)
        self.assertFalse(facts["verifier_holds"])


class MissionPackScopeStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()
        self.pack = _init_pack(self.root)
        self.ledger = _ledger(self.tmp.name)

    def test_evaluate_scope_on_pack_writes_jev_evals_jsonl(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, determination = mr.evaluate_scope_on_pack(
            self.pack,
            policy,
            evidence_summary="attempted tests",
            verifier_holds=True,
        )
        self.assertFalse(determination["complete"])
        self.assertEqual(structural["site"], "hul_scope")
        evals = mr.load_jev_evals(self.pack)
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0]["site"], "hul_scope")
        self.assertFalse(evals[0]["determination"]["complete"])
        self.assertTrue(evals[0]["verifier_holds"])
        ledger_events = _jev_evals(self.ledger)
        self.assertEqual(len(ledger_events), 1)

    def test_evaluate_scope_on_pack_keyed_green_stores_complete(self):
        evaluator = _FixedEvaluator(_good_answers())
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = mr.evaluate_scope_on_pack(
            self.pack, policy,
            evidence_summary="all gate tests green",
            verifier_holds=True)
        self.assertTrue(determination["complete"])
        evals = mr.load_jev_evals(self.pack)
        self.assertTrue(evals[0]["determination"]["complete"])
        self.assertEqual(evals[0]["site"], "hul_scope")

    def test_missing_scope_on_pack_blocks_complete(self):
        pack = _init_pack(self.root, mid="m-scope-empty", in_scope=[])
        evaluator = _FixedEvaluator(_good_answers())
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=evaluator, governor=_CountingGovernor(),
            ledger=self.ledger)
        _r, _s, determination = mr.evaluate_scope_on_pack(
            pack, policy, evidence_summary="x", verifier_holds=True)
        self.assertFalse(determination["complete"])

    def test_evaluate_scope_on_pack_requires_existing_pack(self):
        ghost = mr.MissionPack(self.root, "m-ghost")
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            mr.evaluate_scope_on_pack(ghost, policy)

    def test_evaluate_scope_on_pack_extra_state_merge(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        _r, _s, determination = mr.evaluate_scope_on_pack(
            self.pack, policy,
            evidence_summary="extra",
            verifier_holds=False,
            state_summary="ss",
            task_id="custom-task",
            extra_state={"note": "merged"},
        )
        self.assertFalse(determination["complete"])
        evals = mr.load_jev_evals(self.pack)
        self.assertEqual(evals[0]["task_id"], "custom-task")

    def test_validate_resume_driver_fields_and_rejects(self):
        base = mr.load_resume(self.pack)
        good = dict(base)
        good.update({
            "stall_counter": 2,
            "errors": 1,
            "tokens": 40,
            "seen_evidence": ["a.md"],
            "attempt_in_flight": 3,
        })
        validated = mr.validate_resume(good, expected_id=self.pack.id)
        self.assertEqual(validated["stall_counter"], 2)
        self.assertEqual(validated["errors"], 1)
        self.assertEqual(validated["tokens"], 40)
        self.assertEqual(validated["seen_evidence"], ["a.md"])
        self.assertEqual(validated["attempt_in_flight"], 3)
        from harness.errors import HarnessError
        for key, bad in (
            ("stall_counter", -1),
            ("stall_counter", True),
            ("errors", "x"),
            ("tokens", -2),
            ("seen_evidence", "not-list"),
            ("seen_evidence", [1]),
            ("attempt_in_flight", -1),
            ("attempt_in_flight", "z"),
        ):
            body = dict(base)
            body[key] = bad
            with self.assertRaises(HarnessError, msg=key):
                mr.validate_resume(body, expected_id=self.pack.id)

    def test_stalled_is_a_terminal_status(self):
        self.assertIn("stalled", mr._TERMINAL_STATUSES)
        resume = mr.mark_terminal(self.pack, outcome="stalled",
                                  findings="# FINDINGS\n\nstalled\n")
        self.assertEqual(resume["status"], "stalled")
        self.assertTrue(mr.is_terminal(self.pack))
        body = self.pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("stalled", body)


if __name__ == "__main__":
    unittest.main()
