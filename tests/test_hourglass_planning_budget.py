"""HV-4: planning over successively smaller allowances, and the waist's
terminal contract.

Two invariants carry this slice. First, a stage cannot raise its own
limits: composition only ever asks ``TokenBudget`` (HV-3) to narrow, so the
ceiling falls stage over stage and is additionally capped by the brief the
stage will actually read. Second, the planning waist may only *stop*,
*plan*, *ask*, or *defer* -- and a plan is validated by the DAG owner, an
evidence request is bounded, and a defer says why.

The second half of this module is the planning *runner* ported across from
the salvaged ``harness/stages.py`` implementation: the decreasing-allowance
ladder, the brief-fit window search, the typed run record, and the
successive-round behaviour. It is kept because losing it would be a real
regression, and it is re-pointed at the ONE surviving owner
(``harness/waist.py``) rather than left beside a second implementation.
"""
import os
import tempfile
import unittest

from harness.brief import build_brief
from harness.dag import TaskDAG
from harness.errors import HarnessError
from harness.token_budget import TokenBudget
from harness.waist import (
    DEFAULT_MAX_PLAN_NODES,
    MAX_EVIDENCE_QUESTIONS,
    MIN_ROUND_TOKENS,
    MIN_STAGE_INPUT_TOKENS,
    OUTCOME_DEFER,
    OUTCOME_EVIDENCE_REQUEST,
    OUTCOME_PLAN,
    OUTCOME_SUFFICIENT,
    PLAN_OUTCOMES,
    STAGE_CONTEXT,
    STAGE_PLANNING,
    PlanningOutcome,
    compose_stages,
    plan_outcome,
    planning_ladder,
    run_planning,
)

PLAN = {
    "nodes": [
        {"node_id": "n1", "instruction": "read the ledger",
         "target_files": ["harness/session.py"], "dependencies": []},
    ],
}

BIG = "".join("line {0}\n".format(n) for n in range(1, 4001))


def _reader(files):
    def read(path):
        return files[path]
    return read


#: The reader that produced every gapped brief below. It is passed to
#: validation as well, because a brief built by a fake reader does not lint
#: against the real filesystem -- and composition refuses to plan on a brief
#: that fails its own lint, which is exactly what would happen if a test
#: forgot it.
GAPPED_FILES = {"a.py": "alpha\n", "b.py": "beta\n", "c.py": "gamma\n"}
GAPPED_READER = _reader(GAPPED_FILES)


def _plan(nodes=1):
    return {"nodes": [
        {"node_id": "n{0}".format(i), "instruction": "do work {0}".format(i),
         "target_files": ["a.py"]} for i in range(nodes)]}


def _run_budget():
    return TokenBudget("run", max_input_tokens=200000, max_output_tokens=64000)


