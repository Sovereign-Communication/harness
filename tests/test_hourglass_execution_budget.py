"""HV-5 acceptance: composed planning and budgeted package dispatch."""
import unittest
from unittest.mock import patch

from harness.dag import DAGNode, TaskDAG
from harness.executor import PlanExecutor
from harness.token_budget import TokenBudget
from harness.waist import (
    STAGE_EXECUTION, STAGE_PLANNING, compose_stages, composition_envelope,
    runtime_stage_budget, stage_budget,
)


class ComposedExecutionBudgetTests(unittest.TestCase):
    def test_selected_planning_stage_invokes_the_planning_owner(self):
        from types import SimpleNamespace

        from harness.spend import SpendGovernor
        from harness.waist import compose_plan
        from tests._fake import FakeTransport, m

        transport = FakeTransport(models=[m("m/cheap")])
        budget = TokenBudget("run", max_input_tokens=200_000,
                             max_output_tokens=64_000)
        captured = []
        outcome = SimpleNamespace(to_dict=lambda: {"kind": "sufficient"})
        with patch("harness.waist.run_planning",
                   side_effect=lambda **kwargs: (captured.append(kwargs), outcome)[1]):
            plan = compose_plan(
                transport=transport, api_key="k",
                governor=SpendGovernor(transport, "sk-test", max_cost=1.0),
                ledger=None, opts_goal="update helper", candidate_files=[],
                root=".", execute=False, token_budget=budget,
                stages=[STAGE_PLANNING])

        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0]["budget"], budget)
        self.assertIsInstance(captured[0]["stage_budget"], TokenBudget)
        self.assertEqual(plan["planning"]["kind"], "sufficient")

    def test_selected_planning_and_execution_have_narrowed_child_allowances(self):
        run = TokenBudget("run", max_input_tokens=200_000,
                          max_output_tokens=64_000)
        composition = compose_stages(budget=run)
        runtime = {"composition": composition, "run_budget": run}
        planning = runtime_stage_budget(runtime, STAGE_PLANNING)
        composed_execution = stage_budget(composition, STAGE_EXECUTION)
        execution = runtime_stage_budget(runtime, STAGE_EXECUTION)

        self.assertIsInstance(planning, TokenBudget)
        self.assertIsInstance(execution, TokenBudget)
        # HV-5 explicitly widens execution after the planning waist while
        # retaining the run budget as the hard ancestor ceiling.
        self.assertGreater(execution.max_input_tokens, planning.max_input_tokens)
        self.assertGreater(execution.max_output_tokens, planning.max_output_tokens)
        for allowance in (planning, execution):
            snapshot = allowance.snapshot()
            self.assertEqual(snapshot["parent"], run.label)
            self.assertLessEqual(snapshot["max_input_tokens"],
                                 run.max_input_tokens)
            self.assertLessEqual(snapshot["max_output_tokens"],
                                 run.max_output_tokens)

        self.assertIs(execution._parent, run)
        self.assertLess(composed_execution.max_input_tokens,
                        planning.max_input_tokens)
        allowance = composition_envelope(composition)[
            "execution_dispatch_allowance"]
        self.assertEqual(allowance["parent"], run.label)
        self.assertTrue(allowance["available"])
        self.assertEqual(allowance["max_input_tokens"], run.max_input_tokens)
        p = planning.allowance(10, max_output_tokens=5, label="planning")
        e = execution.allowance(20, max_output_tokens=7, label="execution")
        self.assertEqual(run.reserved(), 42)
        planning.cancel(p)
        execution.cancel(e)
        self.assertEqual(run.reserved(), 0)

    def test_dispatch_carries_the_execution_allowance_and_exact_package_consent(self):
        execution = TokenBudget("execution", max_input_tokens=20_000,
                                max_output_tokens=8_000)
        calls = []

        def apply(target, node, route_kwargs, task_runner):
            calls.append((target, node, route_kwargs, task_runner))
            return {"status": "ok", "node_id": node.node_id}

        executor = PlanExecutor(
            engine=object(), node_routes={}, parallel=False, isolate=False,
            apply=apply, token_budget=execution)
        result = executor.run_node(DAGNode(
            "pkg-1", "edit the requested function", target_files=("src/a.py",),
            local_gate="python -m unittest"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)
        target, node, kwargs, _runner = calls[0]
        self.assertEqual(target, "src/a.py")
        self.assertEqual(node.instruction, "edit the requested function")
        self.assertIs(kwargs["token_budget"], execution)

        engine = type("Engine", (), {})()
        default_executor = PlanExecutor(
            engine=engine, node_routes={}, parallel=False, isolate=False,
            token_budget=execution)
        self.assertIs(default_executor.base_apply_kwargs["require_consent"], True)
        self.assertIs(default_executor.base_apply_kwargs["renew_consent"], True)
        self.assertEqual(default_executor.resolve_final_gate(TaskDAG.from_dict({
            "nodes": [{"node_id": "pkg", "instruction": "edit",
                       "target_files": ["src/a.py"],
                       "local_gate": "python -m unittest"}]})),
                         "python -m unittest")

    def test_final_gate_failure_invalidates_successful_package_results(self):
        executor = PlanExecutor(
            engine=object(), node_routes={}, parallel=False, isolate=False,
            apply=lambda *_a: {"status": "ok"},
            final_gate="python -m unittest",
            final_gate_runner=lambda *_a, **_k: (1, "failed"))
        results = executor.execute(TaskDAG.from_dict({
            "nodes": [{"node_id": "pkg", "instruction": "edit",
                       "target_files": ["src/a.py"]}]}))
        summary = executor.summarize(results)
        self.assertEqual(results["pkg"]["status"], "ok")
        self.assertEqual(summary["final_gate"]["status"], "verify_failed")
        self.assertFalse(summary["all_ok"])


if __name__ == "__main__":
    unittest.main()
