"""Hermetic unit tests for the high-level planning surface (CLI & MCP)."""
import os
import tempfile as _tempfile
import unittest
from unittest.mock import MagicMock

from harness.cli_parser import build_parser
from harness.dag import TaskDAG, heuristic_decompose_goal, plan_task
from harness.errors import HarnessError
from harness.mcp_lanes import lane_for
from harness.mcp_schemas import TOOL_SCHEMAS
from harness.repo_scope import rebase_gate


class TestPlanningSurface(unittest.TestCase):
    def test_heuristic_decompose_single_goal(self):
        dag = heuristic_decompose_goal("Refactor auth system", candidate_files=["harness/auth.py"])
        self.assertIsInstance(dag, TaskDAG)
        self.assertEqual(len(dag.nodes), 1)
        self.assertIn("task_1", dag.nodes)
        self.assertEqual(dag.nodes["task_1"].target_files, ("harness/auth.py",))

    def test_heuristic_decompose_empty_goal_fails(self):
        with self.assertRaises(HarnessError):
            heuristic_decompose_goal("")

    def test_heuristic_decompose_multistep_goal(self):
        goal = (
            "1. Define schemas in types.py\n"
            "2. Implement validator in validate.py\n"
            "3. Add test coverage in test_validate.py"
        )
        dag = heuristic_decompose_goal(goal)
        self.assertEqual(len(dag.nodes), 3)
        self.assertEqual(dag.nodes["task_1"].dependencies, ())
        self.assertEqual(dag.nodes["task_2"].dependencies, ("task_1",))
        self.assertEqual(dag.nodes["task_3"].dependencies, ("task_2",))

    def test_heuristic_decompose_multifile_goal(self):
        files = ["harness/engine.py", "tests/test_engine.py"]
        dag = heuristic_decompose_goal("Update engine logic", candidate_files=files)
        self.assertEqual(len(dag.nodes), 2)
        # test_engine node should depend on engine node
        self.assertEqual(dag.nodes["task_2"].dependencies, ("task_1",))

    def test_plan_task_structure_and_tiers(self):
        res = plan_task(
            goal="Refactor concurrency architecture and eliminate race condition",
            candidate_files=["harness/sync.py"],
            custom_frontier="gpt-6",
            use_free=False,
        )
        self.assertEqual(res["status"], "planned")
        self.assertEqual(res["total_nodes"], 1)
        node_info = res["nodes"][0]
        self.assertEqual(node_info["complexity_tier"], 2)
        self.assertEqual(node_info["recommended_model"], "openai/gpt-6")
        self.assertGreater(node_info["cost_ceiling"], 0.0)

    def test_plan_task_derives_gate_for_each_declared_target(self):
        with _tempfile.TemporaryDirectory() as tmp:
            root = os.path.abspath(tmp)
            target = os.path.join(root, "module.py")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("value = 1\n")
            plan = plan_task("Update module", candidate_files=[target], root=root)
            node = plan["nodes"][0]
            self.assertEqual(node["target_files"], [target])
            self.assertEqual(
                node["local_gate"],
                f'python -m py_compile "{target}"')
            self.assertEqual(plan["dag"]["nodes"][0]["local_gate"],
                             node["local_gate"])

    def test_rebase_gate_replaces_root_once(self):
        gate = r'python -m py_compile "C:\repo\a.py"'
        rebased = rebase_gate(gate, r"C:\repo", r"C:\repo\.harness\wt\n1")
        self.assertEqual(
            rebased,
            r'python -m py_compile "C:\repo\.harness\wt\n1\a.py"')
        self.assertNotIn(r".harness\wt\n1\.harness", rebased)

    def test_rebase_gate_is_host_independent(self):
        """Roots are path spellings, not host paths: a POSIX host rebasing a
        Windows-shaped gate must not abspath-resolve the root against its
        own CWD (the regression that broke Linux CI), and a Windows host
        must not drive-letter a POSIX-shaped root. Both shapes rebase
        identically on every OS."""
        self.assertEqual(
            rebase_gate(r'python -m py_compile "C:\repo\a.py"',
                        r"C:\repo", r"C:\repo\.harness\wt\n1"),
            r'python -m py_compile "C:\repo\.harness\wt\n1\a.py"')
        self.assertEqual(
            rebase_gate('python -m py_compile "/repo/a.py"',
                        "/repo", "/repo/.harness/wt/n1"),
            'python -m py_compile "/repo/.harness/wt/n1/a.py"')
        # Same-root or unmatched spelling: command passes through untouched.
        self.assertEqual(rebase_gate('x "/repo/a.py"', "/repo", "/repo"),
                         'x "/repo/a.py"')
        self.assertEqual(
            rebase_gate('python -m py_compile "/repo/a.py"', "/other", "/another"),
            'python -m py_compile "/repo/a.py"')

    def test_cli_parser_plan_subcommand(self):
        p = build_parser()
        opts = p.parse_args([
            "plan", "--goal", "Refactor X", "--execute", "--parallel",
            "--max-workers", "8", "--max-cost", "0.5", "--persist-state", "p.json",
        ])
        self.assertEqual(opts.command, "plan")
        self.assertEqual(opts.goal, "Refactor X")
        self.assertTrue(opts.execute)
        self.assertTrue(opts.parallel)
        self.assertEqual(opts.max_workers, 8)
        self.assertEqual(opts.max_cost, 0.5)
        self.assertEqual(opts.persist_state, "p.json")

    def test_cli_cmd_plan_preview_with_governor(self):
        from harness.cli import _cmd_plan
        from types import SimpleNamespace
        from unittest.mock import patch, MagicMock

        opts = SimpleNamespace(
            goal="1. Plan auth\n2. Plan tokens",
            file=None,
            frontier_model=None,
            execute=False,
            decompose_llm=True,
            confirm=True,
            plan_consensus=False,
            out=None,
            max_cost=1.0,
            task_max_cost=None,
        )
        settings = SimpleNamespace(use_free=True, frontier_model=None,
                                   hourglass_confirm=True,
                                   hourglass_parallel=False,
                                   hourglass_decompose=True)

        mock_gov = MagicMock()
        mock_gov.max_cost = 1.0
        with patch("harness.cli._governor", return_value=("k", mock_gov)) as mock_g, \
             patch("harness.cli._plan_compose", return_value={"status": "planned", "goal": "g"}), \
             patch("harness.cli._emit") as mock_emit:
            _cmd_plan(opts, settings)
            mock_g.assert_called_once_with(settings, 1.0)
            mock_emit.assert_called_once()

    def test_mcp_schemas_and_lanes_plan_and_execute(self):
        tool_names = [s["name"] for s in TOOL_SCHEMAS]
        self.assertIn("plan_and_execute", tool_names)
        self.assertEqual(lane_for("plan_and_execute"), "mutation")

    def test_mcp_plan_and_execute_preview_and_execute(self):
        from harness.mcp import McpServer

        # Mock dependencies
        mock_gov = MagicMock()
        mock_gov.spent = 0.0
        mock_gov.max_cost = 1.0
        mock_ledger = MagicMock()
        mock_ledger.participation_report.return_value = {}
        mock_router = MagicMock()
        mock_router.judge = "judge-model"
        mock_router.panel_pool = ["m1"]
        mock_engine = MagicMock()
        mock_engine.reasoning_token_budget = 0.4
        mock_engine.reasoning_effort = "auto"
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}

        server = McpServer(
            transport=MagicMock(),
            api_key="key",
            governor=mock_gov,
            ledger=mock_ledger,
            router=mock_router,
            engine=mock_engine,
            allow_write=True,
            allow_verify=True,
        )

        # 1. Preview mode (execute=False); the hourglass defaults are ON,
        # so a preview with confirm would attempt a frontier call -- the
        # host opts out per request (this test targets the planner only).
        preview_res = server._invoke("plan_and_execute", {"goal": "1. Step A\n2. Step B", "execute": False, "confirm": False, "decompose_llm": False})
        self.assertEqual(preview_res["status"], "planned")
        self.assertEqual(preview_res["total_nodes"], 2)

        # 2. Execution mode without allow_write on server or call fails
        server.allow_write = False
        with self.assertRaises(HarnessError):
            server._invoke("plan_and_execute", {"goal": "Step A", "execute": True, "allow_write": False})

        # 3. Execution mode with allow_write succeeds and exercises target_files
        server.allow_write = True
        exec_res = server._invoke("plan_and_execute", {"goal": "Step A", "file": ["foo.py"], "execute": True, "confirm": False, "require_diff_authorization": False, "decompose_llm": False, "final_gate": False})
        self.assertEqual(exec_res["status"], "ok")
        self.assertEqual(exec_res["completed_nodes"], 1)
        mock_engine.apply_edit.assert_called()
        # MCP parity with the CLI plan lane: the node's tier ladder reaches
        # apply_edit as the per-request pool (engine orders it; no pin).
        self.assertIn("apply_pool", mock_engine.apply_edit.call_args[1])

    def test_cli_cmd_plan_preview(self):
        from harness.cli import _cmd_plan
        from types import SimpleNamespace
        from unittest.mock import patch

        opts = SimpleNamespace(
            goal="1. Plan auth\n2. Plan tokens",
            file=None,
            frontier_model=None,
            execute=False,
            decompose_llm=False,
            out=None,
        )
        settings = SimpleNamespace(use_free=True, frontier_model=None,
                                   hourglass_confirm=False,
                                   hourglass_parallel=False,
                                   hourglass_decompose=False)

        with patch("harness.cli._emit") as mock_emit:
            _cmd_plan(opts, settings)
            mock_emit.assert_called_once()
            res = mock_emit.call_args[0][0]
            self.assertEqual(res["status"], "planned")
            self.assertEqual(res["total_nodes"], 2)

    def test_cli_cmd_plan_execute_sequential_and_parallel(self):
        from harness.cli import _cmd_plan
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        mock_engine = MagicMock()
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.002}

        # 1. Sequential execution (parallel=False)
        opts_seq = SimpleNamespace(
            goal="Refactor auth system",
            file=["harness/auth.py"],
            frontier_model="fable-5.1",
            execute=True,
            parallel=False,
            max_workers=1,
            max_cost=0.1,
            keep_going=False,
            out=None,
            model=None,
            max_tokens=None,
            task_max_cost=None,
            allow_escalation=False,
            reasoning_effort=None,
            max_rotations=3,
            decompose_llm=False,
            final_gate=False,
        )
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   hourglass_confirm=False,
                                   hourglass_isolate=False,
                                   hourglass_require_attestation=False,
                                   hourglass_decompose=False)

        with patch("harness.cli._session", return_value=mock_engine), patch("harness.cli._emit_by_status") as mock_emit:
            _cmd_plan(opts_seq, settings)
            mock_emit.assert_called_once()
            res = mock_emit.call_args[0][0]
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["completed_nodes"], 1)
            self.assertAlmostEqual(res["cost"], 0.002)

        # 2. Parallel execution with partial failure and keep_going=True
        mock_engine.apply_edit.side_effect = [
            {"status": "ok", "cost": 0.001},
            {"status": "verify_failed", "cost": 0.001},
        ]
        opts_par = SimpleNamespace(
            goal="1. Step one\n2. Step two",
            file=None,
            frontier_model=None,
            execute=True,
            parallel=True,
            max_workers=2,
            max_cost=None,
            keep_going=True,
            out=None,
            model=None,
            max_tokens=None,
            task_max_cost=None,
            allow_escalation=False,
            reasoning_effort=None,
            max_rotations=3,
            decompose_llm=False,
            final_gate=False,
        )
        with patch("harness.cli._session", return_value=mock_engine), patch("harness.cli._emit_by_status") as mock_emit:
            _cmd_plan(opts_par, settings)
            mock_emit.assert_called_once()
            res = mock_emit.call_args[0][0]
            self.assertEqual(res["status"], "failed")
            self.assertEqual(res["completed_nodes"], 1)

    def test_node_apply_kwargs_paid_tier_ladder_and_ceiling(self):
        from harness.dag import node_apply_kwargs

        plan = plan_task(
            goal="Refactor concurrency architecture and eliminate race condition",
            candidate_files=["harness/sync.py"],
            custom_frontier="gpt-6",
            use_free=False,
        )
        detail = plan["nodes"][0]
        kwargs = node_apply_kwargs(detail)
        self.assertEqual(kwargs["apply_pool"], detail["route"]["ladder"])
        self.assertEqual(kwargs["task_max_cost"], detail["route"]["cost_ceiling"])
        self.assertGreater(kwargs["task_max_cost"], 0.0)
        self.assertIn("openai/gpt-6", kwargs["apply_pool"])

    def test_node_apply_kwargs_free_tier_skips_zero_ceiling(self):
        from harness.dag import node_apply_kwargs

        plan = plan_task(
            goal="Fix typo in docstring and format",
            candidate_files=["harness/sync.py"],
            use_free=True,
        )
        detail = plan["nodes"][0]
        kwargs = node_apply_kwargs(detail)
        self.assertEqual(kwargs["apply_pool"], detail["route"]["ladder"])
        # A $0 ceiling is never passed: a zero task budget would refuse the
        # escalation ladder for hard nodes; the governor owns free-tier cost.
        self.assertNotIn("task_max_cost", kwargs)

    def test_node_apply_kwargs_explicit_pins_win(self):
        from harness.dag import node_apply_kwargs

        detail = {"route": {"ladder": ["m1", "m2"], "cost_ceiling": 0.04}}
        # An explicit model pin means manual routing: no pool override at all.
        self.assertEqual(node_apply_kwargs(detail, explicit_model="mine"), {})
        # An explicit task-max pin suppresses only the ceiling.
        kwargs = node_apply_kwargs(detail, explicit_task_max_cost=0.25)
        self.assertEqual(kwargs, {"apply_pool": ["m1", "m2"]})

    def test_node_apply_kwargs_missing_detail_degrades_to_defaults(self):
        from harness.dag import node_apply_kwargs

        self.assertEqual(node_apply_kwargs(None), {})
        self.assertEqual(node_apply_kwargs({}), {})
        self.assertEqual(
            node_apply_kwargs({"route": {"ladder": [], "cost_ceiling": None}}), {})

    def test_node_apply_kwargs_malformed_route_degrades_never_fails_open(self):
        """MR-1 frontier finding, confirmed hermetically: malformed route
        data must degrade to previous behavior ({}), never crash and never
        poison routing."""
        from harness.dag import node_apply_kwargs

        # Truthy non-mapping detail/route used to raise AttributeError.
        for bad_detail in ("x", 42, ["a"], ("a",)):
            self.assertEqual(node_apply_kwargs(bad_detail), {})
        self.assertEqual(node_apply_kwargs({"route": "x"}), {})
        self.assertEqual(node_apply_kwargs({"route": 42}), {})
        # A string ladder used to become per-character "model ids".
        self.assertEqual(node_apply_kwargs({"route": {"ladder": "gpt-6"}}), {})
        # A non-iterable ladder used to raise TypeError.
        self.assertEqual(node_apply_kwargs({"route": {"ladder": 7}}), {})
        # An inf ceiling used to pass through as an unbounded task budget.
        self.assertEqual(
            node_apply_kwargs({"route": {"cost_ceiling": float("inf")}}), {})
        # Well-formed routes are untouched.
        self.assertEqual(
            node_apply_kwargs({"route": {"ladder": ["a/b", "c/d"],
                                         "cost_ceiling": 0.04}}),
            {"apply_pool": ["a/b", "c/d"], "task_max_cost": 0.04})

    def test_cli_cmd_plan_execute_threads_node_route(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from harness.cli import _cmd_plan

        mock_engine = MagicMock()
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.002}
        goal = "Refactor concurrency architecture and eliminate race condition"
        opts = SimpleNamespace(
            goal=goal,
            file=["harness/sync.py"],
            frontier_model="gpt-6",
            execute=True,
            parallel=False,
            max_workers=1,
            max_cost=1.0,
            keep_going=False,
            out=None,
            model=None,
            max_tokens=None,
            task_max_cost=None,
            allow_escalation=False,
            reasoning_effort=None,
            max_rotations=3,
        )
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   hourglass_confirm=False,
                                   hourglass_isolate=False,
                                   hourglass_require_attestation=False)

        with patch("harness.cli._session", return_value=mock_engine), \
             patch("harness.cli._emit_by_status") as mock_emit:
            _cmd_plan(opts, settings)
            self.assertEqual(mock_emit.call_count, 1)
        kwargs = mock_engine.apply_edit.call_args[1]
        route = plan_task(goal=goal, candidate_files=["harness/sync.py"],
                          custom_frontier="gpt-6", use_free=False)["nodes"][0]["route"]
        self.assertEqual(kwargs["apply_pool"], route["ladder"])
        self.assertEqual(kwargs["task_max_cost"], route["cost_ceiling"])
        # Explicit pins stay absent when unset.
        self.assertEqual(kwargs["model"], None)

    def test_plan_hourglass_flags_resolve_to_settings_defaults(self):
        """Auto-scaling hourglass: with no flags given, the plan lane
        confirms at the waist, runs stages in parallel, isolates them in
        git worktrees, and attests every node write -- each opt-out-able
        by flag or settings file."""
        from types import SimpleNamespace

        from harness.cli import _resolve_hourglass

        parser = build_parser()
        settings = SimpleNamespace(
            hourglass_confirm=True, hourglass_isolate=True,
            hourglass_parallel=True, hourglass_require_attestation=True)
        defaults = parser.parse_args(["plan", "--goal", "g"])
        self.assertIsNone(defaults.confirm)
        self.assertIsNone(defaults.isolate)
        self.assertIsNone(defaults.parallel)
        self.assertIsNone(defaults.require_diff_authorization)
        resolved = _resolve_hourglass(defaults, settings)
        self.assertEqual(resolved, {"confirm": True, "isolate": True,
                                    "parallel": True,
                                    "require_diff_authorization": True,
                                    # HG-decompose-default: rides the hourglass.
                                    "decompose": True})
        # Explicit flags win over settings; opt-outs honored.
        off = parser.parse_args(["plan", "--goal", "g", "--no-confirm",
                                 "--no-isolate", "--no-parallel",
                                 "--no-attestation"])
        self.assertEqual(_resolve_hourglass(off, settings), {
            "confirm": False, "isolate": False, "parallel": False,
            "require_diff_authorization": False, "decompose": False})
        settings_off = SimpleNamespace(
            hourglass_confirm=False, hourglass_isolate=False,
            hourglass_parallel=False, hourglass_require_attestation=False)
        self.assertEqual(_resolve_hourglass(defaults, settings_off), {
            "confirm": False, "isolate": False, "parallel": False,
            "require_diff_authorization": False, "decompose": False})
        mixed = parser.parse_args(["plan", "--goal", "g", "--no-confirm"])
        self.assertIs(_resolve_hourglass(mixed, settings)["confirm"], False)
        self.assertIs(_resolve_hourglass(mixed, settings)["isolate"], True)
        # Parallel still on => decompose still defaults True.
        self.assertIs(_resolve_hourglass(mixed, settings)["decompose"], True)

    def test_plan_isolation_flags_parse(self):
        parser = build_parser()
        opts = parser.parse_args(["plan", "--goal", "g", "--isolate",
                                  "--stage-gate", "python -m py_compile x.py"])
        self.assertTrue(opts.isolate)
        self.assertEqual(opts.stage_gate, "python -m py_compile x.py")

    @staticmethod
    def _canned_plan(*args, **kwargs):
        """compose_plan stand-in for execute-lane tests: the hourglass
        defaults are resolved UPSTREAM of this owner, so the lane tests
        only need a valid planned DAG."""
        return {
            "status": "planned",
            "goal": kwargs.get("opts_goal", ""),
            "dag": {"nodes": [
                {"node_id": "task_1", "instruction": "do a",
                 "target_files": ["iso_a.py"], "dependencies": []},
                {"node_id": "task_2", "instruction": "do b",
                 "target_files": ["iso_b.py"], "dependencies": []},
            ]},
            "nodes": [
                {"node_id": "task_1", "route": {"ladder": ["m/a"], "cost_ceiling": 0.04}},
                {"node_id": "task_2", "route": {"ladder": ["m/b"], "cost_ceiling": 0.04}},
            ],
        }

    def test_cli_cmd_plan_execute_isolated_parallel_with_stage_gate(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from harness.cli import _cmd_plan

        def fake_apply(**kwargs):
            runner = kwargs.get("task_runner")
            if runner is not None:
                runner("git status --porcelain")
            return {"status": "ok", "cost": 0.001}

        mock_engine = MagicMock()
        mock_engine.apply_edit.side_effect = fake_apply
        opts = SimpleNamespace(
            goal="Update the modules",
            file=["iso_a.py", "iso_b.py"],
            frontier_model=None,
            execute=True,
            parallel=True,
            max_workers=2,
            max_cost=1.0,
            keep_going=False,
            out=None,
            model=None,
            max_tokens=None,
            task_max_cost=None,
            allow_escalation=False,
            reasoning_effort=None,
            max_rotations=3,
            isolate=True,
            stage_gate="git rev-parse HEAD",
            decompose_llm=False,
            final_gate=False,
        )
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   hourglass_confirm=True,
                                   hourglass_require_attestation=False,
                                   hourglass_decompose=False,
                                   ledger_path=os.path.join(
                                       _tempfile.mkdtemp(), 'l.jsonl'))

        with patch("harness.cli._session", return_value=mock_engine), \
             patch("harness.cli._compose_plan", side_effect=self._canned_plan) as cp, \
             patch("harness.cli._emit_by_status") as mock_emit:
            _cmd_plan(opts, settings)
        res = mock_emit.call_args[0][0]
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["completed_nodes"], 2)
        # The hourglass default reached the waist: confirm resolved on.
        self.assertTrue(cp.call_args[1]["confirm"])
        # The isolated lane hands apply_edit a worktree-scoped runner.
        for call in mock_engine.apply_edit.call_args_list:
            self.assertIn("task_runner", call[1])

    def test_cli_plan_lane_passes_its_run_ceiling_to_the_executor(self):
        """The CLI lane tells the shared assembly the budget it is really
        running under (the governor ceiling it built), so a node reservation
        is bounded by that instead of by the engine's nominal default."""
        from types import SimpleNamespace
        from unittest.mock import patch

        import harness.executor as executor_module
        from harness.cli import _cmd_plan
        from tests._fake import FakeTransport, _gov

        class _StubEngine:
            def __init__(self, governor):
                self.governor = governor
                self.transport = None
                self.api_key = None
                self.default_task_max_cost = 0.10
                self.calls = []

            def apply_edit(self, **kwargs):
                self.calls.append(kwargs)
                return {"status": "ok", "cost": 0.0}

        captured = {}

        def spy(engine_arg, routes, **kwargs):
            captured.update(kwargs)
            captured["exec"] = executor_module.PlanExecutor(
                engine_arg, routes, **kwargs)
            return captured["exec"]

        def single_node_plan(*args, **kwargs):
            return {
                "status": "planned", "goal": kwargs.get("opts_goal", ""),
                "dag": {"nodes": [
                    {"node_id": "task_1", "instruction": "do a",
                     "target_files": ["iso_a.py"], "dependencies": []}]},
                "nodes": [
                    {"node_id": "task_1",
                     "route": {"ladder": ["m/a"], "cost_ceiling": 0.04}}],
            }

        opts = SimpleNamespace(
            goal="Update the module", file=["iso_a.py"],
            frontier_model=None, execute=True, parallel=True, max_workers=2,
            max_cost=None, keep_going=False, out=None, model=None,
            max_tokens=None, task_max_cost=None, allow_escalation=False,
            reasoning_effort=None, max_rotations=3, isolate=False,
            stage_gate=None)
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   hourglass_confirm=False,
                                   hourglass_require_attestation=False)
        gov = _gov(FakeTransport(), max_cost=0.05)
        engine = _StubEngine(gov)

        with patch("harness.cli._session", return_value=engine), \
             patch("harness.cli._compose_plan", side_effect=single_node_plan), \
             patch("harness.cli.PlanExecutor", side_effect=spy), \
             patch("harness.cli._emit_by_status") as mock_emit:
            _cmd_plan(opts, settings)
        res = mock_emit.call_args[0][0]
        self.assertEqual(res["status"], "ok")
        self.assertEqual(captured["run_ceiling"], 0.05)
        self.assertEqual(captured["exec"].reserver.run_ceiling, 0.05)
        # The declared $0.04 tier ceiling was reserved, then reconciled.
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(gov.spent, 0.0)

    def test_cli_cmd_plan_isolate_unavailable_degrades_loudly(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from harness.cli import _cmd_plan

        mock_engine = MagicMock()
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
        opts = SimpleNamespace(
            goal="Update the modules",
            file=["iso_a.py", "iso_b.py"],
            frontier_model=None,
            execute=True,
            parallel=True,
            max_workers=2,
            max_cost=1.0,
            keep_going=False,
            out=None,
            model=None,
            max_tokens=None,
            task_max_cost=None,
            allow_escalation=False,
            reasoning_effort=None,
            max_rotations=3,
            isolate=True,
            stage_gate=None,
        )
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   hourglass_confirm=True,
                                   hourglass_require_attestation=False,
                                   ledger_path=os.path.join(
                                       _tempfile.mkdtemp(), 'l.jsonl'))

        # Isolation lives in the ONE assembly every lane builds
        # (executor.PlanExecutor), so the seam is patched there.
        with patch("harness.cli._session", return_value=mock_engine), \
             patch("harness.cli._compose_plan", side_effect=self._canned_plan), \
             patch("harness.executor.WorktreeIsolation") as mock_iso_cls, \
             patch("harness.executor.eprint") as mock_eprint, \
             patch("harness.cli._emit_by_status") as mock_emit:
            mock_iso_cls.return_value.available.return_value = False
            _cmd_plan(opts, settings)
        res = mock_emit.call_args[0][0]
        # Degraded but usable: shared-tree mutex execution still runs.
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["completed_nodes"], 2)
        self.assertIn("unavailable", str(mock_eprint.call_args))

    def test_hourglass_mapping_has_one_owner(self):
        """The CLI flags, the MCP defaults, and the agent lane all resolve
        the same switches from the same settings through config.
        resolve_hourglass -- no lane re-derives the mapping."""
        from types import SimpleNamespace

        from harness.cli import _resolve_hourglass
        from harness.config import resolve_hourglass

        settings = SimpleNamespace(hourglass_confirm=True,
                                   hourglass_parallel=True,
                                   hourglass_isolate=True,
                                   hourglass_require_attestation=True)
        flags_off = SimpleNamespace(confirm=False, parallel=False, isolate=False,
                                    require_diff_authorization=False)
        self.assertEqual(_resolve_hourglass(flags_off, settings), {
            "confirm": False, "parallel": False, "isolate": False,
            "require_diff_authorization": False, "decompose": False})
        # An explicit flag wins; a missing/None flag inherits the settings
        # file, which is exactly what the agent lane (opts=None) reads.
        self.assertTrue(resolve_hourglass(settings, SimpleNamespace())['confirm'])
        self.assertEqual(resolve_hourglass(settings), _resolve_hourglass(None, settings))

    def test_cli_cmd_plan_stage_gate_failure_aborts(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from harness.cli import _cmd_plan

        mock_engine = MagicMock()
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
        opts = SimpleNamespace(
            goal="Refactor auth system",
            file=["iso_a.py"],
            frontier_model=None,
            execute=True,
            parallel=False,
            max_workers=1,
            max_cost=1.0,
            keep_going=False,
            out=None,
            model=None,
            max_tokens=None,
            task_max_cost=None,
            allow_escalation=False,
            reasoning_effort=None,
            max_rotations=3,
            isolate=False,
            stage_gate="git rev-parse --verify refs/heads/no-such-ref",
        )
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   hourglass_confirm=True,
                                   hourglass_require_attestation=False,
                                   ledger_path=os.path.join(
                                       _tempfile.mkdtemp(), 'l.jsonl'))

        with patch("harness.cli._session", return_value=mock_engine), \
             patch("harness.cli._compose_plan", side_effect=self._canned_plan):
            with self.assertRaises(HarnessError) as ctx:
                _cmd_plan(opts, settings)
        # Fail-closed: a red composed tree stops dependent stages.
        self.assertIn("stage gate failed", str(ctx.exception))

    def test_mcp_plan_and_execute_defaults_to_full_hourglass(self):
        """GUI default request: no per-request args needed -- parallel +
        isolation + waist confirmation + write attestation all engage from
        the server's hourglass defaults."""
        from unittest.mock import patch

        from harness.mcp import McpServer

        mock_engine = MagicMock()
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
        mock_gov = MagicMock()
        mock_gov.spent = 0.0
        mock_gov.max_cost = 1.0
        mock_ledger = MagicMock()
        mock_ledger.participation_report.return_value = {}
        mock_router = MagicMock()
        mock_router.judge = "judge-model"
        mock_router.panel_pool = ["m1"]

        server = McpServer(
            transport=MagicMock(),
            api_key="key",
            governor=mock_gov,
            ledger=mock_ledger,
            router=mock_router,
            engine=mock_engine,
            allow_write=True,
            allow_verify=True,
        )
        canned = {
            "status": "planned", "goal": "Refactor auth system",
            "dag": {"nodes": [
                {"node_id": "task_1", "instruction": "Refactor auth system",
                 "target_files": ["iso_e.py"], "dependencies": []}]},
            "nodes": [{"node_id": "task_1",
                       "route": {"ladder": ["m/a"], "cost_ceiling": 0.04}}],
        }
        with patch("harness.mcp.compose_plan", return_value=canned) as cp:
            plan_res = server._invoke("plan_and_execute", {
                "goal": "Refactor auth system", "file": ["iso_e.py"],
                "execute": True, "allow_write": True, "final_gate": False,
            })
        self.assertEqual(plan_res["status"], "ok")
        # The waist-confirmation default reached the ONE owner.
        self.assertTrue(cp.call_args[1]["confirm"])
        # The write-attestation default reached every node write.
        call = mock_engine.apply_edit.call_args
        self.assertTrue(call[1]["require_diff_authorization"])

    def test_mcp_plan_and_execute_per_request_opt_out_wins(self):
        """A host can still run a bare lane: per-request args override the
        hourglass defaults."""
        from unittest.mock import patch

        from harness.mcp import McpServer

        mock_engine = MagicMock()
        mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
        server = McpServer(
            transport=MagicMock(), api_key="key",
            governor=MagicMock(), ledger=MagicMock(),
            router=MagicMock(), engine=mock_engine,
            allow_write=True, allow_verify=True,
        )
        canned = {
            "status": "planned", "goal": "g",
            "dag": {"nodes": [
                {"node_id": "task_1", "instruction": "do a",
                 "target_files": ["iso_e.py"], "dependencies": []}]},
            "nodes": [{"node_id": "task_1",
                       "route": {"ladder": ["m/a"], "cost_ceiling": 0.04}}],
        }
        with patch("harness.mcp.compose_plan", return_value=canned) as cp:
            server._invoke("plan_and_execute", {
                "goal": "g", "file": ["iso_e.py"],
                "execute": True, "allow_write": True,
                "confirm": False, "parallel": False,
                "require_diff_authorization": False,
                "decompose_llm": False, "final_gate": False,
            })
        self.assertFalse(cp.call_args[1]["confirm"])
        call = mock_engine.apply_edit.call_args
        self.assertFalse(call[1]["require_diff_authorization"])
        # No isolation attempted with parallel off.
        self.assertNotIn("task_runner", call[1])

    def test_mcp_plan_and_execute_parallel_isolated(self):
        from harness.mcp import McpServer

        def fake_apply(**kwargs):
            runner = kwargs.get("task_runner")
            if runner is not None:
                runner("git status --porcelain")
            return {"status": "ok", "cost": 0.001}

        mock_gov = MagicMock()
        mock_gov.spent = 0.0
        mock_gov.max_cost = 1.0
        mock_ledger = MagicMock()
        mock_ledger.participation_report.return_value = {}
        mock_router = MagicMock()
        mock_router.judge = "judge-model"
        mock_router.panel_pool = ["m1"]
        mock_engine = MagicMock()
        mock_engine.reasoning_token_budget = 0.4
        mock_engine.reasoning_effort = "auto"
        mock_engine.apply_edit.side_effect = fake_apply

        server = McpServer(
            transport=MagicMock(),
            api_key="key",
            governor=mock_gov,
            ledger=mock_ledger,
            router=mock_router,
            engine=mock_engine,
            allow_write=True,
            allow_verify=True,
        )
        exec_res = server._invoke("plan_and_execute", {
            "goal": "Update the modules",
            "file": ["iso_c.py", "iso_d.py"],
            "execute": True, "allow_write": True, "parallel": True,
            "confirm": False, "decompose_llm": False, "final_gate": False,
        })
        self.assertEqual(exec_res["status"], "ok")
        self.assertEqual(exec_res["completed_nodes"], 2)
        # MCP parity with the CLI isolate lane: worktree-scoped runners.
        for call in mock_engine.apply_edit.call_args_list:
            self.assertIn("task_runner", call[1])

    def test_cli_plan_confirm_refusal_fails_closed(self):
        import json as _json
        import os as _os
        import shutil as _shutil
        import tempfile as _tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        from harness.cli import _cmd_plan
        from harness.spend import SpendGovernor
        from tests._fake import FakeTransport, m

        refuse = {"choices": [{"message": {"content": _json.dumps({
            "verdict": "refuse", "reason": "tier mismatch",
            "evidence": "brief: task_1 needs frontier"})},
            "finish_reason": "stop"}],
            "usage": {"cost": 0.0001, "is_byok": False}}
        fake = FakeTransport(models=[m("front/x")], posts=[refuse])
        gov = SpendGovernor(fake, "sk-test")
        ledger_dir = _tempfile.mkdtemp()
        self.addCleanup(_shutil.rmtree, ledger_dir, ignore_errors=True)
        opts = SimpleNamespace(
            goal="Refactor concurrency architecture", file=["harness/sync.py"],
            frontier_model="front/x", execute=True, parallel=False,
            max_workers=1, max_cost=1.0, keep_going=False, out=None,
            model=None, max_tokens=None, task_max_cost=None,
            allow_escalation=False, reasoning_effort=None, max_rotations=3,
            decompose_llm=False, confirm=True)
        settings = SimpleNamespace(use_free=False, frontier_model=None,
                                   ledger_path=_os.path.join(ledger_dir, "l.jsonl"))

        mock_engine = MagicMock()
        mock_engine.governor = gov
        mock_engine.transport = fake
        mock_engine.api_key = "k"
        with patch("harness.cli._governor", return_value=("k", gov)), \
             patch("harness.cli.HttpTransport", return_value=fake), \
             patch("harness.cli._session", return_value=mock_engine):
            with self.assertRaises(SystemExit) as ctx:
                _cmd_plan(opts, settings)
        # Fail-closed: exit 2, and despite --execute the DAG never dispatches
        # (the engine builds first to share its ceiling; the gate is the waist).
        self.assertEqual(ctx.exception.code, 2)
        mock_engine.apply_edit.assert_not_called()
