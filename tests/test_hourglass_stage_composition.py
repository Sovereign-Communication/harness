"""HV-4: which stages run, in what order, and why the others do not.

The contract: compose optional context/planning/execution/verification
stages from the shared contracts; supplied brief and plan paths bypass
omitted stages; preserve Hourglass defaults where compatibility requires
them.

These tests are about the composition contract itself -- an unknown stage
name is refused, a skip always carries a reason, a supplied brief buys a
bypass only while it is fresh, and a stage that is selected but not run
says ``pending`` instead of vanishing.
"""
import os
import tempfile
import unittest

from harness.brief import build_brief
from harness.errors import HarnessError
from harness.stages import (
    STAGE_ORDER,
    STAGE_STATES,
    STATE_COMPLETED,
    STATE_PENDING,
    STATE_SKIPPED,
    Composition,
    StagePlan,
    StageSpec,
    compose_stages,
    resolve_stages,
    stage_selection_from_settings,
)
from harness.token_budget import TokenBudget


def _reader(files):
    def read(path):
        return files[path]
    return read


class StageSelectionTests(unittest.TestCase):
    def test_the_default_composition_is_the_whole_hourglass(self):
        plan = resolve_stages(goal="ship the slice")
        self.assertEqual(plan.selected(), STAGE_ORDER)
        self.assertEqual(plan.skipped(), ())
        # Compatibility: the default posture is every stage, not a subset.
        self.assertEqual(stage_selection_from_settings(object()),
                         list(STAGE_ORDER))

    def test_an_explicit_selection_is_a_subset_in_declared_order(self):
        plan = resolve_stages(goal="g", stages=["verification", "context"])
        # Order is the declared ladder, never the caller's: a stage cannot be
        # moved before a stage it depends on.
        self.assertEqual(plan.selected(), ("context", "verification"))
        self.assertEqual(plan.skipped(), ("planning", "execution"))
        for name in plan.skipped():
            self.assertEqual(plan.spec(name).skip_reason, "not_selected")

    def test_a_string_selection_and_a_config_selection_agree(self):
        class FakeSettings:
            hourglass_stages = "context, planning"

        self.assertEqual(list(resolve_stages(
            goal="g", stages="context,planning").selected()),
            stage_selection_from_settings(FakeSettings()))
        # A config that names nothing means every stage, not none.
        class Unset:
            hourglass_stages = None
        self.assertEqual(stage_selection_from_settings(Unset()),
                         list(STAGE_ORDER))
        self.assertEqual(stage_selection_from_settings(Unset(),
                                                       default=["planning"]),
                         ["planning"])

    def test_an_unknown_stage_is_refused_not_dropped(self):
        for bad in (["nope"], "context,nope", ["context", "CONTEXT"]):
            with self.assertRaises(HarnessError, msg=bad) as ctx:
                resolve_stages(goal="g", stages=bad)
            self.assertIn("unknown Hourglass stage", str(ctx.exception))
        with self.assertRaises(HarnessError):
            StageSpec("nope", False, "not_selected")
        # Config only splits; the ONE composition owner validates. A name that
        # survives config is refused where the ladder is resolved.
        bogus = stage_selection_from_settings(
            type("S", (), {"hourglass_stages": "nope"})())
        self.assertEqual(bogus, ["nope"])
        with self.assertRaises(HarnessError):
            resolve_stages(goal="g", stages=bogus)


