"""HV-4: planning over successively smaller allowances, and the waist's
terminal contract.

Two invariants carry this slice. First, a stage cannot raise its own
limits: composition only ever asks ``TokenBudget`` (HV-3) to narrow, so the
ceiling falls stage over stage and is additionally capped by the brief the
stage will actually read. Second, the planning waist may only *stop*,
*plan*, *ask*, or *defer* -- and a plan is validated by the DAG owner, an
evidence request is bounded, and a defer says why.
"""
import unittest

from harness.dag import TaskDAG
from harness.errors import HarnessError
from harness.token_budget import TokenBudget
from harness.waist import (
    MAX_EVIDENCE_QUESTIONS,
    MIN_STAGE_INPUT_TOKENS,
    OUTCOME_DEFER,
    OUTCOME_EVIDENCE_REQUEST,
    OUTCOME_PLAN,
    OUTCOME_SUFFICIENT,
    PLAN_OUTCOMES,
    STAGE_CONTEXT,
    STAGE_PLANNING,
    compose_stages,
    plan_outcome,
)

PLAN = {
    "nodes": [
        {"node_id": "n1", "instruction": "read the ledger",
         "target_files": ["harness/session.py"], "dependencies": []},
    ],
}


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
