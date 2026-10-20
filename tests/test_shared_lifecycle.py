"""Behavior checks for the typed lifecycle across each task lane."""
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from harness.history import load_chat_checkpoint, save_chat_checkpoint
from harness.lifecycle import (
    LifecycleCoordinator,
    adapter_for,
    validate_lifecycle_state,
)
from harness import provision as pv
from harness.jev_policy import JevPolicy
from harness.ledger import AutonomyLedger
from harness.server import run_driver_task
from tests.test_provision import FakeRunner, fake_which


def _settings():
    settings = load_settings()
    settings.jev_api_key = None
    settings.jev_disabled = True
    settings.allow_escalation = False
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    return settings


class TypedLifecycleContractTests(unittest.TestCase):
    def test_parallel_effect_receipts_are_serialized_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = coordinator.begin("auto_edit", "parallel-edit", "edit files")
            worker_count = 12

            def record_effect(index):
                effect_id = f"edit:{index}"
                action, _ = coordinator.begin_effect(
                    state, effect_id, "edit.apply",
                    approval_state={"required": True})
                if action != "run":
                    return action
                coordinator.complete_effect(
                    state, effect_id, {"node": index, "status": "ok"})
                return "completed"

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                results = list(pool.map(record_effect, range(worker_count)))

            reloaded = coordinator.load("parallel-edit")
            self.assertEqual(results, ["completed"] * worker_count)
            self.assertEqual(
                set(reloaded["execution"]["effects"]),
                {f"edit:{index}" for index in range(worker_count)})
            self.assertTrue(all(
                effect["status"] == "completed"
                for effect in reloaded["execution"]["effects"].values()))

    def test_terminal_transition_settles_unknown_provider_usage_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "chat").begin(
                "usage-terminal", "answer the question")
            coordinator.provider_started(state, "answer-call", 0.025)
            coordinator.finish(state, "complete", candidate="answer")
            saved = coordinator.load("usage-terminal")
            self.assertEqual(saved["usage"]["provider_calls"][
                "answer-call"]["status"], "unknown")
            self.assertEqual(saved["usage"]["unknown_liability"], 0.025)

            bad = dict(saved)
            bad["usage"] = dict(saved["usage"])
            bad["usage"]["provider_calls"] = {
                "answer-call": dict(saved["usage"]["provider_calls"]["answer-call"])}
            bad["usage"]["provider_calls"]["answer-call"]["status"] = "dispatched"
            with self.assertRaises(HarnessError):
                validate_lifecycle_state(bad)

    def test_unknown_provider_liability_is_not_charged_again_at_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "audit").begin(
                "usage-unknown", "audit the ledger")
            coordinator.provider_started(state, "review-call", 0.01)
            coordinator.interrupt(
                state, ProviderUsageUnknown("usage response lost",
                                            reserved_cost=0.01),
                call_id="review-call")
            coordinator.finish(state, "complete", result={"status": "ok"})
            saved = coordinator.load("usage-unknown")
            self.assertEqual(saved["usage"]["unknown_liability"], 0.01)
            self.assertEqual(saved["usage"]["provider_calls"][
                "review-call"]["status"], "unknown")