class BypassTests(unittest.TestCase):
    def _fresh_brief(self, files):
        return build_brief("g", files, reader=_reader(files))

    def test_a_supplied_fresh_brief_bypasses_context_only(self):
        files = {"a.py": "alpha\n"}
        brief = self._fresh_brief(files)
        plan = resolve_stages(goal="g", brief=brief, reader=_reader(files))
        self.assertEqual(plan.spec("context").skip_reason, "brief_supplied")
        # The brief replaces the context stage and NOTHING else: planning,
        # execution, and verification all stay selected and say so.
        self.assertTrue(plan.spec("planning").selected)
        self.assertTrue(plan.spec("execution").selected)
        self.assertTrue(plan.spec("verification").selected)

    def test_a_supplied_plan_bypasses_planning_only(self):
        files = {"a.py": "alpha\n"}
        brief = self._fresh_brief(files)
        plan = resolve_stages(goal="g", brief=brief, plan={"nodes": [{}]},
                              reader=_reader(files))
        self.assertEqual(plan.spec("planning").skip_reason, "plan_supplied")
        # A supplied plan does not make verification optional: independent
        # verification is completion authority and stays in the run.
        self.assertTrue(plan.spec("verification").selected)
        self.assertTrue(plan.spec("execution").selected)

    def test_a_stale_supplied_brief_buys_no_bypass(self):
        files = {"a.py": "v1\n"}
        brief = build_brief("g", ["a.py"], reader=_reader(files))
        files["a.py"] = "v2 -- drifted from the pin\n"
        plan = resolve_stages(goal="g", brief=brief, reader=_reader(files))
        spec = plan.spec("context")
        # The stage RUNS: a drifted brief is not a bypass, it is a reason to
        # rebuild. And the caller can see that its supply was refused.
        self.assertTrue(spec.selected)
        self.assertIsNone(spec.skip_reason)
        self.assertEqual(spec.denied_bypass, "supplied_brief_is_not_fresh")
        self.assertEqual(plan.to_dict()["stages"][0]["denied_bypass"],
                         "supplied_brief_is_not_fresh")
        from harness.brief import freshness_report
        self.assertFalse(freshness_report(brief, reader=_reader(files))["fresh"])

    def test_a_brief_with_no_sources_is_never_fresh(self):
        plan = resolve_stages(goal="g", brief={"grounding": {"sources": []}})
        spec = plan.spec("context")
        self.assertTrue(spec.selected)
        self.assertEqual(spec.denied_bypass, "supplied_brief_is_not_fresh")


