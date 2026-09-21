"""CLI resume/final-gate/decompose coverage pins (D12 changed lines)."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.cli import _cmd_plan
from harness.pyramid_state import persist_state


class CliResumeCoverageTests(unittest.TestCase):
    def _opts(self, **kw):
        base = dict(
            goal="Resume work", file=["harness/sync.py"], frontier_model=None,
            execute=True, parallel=False, max_workers=1, max_cost=1.0,
            keep_going=False, out=None, model=None, max_tokens=None,
            task_max_cost=None, allow_escalation=False, reasoning_effort=None,
            max_rotations=3, decompose_llm=False, final_gate=False,
            resume=None, plan_consensus=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def _settings(self):
        return SimpleNamespace(
            use_free=False, frontier_model=None,
            hourglass_confirm=False, hourglass_parallel=False,
            hourglass_isolate=False, hourglass_require_attestation=False,
            hourglass_decompose=False,
            ledger_path=os.path.join(tempfile.mkdtemp(), "l.jsonl"))

    def test_resume_skips_completed_and_persists_state(self):
        tmp = tempfile.mkdtemp()
        state_path = os.path.join(tmp, "pyramid.json")
        dag = {"nodes": [
            {"node_id": "task_1", "instruction": "a",
             "target_files": ["a.py"], "dependencies": []},
            {"node_id": "task_2", "instruction": "b",
             "target_files": ["b.py"], "dependencies": ["task_1"]},
        ]}
        persist_state(state_path, goal="Resume work", dag=dag,
                      node_results={"task_1": {"status": "ok", "cost": 0.01}},
                      spent=0.01)
        engine = MagicMock()
        engine.apply_edit.return_value = {"status": "ok", "cost": 0.002}
        dispatched = []

        class SpyExec:
            def __init__(self, engine_arg, routes, **kwargs):
                self.routes = routes
                self._inner = None

            def execute(self, dag_arg):
                dispatched.extend(dag_arg.nodes)
                return {"task_2": {"status": "ok", "cost": 0.002}}

        canned = {
            "status": "planned", "goal": "Resume work",
            "dag": dag,
            "nodes": [
                {"node_id": "task_1", "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}},
                {"node_id": "task_2", "route": {"ladder": ["m/b"], "cost_ceiling": 0.0}},
            ],
            "composed_worst_case": {"composed_worst_case": 0.0},
        }
        emitted = {}
        with patch("harness.cli._session", return_value=engine), \
             patch("harness.cli._compose_plan", return_value=canned), \
             patch("harness.cli.PlanExecutor", side_effect=SpyExec), \
             patch("harness.cli._emit_by_status", side_effect=lambda r, o=None: emitted.update(r)):
            _cmd_plan(self._opts(resume=state_path), self._settings())
        self.assertEqual(dispatched, ["task_2"])
        self.assertTrue(emitted.get("resumed"))
        self.assertIn("task_1", emitted.get("skipped_completed", []))
        with open(state_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertIn("task_2", saved.get("node_results", {}))

    def test_resume_empty_pending_short_circuits(self):
        tmp = tempfile.mkdtemp()
        state_path = os.path.join(tmp, "done.json")
        dag = {"nodes": [
            {"node_id": "task_1", "instruction": "a",
             "target_files": ["a.py"], "dependencies": []}]}
        persist_state(state_path, goal="g", dag=dag,
                      node_results={"task_1": {"status": "ok", "cost": 0.0}},
                      spent=0.0)
        emitted = {}
        with patch("harness.cli._session", return_value=MagicMock()), \
             patch("harness.cli._compose_plan", return_value={
                 "status": "planned", "goal": "g", "dag": dag, "nodes": [],
                 "composed_worst_case": {"composed_worst_case": 0.0}}), \
             patch("harness.cli._emit_by_status",
                   side_effect=lambda r, o=None: emitted.update(r)):
            _cmd_plan(self._opts(resume=state_path), self._settings())
        self.assertEqual(emitted["status"], "ok")
        self.assertEqual(emitted["completed_nodes"], 0)

    def test_final_gate_flag_reaches_executor(self):
        engine = MagicMock()
        engine.apply_edit.return_value = {"status": "ok", "cost": 0.0}
        captured = {}

        def spy(engine_arg, routes, **kwargs):
            captured.update(kwargs)

            class _E:
                def execute(self, dag):
                    return {"task_1": {"status": "ok", "cost": 0.0}}

            return _E()

        canned = {
            "status": "planned", "goal": "g",
            "dag": {"nodes": [{
                "node_id": "task_1", "instruction": "x",
                "target_files": ["harness/sync.py"], "dependencies": [],
                "local_gate": "python -m py_compile x.py"}]},
            "nodes": [{"node_id": "task_1", "local_gate": "python -m py_compile x.py",
                       "route": {"ladder": ["m/a"], "cost_ceiling": 0.0}}],
        }
        with patch("harness.cli._session", return_value=engine), \
             patch("harness.cli._compose_plan", return_value=canned), \
             patch("harness.cli.PlanExecutor", side_effect=spy), \
             patch("harness.cli._emit_by_status"):
            _cmd_plan(self._opts(final_gate="python -m unittest tests.test_tokens"),
                      self._settings())
        self.assertEqual(captured["final_gate"],
                         "python -m unittest tests.test_tokens")
        self.assertEqual(captured["run_gate"], "python -m py_compile x.py")


if __name__ == "__main__":
    unittest.main()
