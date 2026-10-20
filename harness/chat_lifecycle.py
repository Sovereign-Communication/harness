"""Conversation answer and continuation lifecycle.

The mixin owns no shared mutable state. AutonomousAgent remains the runtime
composition root; this module owns the chat route's bounded answer transitions.
ChatCheckpoint's persisted shape and disk I/O are owned by harness.history.
"""

import re
from typing import Any, Dict, Optional

from .config import resolve_api_key
from .edit_lifecycle import EXECUTION_PHRASES
from .hourglass_lifecycle import HourglassLifecycleMixin
from .conversation_lifecycle import ConversationLifecycleMixin
from .request_intent import classify_prompt_intent
from .simple_answer_lifecycle import SimpleAnswerLifecycleMixin

from .errors import HarnessError
from .history import (
    load_chat_checkpoint,
    save_chat_turn,
)


class ChatLifecycleMixin(
    SimpleAnswerLifecycleMixin, HourglassLifecycleMixin, ConversationLifecycleMixin
):
    """Conversation lifecycle behavior supplied by AutonomousAgent."""

    def _escalation_authorized(
        self, prompt: str, marker_defer: bool, status: Optional[str]
    ) -> bool:
        """Gate chat-to-plan escalation on an explicit reason and authority."""
        lowered = (prompt or "").lower()
        explicit_directive = any(p in lowered for p in EXECUTION_PHRASES)
        intent = classify_prompt_intent(prompt)
        plan_trigger = status == "plan_required" and intent == "edit"
        trigger = bool(marker_defer or plan_trigger or explicit_directive)
        return bool(trigger and (self._auto_escalation_armed() or explicit_directive))

    def _auto_escalation_armed(self) -> bool:
        """Require the operator gate and a usable paid key for auto-escalation."""
        if not self.settings.allow_escalation:
            return False
        return bool(resolve_api_key())

    @staticmethod
    def _classify_prompt_intent(prompt: str) -> str:
        """Expose the single intent policy to chat routes."""
        return classify_prompt_intent(prompt)

    def _finish_chat_request(
        self, session_id: str, result: Dict[str, Any], *, candidate=None, resumable: bool = False
    ):
        """Persist chat terminal state through the shared lifecycle owner."""
        state = self.lifecycle.load(session_id)
        if state is None or state["lane"] != "chat":
            return None
        if state["status"] in ("complete", "deferred", "failed"):
            return state
        result_status = result.get("status")
        terminal = (
            "complete"
            if result_status in {"ok", "needs_iteration", "plan_required"}
            else "deferred"
            if result_status == "deferred"
            else "failed"
        )
        return self.lifecycle.finish(
            state,
            terminal,
            candidate=(candidate if candidate is not None else result.get("response")),
            result=result,
            resumable=bool(resumable and terminal == "deferred"),
        )

    @staticmethod
    def _continuation_suffix(prompt: str) -> Optional[str]:
        match = re.fullmatch(
            r"\s*(?:continue|resume|pick\s+up|keep\s+going|carry\s+on)"
            r"(?:\s+(?:with|by|and)\s+(.+?))?[.!]?\s*",
            str(prompt or ""),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        followup = (match.group(1) or "").strip()
        return followup

    @staticmethod
    def _is_continue_command(prompt: str) -> bool:
        return bool(
            re.fullmatch(
                r"\s*(?:continue|resume|pick\s+up|keep\s+going|carry\s+on)[.!]?\s*",
                str(prompt or ""),
                flags=re.IGNORECASE,
            )
        )

    def _workspace_context_deferred(self, prompt: str, session_id: str):
        checkpoint = None
        try:
            checkpoint = load_chat_checkpoint(session_id, self.history_dir)
        except HarnessError:
            pass
        if checkpoint is None:
            checkpoint = {
                "route": "hourglass",
                "status": "interrupted",
                "stage": "workspace_selection",
                "prompt": prompt,
                "intent": "conversation",
                "web_requested": False,
                "workspace_root": "",
                "workspace_context_used": True,
                "known_cost": 0.0,
                "unknown_liability": 0.0,
                "usage_unknown": False,
                "next_round": 1,
                "rounds": [],
            }
        else:
            checkpoint["status"] = "interrupted"
            checkpoint.setdefault("stage", "workspace_selection")
        self._persist_chat_checkpoint(session_id, checkpoint)
        result = {
            "status": "deferred",
            "intent": "conversation",
            "workflow_tier": "workspace-inspection",
            "prompt": prompt,
            "response": ("Select a workspace folder so I can inspect the repository you mean."),
            "model": None,
            "cost": 0.0,
            "defer_reason": "workspace context is not selected",
            "next_step": "select the repository folder and continue",
        }
        save_chat_turn(session_id, result, self.history_dir)
        return result

    def _live_jev_available(self) -> bool:
        return bool(
            getattr(self.settings, "jev_api_key", None)
            and not getattr(self.settings, "jev_disabled", False)
        )
