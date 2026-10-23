"""Hourglass Vision HV-6: Surface parity across CLI, MCP, and Server/API faces.

Exposes stage selection, token budgets, context brief, and dogfood across
all interfaces using shared policy owners:
- Stage selection: harness.waist.resolve_stages / compose_arguments
- Token budgets: harness.token_budget.TokenBudget / budget_from_settings
- Context brief: harness.brief / harness.waist.intake_brief
- Dogfood loop: harness.service.run_dogfood (ground -> verify -> apply)
"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from harness.cli_parser import build_parser
from harness.config import load_settings, update_config
from harness.errors import HarnessError
from harness.mcp import McpServer
from harness.mcp_schemas import TOOL_SCHEMAS
from harness.server import RUNNERS, validate_dispatch
from harness.service import run_dogfood
from harness.token_budget import budget_from_settings
from harness.waist import (
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STAGE_ORDER,
    STAGE_PLANNING,
    STAGE_VERIFICATION,
    compose_arguments,
    resolve_stages,
)


def _cfg(**kw):
    return load_settings(overrides=kw) if kw else load_settings()


class StageSelectionSurfaceParityTests(unittest.TestCase):
    """Stage selection operates identically across CLI, MCP, and Server."""

    def test_resolve_stages_is_the_single_owner(self):
        # Full default stages
        full = resolve_stages()
        self.assertEqual(full["stages"], list(STAGE_ORDER))

        # Subset preserved in pipeline order even if requested in reverse
        subset = resolve_stages(["planning", "context"])
        self.assertEqual(subset["stages"], [STAGE_CONTEXT, STAGE_PLANNING])

        # Unknown stage is refused fail-closed
        with self.assertRaises(HarnessError):
            resolve_stages(["unknown_stage"])

    def test_cli_parser_and_compose_arguments_stage_forwarding(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "--goal", "test goal", "--stages", "context,planning"])
        self.assertEqual(args.stages, "context,planning")

        cfg = _cfg(use_free=True)
        composed = compose_arguments(cfg, goal=args.goal, files=[], stages=args.stages)
        self.assertEqual(composed["stages"], [STAGE_CONTEXT, STAGE_PLANNING])

        # Invalid stage via CLI flags raises HarnessError
        with self.assertRaises(HarnessError):
            compose_arguments(cfg, goal="test", files=[], stages="invalid_stage")

    def test_mcp_plan_and_execute_stage_selection_parity(self):
        cfg = _cfg(use_free=True)
        schema = next(s for s in TOOL_SCHEMAS if s["name"] == "plan_and_execute")
        self.assertIn("stages", schema["inputSchema"]["properties"])

        # String comma-separated
        args_str = {"goal": "test goal", "stages": "context,planning"}
        comp_str = compose_arguments(cfg, goal=args_str["goal"], files=[], stages=args_str["stages"])
        self.assertEqual(comp_str["stages"], [STAGE_CONTEXT, STAGE_PLANNING])

        # List of stages
        args_list = {"goal": "test goal", "stages": ["execution", "verification"]}
        comp_list = compose_arguments(cfg, goal=args_list["goal"], files=[], stages=args_list["stages"])
        self.assertEqual(comp_list["stages"], [STAGE_EXECUTION, STAGE_VERIFICATION])

    def test_server_update_config_validates_stages(self):
        # Valid stages list passes and is saved
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("harness.config.CONFIG_DIR", tmpdir):
                updated = update_config({"hourglass_stages": "context,planning"})
                self.assertEqual(updated.hourglass_stages, ["context", "planning"])

                # Invalid stage fails closed before write
                with self.assertRaises(HarnessError):
                    update_config({"hourglass_stages": "invalid_stage"})


class TokenBudgetSurfaceParityTests(unittest.TestCase):
    """Token allowances and ceilings operate through TokenBudget across all surfaces."""

    def test_budget_from_settings_overrides(self):
        cfg = _cfg(use_free=True, token_budget_input=8000, token_budget_output=2000)
        default_b = budget_from_settings(cfg)
        self.assertEqual(default_b.max_input_tokens, 8000)
        self.assertEqual(default_b.max_output_tokens, 2000)

        # Explicit overrides take precedence
        override_b = budget_from_settings(cfg, max_input_tokens=16000, max_output_tokens=4000)
        self.assertEqual(override_b.max_input_tokens, 16000)
        self.assertEqual(override_b.max_output_tokens, 4000)

    def test_cli_token_budget_flags_forwarding(self):
        parser = build_parser()
        args = parser.parse_args([
            "plan", "--goal", "test goal",
            "--token-budget-input", "15000",
            "--token-budget-output", "3000",
        ])
        self.assertEqual(args.token_budget_input, 15000)
        self.assertEqual(args.token_budget_output, 3000)

        cfg = _cfg(use_free=True)
        composed = compose_arguments(
            cfg, goal=args.goal, files=[],
            max_input_tokens=args.token_budget_input,
            max_output_tokens=args.token_budget_output)
        self.assertEqual(composed["token_budget"].max_input_tokens, 15000)
        self.assertEqual(composed["token_budget"].max_output_tokens, 3000)

    def test_mcp_token_budget_schema_and_forwarding(self):
        schema = next(s for s in TOOL_SCHEMAS if s["name"] == "plan_and_execute")
        self.assertIn("token_budget_input", schema["inputSchema"]["properties"])
        self.assertIn("token_budget_output", schema["inputSchema"]["properties"])

        cfg = _cfg(use_free=True)
        composed = compose_arguments(
            cfg, goal="test goal", files=[],
            max_input_tokens=18000, max_output_tokens=5000)
        self.assertEqual(composed["token_budget"].max_input_tokens, 18000)
        self.assertEqual(composed["token_budget"].max_output_tokens, 5000)

    def test_server_update_config_allows_token_budgets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("harness.config.CONFIG_DIR", tmpdir):
                updated = update_config({
                    "token_budget_input": 25000,
                    "token_budget_output": 6000,
                })
                self.assertEqual(updated.token_budget_input, 25000)
                self.assertEqual(updated.token_budget_output, 6000)


class BriefIntakeSurfaceParityTests(unittest.TestCase):
    """Context brief intake and bypass parity across surfaces."""

    def test_cli_and_mcp_brief_file_intake_and_bypass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            brief_file = os.path.join(tmpdir, "test_brief.json")
            brief_data = {
                "version": "2.0.0",
                "goal": "implement feature",
                "grounding": {"sources": [{"path": "main.py", "bytes": 100, "observed_at": 1000.0}]},
                "scope": {"included": ["main.py"], "excluded": []},
                "omitted": [],
                "conflicts": [],
                "coverage": {"sources_total": 1, "sources_represented": 1, "gaps": 0},
                "built_at": 1000.0,
                "estimated_tokens": 150,
            }
            with open(brief_file, "w", encoding="utf-8") as f:
                json.dump(brief_data, f)

            cfg = _cfg(use_free=True)

            # Via CLI flag path
            composed_cli = compose_arguments(cfg, goal="implement feature", files=["main.py"], brief=brief_file)
            self.assertTrue(composed_cli["supplied_brief"])
            self.assertEqual(composed_cli["brief"]["version"], "2.0.0")

            # Via MCP object dictionary
            composed_mcp = compose_arguments(cfg, goal="implement feature", files=["main.py"], brief=brief_data)
            self.assertTrue(composed_mcp["supplied_brief"])
            self.assertEqual(composed_mcp["brief"]["version"], "2.0.0")


class DogfoodSurfaceParityTests(unittest.TestCase):
    """Dogfood loop canonical service layer and surface interfaces."""

    def test_schema_registers_dogfood_tool(self):
        schema = next((s for s in TOOL_SCHEMAS if s["name"] == "dogfood"), None)
        self.assertIsNotNone(schema)
        self.assertIn("file", schema["inputSchema"]["properties"])
        self.assertIn("instruction", schema["inputSchema"]["properties"])
        self.assertIn("file", schema["inputSchema"]["required"])

    def test_server_validate_dispatch_and_runner_registered(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"content")
            target_path = f.name
        try:
            # validate_dispatch accepts dogfood
            validated = validate_dispatch("dogfood", {
                "file": target_path,
                "instruction": "fix bug",
                "verify": "python -m unittest",
                "max_rounds": 2,
            })
            self.assertEqual(validated["file"], target_path)
            self.assertEqual(validated["instruction"], "fix bug")
            self.assertEqual(validated["max_rounds"], 2)

            # Nonexistent file fails closed
            with self.assertRaises(HarnessError):
                validate_dispatch("dogfood", {"file": "nonexistent_file_xyz.py", "instruction": "fix"})

            # RUNNERS contains dogfood
            self.assertIn("dogfood", RUNNERS)
        finally:
            if os.path.exists(target_path):
                os.unlink(target_path)

    def test_run_dogfood_ground_phase_rejection(self):
        # When lint fails (e.g. ungrounded claim), dogfood fails closed on ground phase
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
            tf.write(b"code")
            target_file = tf.name

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as cf:
            # Claim citing file that doesn't match source window
            claims_doc = {
                "claims": [
                    {
                        "claim_id": "c1",
                        "kind": "defect",
                        "text": "some defect assertion",
                        "source_refs": [999],
                    }
                ]
            }
            cf.write(json.dumps(claims_doc).encode("utf-8"))
            claims_path = cf.name

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as sf:
            sf.write(b"completely different source text\nline 2")
            source_path = sf.name

        try:
            cfg = _cfg(use_free=True)
            report = run_dogfood(
                cfg,
                file=target_file,
                instruction="fix defect",
                claims_file=claims_path,
                source_file=source_path)
            self.assertEqual(report["status"], "ungrounded")
            self.assertEqual(len(report["phases"]), 1)
            self.assertEqual(report["phases"][0]["phase"], "ground")
            self.assertEqual(report["phases"][0]["status"], "rejected")
        finally:
            for p in (target_file, claims_path, source_path):
                if os.path.exists(p):
                    os.unlink(p)

    def test_mcp_dogfood_requires_allow_write(self):
        server = McpServer(
            transport=MagicMock(),
            api_key="sk-test",
            governor=MagicMock(),
            ledger=MagicMock(),
            router=MagicMock(),
            engine=MagicMock(),
            allow_write=False,
            settings=_cfg(use_free=True),
        )
        with self.assertRaises(HarnessError) as ctx:
            server._invoke("dogfood", {
                "file": "some_file.py",
                "instruction": "fix something",
                "allow_write": False,
            })
        self.assertIn("allow_write", str(ctx.exception))

    def test_mcp_dogfood_validation_and_execution(self):
        server = McpServer(
            transport=MagicMock(),
            api_key="sk-test",
            governor=MagicMock(),
            ledger=MagicMock(),
            router=MagicMock(),
            engine=MagicMock(),
            allow_write=True,
            settings=_cfg(use_free=True),
        )
        with self.assertRaises(HarnessError):
            server._invoke("dogfood", {})
        with self.assertRaises(HarnessError):
            server._invoke("dogfood", {"file": "a.py"})

        mock_ret = {"status": "ok"}
        with patch("harness.mcp._service_run_dogfood", return_value=mock_ret) as mock_run:
            res = server._invoke("dogfood", {
                "file": "a.py",
                "instruction": "fix bug",
                "verify": "pytest",
                "max_cost": 0.05,
                "claims_file": "c.json",
                "source_file": "s.txt",
                "definitions_file": "d.json",
                "claim_context": "ctx",
                "max_rounds": 4,
            })
            self.assertEqual(res, mock_ret)
            self.assertTrue(mock_run.called)
            kwargs = mock_run.call_args[1]
            self.assertEqual(kwargs["file"], "a.py")
            self.assertEqual(kwargs["verify_cmd"], "pytest")
            self.assertEqual(kwargs["max_cost"], 0.05)
            self.assertEqual(kwargs["max_rounds"], 4)

    def test_server_dogfood_validation_and_runner(self):
        from harness.server import run_dogfood_task
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
            target_file = tf.name
        try:
            with self.assertRaises(HarnessError):
                validate_dispatch("dogfood", {"file": target_file, "instruction": "fix", "claims_file": "nonexistent.json"})
            with self.assertRaises(HarnessError):
                validate_dispatch("dogfood", {"file": target_file, "instruction": "fix", "source_file": "nonexistent.txt"})
            with self.assertRaises(HarnessError):
                validate_dispatch("dogfood", {"file": target_file, "instruction": "fix", "definitions_file": "nonexistent.json"})
            validated = validate_dispatch("dogfood", {"file": target_file, "instruction": "fix", "max_cost": 0.05})
            self.assertEqual(validated["max_cost"], 0.05)

            mock_ret = {"status": "ok"}
            with patch("harness.service.run_dogfood", return_value=mock_ret) as mock_svc:
                task_res = run_dogfood_task("task1", {"file": target_file, "instruction": "fix"}, lambda: False)
                self.assertEqual(task_res, mock_ret)
                self.assertTrue(mock_svc.called)
        finally:
            if os.path.exists(target_file):
                os.unlink(target_file)

    def test_config_save_settings_stages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("harness.config.CONFIG_DIR", tmpdir):
                update_config({"hourglass_stages": ["context", "planning"]})
                with self.assertRaises(HarnessError):
                    update_config({"hourglass_stages": 12345})

    def test_service_run_dogfood_edge_cases(self):
        from harness.claims import Claim
        from harness.service import ToolCancelled
        cfg = _cfg(use_free=True)
        with self.assertRaises(HarnessError):
            run_dogfood(cfg, file="a.py", instruction="fix")

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
            target_file = tf.name
        try:
            claim = Claim(claim_id="c1", kind="reassurance", text="ok", source_refs=[1])
            with self.assertRaises(ToolCancelled):
                run_dogfood(
                    cfg,
                    file=target_file,
                    instruction="fix",
                    claims=[claim],
                    source_text="source text\nline 2",
                    cancel_check=lambda: True)

            calls = 0
            def cancel_after_verify():
                nonlocal calls
                calls += 1
                return calls >= 2

            def mock_verify_saturated(*args, **kwargs):
                return {
                    "panel_failures": [{"status": "429 rate limit", "reason": "rate limited"}],
                    "convergence": {"tally": {"claims": {"c1": {"converged": True, "verdict": "real"}}}},
                }

            with self.assertRaises(ToolCancelled):
                run_dogfood(
                    cfg,
                    file=target_file,
                    instruction="fix",
                    claims=[claim],
                    source_text="source text\nline 2",
                    run_verify_fn=mock_verify_saturated,
                    cancel_check=cancel_after_verify)
        finally:
            if os.path.exists(target_file):
                os.unlink(target_file)


if __name__ == "__main__":
    unittest.main()
