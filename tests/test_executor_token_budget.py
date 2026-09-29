"""HV-3-use: PlanExecutor forwards the composed allowance unchanged."""
import unittest

from harness.dag import DAGNode, TaskDAG
from harness.errors import HarnessError
from harness.executor import PlanExecutor, budget_for_composed_stage
from harness.token_budget import TokenBudget


def _dag(*names):
    nodes = {
        name: DAGNode(node_id=name, instruction=name)
        for name in names
    }
    return TaskDAG(nodes=nodes)


class _Governor:
    def reserve(self, amount, label):
        return (amount, label)

    def reconcile(self, _reservation, _actual):
        return None


class PlanExecutorTokenBudgetTests(unittest.TestCase):
    def test_same_budget_reaches_serial_and_concurrent_node_callbacks(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                budget = TokenBudget(max_input_tokens=100, max_output_tokens=20)
                seen = []

                def apply(_target, node, route_kwargs, _task_runner):
                    seen.append((node.node_id, route_kwargs["token_budget"]))
                    return {"status": "ok", "node_id": node.node_id}

                executor = PlanExecutor(
                    type("Engine", (), {"governor": _Governor()})(), {},
                    parallel=parallel, isolate=False,
                    apply=apply, token_budget=budget, final_gate=False)
                result = executor.execute(_dag("a", "b"))

                self.assertEqual(result["a"]["status"], "ok", result)
                self.assertEqual(result["b"]["status"], "ok")
                self.assertEqual({id(value) for _, value in seen}, {id(budget)})

    def test_route_kwargs_cannot_replace_budget_or_mutate_shared_route(self):
        budget = TokenBudget(max_input_tokens=100, max_output_tokens=20)
        hostile = object()
        shared_route_kwargs = {"token_budget": hostile, "marker": "kept"}
        seen = []

        def route_kwargs(_route):
            return shared_route_kwargs

        def apply(_target, node, route, _task_runner):
            seen.append(route)
            return {"status": "ok", "node_id": node.node_id}

        executor = PlanExecutor(
            type("Engine", (), {"governor": _Governor()})(), {},
            parallel=True, isolate=False, apply=apply,
            route_kwargs_fn=route_kwargs, token_budget=budget, final_gate=False)
        executor.execute(_dag("a", "b"))

        self.assertTrue(all(route["token_budget"] is budget for route in seen))
        self.assertTrue(all(route is not shared_route_kwargs for route in seen))
        self.assertIs(shared_route_kwargs["token_budget"], hostile)
        self.assertEqual(shared_route_kwargs["marker"], "kept")

    def test_missing_budget_preserves_callback_route_kwargs(self):
        seen = []

        def apply(_target, node, route_kwargs, _task_runner):
            seen.append(route_kwargs)
            return {"status": "ok", "node_id": node.node_id}

        executor = PlanExecutor(
            type("Engine", (), {"governor": _Governor()})(), {},
            parallel=False, isolate=False, apply=apply,
            final_gate=False)
        executor.execute(_dag("a"))

        self.assertEqual(seen, [{}])

    def test_invalid_budget_type_fails_closed_at_construction(self):
        with self.assertRaises(HarnessError):
            PlanExecutor(None, {}, token_budget=object())

    def test_composed_stage_budget_uses_declared_ceiling_and_run_parent(self):
        run = TokenBudget("run", max_input_tokens=1000,
                          max_output_tokens=256)
        plan = {"composition": {"stages": [{
            "stage": "planning", "max_input_tokens": 500,
            "max_output_tokens": 128,
        }, {
            "stage": "execution", "max_input_tokens": 900,
            "max_output_tokens": 200,
        }]}}

        budget = budget_for_composed_stage(run, plan, "execution")

        self.assertIsInstance(budget, TokenBudget)
        self.assertEqual(budget.label, "execution")
        self.assertEqual(budget.max_input_tokens, 900)
        self.assertEqual(budget.max_output_tokens, 200)
        self.assertEqual(budget.snapshot()["parent"], "run")

    def test_missing_stage_returns_none_and_uncomposed_plan_needs_no_budget(self):
        run = TokenBudget("run", max_input_tokens=100,
                          max_output_tokens=20)
        self.assertIsNone(budget_for_composed_stage(run, {}, "execution"))
        self.assertIsNone(budget_for_composed_stage(
            run, {"composition": {"stages": [{
                "stage": "planning", "max_input_tokens": 50,
                "max_output_tokens": 10,
            }]}}, "execution"))

    def test_duplicate_or_malformed_stage_ceilings_fail_closed(self):
        run = TokenBudget("run", max_input_tokens=100,
                          max_output_tokens=20)
        duplicate = {"composition": {"stages": [{
            "stage": "execution", "max_input_tokens": 50,
            "max_output_tokens": 10,
        }, {
            "stage": "execution", "max_input_tokens": 50,
            "max_output_tokens": 10,
        }]}}
        malformed = {"composition": {"stages": [{
            "stage": "execution", "max_input_tokens": True,
            "max_output_tokens": 10,
        }]}}
        for plan in (duplicate, malformed):
            with self.subTest(plan=plan), self.assertRaises(HarnessError):
                budget_for_composed_stage(run, plan, "execution")


if __name__ == "__main__":
    unittest.main()
