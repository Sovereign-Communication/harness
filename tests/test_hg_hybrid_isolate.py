"""HG-hybrid-isolate: overlap-free concurrent nodes isolate; overlapping serialize."""
import unittest
from unittest.mock import MagicMock

from harness.dag import DAGNode, TaskDAG
from harness.executor import PlanExecutor, partition_by_target_overlap


class PartitionTests(unittest.TestCase):
    def test_overlap_free_nodes_go_to_isolated_arm(self):
        a = DAGNode(node_id="a", instruction="x", target_files=("a.py",))
        b = DAGNode(node_id="b", instruction="y", target_files=("b.py",))
        iso, shared = partition_by_target_overlap([a, b])
        self.assertEqual([n.node_id for n in iso], ["a", "b"])
        self.assertEqual(shared, [])

    def test_overlapping_nodes_serialize_shared_tree(self):
        a = DAGNode(node_id="a", instruction="x", target_files=("shared.py",))
        b = DAGNode(node_id="b", instruction="y", target_files=("shared.py",))
        c = DAGNode(node_id="c", instruction="z", target_files=("solo.py",))
        iso, shared = partition_by_target_overlap([a, b, c])
        self.assertEqual([n.node_id for n in iso], ["c"])
        self.assertEqual(sorted(n.node_id for n in shared), ["a", "b"])

    def test_declared_overlap_across_different_files_in_same_stage(self):
        a = DAGNode(node_id="a", instruction="x", target_files=("a.py", "b.py"))
        b = DAGNode(node_id="b", instruction="y", target_files=("b.py", "c.py"))
        iso, shared = partition_by_target_overlap([a, b])
        self.assertEqual(iso, [])
        self.assertEqual(sorted(n.node_id for n in shared), ["a", "b"])


class HybridIsolateExecutionTests(unittest.TestCase):
    def test_overlapping_stage_runs_serially_in_shared_tree(self):
        engine = MagicMock()
        order = []
        active = {"n": 0, "max": 0}

        def apply(target, node, route_kwargs, task_runner):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            order.append((node.node_id, task_runner is not None))
            active["n"] -= 1
            return {"status": "ok", "cost": 0.0}

        class FakeIso:
            def available(self):
                return True

            def create(self, node_id):
                raise AssertionError(
                    f"overlapping node {node_id} must not enter a worktree")

            def audit(self, handle, declared):
                return []

            def merge(self, handle, declared):
                return None

            def discard(self, handle):
                return None

        dag = TaskDAG(nodes={
            "a": DAGNode(node_id="a", instruction="x", target_files=("shared.py",)),
            "b": DAGNode(node_id="b", instruction="y", target_files=("shared.py",)),
        })
        routes = {
            "a": {"node_id": "a", "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}},
            "b": {"node_id": "b", "route": {"ladder": ["m/b"], "cost_ceiling": 0.0}},
        }
        plan_exec = PlanExecutor(
            engine, routes, parallel=True, isolate=True, max_workers=2,
            apply=apply, final_gate=False)
        # Force the isolator seam: partition must keep overlapping nodes out.
        plan_exec.isolator = FakeIso()
        results = plan_exec.execute(dag)
        self.assertEqual(results["a"]["status"], "ok")
        self.assertEqual(results["b"]["status"], "ok")
        # Shared-tree arm: no worktree task_runner on either overlapping node.
        self.assertEqual(active["max"], 1)
        for node_id, had_runner in order:
            self.assertFalse(had_runner, node_id)

    def test_overlap_free_stage_uses_worktrees(self):
        engine = MagicMock()
        seen_runners = []

        def apply(target, node, route_kwargs, task_runner):
            seen_runners.append((node.node_id, task_runner is not None))
            return {"status": "ok", "cost": 0.0}

        class FakeIso:
            def __init__(self):
                self.created = []

            def available(self):
                return True

            def create(self, node_id):
                self.created.append(node_id)
                return {"path": f"/wt/{node_id}", "node_id": node_id}

            def audit(self, handle, declared):
                return []

            def merge(self, handle, declared):
                return None

            def discard(self, handle):
                return None

        dag = TaskDAG(nodes={
            "a": DAGNode(node_id="a", instruction="x", target_files=("a.py",)),
            "b": DAGNode(node_id="b", instruction="y", target_files=("b.py",)),
        })
        routes = {
            "a": {"node_id": "a", "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}},
            "b": {"node_id": "b", "route": {"ladder": ["m/b"], "cost_ceiling": 0.0}},
        }
        plan_exec = PlanExecutor(
            engine, routes, parallel=True, isolate=True, max_workers=2,
            apply=apply, final_gate=False)
        iso = FakeIso()
        plan_exec.isolator = iso
        results = plan_exec.execute(dag)
        self.assertEqual(results["a"]["status"], "ok")
        self.assertEqual(results["b"]["status"], "ok")
        self.assertEqual(sorted(iso.created), ["a", "b"])
        for node_id, had_runner in seen_runners:
            self.assertTrue(had_runner, node_id)


if __name__ == "__main__":
    unittest.main()
