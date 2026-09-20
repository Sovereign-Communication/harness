"""Hermetic parity checks for the shared JEV-P1 lane envelope."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from harness.apply import ApplyEngine
from harness.batch import BatchOptions
from harness.config import load_settings
from harness.ledger import AutonomyLedger
from harness.router import Router
from harness.waist import compose_plan
from harness.jev_policy import policy_for
from tests._fake import FakeTransport, comp, m


ORIGINAL = "x = 1\n"
CHANGED = "x = 2\n"
MODEL = "test/model"


class LaneParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "x.py")
        with open(self.target, "w", encoding="utf-8") as stream:
            stream.write(ORIGINAL)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))

    def _engine(self, *, posts=None, policy=None):
        transport = FakeTransport(
            models=[m(MODEL), m("judge/model")], posts=posts)
        from harness.spend import SpendGovernor
        governor = SpendGovernor(transport, "sk-test")
        engine = ApplyEngine(
            transport, "sk-test", governor, self.ledger,
            Router([MODEL], "judge/model", MODEL),
            default_require_consent=False, default_renew_consent=False,
            jev_policy=policy,
        )
        return engine, transport

    def test_apply_envelope_contains_structural_for_unkeyed_policy(self):
        settings = load_settings()
        settings.jev_api_key = None
        from harness.jev_policy import policy_for
        engine, _ = self._engine(policy=policy_for(settings))
        engine.transport.posts = [comp(CHANGED)]
        result = engine.apply_edit(
            task_id="apply", file_path=self.target, instruction="change x",
            verify_cmd="true", require_consent=False,
            edit_snippet=CHANGED,
        )
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])

    def test_waist_envelope_contains_structural(self):
        settings = load_settings()
        settings.jev_api_key = None
        from harness.jev_policy import policy_for
        policy = policy_for(settings)
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="Update x.py", candidate_files=[self.target],
            execute=False, confirm=False, jev_policy=policy,
        )
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])

    def test_cli_plan_preview_exposes_structural_envelope(self):
        from harness.cli import _cmd_plan

        settings = load_settings()
        settings.jev_api_key = None
        settings.hourglass_confirm = False
        settings.frontier_model = None
        opts = SimpleNamespace(
            goal="Update x.py", file=[self.target], frontier_model=None,
            execute=False, out=None, decompose_llm=False,
            allow_escalation=False, max_tokens=None, max_cost=None,
        )
        with patch("harness.cli._emit") as emit:
            _cmd_plan(opts, settings)
        result = emit.call_args[0][0]
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        self.assertEqual(result["structural"]["site"], "cli")

    def test_mcp_plan_preview_exposes_structural_envelope(self):
        from harness.mcp import McpServer
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings)
        engine = MagicMock()
        engine.jev_policy = policy
        server = McpServer(
            transport=MagicMock(), api_key=None,
            governor=MagicMock(), ledger=self.ledger,
            router=MagicMock(judge="judge", panel_pool=[]), engine=engine,
        )
        result = server._invoke("plan_and_execute", {
            "goal": "Update x.py", "file": [self.target],
            "execute": False, "confirm": False,
        })
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        self.assertEqual(result["structural"]["site"], "mcp")

    def test_batch_aggregates_child_structural_envelopes(self):
        settings = load_settings()
        settings.jev_api_key = None
        from harness.jev_policy import policy_for
        engine, transport = self._engine(
            posts=[comp(CHANGED)], policy=policy_for(settings))
        result = engine.apply_batch(
            [self.target], options=BatchOptions(
                instruction="change x", verify_cmd="true",
                require_consent=False, edit_snippet=CHANGED))
        # A one-file batch intentionally returns the child terminal shape; the
        # child is the parity surface and carries the same stable block.
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])


if __name__ == "__main__":
    unittest.main()
