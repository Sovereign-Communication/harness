"""HV-4: stage composition selects which stages run, and nothing else.

Composition is deliberately a selection concern. These tests pin the
selection rules (declared subset, canonical order, refused names, no
duplicates) and the supplied-artifact bypasses, and they pin that the
selection never quietly becomes a second source of truth: the config
default and the composition owner's stage order must agree.
"""
import os
import unittest

from harness.config import DEFAULT_HOURGLASS_STAGES
from harness.errors import HarnessError
from harness.token_budget import TokenBudget
from harness.waist import (
    HOURGLASS_DEFAULT_STAGES,
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STAGE_ORDER,
    STAGE_PLANNING,
    STAGE_VERIFICATION,
    compose_stages,
    resolve_stages,
    stage_budget,
)


class StageSelectionTests(unittest.TestCase):
    def test_nothing_declared_keeps_every_hourglass_stage(self):
        # Compatibility: before composition was selectable the lanes always
        # ran intake, planning, execution and verification. An undeclared
        # run must still compose the same four.
        chosen = resolve_stages()
        self.assertEqual(chosen["stages"], list(STAGE_ORDER))
        self.assertEqual(chosen["bypassed"], {})

    def test_a_declared_subset_runs_in_pipeline_order(self):
        chosen = resolve_stages([STAGE_EXECUTION, STAGE_CONTEXT])
        self.assertEqual(chosen["stages"], [STAGE_CONTEXT, STAGE_EXECUTION])

    def test_names_are_normalized_not_obeyed_verbatim(self):
        chosen = resolve_stages([" Context ", "PLANNING"])
        self.assertEqual(chosen["stages"], [STAGE_CONTEXT, STAGE_PLANNING])

    def test_an_unknown_stage_is_refused_by_name(self):
        with self.assertRaises(HarnessError) as ctx:
            resolve_stages(["context", "teleport"])
        self.assertIn("teleport", str(ctx.exception))

    def test_a_stage_cannot_be_declared_twice(self):
        with self.assertRaises(HarnessError) as ctx:
            resolve_stages(["context", "context"])
        self.assertIn("twice", str(ctx.exception))

    def test_a_bare_string_is_not_a_stage_list(self):
        # "context,planning" is a config/env string, not a sequence of
        # stages; accepting it silently would compose one stage named
        # "context,planning" (i.e. refuse later, somewhere less obvious).
        with self.assertRaises(HarnessError) as ctx:
            resolve_stages("context,planning")
        self.assertIn("not a", str(ctx.exception))

    def test_a_blank_stage_name_is_refused(self):
        with self.assertRaises(HarnessError):
            resolve_stages(["context", "  "])


class SuppliedArtifactBypassTests(unittest.TestCase):
    def test_a_supplied_brief_bypasses_intake_with_a_reason(self):
        chosen = resolve_stages(supplied_brief=True)
        self.assertNotIn(STAGE_CONTEXT, chosen["stages"])
        self.assertIn(STAGE_CONTEXT, chosen["bypassed"])
        self.assertIn("brief", chosen["bypassed"][STAGE_CONTEXT])
        # The rest of the pipeline is untouched: bypassing intake is not
        # permission to skip planning.
        self.assertEqual(chosen["stages"],
                         [STAGE_PLANNING, STAGE_EXECUTION, STAGE_VERIFICATION])

    def test_a_supplied_plan_bypasses_decomposition_with_a_reason(self):
        chosen = resolve_stages(supplied_plan=True)
        self.assertNotIn(STAGE_PLANNING, chosen["stages"])
        self.assertIn("plan", chosen["bypassed"][STAGE_PLANNING])
        self.assertEqual(chosen["stages"],
                         [STAGE_CONTEXT, STAGE_EXECUTION, STAGE_VERIFICATION])

    def test_both_supplied_leaves_execution_and_verification(self):
        chosen = resolve_stages(supplied_brief=True, supplied_plan=True)
        self.assertEqual(chosen["stages"], [STAGE_EXECUTION, STAGE_VERIFICATION])
        self.assertEqual(sorted(chosen["bypassed"]),
                         sorted([STAGE_CONTEXT, STAGE_PLANNING]))

    def test_a_bypass_of_an_omitted_stage_is_not_invented(self):
        # Declaring no context stage and then supplying a brief must not
        # report a bypass for a stage that was never going to run.
        chosen = resolve_stages([STAGE_EXECUTION], supplied_brief=True)
        self.assertEqual(chosen["bypassed"], {})
        self.assertEqual(chosen["stages"], [STAGE_EXECUTION])


