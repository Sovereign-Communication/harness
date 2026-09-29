"""HV-4: stage composition selects which stages run, and nothing else.

Composition is deliberately a selection concern. These tests pin the
selection rules (declared subset, canonical order, refused names, no
duplicates) and the supplied-artifact bypasses, and they pin that the
selection never quietly becomes a second source of truth: the config
default and the composition owner's stage order must agree.

The freshness-gated bypass and the per-stage state bound are ported across
from the salvaged ``harness/stages.py`` implementation and re-pointed at
this one owner. They are kept because losing either would be a real
regression: a drifted brief that skipped intake, and a selected stage that
was silently absent, are both failures this slice exists to prevent.
"""
import os
import unittest

from harness.brief import build_brief, freshness_report
from harness.config import DEFAULT_HOURGLASS_STAGES
from harness.errors import HarnessError
from harness.token_budget import TokenBudget
from harness.waist import (
    HOURGLASS_DEFAULT_STAGES,
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STAGE_ORDER,
    STAGE_PLANNING,
    STAGE_STATES,
    STAGE_VERIFICATION,
    STATE_COMPLETED,
    STATE_PENDING,
    STATE_SKIPPED,
    compose_stages,
    composition_envelope,
    resolve_stages,
    stage_budget,
    stage_states,
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
        execute = kwargs.pop("execute", False)
        return compose_plan(
            transport=fake, api_key="k", governor=gov, ledger=None,
            opts_goal="Update the shipments helper", candidate_files=[],
            root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            execute=execute, **kwargs)

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

    def test_selected_planning_invokes_owner_and_reports_result_and_allowance(self):
        from unittest.mock import patch
        from harness.waist import PlanningOutcome, OUTCOME_DEFER
        brief = {"estimated_tokens": 17, "grounding": {"sources": []}}
        outcome = PlanningOutcome(OUTCOME_DEFER, reason="no_evidence_cited",
                                  brief=brief,
                                  budget={"label": "planning", "max_input_tokens": 8000})
        budget = TokenBudget("run", max_input_tokens=20000,
                             max_output_tokens=4000)
        with patch("harness.waist.run_planning", return_value=outcome) as run:
            plan = self._plan(token_budget=budget,
                              stages=[STAGE_PLANNING], brief=brief)
        run.assert_called_once()
        args = run.call_args.kwargs
        self.assertEqual(args["goal"], "Update the shipments helper")
        self.assertIs(args["brief"], brief)
        self.assertIs(args["budget"], budget)
        self.assertEqual(args["stage_budget"].snapshot()["parent"], "run")
        self.assertEqual(plan["planning"]["kind"], OUTCOME_DEFER)
        self.assertEqual(plan["planning"]["budget"]["label"], "planning")
        stage = next(s for s in plan["composition"]["stages"]
                     if s["stage"] == STAGE_PLANNING)
        self.assertEqual(stage["state"], "completed")

    def test_planning_outcomes_gate_only_the_composed_dag_when_not_sufficient(self):
        from unittest.mock import patch
        from harness.waist import (
            PlanningOutcome, OUTCOME_DEFER, OUTCOME_EVIDENCE_REQUEST,
            OUTCOME_PLAN, OUTCOME_SUFFICIENT,
        )

        outcomes = [
            (PlanningOutcome(OUTCOME_SUFFICIENT), "planned", None),
            (PlanningOutcome(OUTCOME_DEFER, reason="evidence conflicts"),
             "refused", "evidence conflicts"),
            (PlanningOutcome(
                OUTCOME_EVIDENCE_REQUEST, reason="need a source",
                evidence_request=[{"source": "README.md", "reason": "verify"}]),
             "refused", "need a source"),
            (PlanningOutcome(OUTCOME_PLAN, reason="validated_bounded_plan",
                             plan={"nodes": []}), "refused",
             "separate plan without an adapter"),
            (type("UnknownOutcome", (), {
                "to_dict": lambda self: {"kind": "unexpected"},
            })(), "refused", "unsupported outcome"),
        ]
        for outcome, expected_status, reason_fragment in outcomes:
            with self.subTest(kind=outcome.to_dict().get("kind")):
                budget = TokenBudget("run", max_input_tokens=20000,
                                     max_output_tokens=4000)
                with patch("harness.waist.run_planning", return_value=outcome):
                    plan = self._plan(
                        execute=True, token_budget=budget,
                        stages=[STAGE_PLANNING])
                self.assertEqual(plan["status"], expected_status)
                self.assertEqual(plan["planning"], outcome.to_dict())
                if expected_status == "refused":
                    self.assertEqual(plan["confirmation"]["verdict"], "refused")
                    self.assertIn(reason_fragment,
                                  plan["confirmation"]["reason"])
                else:
                    self.assertNotIn("confirmation", plan)

    def test_selected_planning_composes_real_defer_once_without_a_live_judge(self):
        brief = {"estimated_tokens": 17, "grounding": {"sources": []}}
        budget = TokenBudget("run", max_input_tokens=20000,
                             max_output_tokens=4000)
        plan = self._plan(token_budget=budget,
                          stages=[STAGE_PLANNING], brief=brief)

        self.assertEqual(plan["planning"]["kind"], "defer")
        self.assertEqual(plan["planning"]["reason"], "no_evidence_cited")
        stage = next(s for s in plan["composition"]["stages"]
                     if s["stage"] == STAGE_PLANNING)
        self.assertEqual(stage["state"], "completed")
        self.assertNotIn(STAGE_PLANNING, plan["composition"]["completed"])
        self.assertNotIn(STAGE_PLANNING, plan["composition"]["skipped"])

    def test_unselected_planning_does_not_invoke_owner(self):
        from unittest.mock import patch
        with patch("harness.waist.run_planning") as run:
            plan = self._plan(token_budget=TokenBudget("run"),
                              stages=[STAGE_CONTEXT])
        run.assert_not_called()
        self.assertNotIn("planning", plan)
        self.assertIn(STAGE_PLANNING, plan["composition"]["skipped"])

    def test_refused_composed_budget_does_not_invoke_planning(self):
        from unittest.mock import patch
        composed = {
            "composed_worst_case": 2.0, "node_ceiling": 1.0,
            "decompose": 0.0, "waist": 1.0, "consensus": 0.0,
            "remaining": 1.0, "plan_ceiling": 1.0,
            "exceeds_remaining": True,
        }
        with patch("harness.waist.composed_worst_case", return_value=composed), \
                patch("harness.waist.run_planning") as run:
            plan = self._plan(
                execute=True, token_budget=TokenBudget("run"),
                stages=[STAGE_PLANNING])
        self.assertEqual(plan["status"], "refused")
        run.assert_not_called()
        self.assertNotIn("planning", plan)

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


class FreshnessGatedBypassTests(unittest.TestCase):
    """A supplied brief buys a bypass only while it is still fresh.

    Ported across from the salvaged ``harness/stages.py`` implementation.
    The surviving owner keeps the stronger of the two bypass rules: the bare
    ``supplied_brief`` flag is trusted, but a real brief *pack* is checked
    against its own pins first, so drifted evidence can never skip intake.
    """

    def _files(self, body):
        return {"a.py": body}

    def _reader(self, files):
        def read(path):
            return files[path]
        return read

    def test_a_fresh_supplied_brief_bypasses_context_only(self):
        files = self._files("alpha" + chr(10))
        brief = build_brief("g", ["a.py"], reader=self._reader(files))
        sel = resolve_stages(brief=brief, reader=self._reader(files))
        self.assertIn(STAGE_CONTEXT, sel["bypassed"])
        self.assertNotIn(STAGE_CONTEXT, sel["stages"])
        # The brief replaces the context stage and NOTHING else: planning,
        # execution, and verification all stay selected and say so.
        for stage in (STAGE_PLANNING, STAGE_EXECUTION, STAGE_VERIFICATION):
            self.assertIn(stage, sel["stages"])

    def test_a_stale_supplied_brief_buys_no_bypass(self):
        files = self._files("v1" + chr(10))
        brief = build_brief("g", ["a.py"], reader=self._reader(files))
        files["a.py"] = "v2 -- drifted from the pin" + chr(10)
        sel = resolve_stages(brief=brief, reader=self._reader(files))
        # The stage RUNS: a drifted brief is not a bypass, it is a reason to
        # rebuild. And the caller can see that its supply was refused.
        self.assertIn(STAGE_CONTEXT, sel["stages"])
        self.assertNotIn(STAGE_CONTEXT, sel["bypassed"])
        self.assertIn(STAGE_CONTEXT, sel["denied"])
        self.assertFalse(
            freshness_report(brief, reader=self._reader(files))["fresh"])

    def test_a_brief_with_no_sources_is_never_fresh(self):
        sel = resolve_stages(brief={"grounding": {"sources": []}})
        self.assertIn(STAGE_CONTEXT, sel["stages"])
        self.assertIn(STAGE_CONTEXT, sel["denied"])

    def test_the_denied_bypass_is_visible_on_the_envelope(self):
        files = self._files("v1" + chr(10))
        brief = build_brief("g", ["a.py"], reader=self._reader(files))
        files["a.py"] = "v2" + chr(10)
        comp = compose_stages(
            budget=TokenBudget("run", max_input_tokens=20000,
                               max_output_tokens=4000),
            brief=brief, reader=self._reader(files))
        env = composition_envelope(comp)
        self.assertIn(STAGE_CONTEXT, env["denied_bypass"])
        self.assertIn(STAGE_CONTEXT,
                      [s["stage"] for s in env["stages"]])


class StageStateTests(unittest.TestCase):
    """Every declared stage is visible in exactly one declared state.

    Ported across from the salvaged ``harness/stages.py`` implementation. The
    capability kept here is the bound itself: a stage that is selected and
    silently absent is the one thing a composition must never produce.
    """

    def _comp(self, **kw):
        kw.setdefault("budget", TokenBudget("run", max_input_tokens=20000,
                                            max_output_tokens=4000))
        return compose_stages(**kw)

    def test_every_declared_stage_always_reports_a_state(self):
        states = stage_states(self._comp())
        self.assertEqual(sorted(states), sorted(STAGE_ORDER))
        for name, state in states.items():
            self.assertIn(state, STAGE_STATES, msg=name)

    def test_a_stage_that_did_not_compose_is_skipped_with_a_reason(self):
        comp = self._comp(declared=[STAGE_CONTEXT, STAGE_PLANNING])
        states = stage_states(comp)
        self.assertEqual(states[STAGE_EXECUTION], STATE_SKIPPED)
        self.assertEqual(states[STAGE_VERIFICATION], STATE_SKIPPED)
        # The skip is visible evidence, not a silent drop.
        env = composition_envelope(comp, states=states)
        self.assertIn(STAGE_EXECUTION, env["skipped"])
        self.assertIn(STAGE_EXECUTION, comp["bypassed"]
                      or {STAGE_EXECUTION: "not selected"})

    def test_execution_and_verification_are_selected_but_pending(self):
        # Both are selected and budgeted, but dispatching a work package is
        # HV-5's contract and verification is independent completion
        # authority -- so this slice composes them and does not run them.
        states = stage_states(self._comp())
        self.assertEqual(states[STAGE_EXECUTION], STATE_PENDING)
        self.assertEqual(states[STAGE_VERIFICATION], STATE_PENDING)

    def test_a_caller_that_actually_ran_a_stage_reports_it(self):
        # This owner budgets stages; it does not perform them, so it cannot
        # mark one completed. A caller that DID run a stage says so through
        # the envelope's ``states`` seam -- which is how HV-5 will report
        # execution, and why ``completed`` is in the declared vocabulary even
        # though nothing here produces it.
        comp = self._comp()
        env = composition_envelope(
            comp, states=dict(stage_states(comp), **{STAGE_CONTEXT:
                                                      STATE_COMPLETED}))
        self.assertEqual(
            [e["state"] for e in env["stages"] if e["stage"] == STAGE_CONTEXT],
            [STATE_COMPLETED])
        self.assertNotIn(STAGE_CONTEXT, env["skipped"])

    def test_the_envelope_round_trips_through_json(self):
        import json
        env = composition_envelope(self._comp())
        self.assertEqual(json.loads(json.dumps(env)), env)
        self.assertIn("run_budget", env)
        for entry in env["stages"]:
            self.assertIn("state", entry)
            self.assertIn("max_input_tokens", entry)


class IntakeBriefTests(unittest.TestCase):
    """HV-2-use: the brief is the composed plan run's intake artifact.

    The consumer condition was tracked as prose for a while and then as its
    own row; these tests are what makes the last half of it a fact rather
    than a claim. Every one is hermetic: real temp files, no network, no
    model, no Jev call.
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="harness-intake-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def _settings(self, **overrides):
        from types import SimpleNamespace
        base = dict(use_free=True, token_budget_input=200000,
                    token_budget_output=64000, hourglass_stages=None)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_intake_brief_measures_and_lints_a_real_file(self):
        from harness.waist import intake_brief
        path = self._write("real.py", "x = 1\n")
        intake = intake_brief("update real.py", [path])
        self.assertEqual(intake["brief"]["goal"], "update real.py")
        self.assertEqual(intake["issues"], [])
        # The size is MEASURED, and it is the number that caps later stages.
        self.assertGreater(intake["tokens"], 0)
        self.assertEqual(intake["excluded"], [])
        self.assertEqual(intake["brief"]["scope"]["included"], [path])

    def test_an_unreadable_candidate_is_excluded_not_fatal(self):
        from harness.waist import intake_brief
        path = self._write("real.py", "x = 1\n")
        missing = os.path.join(self.tmp, "not_yet.py")
        # A goal whose first node creates a file is an ordinary goal, so a
        # candidate that does not exist may not take the run down. It is
        # recorded through the pack's own exclusion vocabulary instead.
        intake = intake_brief("add not_yet.py", [path, missing])
        self.assertEqual(intake["excluded"], [missing])
        self.assertIn(missing, intake["brief"]["scope"]["excluded"])
        self.assertEqual(intake["brief"]["scope"]["included"], [path])
        self.assertEqual(intake["brief"]["coverage"]["omitted"], 0)

    def test_the_arguments_hand_the_brief_only_when_context_is_selected(self):
        from harness.waist import compose_arguments
        path = self._write("real.py", "x = 1\n")
        from harness.brief import estimate_brief_tokens
        selected = compose_arguments(self._settings(), goal="g", files=[path])
        self.assertTrue(selected["supplied_brief"])
        self.assertEqual(selected["brief"]["goal"], "g")
        # Re-measured, exactly as the planning ladder measures a pack, rather
        # than read back from the pack's own `estimated_tokens` field: that
        # field is written before it exists, so it is a slightly different
        # number and the ladder's convention is the fresh one.
        self.assertEqual(selected["brief_tokens"],
                         estimate_brief_tokens(selected["brief"]))
        self.assertIn(STAGE_CONTEXT, selected["stages"])
        # A run that dropped `context` must not pay to curate an artifact
        # nothing will consume, and must not claim it has one.
        dropped = compose_arguments(self._settings(hourglass_stages=["planning"]),
                                   goal="g", files=[path])
        self.assertNotIn("brief", dropped)
        self.assertNotIn("supplied_brief", dropped)
        self.assertNotIn("brief_tokens", dropped)

    def test_the_plan_lane_consumes_a_supplied_brief(self):
        from harness.spend import SpendGovernor
        from harness.waist import compose_arguments, compose_plan
        from tests._fake import FakeTransport, m
        path = self._write("real.py", "x = 1\n")
        settings = self._settings()
        arguments = compose_arguments(settings, goal="update real.py",
                                      files=[path])
        fake = FakeTransport(models=[m("m/cheap")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
        plan = compose_plan(
            transport=fake, api_key="k", governor=gov, ledger=None,
            opts_goal="update real.py", candidate_files=[path],
            root=self.tmp, execute=False, **arguments)
        comp = plan["composition"]
        # Consumed, not merely accepted: the stage's work already exists, so
        # it does not re-run, and its outcome is complete rather than skipped.
        self.assertIn(STAGE_CONTEXT, comp["bypassed"])
        self.assertIn(STAGE_CONTEXT, comp["completed"])
        self.assertNotIn(STAGE_CONTEXT, comp["skipped"])
        # Its measured size is what caps the LATER stages. `planning` is the
        # first *composed* stage here (the context stage did not compose), and
        # HV-4's contract is that the first composing stage inherits the run's
        # own ceiling -- so the brief's cap lands on the stage after it,
        # floored at the contract's minimum stage allowance. That is what
        # "preflights the later stages" means, and this pins both halves.
        from harness.waist import MIN_STAGE_INPUT_TOKENS
        planning = [entry for entry in comp["stages"]
                    if entry["stage"] == STAGE_PLANNING][0]
        execution = [entry for entry in comp["stages"]
                     if entry["stage"] == STAGE_EXECUTION][0]
        self.assertEqual(planning["max_input_tokens"], 200000)
        self.assertLessEqual(execution["max_input_tokens"],
                             max(arguments["brief_tokens"],
                                 MIN_STAGE_INPUT_TOKENS))
        # Every declared stage still lands in exactly one bucket.
        buckets = ([entry["stage"] for entry in comp["stages"]]
                   + list(comp["skipped"]) + list(comp["completed"]))
        self.assertEqual(sorted(buckets), sorted(HOURGLASS_DEFAULT_STAGES))

    def test_a_brief_over_no_readable_source_buys_no_bypass(self):
        from harness.spend import SpendGovernor
        from harness.waist import compose_arguments, compose_plan
        from tests._fake import FakeTransport, m
        missing = os.path.join(self.tmp, "not_yet.py")
        arguments = compose_arguments(self._settings(), goal="add it",
                                      files=[missing])
        fake = FakeTransport(models=[m("m/cheap")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
        plan = compose_plan(
            transport=fake, api_key="k", governor=gov, ledger=None,
            opts_goal="add it", candidate_files=[missing], root=self.tmp,
            execute=False, **arguments)
        comp = plan["composition"]
        # An empty pack is a brief, but it is not EVIDENCE -- so the bypass is
        # denied and the stage composes instead of pretending intake is done.
        self.assertNotIn(STAGE_CONTEXT, comp["bypassed"])
        self.assertIn(STAGE_CONTEXT,
                      [entry["stage"] for entry in comp["stages"]])
