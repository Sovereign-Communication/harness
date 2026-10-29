"""Focused, type-checked regressions for issue #293's historic edge paths."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from harness.agent import AutonomousAgent
from harness.config import load_settings
from harness.errors import HarnessError
from harness.history import (
    load_chat_checkpoint,
    save_chat_checkpoint,
    save_chat_turn,
)
from harness.simple_request import is_workspace_inspection_request


class HistoricEdgeCoverageTests(unittest.TestCase):
    def test_checkpoint_file_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = save_chat_checkpoint("session", {"prompt": "x"}, root)

            def is_target(candidate: Path) -> bool:
                return candidate == path

            with patch.object(
                    Path, "is_symlink", autospec=True, side_effect=is_target):
                with self.assertRaisesRegex(
                        HarnessError, "request lifecycle state is invalid"):
                    load_chat_checkpoint("session", root)

    def test_history_rejects_empty_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                    HarnessError, "invalid session_id for chat history"):
                save_chat_turn("", {"prompt": "x"}, Path(tmp))

    def test_empty_prompt_is_not_a_workspace_inspection_request(self) -> None:
        self.assertFalse(is_workspace_inspection_request(""))

    def test_workspace_intent_records_nonzero_route_preflight_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(
                settings=load_settings(), root_dir=root, history_dir=root)
            policy = MagicMock()
            policy.evaluate_request_workflow.return_value = (
                SimpleNamespace(
                    answers={"workflow_tier": {
                        "choice": "workspace-inspection",
                    }},
                    is_fallback=False,
                    cost=0.0,
                    model="jev-test",
                    reasons=[],
                ),
                {"cost": 0.0, "native": True},
            )
            policy.evaluate_answer.return_value = (
                SimpleNamespace(
                    answers={},
                    is_fallback=False,
                    fallback_reason=None,
                    verdict="pass",
                    supported=1.0,
                    reasons=[],
                    model="jev-test",
                ),
                {
                    "native": True,
                    "answer_sufficient": 0.99,
                    "iteration_required": False,
                    "cost": 0.0,
                },
            )
            governor = MagicMock()
            request_preflight: dict[str, Any] = {
                "source": "jev_request_workflow",
                "choice": "web-research",
                "native": True,
                "cost": 0.002,
            }

            with patch(
                    "harness.hourglass_lifecycle.policy_for",
                    return_value=policy), \
                 patch(
                    "harness.hourglass_lifecycle.governor_for",
                    return_value=(None, governor)), \
                 patch(
                    "harness.hourglass_lifecycle.ledger_for",
                    return_value=MagicMock()), \
                 patch.object(
                    agent, "_hourglass_answer_once",
                    return_value={
                        "model": "m1", "answer": "local review", "cost": 0.001,
                    }), \
                 patch.object(
                    agent, "_gather_web_context",
                    side_effect=AssertionError(
                        "workspace work must not search")), \
                 patch("harness.hourglass_lifecycle.save_chat_turn"):
                result = agent.run_hourglass_request(
                    "Can you audit this repo?", web=True,
                    request_preflight=request_preflight)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["workflow_tier"], "workspace-inspection")
            self.assertFalse(result["web_used"])
            governor.record_actual.assert_any_call(
                0.002, "chat-route-preflight", lane="answer")
