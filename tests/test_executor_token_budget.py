"""The shared plan executor forwards its composed execution allowance."""
import unittest
from unittest.mock import MagicMock

from harness.dag import DAGNode
from harness.errors import HarnessError
from harness.executor import PlanExecutor
from harness.token_budget import TokenBudget


class PlanExecutorTokenBudgetTests(unittest.TestCase):
    def test_planned_apply_forces_package_consent_and_renewal(self):
        engine = MagicMock()
        executor = PlanExecutor(engine, {}, parallel=False, isolate=False,
                                token_budget=TokenBudget())
        node = DAGNode("one", "edit the target", target_files=("a.py",))

        executor._apply_edit("a.py", node, {}, None)

        kwargs = engine.apply_edit.call_args.kwargs
        self.assertIs(kwargs["require_consent"], True)
        self.assertIs(kwargs["renew_consent"], True)

    def test_execution_budget_reaches_custom_apply_lane(self):
        budget = TokenBudget("execution", max_input_tokens=8_000,
                             max_output_tokens=2_000)
        captured = {}

        def apply(target, node, route_kwargs, task_runner):
            captured.update(route_kwargs)
            return {"status": "ok"}

        executor = PlanExecutor(
            engine=object(), node_routes={}, parallel=False,
            apply=apply, token_budget=budget)
        executor.run_node(DAGNode(node_id="one", instruction="edit"))
        self.assertIs(captured["token_budget"], budget)

    def test_non_budget_is_rejected_at_plan_boundary(self):
        with self.assertRaises(HarnessError):
            PlanExecutor(engine=object(), node_routes={}, token_budget=object())


if __name__ == "__main__":
    unittest.main()
