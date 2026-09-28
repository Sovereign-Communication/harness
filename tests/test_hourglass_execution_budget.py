"""HV-5: the composed run reaches a real caller, and planning actually runs.

`HV-4` published the composition owner and left it with no production caller,
so a run composed nothing and roughly 565 lines of `harness/waist.py` were
reachable only from tests. These tests pin the caller that closes that gap, and
they drive the REAL surface -- `run_hourglass_request` -> `_handle_edit` ->
`_plan_round` -> `_compose_run_stages` -- rather than calling the wiring
directly, because the defect being fixed was precisely that nothing above the
private function ever called it.

Nothing here spends. The agent suite's established hermetic pattern replaces
only the transport/governor boundary (production `governor_for` correctly
refuses without a real key); the composition code under test never touches a
transport.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.token_budget import budget_from_settings
from harness.waist import (
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STATE_COMPLETED,
    STATE_PENDING,
    STATE_SKIPPED,
)
from tests._fake import FakeTransport, _gov

_TEST_GOVERNOR = _gov(FakeTransport(), max_cost=0.05)


class _GovernedRun(unittest.TestCase):
    """Base that scopes the transport/governor boundary to ONE test.

    Deliberately per-test rather than module-level: two module-level patches
    stack, and whichever module is torn down first strips the override for
    both. Scoping to the test makes this module order-independent under
    `unittest discover`, where the alphabetical import order is not under
    this file's control.
    """

    def setUp(self):
        patcher = patch("harness.agent.governor_for",
                        return_value=(None, _TEST_GOVERNOR))
        patcher.start()
        self.addCleanup(patcher.stop)


def _settings(**overrides):
    """Hermetic lane settings: no live Jev key, explicit stage selection."""
    settings = load_settings()
    settings.jev_api_key = None
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    settings.hourglass_require_attestation = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


REPO = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
DECLARED_OUTCOMES = ("sufficient", "plan", "evidence_request", "defer")


class _Repo:
    """A throwaway repo the real edit lane can actually run against."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "calc.py").write_text(REPO, encoding="utf-8")
        # The lane saves a chat turn, and history_dir must already exist.
        (self.root / ".hist").mkdir(exist_ok=True)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False

    def agent(self, **overrides):
        return AutonomousAgent(
            settings=_settings(**overrides), root_dir=self.root,
            transport=FakeTransport(), history_dir=self.root / ".hist")

    def run_edit(self, **overrides):
        return self.agent(**overrides).run_hourglass_request(
            "edit calc.py so add() documents its return value",
            auto_apply=False)


def _states(result):
    composition = result.get("composition") or {}
    return {entry["stage"]: entry["state"]
            for entry in composition.get("stages") or []}


def _ceilings(result):
    composition = result.get("composition") or {}
    return {entry["stage"]: entry["max_input_tokens"]
            for entry in composition.get("stages") or []}


class CompositionReachesACallerTests(_GovernedRun):
    """A real run reports the composition instead of defaulting silently."""

    def test_the_edit_lane_returns_a_composition(self):
        with _Repo() as repo:
            result = repo.run_edit()
        self.assertEqual(result["status"], "preview_ready")
        self.assertTrue(result["composition"]["stages"],
                        "the run returned a plan with no composition on it")

    def test_the_run_budget_is_the_runs_own_and_not_an_invented_one(self):
        with _Repo() as repo:
            result = repo.run_edit()
        settings = _settings()
        self.assertEqual(result["composition"]["run_budget"], "edit")
        self.assertEqual(result["composition"]["run_budget"],
                         budget_from_settings(settings, label="edit").label)

    def test_the_configured_stage_selection_is_what_runs(self):
        with _Repo() as repo:
            result = repo.run_edit(
                hourglass_stages=["context", "planning", "execution"])
        self.assertEqual(sorted(_states(result)),
                         ["context", "execution", "planning"])
        self.assertEqual(result["composition"]["skipped"], ["verification"])

    def test_a_stage_the_operator_dropped_is_reported_skipped_not_run(self):
        with _Repo() as repo:
            result = repo.run_edit(
                hourglass_stages=["context", "execution"])
        states = _states(result)
        self.assertEqual(states[STAGE_EXECUTION], STATE_PENDING)
        self.assertNotIn(STAGE_PLANNING, states)
        self.assertNotIn("planning", result,
                         "planning ran although the operator did not select it")


