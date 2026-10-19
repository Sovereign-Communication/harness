"""Hermetic unit tests for GAP-plan-http: HTTP planning runner and Web UI panes.

Validates that `POST /api/runs {"kind": "plan", ...}` accepts planning jobs,
validates arguments at the CLI boundary, runs through `run_plan_task` asynchronously,
and records public run records pollable via `/api/runs/{id}/result`.
Also verifies Apply and Dogfood panes are registered in `panes.js`.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness.errors import HarnessError
from harness.server import (
    RUNNERS,
    run_apply_task,
    run_bench_task,
    run_continue_task,
    run_plan_task,
    validate_dispatch,
)
from tests.test_server import ServerHarness, _request


class TestPlanDispatchValidation(unittest.TestCase):
    """Test validation of plan dispatch inputs."""

    def test_plan_requires_goal(self):
        with self.assertRaises(HarnessError) as ctx:
            validate_dispatch("plan", {})
        self.assertIn("required", str(ctx.exception).lower())

    def test_plan_validates_types_and_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            args = validate_dispatch("plan", {
                "root_dir": root,
                "goal": "Refactor billing system",
                "execute": "true",
                "max_workers": 6,
                "max_cost": 0.05,
                "task_max_cost": 0.02,
            })
        self.assertEqual(args["goal"], "Refactor billing system")
        self.assertTrue(args["execute"])
        self.assertEqual(args["max_workers"], 6)
        self.assertEqual(args["max_cost"], 0.05)
        self.assertEqual(args["task_max_cost"], 0.02)

    def test_plan_candidate_path_is_resolved_under_explicit_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            candidate = os.path.join(root, "candidate.py")
            with open(candidate, "w", encoding="utf-8") as stream:
                stream.write("pass\n")
            args = validate_dispatch("plan", {
                "root_dir": root, "goal": "review", "files": "candidate.py"})
            self.assertEqual(Path(args["files"][0]).resolve(),
                             Path(candidate).resolve())


class TestPlanTaskRunner(unittest.TestCase):
    """Test direct run_plan_task execution."""

    def test_runners_contains_plan(self):
        self.assertIn("plan", RUNNERS)
        self.assertEqual(RUNNERS["plan"], run_plan_task)

    @mock.patch("harness.server.governor_for")
    @mock.patch("harness.waist.compose_plan")
    def test_run_plan_task_plan_only(self, mock_compose, mock_gov):
        mock_gov.return_value = ("fake-key", mock.MagicMock())
        mock_compose.return_value = {
            "status": "ok",
            "goal": "Build dashboard",
            "dag": {"nodes": []},
        }
        with tempfile.TemporaryDirectory() as root:
            candidate = os.path.join(root, "sample.py")
            with open(candidate, "w", encoding="utf-8") as stream:
                stream.write("pass\n")
            res = run_plan_task("task-p1", {
                "root_dir": root, "goal": "Build dashboard", "execute": False,
                "files": candidate}, cancel_check=None)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["goal"], "Build dashboard")
        mock_compose.assert_called_once()
        self.assertEqual(mock_compose.call_args.kwargs["candidate_files"],
                         ["sample.py"])
        mock_gov.assert_called_once()

    @mock.patch("harness.server.run_meta", return_value={"source": "test"})
    @mock.patch("harness.server.apply_session")
    @mock.patch("harness.server.load_settings", return_value=object())
    def test_run_apply_task_uses_only_selected_workspace(self, mock_settings,
                                                         mock_session,
                                                         mock_meta):
        engine = mock.MagicMock()
        engine.apply_batch.return_value = {"status": "ok"}
        engine.governor = object()
        mock_session.return_value = engine
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "sample.py")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("pass\n")
            result = run_apply_task("task-apply", {
                "root_dir": root, "file": target, "instruction": "review"}, None)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["meta"], {"source": "test"})
        self.assertEqual(mock_session.call_args.kwargs["allowed_roots"],
                         [os.path.realpath(root)])

    @mock.patch("harness.server.apply_session")
    @mock.patch("harness.server.load_settings", return_value=object())
    def test_run_continue_task_confines_saved_target(self, mock_settings,
                                                     mock_session):
        engine = mock.MagicMock()
        engine.apply_batch.return_value = {"status": "ok"}
        mock_session.return_value = engine
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "sample.py")
            state = os.path.join(root, "resume.json")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("pass\n")
            with open(state, "w", encoding="utf-8") as stream:
                import json
                json.dump({"file_path": target, "verify_only": True}, stream)
            result = run_continue_task("task-continue", {
                "root_dir": root, "state": state}, None)
        self.assertEqual(result["status"], "ok")
        continuation = engine.apply_batch.call_args.kwargs["options"].continuation
        self.assertEqual(continuation["file_path"], target)
        self.assertEqual(mock_session.call_args.kwargs["allowed_roots"],
                         [os.path.realpath(root)])

    @mock.patch("harness.bench.run_bench", return_value={"status": "ok"})
    @mock.patch("harness.bench.load_manifest", return_value=[{}])
    @mock.patch("harness.server.apply_session")
    @mock.patch("harness.server.load_settings", return_value=object())
    def test_run_bench_task_scopes_engine_and_round_limit(self, mock_settings,
                                                          mock_session,
                                                          mock_manifest,
                                                          mock_bench):
        with tempfile.TemporaryDirectory() as root:
            result = run_bench_task("task-bench", {
                "root_dir": root, "manifest": "manifest.json", "max_rounds": 2}, None)
        self.assertEqual(result["status"], "ok")
        tasks = mock_bench.call_args.args[1]
        self.assertEqual(tasks[0]["max_rounds"], 2)
        self.assertEqual(mock_session.call_args.kwargs["allowed_roots"],
                         [os.path.realpath(root)])

    @mock.patch("harness.server.apply_session")
    @mock.patch("harness.waist.compose_plan")
    def test_run_plan_task_execute_no_dag(self, mock_compose, mock_session):
        mock_engine = mock.MagicMock()
        mock_engine.governor = mock.MagicMock()
        mock_engine.transport = mock.MagicMock()
        mock_engine.api_key = "fake-key"
        mock_session.return_value = mock_engine

        mock_compose.return_value = {
            "status": "ok",
            "goal": "Empty dag",
            "dag": {"nodes": []},
        }
        with tempfile.TemporaryDirectory() as root:
            res = run_plan_task("task-p2", {"root_dir": root, "goal": "Empty dag", "execute": True}, cancel_check=None)
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("execution", res)

    @mock.patch("harness.executor.PlanExecutor")
    @mock.patch("harness.server.apply_session")
    @mock.patch("harness.waist.compose_plan")
    def test_run_plan_task_execute_with_dag(self, mock_compose, mock_session, mock_executor_cls):
        mock_engine = mock.MagicMock()
        mock_engine.governor = mock.MagicMock()
        mock_engine.transport = mock.MagicMock()
        mock_engine.api_key = "fake-key"
        mock_session.return_value = mock_engine

        mock_compose.return_value = {
            "status": "ok",
            "goal": "Run tasks",
            "dag": {"nodes": [{"node_id": "n1", "instruction": "edit code", "target_files": ["a.py"]}]},
        }
        mock_executor = mock.MagicMock()
        mock_executor.execute.return_value = {"status": "ok", "nodes_completed": 1}
        mock_executor_cls.return_value = mock_executor

        with tempfile.TemporaryDirectory() as root:
            res = run_plan_task("task-p3", {"root_dir": root, "goal": "Run tasks", "execute": True}, cancel_check=None)
        self.assertEqual(res["status"], "ok")
        self.assertIn("execution", res)
        self.assertEqual(res["execution"]["nodes_completed"], 1)


class TestPlanHttpServer(ServerHarness):
    """Test HTTP dispatch of plan runs over loopback."""

    @mock.patch("harness.server.governor_for")
    @mock.patch("harness.waist.compose_plan")
    def test_post_runs_plan_asynchronous(self, mock_compose, mock_gov):
        mock_gov.return_value = ("fake-key", mock.MagicMock())
        mock_compose.return_value = {
            "status": "ok",
            "goal": "Write docs",
            "dag": {"nodes": []},
        }

        import http.client
        with tempfile.TemporaryDirectory() as root:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                status, data = _request(conn, "POST", "/api/runs", {
                    "kind": "plan",
                    "args": {"root_dir": root, "goal": "Write docs"},
                })
                self.assertEqual(status, 201)
                run_id = data["id"]
                self.assertEqual(data["kind"], "plan")
                self.assertIn(data["status"], ("running", "ok"))

                # Poll for result
                for _ in range(50):
                    st, rdata = _request(conn, "GET", f"/api/runs/{run_id}/result")
                    self.assertEqual(st, 200)
                    if rdata["status"] != "running":
                        break
                    time.sleep(0.05)

                self.assertEqual(rdata["status"], "ok")
                self.assertIsNotNone(rdata["result"])
                self.assertEqual(rdata["result"]["goal"], "Write docs")
            finally:
                conn.close()

    def test_provision_plan_requires_explicit_workspace_root(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/provision/plan", {
                "goal": "Prepare a development environment"})
            self.assertEqual(status, 400)
            self.assertIn("explicit workspace root", data["error"])
        finally:
            conn.close()


class TestPanesJsStructure(unittest.TestCase):
    """Verify that panes.js registers Apply and Dogfood panes."""

    def test_panes_registered(self):
        panes_js_path = os.path.join(os.path.dirname(__file__), "..", "harness", "ui", "panes.js")
        with open(panes_js_path, encoding="utf-8") as f:
            content = f.read()

        self.assertIn('["apply", "Apply", renderApply]', content)
        self.assertIn('["dogfood", "Dogfood", renderDogfood]', content)
        self.assertIn('async function renderApply(', content)
        self.assertIn('async function renderDogfood(', content)


if __name__ == "__main__":
    unittest.main()
