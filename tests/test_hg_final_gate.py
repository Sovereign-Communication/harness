"""HG-final-gate: default post-DAG gate blocks a false-ok envelope."""
import unittest
from unittest.mock import MagicMock

from harness.dag import DAGNode, TaskDAG
from harness.executor import PlanExecutor


def _ok_apply(target, node, route_kwargs, task_runner):
    return {"status": "ok", "cost": 0.0}


class FinalGateTests(unittest.TestCase):
    def test_default_final_gate_blocks_false_ok(self):
        """Nodes all report ok, but the discovered/declared final gate fails
        -- the run must NOT summarize as all_ok (composed tree is red)."""
        engine = MagicMock()
        routes = {
            "task_1": {"node_id": "task_1",
                       "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}},
        }
        dag = TaskDAG(nodes={
            "task_1": DAGNode(
                node_id="task_1", instruction="edit a",
                target_files=("harness/sync.py",),
                local_gate='python -c "import sys; sys.exit(1)"'),
        })
        executed = []

        def failing_runner(command, timeout=None, cwd=None):
            executed.append(command)
            return 1, "suite red"

        plan_exec = PlanExecutor(
            engine, routes, parallel=False, isolate=False,
            apply=_ok_apply, final_gate_runner=failing_runner)
        results = plan_exec.execute(dag)
        self.assertIn("final_gate", results)
        self.assertEqual(results["final_gate"]["status"], "verify_failed")
        self.assertTrue(executed)
        summary = PlanExecutor.summarize(results)
        self.assertFalse(summary["all_ok"])
        self.assertEqual(summary["final_gate"]["status"], "verify_failed")

    def test_final_gate_opt_out_keeps_node_verdict(self):
        engine = MagicMock()
        routes = {"task_1": {"node_id": "task_1",
                             "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}}}
        dag = TaskDAG(nodes={
            "task_1": DAGNode(
                node_id="task_1", instruction="edit a",
                target_files=("harness/sync.py",),
                local_gate='python -c "import sys; sys.exit(1)"'),
        })

        def failing_runner(command, timeout=None, cwd=None):
            return 1, "suite red"

        plan_exec = PlanExecutor(
            engine, routes, parallel=False, isolate=False,
            apply=_ok_apply, final_gate=False, final_gate_runner=failing_runner)
        results = plan_exec.execute(dag)
        self.assertNotIn("final_gate", results)
        self.assertTrue(PlanExecutor.summarize(results)["all_ok"])

    def test_final_gate_override_command_is_used(self):
        engine = MagicMock()
        routes = {}
        dag = TaskDAG(nodes={
            "task_1": DAGNode(
                node_id="task_1", instruction="edit a",
                target_files=("harness/sync.py",), local_gate=None),
        })
        seen = []

        def runner(command, timeout=None, cwd=None):
            seen.append(command)
            return 0, "ok"

        plan_exec = PlanExecutor(
            engine, routes, parallel=False, isolate=False,
            apply=_ok_apply, final_gate="python -m unittest tests.test_tokens",
            final_gate_runner=runner)
        results = plan_exec.execute(dag)
        self.assertEqual(seen, ["python -m unittest tests.test_tokens"])
        self.assertEqual(results["final_gate"]["status"], "ok")
        self.assertTrue(PlanExecutor.summarize(results)["all_ok"])

    def test_no_declared_gate_means_no_default_final_gate(self):
        engine = MagicMock()
        dag = TaskDAG(nodes={
            "task_1": DAGNode(
                node_id="task_1", instruction="edit a",
                target_files=("docs/notes.md",), local_gate=None),
        })

        def runner(command, timeout=None, cwd=None):
            raise AssertionError("final gate should not run")

        plan_exec = PlanExecutor(
            engine, {}, parallel=False, isolate=False,
            apply=_ok_apply, final_gate_runner=runner)
        results = plan_exec.execute(dag)
        self.assertNotIn("final_gate", results)


if __name__ == "__main__":
    unittest.main()
