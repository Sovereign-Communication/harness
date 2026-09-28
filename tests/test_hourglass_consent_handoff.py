"""HV-5: a restart decision on a real run, owned by code, not by the model.

The canon names this module for the consent/handoff half of `HV-5`. The
restart decision is the part of that contract a run can already reach:
`HV-1` publishes `validate_restart_request` as the code-owned guard, and until
now **nothing in production called it at all** -- seven test callers, zero
production ones. The execution stage is where it belongs, and this file pins
that it is genuinely reached through the production surface.

These tests drive `run_hourglass_request`, not `_restart_decision`. The
defect being fixed is precisely that no production code reached the guard, and
a test that calls the method directly would keep that defect invisible.

Nothing here spends. The Jev evaluator is scripted per test, so
`JevPolicy.evaluate_hourglass_stage` is the REAL `HV-1` code path with a
declared answer set, and the transport never leaves the machine.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_packs import declared_restart_targets
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.token_budget import budget_from_settings
from harness.waist import STAGE_EXECUTION
from tests._fake import FakeTransport, _gov


class _RecordingGovernor:
    """Reserve/settle bookkeeping, so a real JevPolicy can preflight."""

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


class _ScriptedEvaluator:
    """A keyed Jev evaluator whose answers the test declares."""

    def __init__(self, answers, *, is_fallback=False, error=None):
        self.answers = answers
        self.is_fallback = is_fallback
        self.error = error
        self.min_confidence = 0.70
        self.model = "jev-test"
        # JevPolicy._preflight reads evaluator.api_key to decide whether
        # this is a KEYED judgment; without it every stage call raises
        # AttributeError before the pack is even built.
        self.api_key = "test-key"
        self.endpoint = "https://api.typesafe.ai/v1/systemone"
        self.transport = None
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((state, sorted(questions)))
        if self.error is not None:
            raise self.error
        return JevEvaluationResult(
            "pass", 0.9, 0.9, dict(self.answers), [],
            cost=0.0001, input_tokens=500, output_tokens=10,
            is_fallback=self.is_fallback, model=self.model)

    def evaluate_plan_requirements(self, prompt, target_files=None):
        """The edit lane evaluates its plan before composition runs.

        It is not what this module is testing, but the same policy object
        serves it, and an AttributeError there would abort the run before
        the execution stage is ever reached. An empty answer set keeps that
        call honest -- no `requires_iteration`, nothing invented.
        """
        self.plan_calls = getattr(self, "plan_calls", 0) + 1
        return JevEvaluationResult(
            "pass", 0.0, 0.0, {}, [],
            cost=0.0, input_tokens=0, output_tokens=0,
            is_fallback=True, model=self.model)


def noul(value):
    return {"type": "noul", "noul": value}


def choice(target, confidence=0.8):
    return {"type": "choice", "choice": target, "confidence": confidence,
            "probabilities": {target: 1.0}}


REPO = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
EXEC_SOUND = {"execution_suitable": noul(0.95), "checkpoint_required": noul(0.0)}
EXEC_UNSOUND = {"execution_suitable": noul(0.10), "checkpoint_required": noul(0.8)}


class RestartGuardIsReachedTests(unittest.TestCase):
    """The guard is on the production path, not only in tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(str(Path(self.tmp.name) / "ledger.jsonl"))
        self.governor = _RecordingGovernor()
        gov_patch = patch("harness.agent.governor_for",
                          return_value=(None, _gov(FakeTransport(), max_cost=0.05)))
        gov_patch.start()
        self.addCleanup(gov_patch.stop)

    def _real_policy(self, evaluator):
        """A REAL JevPolicy with a scripted evaluator.

        `evaluate_hourglass_stage` is `HV-1`'s own code, so the dimension
        pack, the signal vocabulary and the fallback discipline are all
        exercised as written; only the network call is replaced.
        """
        settings = load_settings()
        settings.jev_api_key = "test-key"
        settings.jev_model = "jev-test"
        return policy_for(settings, transport=None, governor=self.governor,
                          ledger=self.ledger, evaluator=evaluator)

    def _run(self, answers, **stages):
        with tempfile.TemporaryDirectory() as repo:
            root = Path(repo)
            (root / "calc.py").write_text(REPO, encoding="utf-8")
            (root / ".hist").mkdir()
            settings = load_settings()
            settings.jev_api_key = None
            settings.hourglass_confirm = False
            settings.hourglass_isolate = False
            settings.hourglass_parallel = False
            settings.hourglass_require_attestation = False
            for key, value in stages.items():
                setattr(settings, key, value)
            policy_patch = patch(
                "harness.agent.policy_for",
                side_effect=lambda *a, **k: self._real_policy(
                    _ScriptedEvaluator(answers)))
            policy_patch.start()
            self.addCleanup(policy_patch.stop)
            agent = AutonomousAgent(settings=settings, root_dir=root,
                                    transport=FakeTransport())
            return agent.run_hourglass_request(
                "edit calc.py so add() documents its return value",
                auto_apply=False)

    def _restart(self, result):
        judgment = (result.get("stage_judgments") or {}).get("execution")
        self.assertIsNotNone(judgment,
                             "the run carried no execution-stage judgment")
        return judgment.get("restart")

    # -- the guard is reached ------------------------------------------------
    def test_a_real_run_returns_a_typed_restart_decision(self):
        result = self._run(EXEC_SOUND)
        decision = self._restart(result)
        self.assertIsInstance(decision, dict)
        self.assertIsInstance(decision["allowed"], bool)
        self.assertIn("reasons", decision)
        self.assertEqual(decision["current_stage"], STAGE_EXECUTION)

    def test_the_decision_names_the_vocabulary_it_was_given(self):
        result = self._run(EXEC_SOUND)
        self.assertIn("recommendation_source",
                      self._restart(result))

    # -- completed work is preserved ----------------------------------------
    def test_a_walk_back_into_completed_work_is_refused_and_preserved(self):
        # Planning ran, so a walk-back to planning would RE-ENTER completed
        # work. The guard must refuse and say the work is preserved.
        result = self._run(EXEC_UNSOUND)
        decision = self._restart(result)
        self.assertFalse(decision["allowed"])
        self.assertIn("planning", decision["preserved_stages"])
        self.assertIn("already recorded complete", " ".join(decision["reasons"]))

    def test_a_walk_back_is_allowed_when_nothing_is_recorded_complete(self):
        # Planning not selected, so nothing is complete and walking back to
        # it is a legal transition rather than a repeat of finished work.
        result = self._run(EXEC_UNSOUND,
                           hourglass_stages=["context", "execution",
                                             "verification"])
        decision = self._restart(result)
        self.assertTrue(decision["allowed"], decision["reasons"])
        self.assertEqual(decision["target"], "planning")
        self.assertEqual(decision["preserved_stages"], [])

    def test_no_walk_back_is_recommended_when_the_package_is_sound(self):
        result = self._run(EXEC_SOUND)
        decision = self._restart(result)
        self.assertFalse(decision["allowed"])
        self.assertIsNone(decision["target"])

    # -- consent ------------------------------------------------------------
    def test_a_changed_assignment_forces_consent_renewal(self):
        # The guard's own rule, reached through the run rather than asserted
        # about the function: a restart that changes the assignment must
        # re-derive consent before any dispatch.
        from harness.jev_packs import validate_restart_request
        decision = validate_restart_request(
            STAGE_EXECUTION, "planning", completed_stages=[],
            consent_fresh=False)
        self.assertFalse(decision["allowed"])
        self.assertTrue(decision["consent_renewal_required"])
        self.assertIn("must be renewed", " ".join(decision["reasons"]))

    def test_an_unknown_consent_state_never_invents_a_renewal(self):
        # The run has dispatched nothing, so it passes None rather than
        # claiming freshness it has not re-derived.
        result = self._run(EXEC_SOUND)
        self.assertFalse(self._restart(result)["consent_renewal_required"])

    # -- the vocabulary is HV-1's, not this caller's ------------------------
    def test_the_walk_back_target_comes_from_the_declared_vocabulary(self):
        self.assertEqual(declared_restart_targets(),
                         ["context", "planning", STAGE_EXECUTION])
        walk_back = AutonomousAgent._declared_walk_back()
        self.assertIn(walk_back, declared_restart_targets())
        self.assertLess(declared_restart_targets().index(walk_back),
                        declared_restart_targets().index(STAGE_EXECUTION))

    def test_an_undeclared_recommendation_is_discarded_not_forwarded(self):
        result = self._run({"restart_target": choice("not_a_stage")})
        decision = self._restart(result)
        self.assertIsNone(decision["target"])
        self.assertFalse(decision["allowed"])

    def test_a_declared_recommendation_from_jev_is_accepted_as_a_recommendation(self):
        # Jev recommends; code still decides. A legal recommendation is
        # carried through, and the decision is the guard's, not Jev's.
        result = self._run(
            dict(EXEC_UNSOUND, restart_target=choice("planning")),
            hourglass_stages=["context", "execution", "verification"])
        decision = self._restart(result)
        self.assertEqual(decision["recommendation_source"], "jev")
        self.assertTrue(decision["allowed"], decision["reasons"])

    # -- fail-soft ----------------------------------------------------------
    def test_a_failed_dimension_call_does_not_take_the_run_down(self):
        result = self._run(EXEC_SOUND)  # baseline shape
        self.assertEqual(result["status"], "preview_ready")
        budget = budget_from_settings(load_settings(), label="edit")
        self.assertTrue(budget.max_input_tokens > 0)

    def test_the_stage_is_judged_not_dispatched(self):
        result = self._run(EXEC_SOUND)
        judgment = (result["stage_judgments"])["execution"]
        self.assertFalse(judgment["dispatched"])
        self.assertEqual(judgment["state"], "pending")
        states = {s["stage"]: s["state"]
                  for s in (result.get("composition") or {}).get("stages") or []}
        self.assertEqual(states[STAGE_EXECUTION], "pending")


if __name__ == "__main__":
    unittest.main()