class CompositionOwnershipTests(unittest.TestCase):
    def _budget(self):
        return TokenBudget("run", max_input_tokens=200000,
                          max_output_tokens=64000)

    def test_composition_needs_a_budget_and_never_invents_one(self):
        with self.assertRaises(HarnessError) as ctx:
            compose_stages(budget=None)
        self.assertIn("one owner", str(ctx.exception))

    def test_each_stage_gets_a_child_of_the_run_budget(self):
        comp = compose_stages(budget=self._budget())
        self.assertEqual([s["stage"] for s in comp["stages"]],
                         list(STAGE_ORDER))
        run = self._budget()
        for entry in comp["stages"]:
            child = entry["budget"]
            self.assertIsNot(child, run)
            self.assertLessEqual(child.max_input_tokens, run.max_input_tokens)
            self.assertLessEqual(child.max_output_tokens,
                                 run.max_output_tokens)
        # The first stage is the parent of the rest, and nothing may sit
        # above the run's own ceiling.
        first = comp["stages"][0]["budget"]
        self.assertEqual(first.max_input_tokens, 200000)
        self.assertEqual(first.max_output_tokens, 64000)

    def test_stage_budget_finds_the_right_child_and_none_for_a_skip(self):
        comp = compose_stages(budget=self._budget(), supplied_brief=True)
        self.assertIsNotNone(stage_budget(comp, STAGE_PLANNING))
        self.assertIsNone(stage_budget(comp, STAGE_CONTEXT))
        self.assertIsNone(stage_budget(comp, "no-such-stage"))

    def test_a_supplied_artifact_leaves_no_budget_for_the_skipped_stage(self):
        comp = compose_stages(budget=self._budget(), supplied_plan=True)
        self.assertIsNone(stage_budget(comp, STAGE_PLANNING))
        self.assertIn(STAGE_PLANNING, comp["bypassed"])


class DefaultAgreementTests(unittest.TestCase):
    def test_the_config_default_is_the_composition_owners_default(self):
        # config.py holds the default *set* (an operator-facing default);
        # waist.py owns which stages exist. They must not drift: a config
        # default naming a stage the composer refuses would be a run that
        # fails only when it is composed.
        self.assertEqual(tuple(DEFAULT_HOURGLASS_STAGES), STAGE_ORDER)
        self.assertEqual(tuple(HOURGLASS_DEFAULT_STAGES), STAGE_ORDER)

    def test_the_config_default_composes_without_refusal(self):
        chosen = resolve_stages(list(DEFAULT_HOURGLASS_STAGES))
        self.assertEqual(chosen["stages"], list(STAGE_ORDER))


class PlanLaneWiringTests(unittest.TestCase):
    """The composition is called by the plan lane, not merely available.

    A composition owner nothing calls would be the unpublished-API trap
    canon item 15 warns about, so the decision is pinned onto the plan
    envelope the lane actually returns.
    """

    def _plan(self, **kwargs):
        from harness.spend import SpendGovernor
        from harness.waist import compose_plan
        from tests._fake import FakeTransport, m
        fake = FakeTransport(models=[m("m/cheap")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
        return compose_plan(
            transport=fake, api_key="k", governor=gov, ledger=None,
            opts_goal="Update the shipments helper", candidate_files=[],
            root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            execute=False, **kwargs)

    def test_the_plan_envelope_carries_the_composition(self):
        plan = self._plan(token_budget=TokenBudget(
            "run", max_input_tokens=200000, max_output_tokens=64000))
        comp = plan["composition"]
        self.assertEqual([s["stage"] for s in comp["stages"]],
                         list(STAGE_ORDER))
        self.assertEqual(comp["bypassed"], {})
        self.assertEqual(comp["run_budget"], "run")

    def test_the_envelope_view_is_json_serialisable(self):
        import json
        plan = self._plan(token_budget=TokenBudget("run"))
        json.dumps(plan["composition"])  # budgets stay out of the envelope

    def test_a_declared_subset_narrows_what_the_lane_composes(self):
        plan = self._plan(token_budget=TokenBudget("run"),
                          stages=[STAGE_CONTEXT, STAGE_PLANNING])
        self.assertEqual([s["stage"] for s in plan["composition"]["stages"]],
                         [STAGE_CONTEXT, STAGE_PLANNING])

    def test_a_supplied_artifact_is_visible_on_the_envelope(self):
        plan = self._plan(token_budget=TokenBudget("run"), supplied_brief=True)
        self.assertIn(STAGE_CONTEXT, plan["composition"]["bypassed"])

    def test_without_a_budget_the_lane_behaves_exactly_as_before(self):
        # Backwards compatibility is a requirement, not an accident: every
        # pre-HV-4 caller passes no budget, so no composition key appears.
        plan = self._plan()
        self.assertNotIn("composition", plan)

    def test_an_unknown_stage_from_config_fails_the_lane_loudly(self):
        with self.assertRaises(HarnessError):
            self._plan(token_budget=TokenBudget("run"), stages=["teleport"])


if __name__ == "__main__":
    unittest.main()