class StageContractTests(unittest.TestCase):
    def test_a_skip_must_carry_a_reason_and_a_selection_must_not(self):
        with self.assertRaises(HarnessError) as ctx:
            StageSpec("context", False)
        self.assertIn("must say why", str(ctx.exception))
        with self.assertRaises(HarnessError):
            StageSpec("context", True, "brief_supplied")

    def test_a_denied_bypass_belongs_only_to_a_stage_that_runs(self):
        with self.assertRaises(HarnessError) as ctx:
            StageSpec("context", False, "not_selected", "supplied_brief_stale")
        self.assertIn("no bypass was denied", str(ctx.exception))

    def test_a_plan_validates_its_stages_and_states(self):
        good = (StageSpec("context", True), StageSpec("planning", False,
                                                      "plan_supplied"))
        plan = StagePlan("g", good, {"context": STATE_COMPLETED,
                                     "planning": STATE_SKIPPED})
        self.assertEqual(plan.state("context"), STATE_COMPLETED)
        self.assertEqual(plan.to_dict()["skipped"], ["planning"])
        with self.assertRaises(HarnessError):
            StagePlan("  ", good)
        with self.assertRaises(HarnessError):
            StagePlan("g", (StageSpec("context", True),
                            StageSpec("context", True)))
        with self.assertRaises(HarnessError):
            StagePlan("g", good, {"context": "mostly_done"})
        with self.assertRaises(HarnessError):
            StagePlan("g", good, {"execution": STATE_PENDING})
        self.assertEqual(set(STAGE_STATES),
                         {STATE_COMPLETED, STATE_SKIPPED, STATE_PENDING})

    def test_a_state_for_an_undeclared_stage_is_refused(self):
        good = (StageSpec("context", True), StageSpec("planning", False,
                                                      "plan_supplied"))
        with self.assertRaises(HarnessError) as ctx:
            StagePlan("g", good, {"verification": STATE_PENDING})
        self.assertIn("undeclared stage", str(ctx.exception))

    def test_looking_up_an_undeclared_stage_returns_nothing(self):
        plan = resolve_stages(goal="g", stages=["context"])
        # A declared stage is always present -- selected or skipped -- and
        # carries its own reason. A name outside the ladder is simply nothing.
        self.assertIsNotNone(plan.spec("execution"))
        self.assertEqual(plan.spec("execution").skip_reason, "not_selected")
        self.assertIsNone(plan.spec("decomposition"))
        self.assertIsNone(plan.state("decomposition"))
        self.assertIsNone(plan.state("execution"))

    def test_a_plan_round_trips_through_its_dict(self):
        plan = resolve_stages(goal="g", stages=["context", "planning"])
        payload = plan.to_dict()
        self.assertEqual([s["stage"] for s in payload["stages"]],
                         list(STAGE_ORDER))
        self.assertEqual(payload["selected"], ["context", "planning"])


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.files = {}
        for name, body in (("a.py", "def a():\n    return 1\n"),
                           ("b.py", "def b():\n    return 2\n")):
            path = os.path.join(self.dir.name, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            self.files[name] = body
        self.paths = [os.path.join(self.dir.name, n) for n in ("a.py", "b.py")]

    def _budget(self, **kw):
        base = dict(max_input_tokens=20000, max_output_tokens=2000)
        base.update(kw)
        return TokenBudget("run", **base)

    def test_a_full_composition_reports_what_ran_and_what_is_pending(self):
        comp = compose_stages(goal="make a better", budget=self._budget(),
                              files=self.paths)
        self.assertEqual(comp.plan.selected(), STAGE_ORDER)
        self.assertEqual(comp.plan.state("context"), STATE_COMPLETED)
        self.assertEqual(comp.plan.state("planning"), STATE_COMPLETED)
        # Execution and verification are SELECTED but performed by later
        # slices. A selected stage that is silently absent is the one thing a
        # composition must never be.
        self.assertEqual(comp.plan.state("execution"), STATE_PENDING)
        self.assertEqual(comp.plan.state("verification"), STATE_PENDING)
        self.assertIsNotNone(comp.planning)
        self.assertIsNotNone(comp.brief)
        payload = comp.to_dict()
        self.assertEqual(payload["plan"]["selected"], list(STAGE_ORDER))
        self.assertGreater(payload["brief_tokens"], 0)
        self.assertEqual(payload["budget"]["open_allowances"], 0)

    def test_a_supplied_brief_bypasses_the_context_build_entirely(self):
        supplied = build_brief("g", [self.paths[0]])
        comp = compose_stages(goal="g", budget=self._budget(),
                              brief=supplied, files=self.paths)
        self.assertEqual(comp.plan.spec("context").skip_reason,
                         "brief_supplied")
        self.assertEqual(comp.plan.state("context"), STATE_SKIPPED)
        # The caller's evidence is what planning read, verbatim.
        self.assertEqual(comp.planning.brief["built_at"],
                         supplied["built_at"])

    def test_a_selection_that_omits_planning_runs_no_planning(self):
        comp = compose_stages(goal="g", budget=self._budget(),
                              files=self.paths, stages=["context"])
        self.assertIsNone(comp.planning)
        self.assertEqual(comp.plan.state("planning"), STATE_SKIPPED)
        self.assertEqual(comp.plan.state("execution"), STATE_SKIPPED)
        self.assertEqual(comp.plan.state("verification"), STATE_SKIPPED)

    def test_composition_without_a_budget_makes_its_own(self):
        comp = compose_stages(goal="g", files=[self.paths[0]])
        self.assertIsInstance(comp, Composition)
        self.assertGreater(comp.budget["max_input_tokens"], 0)

    def test_a_goal_is_required(self):
        with self.assertRaises(HarnessError):
            compose_stages(goal="   ", budget=self._budget())


if __name__ == "__main__":
    unittest.main()