class SharedLaneBehaviorTests(unittest.TestCase):
    def test_chat_checkpoint_merges_partial_updates_and_followup_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_chat_checkpoint("chat", {
                "route": "simple-answer", "status": "interrupted",
                "stage": "answer_review", "prompt": "Explain photosynthesis",
                "answer": "Keep this candidate.", "known_cost": 0.002,
                "unknown_liability": 0.004, "usage_unknown": True,
            }, root)
            # Incremental legacy writers may omit the original request.
            save_chat_checkpoint("chat", {
                "status": "interrupted", "stage": "answer_review",
                "post_skipped": "deadline exhausted",
            }, root)
            payload = load_chat_checkpoint("chat", root)
            self.assertEqual(payload["prompt"], "Explain photosynthesis")
            self.assertEqual(payload["answer"], "Keep this candidate.")
            self.assertEqual(payload["unknown_liability"], 0.004)

            coordinator = LifecycleCoordinator(history_dir=root)
            resumed = coordinator.resume("chat", followup="include the limits")
            self.assertEqual(resumed["lane"], "chat")
            self.assertEqual(resumed["usage"]["unknown_liability"], 0.004)
            self.assertEqual(
                coordinator.active_request(resumed),
                "Explain photosynthesis\n\nFollow-up instruction: include the limits")

    def test_audit_is_local_and_persists_a_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), history_dir=Path(tmp))
            ledger = MagicMock()
            ledger.verify.return_value = (True, None)
            ledger.chain_status.return_value = {"latest_seq": 4, "quarantined": 0}
            with patch("harness.task_lifecycle.ledger_for", return_value=ledger), \
                 patch("harness.web.gather_web_context",
                       side_effect=AssertionError("audit must stay local")):
                result = agent._handle_audit("audit this ledger", "audit-1")
            state = agent.lifecycle.load("audit-1")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(state["lane"], "audit")
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["data"]["result"], result)
            ledger.verify.assert_called_once_with()

    def test_auto_edit_continue_restores_the_preview_without_replanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_settings(), root_dir=root,
                                    history_dir=root)
            plan = {
                "status": "ok", "total_nodes": 1, "total_cost_ceiling": 0.01,
                "target_files": ["app.py"],
                "dag": {"nodes": [{"node_id": "edit-1"}]},
                "nodes": [{"node_id": "edit-1", "instruction": "review app.py",
                           "target_files": ["app.py"]}],
                "composition": {"stages": [{"stage": "execution"}]},
                "planning": {}, "stage_judgments": {},
            }
            policy = SimpleNamespace(evaluate_plan=lambda *a, **k: (
                SimpleNamespace(answers={}), {"cost": 0.0}))
            with patch.object(agent, "_triage_scope", return_value=["app.py"]), \
                 patch.object(agent, "_plan_round", return_value=plan) as planning, \
                 patch("harness.edit_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.edit_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.edit_lifecycle.policy_for", return_value=policy), \
                 patch("harness.edit_lifecycle.discover_verification_gate", return_value=None):
                preview = agent._handle_edit("review app.py", "edit-1", False)
                state = agent.lifecycle.load("edit-1")
                self.assertEqual(preview["status"], "preview_ready")
                self.assertEqual(state["lane"], "auto_edit")
                self.assertEqual(state["status"], "deferred")
                self.assertTrue(state["data"]["resumable"])
                continued = agent.run_prompt("continue", session_id="edit-1")
            self.assertEqual(continued, preview)
            planning.assert_called_once()

    def test_auto_edit_continue_reuses_completed_apply_and_consent_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_settings(), root_dir=root,
                                    history_dir=root)
            plan = {
                "status": "ok", "total_nodes": 1, "total_cost_ceiling": 0.01,
                "dag": {"nodes": [{
                    "node_id": "edit-1", "instruction": "set value to 2",
                    "target_files": ["app.py"], "dependencies": [],
                    "complexity_tier": 1,
                }]},
                "nodes": [{"node_id": "edit-1", "instruction": "set value to 2",
                           "target_files": ["app.py"], "local_gate": None}],
                "composition": {"stages": [{"stage": "execution",
                                               "max_input_tokens": None,
                                               "max_output_tokens": None}]},
                "planning": {}, "stage_judgments": {},
            }
            policy = SimpleNamespace(evaluate_plan=lambda *a, **k: (
                SimpleNamespace(answers={}), {"cost": 0.0}))
            engine = SimpleNamespace()
            engine.default_task_max_cost = 0.01
            engine.router = SimpleNamespace(apply_pool=["test-model"])
            engine.governor = SimpleNamespace(snapshot=lambda: {})
            engine.jev_policy = JevPolicy(_settings())
            calls = []

            def apply_edit(**kwargs):
                calls.append(kwargs)
                Path(kwargs["file_path"]).write_text("value = 2\n", encoding="utf-8")
                return {"status": "ok", "diff": "-value = 1\n+value = 2\n",
                        "content": "value = 2\n", "cost": 0.001,
                        "model_requested": "test-model",
                        "model_observed": ["test-model"]}

            engine.apply_edit = apply_edit
            budget = MagicMock()
            budget.stage.return_value = None
            budget.snapshot.return_value = {}
            drive_calls = {"count": 0}

            class FakePlanExecutor:
                def __init__(self, _engine, routes, *, apply, **kwargs):
                    self.routes, self.apply = routes, apply

                def execute(self, dag):
                    node = next(iter(dag.nodes.values()))
                    result = self.apply(
                        node.target_files[0], node, {}, None)
                    return {node.node_id: result}

            def fake_drive(**kwargs):
                drive_calls["count"] += 1
                applied = kwargs["execute_plan"](plan)
                if drive_calls["count"] == 1:
                    raise ToolCancelled("interrupted after the saved edit receipt")
                return {
                    "status": "ok", "all_results": applied,
                    "total_cost": 0.001, "rounds_history": [],
                    "final_all_ok": True, "remaining_scope": "",
                    "handoff": None, "plan": plan,
                }

            with patch.object(agent, "_triage_scope", return_value=["app.py"]), \
                 patch.object(agent, "_plan_round", return_value=plan), \
                 patch("harness.edit_lifecycle.governor_for", return_value=(None, MagicMock())), \
                 patch("harness.edit_lifecycle.ledger_for", return_value=MagicMock()), \
                 patch("harness.edit_lifecycle.policy_for", return_value=policy), \
                 patch("harness.edit_lifecycle.budget_from_settings", return_value=budget), \
                 patch("harness.edit_lifecycle.apply_session", return_value=engine), \
                 patch("harness.edit_lifecycle.PlanExecutor", FakePlanExecutor), \
                 patch("harness.edit_lifecycle.drive", side_effect=fake_drive), \
                 patch("harness.edit_lifecycle.discover_verification_gate", return_value=None):
                with self.assertRaises(ToolCancelled):
                    agent._handle_edit("update app.py", "edit-apply-1", True)
                state = agent.lifecycle.load("edit-apply-1")
                self.assertEqual(state["status"], "interrupted")
                self.assertEqual(state["execution"]["effects"][
                    "edit:" + state["request_id"] + ":edit-1:1"]["status"],
                    "completed")
                result = agent.run_prompt("continue", session_id="edit-apply-1")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]["require_consent"])

    def test_driver_reuses_completed_step_and_does_not_expand_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "driver").begin("driver-1", "inspect")
            driver = MagicMock()
            driver.step.return_value = {
                "step_id": "stable", "ok": True, "stopped_at": "execute",
                "reason": None, "cost_usd": 0.001,
                "decision": {"usable": True, "native": True,
                             "confidence": 0.95, "cost": 0.0},
            }
            driver.verify.return_value = {"ok": True}
            args = {"goal": "inspect", "max_steps": 3,
                    "request_id": state["request_id"]}
            with patch("harness.server._driver_adapter", return_value=driver):
                first = run_driver_task("driver-1", args, None,
                                        lifecycle=coordinator,
                                        lifecycle_state=state)
                coordinator.interrupt(state, ToolCancelled("resume after receipt"))
                resumed = coordinator.resume("driver-1")
                second = run_driver_task("driver-1", args, None,
                                         lifecycle=coordinator,
                                         lifecycle_state=resumed)
            self.assertEqual(first["status"], "done")
            self.assertEqual(second["steps"], first["steps"])
            self.assertEqual(driver.step.call_count, 1)
            self.assertIsNone(driver.step.call_args.kwargs["consent"])

    def test_unknown_edit_effect_stops_continue_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_settings(), root_dir=Path(tmp),
                                    history_dir=Path(tmp))
            state = adapter_for(agent.lifecycle, "auto_edit").begin(
                "edit-unknown", "update app.py")
            effect_id = "edit:" + state["request_id"] + ":edit-1:1"
            agent.lifecycle.begin_effect(
                state, effect_id, "auto_edit.apply",
                approval_state={"required": True})
            agent.lifecycle.unknown_effect(state, effect_id)
            agent.lifecycle.interrupt(
                state, ToolCancelled("provider disconnected after dispatch"))
            with patch.object(agent, "_handle_edit",
                              side_effect=AssertionError("must reconcile before replay")):
                result = agent.run_prompt("continue", session_id="edit-unknown")
            self.assertEqual(result["status"], "deferred")
            self.assertIn(effect_id, result["lifecycle"]["requires_reconciliation"])

    def test_provisioning_keeps_approval_and_verified_done_receipts(self):
        # Provisioning path validation rejects Windows 8.3 aliases. Keep the
        # approved project root under the checkout so it uses a canonical path.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp) / "approved"
            root.mkdir()
            probe = pv.HostProbe(
                "Linux", "x86_64", "3.12.1", "python", False, (), 10**9,
                (pv.PackageManager("pip", "/usr/bin/pip", "24.0", True),),
                str(root))
            policy = pv.policy_for_probe(probe, [str(root)])
            plan = pv.plan_provision("provision ruff==0.6.1", probe,
                                     policy=policy)
            gate = pv.ApprovalGate()
            gate.approve_plan(plan, "operator-test")
            ledger = AutonomyLedger(str(Path(tmp) / "provision-ledger.jsonl"))
            runner = FakeRunner()
            coordinator = LifecycleCoordinator(history_dir=Path(tmp))
            state = adapter_for(coordinator, "provisioning").begin(
                "provision-1", plan.goal, stage="approval",
                data={"plan": plan.to_dict(), "plan_digest": plan.digest})
            report = pv.execute_plan(
                plan, gate, policy=policy, ledger=ledger, dry_run=False,
                runner=runner, which=fake_which, lifecycle=coordinator,
                lifecycle_state=state)
            call_count = len(runner.calls)
            replay = pv.execute_plan(
                plan, gate, policy=policy, ledger=ledger, dry_run=False,
                runner=runner, which=fake_which, lifecycle=coordinator,
                lifecycle_state=state)
            self.assertEqual(report.outcome, pv.COMPLETED)
            self.assertEqual(replay.outcome, pv.COMPLETED)
            self.assertEqual(len(runner.calls), call_count)
            self.assertTrue(state["execution"]["approval_state"])
            self.assertTrue(all(
                item["status"] == "completed"
                for item in state["execution"]["effects"].values()))


if __name__ == "__main__":
    unittest.main()
