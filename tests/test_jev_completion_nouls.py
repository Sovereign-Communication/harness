"""JEV-P3-completion: artifact/goal nouls before the generative judge."""
import os
import tempfile
import unittest
from pathlib import Path

from harness import orchestrator as orch
from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


class _CompletionTransport:
    def __init__(self, present=0.95, achieved=0.9):
        self.present = present
        self.achieved = achieved
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, {
            "model": "jev-test",
            "answers": {
                "named_artifacts_present": {"type": "noul", "noul": self.present},
                "goal_achieved": {"type": "noul", "noul": self.achieved},
            },
            "usage": {"input_tokens": 20, "output_tokens": 2},
        }


class _CompletionFallbackEvaluator:
    api_key = None
    model = "jev-latest"

    def evaluate(self, state, questions):
        return JevEvaluationResult("pass", 0.0, 1.0, {}, ["fallback"],
                                   is_fallback=True, model=self.model)


def _unkeyed():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


class CompletionNoulsTests(unittest.TestCase):
    def test_missing_named_artifact_cannot_complete_code_owned(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "pkg").mkdir()
        (root / "pkg" / "types.py").write_text("class X: ...\n", encoding="utf-8")
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_completion_nouls(
            "Create pkg/types.py and pkg/schema.py",
            "- subtask t1 status=ok",
            named_artifacts=["pkg/types.py", "pkg/schema.py"],
            root_dir=root,
            site="completion")
        self.assertTrue(structural["cannot_complete"])
        self.assertIn("pkg/schema.py", structural["missing_artifacts"])
        self.assertEqual(result.answers["named_artifacts_present"], 0.0)
        self.assertEqual(structural["site"], "completion")

    def test_present_artifacts_unkeyed_do_not_force_cannot_complete(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "out.md").write_text("done\n", encoding="utf-8")
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_completion_nouls(
            "Write out.md", "state ok",
            named_artifacts=["out.md"], root_dir=root, site="completion")
        self.assertFalse(structural["cannot_complete"])
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["missing_artifacts"], [])

    def test_keyed_nouls_can_refuse_when_goal_not_achieved(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "out.md").write_text("done\n", encoding="utf-8")
        settings = load_settings({"jev_api_key": "jev-key"})
        transport = _CompletionTransport(present=0.95, achieved=0.2)
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.50)
        policy = policy_for(settings, transport=transport, governor=governor)
        result, structural = policy.evaluate_completion_nouls(
            "Write out.md", "partial state",
            named_artifacts=["out.md"], root_dir=root, site="completion")
        self.assertFalse(result.is_fallback)
        self.assertTrue(structural["cannot_complete"])
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(len(transport.calls), 1)

    def test_keyed_nouls_pass_when_both_affirmed(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "out.md").write_text("done\n", encoding="utf-8")
        settings = load_settings({"jev_api_key": "jev-key"})
        transport = _CompletionTransport(present=0.95, achieved=0.92)
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.50)
        policy = policy_for(settings, transport=transport, governor=governor,
                            ledger=AutonomyLedger(os.path.join(td.name, "led.jsonl")))
        result, structural = policy.evaluate_completion_nouls(
            "Write out.md", "all nodes ok",
            named_artifacts=["out.md"], root_dir=root, site="completion")
        self.assertEqual(result.verdict, "pass")
        self.assertFalse(structural["cannot_complete"])


class OrchestratorCompletionSeamTests(unittest.TestCase):
    def test_assess_completion_nouls_pure_missing_blocks_complete(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        verdict = orch.assess_completion_nouls(
            "ship feature.py and schema.py", "state",
            jev_policy=None,
            named_artifacts=["feature.py", "schema.py"],
            root_dir=root)
        self.assertTrue(verdict["cannot_complete"])
        self.assertIn("schema.py", verdict["missing_artifacts"])
        self.assertIn("MISSING", verdict["remaining"])

    def test_drive_missing_artifact_with_policy_skips_generative_judge(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                           "target_files": ["pkg/types.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}
        judge_calls = []
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, evaluator=_CompletionFallbackEvaluator())
        driven = orch.drive(
            goal="Create pkg/types.py",
            target_files=["pkg/types.py"],
            initial_plan=plan,
            root_dir=root,
            plan_round=lambda goal: plan,
            execute_plan=lambda current: {"n1": {"status": "ok"}},
            completion_chat=lambda prompt: (
                judge_calls.append(prompt) or '{"complete": true}'),
            emit=lambda *args, **kwargs: None,
            max_rounds=2,
            jev_policy=policy)
        self.assertFalse(driven["final_all_ok"])
        self.assertEqual(judge_calls, [])
        self.assertIn("pkg/types.py", driven["remaining_scope"])

    def test_drive_without_policy_keeps_existing_override_contract(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                           "target_files": ["a.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}
        driven = orch.drive(
            goal="edit a.py", target_files=[], initial_plan=plan,
            root_dir=".", plan_round=lambda goal: plan,
            execute_plan=lambda current: {"n1": {"status": "failed"}},
            completion_chat=lambda prompt: '{"complete": true}',
            emit=lambda *args, **kwargs: None)
        self.assertFalse(driven["final_all_ok"])
        self.assertIn("did not complete", driven["remaining_scope"])


if __name__ == "__main__":
    unittest.main()
