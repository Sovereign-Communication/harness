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

    def test_load_state_allow_missing(self):
        from harness.errors import HarnessError
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "nonexistent.json")
            self.assertIsNone(load_state(missing_path, allow_missing=True))
            with self.assertRaises(HarnessError):
                load_state(missing_path, allow_missing=False)

    def test_cold_start_bootstrap_persists_fresh_state_and_resumes(self):
        """DF-HG-2: plan --resume PATH bootstraps fresh if PATH doesn't exist,
        persists on completion, and second run resumes it."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from harness.cli import _cmd_plan

        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "bootstrap.json")
            self.assertFalse(os.path.exists(state_path))

            opts = SimpleNamespace(
                goal="Cold start task",
                file=["harness/sync.py"],
                frontier_model=None,
                execute=True,
                parallel=False,
                max_workers=1,
                max_cost=1.0,
                task_max_cost=None,
                keep_going=False,
                out=None,
                model=None,
                max_tokens=None,
                allow_escalation=False,
                reasoning_effort=None,
                max_rotations=3,
                decompose_llm=False,
                confirm=False,
                plan_consensus=False,
                final_gate=False,
                resume=state_path,
                stage_gate=None,
                persist_state=None,
            )
            settings = SimpleNamespace(
                use_free=False,
                frontier_model=None,
                hourglass_confirm=False,
                hourglass_parallel=False,
                hourglass_isolate=False,
                hourglass_require_attestation=False,
                hourglass_decompose=False,
                ledger_path=os.path.join(tmp, "ledger.jsonl"),
            )

            dag_dict = {"nodes": [
                {"node_id": "task_1", "instruction": "Step 1",
                 "target_files": ["a.py"], "dependencies": []},
            ]}
            canned_plan = {
                "status": "planned",
                "goal": "Cold start task",
                "dag": dag_dict,
                "nodes": [
                    {"node_id": "task_1", "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}},
                ],
                "composed_worst_case": {"composed_worst_case": 0.0},
            }

            engine = MagicMock()
            engine.governor = MagicMock()
            engine.governor.max_cost = 1.0

            class SpyExec:
                def __init__(self, engine_arg, routes, **kwargs):
                    self.routes = routes

                def execute(self, dag_arg):
                    return {"task_1": {"status": "ok", "cost": 0.005}}

            emitted1 = {}
            with patch("harness.cli._session", return_value=engine), \
                 patch("harness.cli._compose_plan", return_value=canned_plan), \
                 patch("harness.cli.PlanExecutor", side_effect=SpyExec), \
                 patch("harness.cli._emit_by_status", side_effect=lambda r, o=None: emitted1.update(r)):
                _cmd_plan(opts, settings)

            self.assertEqual(emitted1.get("status"), "ok")
            self.assertTrue(os.path.exists(state_path))
            loaded = load_state(state_path)
            self.assertEqual(loaded["goal"], "Cold start task")
            self.assertIn("task_1", loaded["node_results"])

            # Run 2: resume should detect task_1 is already complete and short-circuit
            emitted2 = {}
            with patch("harness.cli._session", return_value=engine), \
                 patch("harness.cli._compose_plan", return_value=canned_plan), \
                 patch("harness.cli.PlanExecutor", side_effect=SpyExec), \
                 patch("harness.cli._emit_by_status", side_effect=lambda r, o=None: emitted2.update(r)):
                _cmd_plan(opts, settings)

            self.assertEqual(emitted2.get("status"), "ok")
            self.assertTrue(emitted2.get("resumed"))
            self.assertEqual(emitted2.get("total_nodes"), 0)


if __name__ == "__main__":
    unittest.main()