class DecreasingAllowanceTests(unittest.TestCase):
    def test_successive_stages_get_strictly_smaller_ceilings(self):
        comp = compose_stages(budget=_run_budget())
        inputs = [s["max_input_tokens"] for s in comp["stages"]]
        outputs = [s["max_output_tokens"] for s in comp["stages"]]
        self.assertEqual(len(inputs), 4)
        for earlier, later in zip(inputs, inputs[1:]):
            self.assertLess(later, earlier)
        for earlier, later in zip(outputs, outputs[1:]):
            self.assertLess(later, earlier)

    def test_the_first_stage_inherits_the_run_and_the_rest_narrow(self):
        comp = compose_stages(budget=_run_budget())
        first, second = comp["stages"][0], comp["stages"][1]
        self.assertEqual(first["max_input_tokens"], 200000)
        self.assertEqual(first["max_output_tokens"], 64000)
        self.assertLess(second["max_input_tokens"], first["max_input_tokens"])
        self.assertLess(second["max_output_tokens"], first["max_output_tokens"])

    def test_a_stage_can_never_widen_its_own_parent(self):
        # Structural, not promised: TokenBudget refuses a child above its
        # parent, so composition cannot hand out a wider stage even by
        # accident. Asserted against the owner, not against a flag.
        run = _run_budget()
        comp = compose_stages(budget=run)
        for entry in comp["stages"][1:]:
            with self.assertRaises(HarnessError) as ctx:
                entry["budget"].stage("sneaky",
                                      max_input_tokens=entry["max_input_tokens"] + 1)
            self.assertIn("only narrow", str(ctx.exception))

    def test_a_measured_brief_caps_the_later_stages(self):
        # Planning is preflighted against the evidence it will read: a
        # 3,000-token brief means the first stage after intake plans over
        # ~3,000 tokens, not over the run's whole 200,000. The cap and the
        # per-stage narrowing compose, so later stages are still smaller.
        small = compose_stages(budget=_run_budget(), brief_tokens=3000)
        unmeasured = compose_stages(budget=_run_budget())
        self.assertEqual(small["stages"][1]["max_input_tokens"], 3000)
        self.assertLess(small["stages"][1]["max_input_tokens"],
                        unmeasured["stages"][1]["max_input_tokens"])
        inputs = [s["max_input_tokens"] for s in small["stages"]]
        for earlier, later in zip(inputs, inputs[1:]):
            self.assertLess(later, earlier)
        for entry in small["stages"][1:]:
            self.assertLessEqual(entry["max_input_tokens"],
                                 max(3000, MIN_STAGE_INPUT_TOKENS))

    def test_a_brief_never_lifts_a_stage_above_its_own_ceiling(self):
        # A brief BIGGER than the run allowance must not widen anything --
        # the ceiling is the composition's, not the brief's.
        comp = compose_stages(budget=_run_budget(), brief_tokens=10_000_000)
        inputs = [s["max_input_tokens"] for s in comp["stages"]]
        self.assertEqual(inputs[0], 200000)
        for earlier, later in zip(inputs, inputs[1:]):
            self.assertLess(later, earlier)

    def test_a_tiny_brief_still_leaves_a_usable_floor(self):
        comp = compose_stages(budget=_run_budget(), brief_tokens=8)
        for entry in comp["stages"][1:]:
            self.assertGreaterEqual(entry["max_input_tokens"],
                                    min(MIN_STAGE_INPUT_TOKENS,
                                        entry["max_input_tokens"]))
            self.assertGreater(entry["max_input_tokens"], 0)

    def test_a_negative_brief_measurement_is_refused(self):
        with self.assertRaises(HarnessError):
            compose_stages(budget=_run_budget(), brief_tokens=-1)

    def test_a_composed_stage_budget_still_refuses_a_call_it_cannot_pay(self):
        # The stage budget is a real HV-3 budget, not a label: a call over
        # the stage ceiling is refused before dispatch even though the run
        # could pay it.
        comp = compose_stages(budget=_run_budget())
        planning = comp["stages"][1]["budget"]
        with self.assertRaises(HarnessError):
            planning.allowance(planning.max_input_tokens + 1)


