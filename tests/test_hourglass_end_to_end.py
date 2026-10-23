"""Hourglass Vision HV-6: End-to-end acceptance and surface flows.

Verifies end-to-end composition across all Hourglass stages:
- Partial-stage execution and supplied-artifact bypasses (brief, plan).
- Token budget narrowing, usage accounting, and spend preflight.
- Composed pipeline flows with hermetic fakes.
- Surface parity: CLI, MCP, and Server dispatch.
- Canonical dogfood loop (ground -> verify -> apply).
"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from harness.cli import _cmd_plan
from harness.cli_parser import build_parser
from harness.config import load_settings
from harness.mcp import McpServer
from harness.service import run_dogfood
from harness.token_budget import TokenBudget
from harness.waist import (
    STAGE_BYPASS_REASONS,
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VERIFICATION,
    STATE_COMPLETED,
    compose_arguments,
    compose_plan,
    compose_stages,
    resolve_stages,
)
from tests._fake import FakeTransport, _gov, m


def _cfg(**kw):
    return load_settings(overrides=kw) if kw else load_settings()


class PartialStageExecutionTests(unittest.TestCase):
    """Partial-stage composition, stage selection, and supplied-artifact bypasses."""

    def test_partial_stage_context_and_planning_only(self):
        tb = TokenBudget("run", max_input_tokens=60000, max_output_tokens=15000)
        resolved = resolve_stages(["context", "planning"])
        self.assertEqual(resolved["stages"], [STAGE_CONTEXT, STAGE_PLANNING])

        composed = compose_stages(budget=tb, declared=["context", "planning"])
        self.assertEqual(len(composed["stages"]), 2)
        self.assertEqual([s["stage"] for s in composed["stages"]], [STAGE_CONTEXT, STAGE_PLANNING])
        from harness.waist import composition_envelope
        env = composition_envelope(composed)
        self.assertEqual(env["skipped"], [STAGE_EXECUTION, STAGE_VERIFICATION])

    def test_supplied_brief_bypasses_context_stage(self):
        tb = TokenBudget("run", max_input_tokens=40000, max_output_tokens=10000)
        brief_data = {
            "version": "2.0.0",
            "goal": "fix edge case in parser",
            "grounding": {"sources": [{"path": "parser.py", "bytes": 50, "observed_at": 100.0}]},
            "scope": {"included": ["parser.py"], "excluded": []},
            "omitted": [],
            "conflicts": [],
            "coverage": {"sources_total": 1, "sources_represented": 1, "gaps": 0},
            "built_at": 100.0,
            "estimated_tokens": 120,
        }
        composed = compose_stages(
            budget=tb,
            supplied_brief=True,
            brief=brief_data,
            reader=lambda p: "data",
            freshness={"fresh": True, "drifted_sources": []},
        )
        self.assertIn(STAGE_CONTEXT, composed["bypassed"])
        self.assertEqual(
            composed["bypassed"][STAGE_CONTEXT],
            STAGE_BYPASS_REASONS[STAGE_CONTEXT],
        )
        # Context stage is not in active stages to run
        active = [s["stage"] for s in composed["stages"]]
        self.assertNotIn(STAGE_CONTEXT, active)
        self.assertIn(STAGE_PLANNING, active)

    def test_supplied_plan_bypasses_planning_stage(self):
        tb = TokenBudget("run", max_input_tokens=40000, max_output_tokens=10000)
        composed = compose_stages(
            budget=tb,
            supplied_plan=True,
        )
        self.assertIn(STAGE_PLANNING, composed["bypassed"])
        self.assertEqual(
            composed["bypassed"][STAGE_PLANNING],
            STAGE_BYPASS_REASONS[STAGE_PLANNING],
        )
        active = [s["stage"] for s in composed["stages"]]
        self.assertNotIn(STAGE_PLANNING, active)
        self.assertIn(STAGE_CONTEXT, active)
        self.assertIn(STAGE_EXECUTION, active)

    def test_supplied_both_brief_and_plan_bypasses_intake_and_planning(self):
        tb = TokenBudget("run", max_input_tokens=40000, max_output_tokens=10000)
        composed = compose_stages(
            budget=tb,
            supplied_brief=True,
            supplied_plan=True,
        )
        self.assertIn(STAGE_CONTEXT, composed["bypassed"])
        self.assertIn(STAGE_PLANNING, composed["bypassed"])
        active = [s["stage"] for s in composed["stages"]]
        self.assertEqual(active, [STAGE_EXECUTION, STAGE_VERIFICATION])


class ComposedPipelineAndBudgetTests(unittest.TestCase):
    """End-to-end composed pipeline runs through compose_plan with TokenBudget."""

    def test_compose_plan_with_token_budget_and_stage_states(self):
        transport = FakeTransport(models=[m("deepseek/deepseek-v4.1-flash")])
        gov = _gov(transport, max_cost=0.50)
        tb = TokenBudget("test_run", max_input_tokens=80000, max_output_tokens=20000)

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "app.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("def run():\n    return 42\n")

            plan = compose_plan(
                transport=transport,
                api_key="sk-test",
                governor=gov,
                ledger=None,
                opts_goal="improve app.py return value",
                stages=["context", "planning"],
                token_budget=tb,
                candidate_files=[file_path],
                root=tmpdir,
                use_free=True,
            )

            # Check envelope structure
            self.assertEqual(plan["status"], "planned")
            self.assertIn("composition", plan)
            self.assertIn("stage_states", plan)
            self.assertIn("token_budget", plan)

            # Composition stages match selected
            comp_stages = [s["stage"] for s in plan["composition"]["stages"]]
            self.assertEqual(comp_stages, [STAGE_CONTEXT, STAGE_PLANNING])
            self.assertEqual(plan["composition"]["skipped"], [STAGE_EXECUTION, STAGE_VERIFICATION])

            # Planning stage executed and marked completed
            self.assertEqual(plan["stage_states"][STAGE_PLANNING], STATE_COMPLETED)
            self.assertIn("planning", plan)
            self.assertIn(plan["planning"]["kind"], ("sufficient", "plan", "evidence_request", "defer"))

            # Token budget snapshot recorded
            snapshot = plan["token_budget"]
            self.assertEqual(snapshot["label"], "test_run")
            self.assertEqual(snapshot["max_input_tokens"], 80000)
            self.assertEqual(snapshot["max_output_tokens"], 20000)

    def test_compose_plan_refuses_when_composed_ceiling_exceeds_remaining(self):
        transport = FakeTransport(models=[m("deepseek/deepseek-v4.1-flash")])
        # Very tiny remaining budget
        gov = _gov(transport, max_cost=0.000001)
        tb = TokenBudget("refuse_run", max_input_tokens=10000, max_output_tokens=2000)

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "main.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("print('hello')\n")

            with patch("harness.waist.composed_worst_case") as mock_wc:
                mock_wc.return_value = {
                    "composed_worst_case": 0.15,
                    "remaining": 0.000001,
                    "exceeds_remaining": True,
                    "node_ceiling": 0.15,
                    "decompose": 0.0,
                    "waist": 0.0,
                    "consensus": 0.0,
                }
                plan = compose_plan(
                    transport=transport,
                    api_key="sk-test",
                    governor=gov,
                    ledger=None,
                    opts_goal="refuse over-budget execution",
                    stages=["context", "planning", "execution"],
                    token_budget=tb,
                    candidate_files=[file_path],
                    root=tmpdir,
                    execute=True,
                    use_free=False,
                )
                self.assertEqual(plan["status"], "refused")
                self.assertIn("exceeds remaining budget", plan["confirmation"]["reason"])


class SurfaceParityEndToEndTests(unittest.TestCase):
    """CLI and MCP surfaces produce identical composed run envelopes."""

    def test_cli_plan_stage_and_budget_envelope_parity(self):
        parser = build_parser()
        args = parser.parse_args([
            "plan",
            "--goal", "implement feature",
            "--stages", "context,planning",
            "--token-budget-input", "32000",
            "--token-budget-output", "8000",
        ])
        cfg = _cfg(use_free=True)
        composed_args = compose_arguments(
            cfg,
            goal=args.goal,
            files=[],
            stages=args.stages,
            max_input_tokens=args.token_budget_input,
            max_output_tokens=args.token_budget_output,
        )
        self.assertEqual(composed_args["stages"], [STAGE_CONTEXT, STAGE_PLANNING])
        self.assertEqual(composed_args["token_budget"].max_input_tokens, 32000)
        self.assertEqual(composed_args["token_budget"].max_output_tokens, 8000)

        # Mock compose_plan to verify _cmd_plan output packaging
        mock_plan_output = {
            "status": "planned",
            "goal": args.goal,
            "composition": {"stages": [{"stage": "context"}, {"stage": "planning"}]},
            "stage_states": {"context": "completed", "planning": "completed"},
            "token_budget": composed_args["token_budget"].snapshot(),
            "brief": {"version": "2.0.0"},
            "planning": {"kind": "sufficient"},
            "stage_judgments": {"planning": {"plan_sound": 0.95}},
        }
        with patch("harness.cli._governor", return_value=("sk-test", MagicMock())), \
             patch("harness.cli._compose_plan", return_value=mock_plan_output), \
             patch("harness.cli._emit") as mock_emit:
            _cmd_plan(args, cfg)
            self.assertTrue(mock_emit.called)
            res = mock_emit.call_args[0][0]
            self.assertEqual(res["status"], "planned")
            self.assertIn("composition", res)
            self.assertIn("token_budget", res)
            self.assertIn("brief", res)
            self.assertIn("stage_judgments", res)
            self.assertEqual(res["token_budget"]["max_input_tokens"], 32000)

    def test_mcp_plan_and_execute_stage_and_budget_parity(self):
        transport = FakeTransport()
        gov = _gov(transport)
        cfg = _cfg(use_free=True)
        server = McpServer(
            transport=transport,
            api_key="sk-test",
            governor=gov,
            ledger=MagicMock(),
            router=MagicMock(),
            engine=MagicMock(),
            allow_write=True,
            settings=cfg,
            hourglass={"parallel": False, "decompose": False, "confirm": False, "require_diff_authorization": False},
        )

        mock_plan_output = {
            "status": "planned",
            "goal": "mcp goal",
            "nodes": [],
            "total_nodes": 0,
            "batches": [],
            "dag": {"nodes": {}},
            "composition": {"stages": [{"stage": "context"}, {"stage": "planning"}]},
            "stage_states": {"context": "completed", "planning": "completed"},
            "token_budget": {"max_input_tokens": 45000, "max_output_tokens": 9000},
            "brief": {"version": "2.0.0"},
            "planning": {"kind": "sufficient"},
            "stage_judgments": {"planning": {"plan_sound": 0.98}},
        }

        with patch("harness.mcp.compose_plan", return_value=mock_plan_output):
            result = server._invoke("plan_and_execute", {
                "goal": "mcp goal",
                "stages": "context,planning",
                "token_budget_input": 45000,
                "token_budget_output": 9000,
            })
            self.assertEqual(result["status"], "planned")
            self.assertIn("composition", result)
            self.assertIn("token_budget", result)
            self.assertIn("brief", result)
            self.assertIn("stage_judgments", result)
            self.assertEqual(result["token_budget"]["max_input_tokens"], 45000)


class DogfoodEndToEndTests(unittest.TestCase):
    """Dogfood service layer end-to-end execution across ground, verify, and apply."""

    def test_run_dogfood_full_three_phase_success(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
            tf.write(b"def compute():\n    return False\n")
            target_file = tf.name

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as cf:
            claims_doc = {
                "claims": [
                    {
                        "claim_id": "c1",
                        "kind": "defect",
                        "text": "return False instead of True",
                        "source_refs": [2],
                    }
                ]
            }
            cf.write(json.dumps(claims_doc).encode("utf-8"))
            claims_path = cf.name

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as sf:
            sf.write(b"def compute():\n    return False\n")
            source_path = sf.name

        try:
            cfg = _cfg(use_free=True)
            mock_verify_verdict = {
                "status": "ok",
                "convergence": {
                    "tally": {
                        "claims": {
                            "c1": {"converged": True, "verdict": "real"}
                        }
                    }
                },
                "panel_failures": 0,
            }
            mock_engine = MagicMock()
            mock_engine.apply_batch.return_value = {"status": "ok", "cost": 0.001}

            with patch("harness.service.run_verify", return_value=mock_verify_verdict):
                with patch("harness.service.apply_session", return_value=mock_engine):
                    report = run_dogfood(
                        cfg,
                        file=target_file,
                        instruction="fix compute return value",
                        verify_cmd="python -c 'import sys; sys.exit(0)'",
                        claims_file=claims_path,
                        source_file=source_path,
                    )
                    self.assertEqual(report["status"], "ok")
                    self.assertEqual(len(report["phases"]), 3)
                    self.assertEqual(report["phases"][0]["phase"], "ground")
                    self.assertEqual(report["phases"][0]["status"], "ok")
                    self.assertEqual(report["phases"][1]["phase"], "verify")
                    self.assertEqual(report["phases"][1]["confirmed_claims"], ["c1"])
                    self.assertEqual(report["phases"][2]["phase"], "apply")
                    self.assertEqual(report["phases"][2]["status"], "ok")
        finally:
            for p in (target_file, claims_path, source_path):
                if os.path.exists(p):
                    os.unlink(p)

    def test_run_dogfood_halts_when_no_defects_confirmed(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
            tf.write(b"def valid():\n    return True\n")
            target_file = tf.name

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as cf:
            claims_doc = {
                "claims": [
                    {
                        "claim_id": "c1",
                        "kind": "defect",
                        "text": "false alarm defect",
                        "source_refs": [1],
                    }
                ]
            }
            cf.write(json.dumps(claims_doc).encode("utf-8"))
            claims_path = cf.name

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as sf:
            sf.write(b"def valid():\n    return True\n")
            source_path = sf.name

        try:
            cfg = _cfg(use_free=True)
            mock_verify_verdict = {
                "status": "ok",
                "convergence": {
                    "tally": {
                        "claims": {
                            "c1": {"converged": True, "verdict": "false_alarm"}
                        }
                    }
                },
                "panel_failures": 0,
            }

            with patch("harness.service.run_verify", return_value=mock_verify_verdict):
                report = run_dogfood(
                    cfg,
                    file=target_file,
                    instruction="fix defect",
                    claims_file=claims_path,
                    source_file=source_path,
                )
                self.assertEqual(report["status"], "not_confirmed")
                self.assertEqual(len(report["phases"]), 2)
                self.assertEqual(report["phases"][0]["phase"], "ground")
                self.assertEqual(report["phases"][1]["phase"], "verify")
        finally:
            for p in (target_file, claims_path, source_path):
                if os.path.exists(p):
                    os.unlink(p)


if __name__ == "__main__":
    unittest.main()