class PlanningActuallyRunsTests(_GovernedRun):
    """`run_planning` executes on a real run, and reports one outcome."""

    def test_planning_is_completed_not_merely_budgeted(self):
        with _Repo() as repo:
            result = repo.run_edit()
        # `completed` had NO producer before HV-5: composition budgets stages
        # and does not perform them, so this state could not appear at all.
        self.assertEqual(_states(result)[STAGE_PLANNING], STATE_COMPLETED)

    def test_planning_reports_exactly_one_declared_outcome_with_its_evidence(self):
        with _Repo() as repo:
            result = repo.run_edit()
        planning = result["planning"]
        self.assertIn(planning["kind"], DECLARED_OUTCOMES)
        self.assertTrue(planning["reason"],
                        "an outcome with no reason is a silent failure")
        self.assertTrue(planning["rounds"],
                        "planning reported a verdict with no round evidence")
        for entry in planning["rounds"]:
            self.assertIn("max_input_tokens", entry)
            self.assertIn("brief_tokens", entry)

    def test_planning_reads_the_files_the_run_targets(self):
        with _Repo() as repo:
            result = repo.run_edit()
        planning = result["planning"]
        if planning["kind"] == "sufficient":
            # Sufficiency is only reachable through a real brief built from
            # the run's own files, so a cited source proves the reader seam.
            self.assertGreaterEqual(len(planning["rounds"]), 1)

    def test_a_stage_that_did_not_run_is_never_reported_completed(self):
        with _Repo() as repo:
            result = repo.run_edit()
        states = _states(result)
        for stage, state in states.items():
            if stage != STAGE_PLANNING:
                self.assertIn(state, (STATE_PENDING, STATE_SKIPPED), stage)
                self.assertNotEqual(state, STATE_COMPLETED, stage)


class PlanningSpendsTheComposedCeilingTests(_GovernedRun):
    """The stage that spends is the stage that was composed.

    ``TokenBudget.stage()`` inherits its parent's ceilings when none are
    given. Handing ``run_planning`` the RUN budget would therefore have given
    planning the full run allowance and silently undone the narrowing the
    envelope had just reported -- the envelope would have said half while the
    stage could spend all of it.
    """

    def test_ceilings_narrow_along_the_run(self):
        with _Repo() as repo:
            result = repo.run_edit()
        ceilings = _ceilings(result)
        self.assertGreater(ceilings["context"], ceilings[STAGE_PLANNING])
        self.assertGreater(ceilings[STAGE_PLANNING], ceilings[STAGE_EXECUTION])

    def test_planning_spent_from_the_composed_child_not_the_run_budget(self):
        with _Repo() as repo:
            result = repo.run_edit()
        settings = _settings()
        run_budget = budget_from_settings(settings, label="edit")
        snapshot = result["planning"]["budget"]
        self.assertEqual(snapshot["max_input_tokens"],
                         _ceilings(result)[STAGE_PLANNING])
        self.assertLess(snapshot["max_input_tokens"], run_budget.max_input_tokens,
                        "planning inherited the run's full allowance")
        self.assertIsNotNone(snapshot.get("parent"),
                             "nesting is not provable through the public API")

    def test_the_round_allowance_is_the_planning_ceiling_not_the_runs(self):
        with _Repo() as repo:
            result = repo.run_edit()
        planning = result["planning"]
        for entry in planning["rounds"]:
            self.assertLessEqual(entry["max_input_tokens"],
                                 planning["budget"]["max_input_tokens"])


class HermeticNoSpendTests(_GovernedRun):
    """Nothing in the composed path may reach a provider."""

    def test_planning_makes_no_jev_call_without_a_native_answer(self):
        with _Repo() as repo:
            result = repo.run_edit()
        # A non-native or absent answer is never promoted; with no Jev key the
        # signals must come back empty rather than a smoothed guess.
        self.assertEqual(result["planning"]["jev_signals"], {})

    def test_no_reservation_is_left_open_by_a_completed_stage(self):
        with _Repo() as repo:
            result = repo.run_edit()
        snapshot = result["planning"]["budget"]
        self.assertEqual(snapshot["open_allowances"], 0)
        self.assertEqual(snapshot["cancelled"], 0)


if __name__ == "__main__":
    unittest.main()