class PlanningWaistOutcomeTests(unittest.TestCase):
    def test_runner_accepts_the_composed_planning_child(self):
        run = _run_budget()
        composition = compose_stages(budget=run, declared=[STAGE_PLANNING])
        stage_budget = composition["stages"][0]["budget"]
        outcome = run_planning(goal="g", budget=run,
                               stage_budget=stage_budget, files=[], rounds=1)
        self.assertEqual(outcome.kind, OUTCOME_DEFER)
        self.assertEqual(outcome.budget["parent"], run.label)

    def test_runner_rejects_foreign_or_wrong_stage_budgets(self):
        run = _run_budget()
        foreign = _run_budget().stage(STAGE_PLANNING)
        wrong_stage = run.stage(STAGE_CONTEXT)
        for candidate in (foreign, wrong_stage):
            with self.subTest(candidate=candidate.label):
                with self.assertRaisesRegex(
                        HarnessError, "planning child of budget"):
                    run_planning(goal="g", budget=run,
                                 stage_budget=candidate, files=[], rounds=1)

    def test_a_sufficient_answer_stops_without_a_plan(self):
        self.assertEqual(plan_outcome(OUTCOME_SUFFICIENT),
                         {"outcome": OUTCOME_SUFFICIENT})

    def test_a_valid_plan_is_validated_by_the_dag_owner(self):
        out = plan_outcome(OUTCOME_PLAN, plan=PLAN)
        self.assertEqual(out["outcome"], OUTCOME_PLAN)
        self.assertEqual(len(out["plan"]["nodes"]), 1)

    def test_a_plan_with_an_unknown_dependency_is_refused(self):
        # Composition must not bless a DAG the executor would reject.
        bad = {"nodes": [{"node_id": "n1", "instruction": "x",
                          "target_files": [], "dependencies": ["nope"]}]}
        with self.assertRaises(HarnessError):
            plan_outcome(OUTCOME_PLAN, plan=bad)

    def test_an_empty_plan_is_not_a_plan(self):
        with self.assertRaises(HarnessError) as ctx:
            plan_outcome(OUTCOME_PLAN, plan={"nodes": []})
        self.assertIn("defer", str(ctx.exception))

    def test_a_bounded_evidence_request_is_accepted(self):
        out = plan_outcome(OUTCOME_EVIDENCE_REQUEST,
                           questions=["which ledger?", "which run?"])
        self.assertEqual(out["outcome"], OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(len(out["questions"]), 2)

    def test_an_evidence_request_that_asks_nothing_is_refused(self):
        with self.assertRaises(HarnessError):
            plan_outcome(OUTCOME_EVIDENCE_REQUEST, questions=["", "  "])
        with self.assertRaises(HarnessError):
            plan_outcome(OUTCOME_EVIDENCE_REQUEST, questions=[])

    def test_an_unbounded_evidence_request_is_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            plan_outcome(OUTCOME_EVIDENCE_REQUEST,
                         questions=["q{0}".format(i)
                                    for i in range(MAX_EVIDENCE_QUESTIONS + 1)])
        self.assertIn("over the bound", str(ctx.exception))
        with self.assertRaises(HarnessError):
            plan_outcome(OUTCOME_EVIDENCE_REQUEST, questions=["x" * 5000])

    def test_an_honest_defer_must_say_why(self):
        out = plan_outcome(OUTCOME_DEFER, reason="the brief has no sources")
        self.assertEqual(out["outcome"], OUTCOME_DEFER)
        self.assertIn("no sources", out["reason"])
        with self.assertRaises(HarnessError) as ctx:
            plan_outcome(OUTCOME_DEFER, reason="   ")
        self.assertIn("silent failure", str(ctx.exception))

    def test_an_unbounded_defer_reason_is_refused(self):
        with self.assertRaises(HarnessError):
            plan_outcome(OUTCOME_DEFER, reason="r" * 5000)

    def test_an_unknown_outcome_is_refused_by_name(self):
        with self.assertRaises(HarnessError) as ctx:
            plan_outcome("wing_it", reason="because")
        self.assertIn("wing_it", str(ctx.exception))
        self.assertEqual(set(PLAN_OUTCOMES),
                         {"sufficient", "plan", "evidence_request", "defer"})


class ComposedRunWalkTests(unittest.TestCase):
    def test_a_full_run_composes_all_stages_with_budgets(self):
        # The shape HV-4 exists to produce: one run budget, four narrowing
        # stages, each with a usable child, and the composition naming the
        # run it belongs to.
        comp = compose_stages(budget=_run_budget())
        self.assertEqual(comp["run_budget"], "run")
        self.assertEqual([s["stage"] for s in comp["stages"]],
                         [STAGE_CONTEXT, STAGE_PLANNING,
                          "execution", "verification"])
        for entry in comp["stages"]:
            self.assertIsInstance(entry["budget"], TokenBudget)

    def test_a_downstream_only_run_skips_intake_and_planning(self):
        # The HV-5/HV-6 shape: a supplied brief plus a supplied plan means
        # only the stages that still have work get an allowance.
        comp = compose_stages(budget=_run_budget(), supplied_brief=True,
                              supplied_plan=True)
        self.assertEqual([s["stage"] for s in comp["stages"]],
                         ["execution", "verification"])
        self.assertEqual(sorted(comp["bypassed"]),
                         sorted([STAGE_CONTEXT, STAGE_PLANNING]))

    def test_the_plan_the_waist_emits_is_the_plan_the_dag_owner_accepts(self):
        out = plan_outcome(OUTCOME_PLAN, plan=PLAN)
        again = TaskDAG.from_dict(out["plan"])
        self.assertEqual([n.node_id for n in again.topological_order()], ["n1"])


if __name__ == "__main__":
    unittest.main()


class FakeJev:
    """A JevPolicy-shaped owner: the composition contract, not a real call.

    The declared signal values sit FLAT on the structural envelope, beside
    ``native`` and ``dimension`` -- the same shape
    ``JevPolicy.evaluate_hourglass_stage`` returns, which is why this fake
    is a contract test and not a convenience.
    """

    def __init__(self, signals=None, native=True, raises=False):
        self.signals = signals or {}
        self.native = native
        self.raises = raises
        self.calls = []

    def evaluate_hourglass_stage(self, dimension, state, **kw):
        self.calls.append((dimension, state, kw))
        if self.raises:
            raise HarnessError("transport is down")
        structural = dict(self.signals)
        structural.update({"native": self.native, "dimension": dimension,
                           "capability": "hourglass-stage-v1"})
        return ({"answers": {}}, structural)


class PlanningLadderTests(unittest.TestCase):
    def test_each_round_is_a_narrower_child_of_the_one_before(self):
        run = TokenBudget("run", max_input_tokens=20000, max_output_tokens=0)
        ladder = planning_ladder(run.stage("planning"), rounds=3)
        self.assertEqual([b.max_input_tokens for b in ladder],
                         [20000, 10000, 5000])
        # A child of the previous round, so the run is charged once for the
        # tokens and no round can out-ask its parent. Read through
        # ``snapshot()`` rather than a private field: parentage is part of the
        # budget's published view, so proving the ladder is nested needs no
        # change to the budget owner.
        self.assertEqual([b.snapshot()["parent"] for b in ladder],
                         [run.label, ladder[0].label, ladder[1].label])

    def test_a_ladder_that_could_widen_or_hold_still_is_refused(self):
        stage = TokenBudget("run", max_input_tokens=8000).stage("planning")
        for bad in (1.0, 1.5, 0, -0.5):
            with self.assertRaises(HarnessError, msg=bad) as ctx:
                planning_ladder(stage, rounds=2, shrink=bad)
            self.assertIn("narrow the ladder", str(ctx.exception))

    def test_the_ladder_stops_before_a_useless_round(self):
        run = TokenBudget("run", max_input_tokens=3000, max_output_tokens=0)
        ladder = planning_ladder(run.stage("planning"), rounds=5)
        # 3000 -> 1500, and 750 < MIN_ROUND_TOKENS, so the ladder ends rather
        # than spending a reservation on a request too small to answer.
        self.assertEqual([b.max_input_tokens for b in ladder], [3000, 1500])
        self.assertGreaterEqual(ladder[-1].max_input_tokens, MIN_ROUND_TOKENS)

    def test_a_ladder_needs_a_budget_and_at_least_one_round(self):
        run = TokenBudget("run", max_input_tokens=8000, max_output_tokens=0)
        with self.assertRaises(HarnessError):
            planning_ladder("not a budget", rounds=2)
        with self.assertRaises(HarnessError):
            planning_ladder(run.stage("planning"), rounds=0)


class BriefFitTests(unittest.TestCase):
    """A brief is shipped only if its MEASURED estimate fits the round."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.big = os.path.join(self.dir.name, "big.py")
        with open(self.big, "w", encoding="utf-8") as handle:
            handle.write(BIG)
        self.small = os.path.join(self.dir.name, "small.py")
        with open(self.small, "w", encoding="utf-8") as handle:
            handle.write("def a():\n    return 1\n")

    def _run(self, **kw):
        base = dict(goal="g", budget=TokenBudget("run",
                                                 max_input_tokens=8000,
                                                 max_output_tokens=0),
                    files=[self.small])
        base.update(kw)
        return run_planning(**base)

    def test_a_round_curates_down_to_what_it_can_pay_for(self):
        outcome = self._run(files=[self.big], budget=TokenBudget(
            "run", max_input_tokens=1024, max_output_tokens=0), rounds=1)
        # 4000 lines cannot fit a 1024-token round whole, so the round curates
        # DOWN to the largest brief that does fit and labels the truncation --
        # instead of shipping a brief it cannot pay for, and instead of
        # refusing to look at the file at all.
        self.assertIsNotNone(outcome.brief)
        self.assertLessEqual(outcome.brief["estimated_tokens"], 1024)
        self.assertEqual(outcome.rounds[0]["brief_tokens"],
                         outcome.brief["estimated_tokens"])
        self.assertEqual(outcome.rounds[0]["grounding_issues"], 0)
        # What it could not represent is visible, not silently dropped.
        self.assertTrue(outcome.brief["windows"][0]["truncated"])

    def test_an_allowance_no_brief_can_meet_defers_instead_of_shipping_one(self):
        # Even a brief with no window at all carries the pack's own metadata.
        # When even that does not fit the round, the honest outcome is a defer
        # naming the reason -- never a brief over budget.
        outcome = self._run(files=[self.big], budget=TokenBudget(
            "run", max_input_tokens=64, max_output_tokens=0), rounds=1)
        self.assertEqual(outcome.kind, OUTCOME_DEFER)
        self.assertEqual([r.get("reason") for r in outcome.rounds],
                         ["brief_exceeds_allowance"])
        self.assertIsNone(outcome.brief)
        self.assertEqual(outcome.reason, "no_brief_fit_any_round")

    def test_a_larger_allowance_is_never_worse_than_a_smaller_one(self):
        # The window search is monotone: a bigger allowance can only buy more
        # evidence, never less. (A step sized from the token excess once failed
        # here, because the estimate is dominated by pack metadata rather than
        # by the window.)
        windows = []
        for allowance in (512, 1024, 2048, 4096):
            outcome = self._run(files=[self.big], rounds=1, budget=TokenBudget(
                "run", max_input_tokens=allowance, max_output_tokens=0))
            self.assertIsNotNone(outcome.brief, allowance)
            windows.append(outcome.brief["coverage"]["windows_cited"])
            self.assertLessEqual(outcome.brief["estimated_tokens"], allowance)
        self.assertEqual(windows, sorted(windows))

    def test_a_large_brief_is_curated_to_fit_a_small_round(self):
        run = TokenBudget("run", max_input_tokens=4000, max_output_tokens=0)
        outcome = self._run(budget=run, files=[self.big] * 6, rounds=1)
        self.assertIsNotNone(outcome.brief)
        self.assertLessEqual(outcome.brief["estimated_tokens"],
                             run.stage("planning").max_input_tokens)
        # The shrink came from the window, never from the allowance.
        self.assertEqual(run.max_input_tokens, 4000)
        self.assertEqual(outcome.rounds[0]["max_input_tokens"], 4000)

    def test_a_declared_conflict_becomes_a_bounded_evidence_request(self):
        reader = _reader({"a.py": "alpha\n", "b.py": "beta\n"})
        brief = build_brief("g", ["a.py", "b.py"], reader=reader,
                            conflicts=[{"description": "a.py and b.py "
                                                     "disagree on the default",
                                        "source_ids": ["s1", "s2"]}])
        outcome = self._run(brief=brief, files=[], reader=reader)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(len(outcome.evidence_request), 1)
        item = outcome.evidence_request[0]
        self.assertEqual(item["source"], "s1, s2")
        self.assertIn("disagree", item["reason"])

    def test_the_evidence_request_bound_holds_with_many_conflicts(self):
        reader = _reader({"a.py": "alpha\n", "b.py": "beta\n",
                          "c.py": "gamma\n", "d.py": "delta\n"})
        brief = build_brief("g", ["a.py", "b.py", "c.py", "d.py"], reader=reader,
                            conflicts=[
                                {"description": f"conflict {i}",
                                 "source_ids": ["s1", "s2"]}
                                for i in range(9)])
        outcome = self._run(brief=brief, files=[], reader=reader)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(len(outcome.evidence_request),
                         MAX_EVIDENCE_QUESTIONS)

    def test_a_measured_fit_shrinks_the_window_rather_than_the_allowance(self):
        run = TokenBudget("run", max_input_tokens=4000, max_output_tokens=0)
        outcome = self._run(budget=run, files=[self.big, self.big])
        allowance = run.stage("planning").max_input_tokens
        # Either the round fitted a brief it can pay for, or it refused to
        # ship one at all. It never ships a brief over its allowance, and it
        # never moved the allowance to make the brief fit.
        if outcome.brief is not None:
            self.assertLessEqual(outcome.brief["estimated_tokens"], allowance)
        else:
            self.assertIn("brief_exceeds_allowance",
                          [r.get("reason") for r in outcome.rounds])
        self.assertEqual(run.max_input_tokens, 4000)


class PlanningOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.a = os.path.join(self.dir.name, "a.py")
        with open(self.a, "w", encoding="utf-8") as handle:
            handle.write("def a():\n    return 1\n")
        self.b = os.path.join(self.dir.name, "b.py")
        with open(self.b, "w", encoding="utf-8") as handle:
            handle.write("def b():\n    return 2\n")
        self.huge = os.path.join(self.dir.name, "huge.py")
        with open(self.huge, "w", encoding="utf-8") as handle:
            handle.write(BIG)

    def _gapped_brief(self):
        """A brief that admits a gap: three sources, room for the first."""
        brief = build_brief("g", ["a.py", "b.py", "c.py"],
                            reader=GAPPED_READER, max_total_chars=6)
        self.assertEqual(brief["omitted"], ["b.py", "c.py"],
                         "test needs a brief with a gap")
        return brief

    def _run(self, *, budget_input=20000, **kw):
        base = dict(goal="g", budget=TokenBudget(
            "run", max_input_tokens=budget_input, max_output_tokens=0),
            files=[self.a])
        base.update(kw)
        return run_planning(**base)

    def test_a_covered_brief_stops_the_ladder_as_sufficient(self):
        jev = FakeJev({"plan_sound": 0.9, "plan_evidence_requested": 0.0})
        outcome = self._run(jev_policy=jev)
        self.assertEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(outcome.reason, "brief_covers_the_request")
        self.assertEqual(len(outcome.rounds), 1)
        # The artifact was already sufficient, so the semantic question was
        # never asked -- no call, no spend.
        self.assertEqual(jev.calls, [])
        self.assertIn("max_input_tokens", outcome.budget)

    def test_an_unrepresented_source_becomes_a_bounded_evidence_request(self):
        reader = _reader({"a.py": "alpha\n", "b.py": "b" * 9000,
                          "c.py": "c" * 9000})
        brief = build_brief("g", ["a.py", "b.py", "c.py"], reader=reader,
                            max_total_chars=300)
        self.assertTrue(brief["omitted"])
        outcome = self._run(brief=brief, files=[], reader=reader)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(outcome.reason, "bounded_evidence_request")
        self.assertLessEqual(len(outcome.evidence_request),
                             MAX_EVIDENCE_QUESTIONS)
        for item in outcome.evidence_request:
            self.assertIn("source", item)
            self.assertIn("reason", item)
        self.assertIn("c.py", [i["source"] for i in outcome.evidence_request])

    def test_a_native_jev_verdict_may_end_the_ladder_on_remaining_evidence(self):
        brief = self._gapped_brief()
        jev = FakeJev({"plan_sound": 0.8, "plan_evidence_requested": 0.0})
        outcome = self._run(brief=brief, files=[], reader=GAPPED_READER,
                           jev_policy=jev)
        self.assertEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(jev.calls[0][0], "plan_soundness")
        self.assertTrue(outcome.jev_signals)
        self.assertTrue(outcome.rounds[0]["jev_native"])

    def test_a_fallback_or_failed_jev_never_ends_the_ladder(self):
        brief = self._gapped_brief()
        fallback = FakeJev({"plan_evidence_requested": 0.0}, native=False)
        outcome = self._run(brief=brief, files=[], reader=GAPPED_READER,
                            jev_policy=fallback)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertFalse(outcome.rounds[0]["jev_native"])
        # The question WAS asked: a non-native answer refuses to end the
        # ladder, it is not the absence of an answer.
        self.assertEqual(len(fallback.calls), 1)

        broken = FakeJev(raises=True)
        outcome = self._run(brief=brief, files=[], reader=GAPPED_READER,
                            jev_policy=broken)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(len(broken.calls), 1)
        self.assertFalse(outcome.rounds[0]["jev_native"])

    def test_an_evidence_request_at_confidence_ends_nothing(self):
        # Jev saying "more evidence required" at 0.9 is a real request; the
        # stopping rule is code's and it is not negotiable by a low noul.
        brief = self._gapped_brief()
        jev = FakeJev({"plan_sound": 0.7, "plan_evidence_requested": 0.9})
        outcome = self._run(brief=brief, files=[], reader=GAPPED_READER,
                           jev_policy=jev)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertTrue(outcome.rounds[0]["jev_native"])
        self.assertAlmostEqual(outcome.jev_signals["plan_evidence_requested"],
                               0.9)

    def test_a_failed_lint_keeps_jev_out_of_it_entirely(self):
        brief = self._gapped_brief()
        brief["windows"][0]["content"] = "not a span of the pinned source"
        jev = FakeJev({"plan_evidence_requested": 0.0})
        outcome = self._run(brief=brief, files=[], reader=GAPPED_READER,
                           jev_policy=jev)
        self.assertNotEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(jev.calls, [])
        self.assertGreater(outcome.rounds[0]["grounding_issues"], 0)

    def test_a_brief_citing_nothing_is_never_sufficient(self):
        # An empty pack has no gaps and no conflicts; without an explicit
        # source bound it would read as clean evidence.
        empty = build_brief("g", [], reader=_reader({}))
        jev = FakeJev({"plan_evidence_requested": 0.0})
        outcome = self._run(brief=empty, files=[], reader=_reader({}),
                            jev_policy=jev)
        self.assertNotEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(outcome.kind, OUTCOME_DEFER)
        self.assertEqual(outcome.reason, "no_evidence_cited")
        self.assertEqual(jev.calls, [])

    def test_a_failed_grounding_lint_outranks_a_jev_yes(self):
        # A brief that does not lint is not evidence, whatever a model says.
        reader = _reader({"a.py": "a\n"})
        brief = build_brief("g", ["a.py"], reader=reader)
        brief["windows"][0]["content"] = "not a span of the pinned source"
        jev = FakeJev({"plan_evidence_requested": 0.0})
        outcome = self._run(brief=brief, files=[], reader=reader,
                            jev_policy=jev)
        self.assertNotEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(jev.calls, [])
        self.assertGreater(outcome.rounds[0]["grounding_issues"], 0)

    def test_a_planner_that_returns_a_valid_bounded_plan_emits_a_plan(self):
        outcome = self._run(brief=self._gapped_brief(), files=[],
                            reader=GAPPED_READER, planner=lambda goal, brief: _plan(2))
        self.assertEqual(outcome.kind, OUTCOME_PLAN)
        self.assertEqual(outcome.reason, "validated_bounded_plan")
        self.assertEqual(len(outcome.plan.nodes), 2)
        self.assertEqual(outcome.to_dict()["plan"]["nodes"][0]["node_id"],
                         "n0")

    def test_an_unbounded_or_invalid_plan_is_refused_not_trimmed(self):
        gapped = self._gapped_brief()
        over = self._run(brief=gapped, files=[], reader=GAPPED_READER,
                         planner=lambda goal, brief: _plan(
                             DEFAULT_MAX_PLAN_NODES + 1))
        self.assertEqual(over.kind, OUTCOME_DEFER)
        self.assertIn("over the bound", over.reason)

        empty = self._run(brief=gapped, files=[], reader=GAPPED_READER,
                          planner=lambda goal, brief: {"nodes": []})
        self.assertEqual(empty.kind, OUTCOME_DEFER)

        malformed = self._run(brief=gapped, files=[], reader=GAPPED_READER,
                              planner=lambda goal, brief: {
                                  "nodes": [{"instruction": "no node_id"}]})
        self.assertEqual(malformed.kind, OUTCOME_DEFER)
        self.assertIn("plan_rejected", malformed.reason)

    def test_the_node_bound_is_configurable_and_still_bounded(self):
        outcome = self._run(brief=self._gapped_brief(), files=[],
                            reader=GAPPED_READER, planner=lambda goal, brief: _plan(3), max_nodes=2)
        self.assertEqual(outcome.kind, OUTCOME_DEFER)
        self.assertIn("over the bound of 2", outcome.reason)

    def test_a_planner_is_never_consulted_without_a_brief(self):
        # No evidence, no plan: a planner cannot invent the input it needs.
        outcome = self._run(files=[self.huge], rounds=1,
                            budget=TokenBudget("run", max_input_tokens=64,
                                               max_output_tokens=0),
                            planner=lambda goal, brief: _plan(1))
        self.assertEqual(outcome.kind, OUTCOME_DEFER)
        self.assertIsNone(outcome.brief)
        self.assertIsNone(outcome.plan)

    def test_a_bad_jev_policy_or_planner_is_refused_up_front(self):
        with self.assertRaises(HarnessError) as ctx:
            self._run(jev_policy=object())
        self.assertIn("evaluate_hourglass_stage", str(ctx.exception))
        with self.assertRaises(HarnessError):
            self._run(planner="not callable")
        with self.assertRaises(HarnessError):
            self._run(budget="not a budget")

    def test_only_declared_outcomes_can_be_constructed(self):
        # Every declared outcome constructs -- and each one carries the
        # payload its own kind requires, because the record is emitted
        # through the same terminal contract the waist validates against.
        self.assertEqual(PlanningOutcome(OUTCOME_SUFFICIENT).kind,
                         OUTCOME_SUFFICIENT)
        asked = [{"source": "a.py", "reason": "not represented"}]
        self.assertEqual(
            PlanningOutcome(OUTCOME_EVIDENCE_REQUEST,
                            evidence_request=asked).kind,
            OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(
            PlanningOutcome(OUTCOME_DEFER, reason="nothing_left").kind,
            OUTCOME_DEFER)
        with self.assertRaises(HarnessError):
            PlanningOutcome("looks_fine")

    def test_an_outcome_may_not_assert_more_than_its_kind_carries(self):
        # An evidence request with nothing to ask, and a defer with no
        # reason, are the two ways this record could claim more than it
        # holds. Both are refused at construction.
        with self.assertRaises(HarnessError):
            PlanningOutcome(OUTCOME_EVIDENCE_REQUEST)
        with self.assertRaises(HarnessError):
            PlanningOutcome(OUTCOME_DEFER, reason="   ")


class ScriptedJev(FakeJev):
    """A Jev owner that answers differently on each call.

    A verdict that cannot change between rounds is not worth paying for
    twice, so the interesting ladder test is a judge whose answer MOVES as
    the evidence narrows.
    """

    def __init__(self, per_call):
        super().__init__(signals=per_call[0], native=True)
        self.per_call = list(per_call)
        self.index = 0

    def evaluate_hourglass_stage(self, dimension, state, **kw):
        self.signals = self.per_call[min(self.index,
                                         len(self.per_call) - 1)]
        self.index += 1
        return super().evaluate_hourglass_stage(dimension, state, **kw)


class SuccessiveRoundTests(unittest.TestCase):
    """A ladder that only ever runs its first round is a decorative ladder."""

    def _run(self, **kw):
        base = dict(goal="g", budget=TokenBudget("run",
                                                 max_input_tokens=20000,
                                                 max_output_tokens=0),
                    files=["a.py", "b.py", "c.py"], reader=GAPPED_READER,
                    rounds=2)
        base.update(kw)
        return run_planning(**base)

    def test_a_second_round_curates_a_narrower_brief_and_asks_again(self):
        # Round one: the code bound says the brief covers the request, so the
        # ladder stops and the judge is never consulted. A second round only
        # happens when round one was genuinely inconclusive.
        jev = ScriptedJev([{"plan_sound": 0.7,
                             "plan_evidence_requested": 0.9}])
        outcome = self._run(jev_policy=jev)
        self.assertEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(len(outcome.rounds), 1)
        self.assertEqual(jev.calls, [])

    def test_a_narrower_round_can_change_the_verdict(self):
        # A supplied brief admits a gap the code bound cannot wave through, so
        # round one is inconclusive and the judge is asked. Round two curates
        # its own, narrower brief from the same files -- which now fit whole --
        # so the code bound is satisfied and the ladder stops there instead of
        # spending a third round.
        jev = ScriptedJev([{"plan_sound": 0.6,
                             "plan_evidence_requested": 0.9}])
        outcome = self._run(brief=self._gapped(), jev_policy=jev, rounds=3)
        self.assertEqual([r["round"] for r in outcome.rounds], [1, 2])
        self.assertLess(outcome.rounds[1]["max_input_tokens"],
                        outcome.rounds[0]["max_input_tokens"])
        # Round one reached the judge; round two needed no judge at all.
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual(outcome.rounds[0]["jev_native"], True)
        self.assertEqual(outcome.kind, OUTCOME_SUFFICIENT)
        self.assertEqual(outcome.reason, "brief_covers_the_request")
        # Round two's brief is not round one's: each round curated its own,
        # and the curation is what closed the gap.
        self.assertGreater(outcome.rounds[0]["omitted"], 0)
        self.assertEqual(outcome.rounds[1]["omitted"], 0)

    def test_a_judge_that_keeps_asking_for_evidence_ends_the_ladder(self):
        jev = ScriptedJev([{"plan_sound": 0.6, "plan_evidence_requested": 0.9}])
        # Nothing to curate and a judge that never stops asking: the ladder
        # runs out of rounds and the stage asks, bounded, for what is missing.
        outcome = self._run(brief=self._gapped(), files=[], rounds=2,
                            jev_policy=jev)
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual([i["source"] for i in outcome.evidence_request],
                         ["b.py", "c.py"])

    def test_a_round_with_nothing_to_curate_does_not_repay_for_a_verdict(self):
        # The supplied brief is the whole evidence set, so a second round would
        # judge identical bytes. That judgment cannot change its answer, so it
        # must not be paid for.
        jev = FakeJev({"plan_evidence_requested": 0.9})
        outcome = self._run(brief=self._gapped(), files=[], rounds=3,
                            jev_policy=jev)
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual(outcome.rounds[-1]["reason"],
                         "nothing_left_to_curate")
        self.assertEqual(outcome.kind, OUTCOME_EVIDENCE_REQUEST)

    def _gapped(self):
        brief = build_brief("g", ["a.py", "b.py", "c.py"],
                            reader=GAPPED_READER, max_total_chars=6)
        self.assertEqual(brief["omitted"], ["b.py", "c.py"])
        return brief


class PlanningBudgetIntegrationTests(unittest.TestCase):
    def test_planning_never_spends_from_the_run_budget_it_did_not_reserve(self):
        run = TokenBudget("run", max_input_tokens=8000, max_output_tokens=0)
        before = run.snapshot()
        outcome = run_planning(goal="g", budget=run, files=(), rounds=2)
        after = run.snapshot()
        # Hermetic planning reads and shapes evidence; it dispatches nothing,
        # so the run's dollar-free token position is untouched and no
        # reservation is left dangling.
        self.assertEqual(after["used_input_tokens"],
                         before["used_input_tokens"])
        self.assertEqual(after["open_allowances"], 0)
        self.assertEqual(after["max_input_tokens"], before["max_input_tokens"])
        self.assertIn("label", outcome.budget)
        self.assertEqual(outcome.budget["label"], "planning")


if __name__ == "__main__":
    unittest.main()
