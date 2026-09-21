"""HG-pyramid-resume: persisted state skips completed ok nodes on --resume."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from harness.executor import PlanExecutor
from harness.pyramid_state import (
    completed_node_ids, dag_for_pending, load_state, pending_node_details,
    persist_state)


class PyramidStateTests(unittest.TestCase):
    def _state(self, path):
        dag = {
            "nodes": [
                {"node_id": "task_1", "instruction": "do a",
                 "target_files": ["a.py"], "dependencies": []},
                {"node_id": "task_2", "instruction": "do b",
                 "target_files": ["b.py"], "dependencies": ["task_1"]},
            ]
        }
        return persist_state(
            path, goal="g", dag=dag,
            node_results={
                "task_1": {"status": "ok", "cost": 0.01},
                "task_2": {"status": "verify_failed", "cost": 0.02},
            },
            spent=0.03)

    def test_resume_does_not_re_dispatch_completed_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pyramid.json")
            self._state(path)
            loaded = load_state(path)
            self.assertEqual(loaded["goal"], "g")
            self.assertEqual(completed_node_ids(loaded), {"task_1"})
            pending = pending_node_details(loaded)
            self.assertEqual([n["node_id"] for n in pending], ["task_2"])
            dag = dag_for_pending(loaded)
            self.assertEqual(list(dag.nodes), ["task_2"])
            # Completed dependency is treated as satisfied.
            self.assertEqual(dag.nodes["task_2"].dependencies, ())

            dispatched = []
            engine = MagicMock()
            routes = {"task_2": {"node_id": "task_2",
                                 "route": {"ladder": ["m/b"], "cost_ceiling": 0.0}}}

            def apply(target, node, route_kwargs, task_runner):
                dispatched.append(node.node_id)
                return {"status": "ok", "cost": 0.0}

            plan_exec = PlanExecutor(
                engine, routes, parallel=False, isolate=False,
                apply=apply, final_gate=False)
            results = plan_exec.execute(dag)
            self.assertEqual(dispatched, ["task_2"])
            self.assertNotIn("task_1", dispatched)
            self.assertEqual(results["task_2"]["status"], "ok")

    def test_persist_roundtrip_is_json_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            self._state(path)
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["version"], 1)
            self.assertEqual(raw["spent"], 0.03)
            self.assertIn("dag", raw)


if __name__ == "__main__":
    unittest.main()
