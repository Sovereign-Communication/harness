"""Prompt routing and explicit continuation dispatch."""

from typing import Any, Callable, Dict, Optional

from .conversation_context import load_memory, sole_site
from .errors import HarnessError, ToolCancelled
from .events import emit
from .history import load_chat_checkpoint, save_chat_turn
from .orchestrator import keyword_fallback, triage_files
from .repo_scope import discover_target_files, enumerate_repo_files
from .request_intent import classify_prompt_intent, is_literal_greeting_request
from .session import governor_for
from .simple_request import (
    is_simple_answer_request,
    is_workspace_inspection_request,
    missing_site_target,
)


class RequestRoutingMixin:
    """Dispatch natural-language requests to their owning lane."""

    def _triage_scope(self, prompt: str) -> list[str]:
        """Resolve a repository request to its explicit or relevant files."""
        explicit = discover_target_files(prompt, self.root_dir)
        if explicit:
            return explicit
        repo_files = enumerate_repo_files(self.root_dir)
        if not repo_files:
            return []
        try:
            gov = governor_for(self.settings)[1]
            picked = triage_files(prompt, repo_files, self._orchestrator_chat_fn(gov))
        except HarnessError:
            picked = []
        if not picked:
            picked = keyword_fallback(prompt, repo_files)
        if picked:
            emit("files_discovered", target_files=picked, triage="orchestrated")
        return picked

    def run_prompt(
        self,
        prompt: str,
        auto_apply: bool = False,
        session_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        force_conversation: bool = False,
        web: bool = False,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        token_budget: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # Process a natural language prompt from intent to verified conclusion.
        # When force_conversation=True (e.g. UI chat box), keep normal chat in
        # the conversational lane while still routing local-workspace and
        # explicit action requests to their owning capabilities.
        if not prompt or not prompt.strip():
            raise HarnessError("Prompt cannot be empty")

        sid = session_id or "default"
        # A site audit with no identifiable target cannot be made safe by a
        # web search or a model guess.  Reconcile before spending a turn; a
        # remembered site is a target, so "audit that site again" proceeds.
        known_site = sole_site(load_memory(self.history_dir), session_id=sid)
        missing = missing_site_target(prompt, known_site=known_site)
        if missing:
            result = {
                "status": "deferred", "intent": "conversation",
                "workflow_tier": "clarification", "prompt": prompt,
                "response": missing, "model": None, "cost": 0.0,
                "defer_reason": "missing site target",
                "next_step": "provide the site URL or domain",
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("clarification_required", reason_code="missing_site_target")
            return result
        followup = self._continuation_suffix(prompt)
        if followup is not None:
            try:
                current = self.lifecycle.load(sid)
                if current is None:
                    # Migrate pre-contract chat checkpoints through their
                    # compatibility view before the coordinator resumes them.
                    legacy = load_chat_checkpoint(sid, self.history_dir)
                    if legacy is not None:
                        self.lifecycle.adopt_chat_payload(sid, legacy)
                resumed = self.lifecycle.resume(sid, followup=followup or None)
            except HarnessError:
                emit(
                    "lifecycle_resume_unavailable", session_id=sid, reason_code="checkpoint_invalid"
                )
                return {
                    "status": "deferred",
                    "intent": "conversation",
                    "workflow_tier": "continuation",
                    "prompt": prompt,
                    "response": (
                        "The saved continuation state is unreadable. "
                        "Paste or restate the task to start again."
                    ),
                    "model": None,
                    "cost": 0.0,
                    "defer_reason": "invalid chat checkpoint",
                    "next_step": "restate the original task",
                }
            if resumed is not None:
                emit(
                    "chat_resume",
                    session_id=sid,
                    lane=resumed["lane"],
                    route=resumed["data"].get("route"),
                    stage=resumed["stage"],
                )
                if resumed["status"] == "deferred":
                    reason = resumed["data"].get(
                        "defer_reason", "an interrupted side effect needs reconciliation"
                    )
                    return {
                        "status": "deferred",
                        "intent": resumed["lane"],
                        "workflow_tier": "continuation",
                        "prompt": self.lifecycle.active_request(resumed),
                        "response": (
                            "I restored the request state, but will not "
                            "replay an operation whose completion is unknown. "
                            "Reconcile the saved operation before continuing."
                        ),
                        "model": None,
                        "cost": resumed["usage"]["known_cost"],
                        "defer_reason": reason,
                        "lifecycle": {
                            "request_id": resumed["request_id"],
                            "lane": resumed["lane"],
                            "stage": resumed["stage"],
                            "unknown_liability": resumed["usage"]["unknown_liability"],
                            "requires_reconciliation": list(
                                resumed["execution"]["requires_reconciliation"]
                            ),
                        },
                    }
                prompt = self.lifecycle.active_request(resumed)
                lane = resumed["lane"]
                if lane == "chat":
                    resume_state = self.lifecycle.chat_payload(sid) or {}
                    if resume_state.get("route") == "hourglass":
                        return self.run_hourglass_request(
                            prompt,
                            session_id=sid,
                            cancel_check=cancel_check,
                            web=bool(resume_state.get("web_requested")),
                            max_tokens=max_tokens,
                            reasoning_effort=reasoning_effort,
                            token_budget=token_budget,
                            resume_state=resume_state,
                        )
                    if resume_state.get("route") == "simple-answer":
                        return self.run_simple_request(
                            prompt,
                            session_id=sid,
                            cancel_check=cancel_check,
                            max_tokens=max_tokens,
                            reasoning_effort=reasoning_effort,
                            token_budget=token_budget,
                            resume_state=resume_state,
                        )
                    if resume_state.get("route") == "conversation":
                        return self._run_fallback_conversation(
                            prompt,
                            sid,
                            cancel_check=cancel_check,
                            web=bool(resume_state.get("web_requested")),
                            max_tokens=max_tokens,
                            reasoning_effort=reasoning_effort,
                            token_budget=token_budget,
                            lifecycle_state=resumed,
                        )
                elif lane == "audit":
                    return self._handle_audit(
                        prompt, sid, cancel_check=cancel_check, lifecycle_state=resumed
                    )
                elif lane == "provisioning":
                    return self._handle_provision(
                        prompt, sid, cancel_check=cancel_check, lifecycle_state=resumed
                    )
                elif lane == "driver":
                    return self._handle_driver_task(
                        prompt, sid, cancel_check=cancel_check, lifecycle_state=resumed
                    )
                elif lane == "auto_edit":
                    return self._handle_edit(
                        prompt,
                        sid,
                        bool(resumed["data"].get("auto_apply")),
                        cancel_check,
                        use_jev_completion=self._live_jev_available(),
                        lifecycle_state=resumed,
                    )
            emit("chat_resume_unavailable", session_id=sid)
            return {
                "status": "deferred",
                "intent": "conversation",
                "workflow_tier": "continuation",
                "prompt": prompt,
                "response": (
                    "There is no interrupted request saved for this "
                    "chat. Paste or restate the task to continue."
                ),
                "model": None,
                "cost": 0.0,
                "defer_reason": "no resumable chat checkpoint",
                "next_step": "restate the original task",
            }
        else:
            # A new request supersedes the session lifecycle; only an explicit
            # continuation carries request state forward.
            self._clear_chat_checkpoint(sid)
        emit("chat_turn_start", prompt=prompt, session_id=sid)

        if is_literal_greeting_request(prompt):
            result = {
                "status": "ok",
                "intent": "conversation",
                "workflow_tier": "answer",
                "prompt": prompt,
                "answer": "Hello.",
                "response": "Hello.",
                "model": None,
                "cost": 0.0,
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("chat_quick_reply", reason_code="literal_greeting")
            return result

        if force_conversation:
            # UI chat is conversational by default -- but an edit-intent
            # prompt (mutation verbs / explicit files) in Auto mode is repo
            # work: it drives the orchestrator loop instead of the chat model
            # narrating code it will never write. Everything else keeps the
            # conversation lane (session history still loads); read-only
            # workspace reviews take the local inspection lane below.
            intent = classify_prompt_intent(prompt)
            emit("intent_classified", intent=intent, prompt=prompt)
            needs_workspace = intent == "provision" or (intent == "edit" and auto_apply)
            if needs_workspace and self.root_dir is None:
                raise HarnessError(
                    "Select a workspace folder before requesting file or provisioning work"
                )
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            if is_workspace_inspection_request(prompt):
                if self.root_dir is None:
                    return self._workspace_context_deferred(prompt, sid)
                return self.run_hourglass_request(
                    prompt,
                    session_id=sid,
                    cancel_check=cancel_check,
                    web=False,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    token_budget=token_budget,
                )
            if intent == "driver":
                return self._handle_driver_task(prompt, sid, cancel_check=cancel_check)
            if intent == "audit":
                return self._handle_audit(prompt, sid, cancel_check=cancel_check)
            if intent == "provision":
                return self._handle_provision(prompt, sid, cancel_check=cancel_check)
            if intent == "simple-action":
                return self._run_simple_action_lifecycle(
                    prompt, session_id=sid, cancel_check=cancel_check
                )
            if intent == "edit" and auto_apply:
                emit("chat_escalated", reason="edit-intent in auto mode", target="plan_lane")
                return self._handle_edit(
                    prompt,
                    sid,
                    auto_apply,
                    cancel_check,
                    use_jev_completion=self._live_jev_available(),
                )
            simple_preflight = None
            if is_simple_answer_request(prompt, intent=intent):
                res = self.run_simple_request(
                    prompt,
                    session_id=sid,
                    cancel_check=cancel_check,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    token_budget=token_budget,
                )
                if res.get("status") == "ok" or (
                    res.get("status") == "deferred"
                    and res.get("simple_request", {}).get("generation_calls", 0) > 0
                ):
                    return res
                pre = res.get("simple_request", {}).get("pre")
                if isinstance(pre, dict):
                    simple_preflight = {
                        "source": "simple_strategy",
                        "choice": pre.get("strategy"),
                        "native": bool(pre.get("native")),
                        "cost": float(pre.get("cost") or 0.0),
                    }
                # Pre-generation deferred simple-answer request falls through
                # to the conversational lane with session history loaded.
            if intent == "conversation" and self._live_jev_available():
                return self.run_hourglass_request(
                    prompt,
                    session_id=sid,
                    cancel_check=cancel_check,
                    web=web,
                    request_preflight=simple_preflight,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    token_budget=token_budget,
                )
            return self._run_fallback_conversation(
                prompt,
                sid,
                cancel_check=cancel_check,
                web=web,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                token_budget=token_budget,
            )

        intent = classify_prompt_intent(prompt)
        emit("intent_classified", intent=intent, prompt=prompt)
        if intent in ("edit", "provision") and self.root_dir is None:
            raise HarnessError(
                "Select a workspace folder before requesting file or provisioning work"
            )

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        if is_workspace_inspection_request(prompt):
            if self.root_dir is None:
                return self._workspace_context_deferred(prompt, sid)
            return self.run_hourglass_request(
                prompt,
                session_id=sid,
                cancel_check=cancel_check,
                web=False,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                token_budget=token_budget,
            )

        if intent == "driver":
            return self._handle_driver_task(prompt, sid, cancel_check=cancel_check)
        elif intent == "simple-action":
            return self._run_simple_action_lifecycle(
                prompt, session_id=sid, cancel_check=cancel_check
            )
        elif intent == "provision":
            return self._handle_provision(prompt, sid, cancel_check=cancel_check)
        elif intent == "conversation":
            simple_preflight = None
            if is_simple_answer_request(prompt, intent=intent):
                res = self.run_simple_request(
                    prompt,
                    session_id=sid,
                    cancel_check=cancel_check,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    token_budget=token_budget,
                )
                if res.get("status") == "ok" or (
                    res.get("status") == "deferred"
                    and res.get("simple_request", {}).get("generation_calls", 0) > 0
                ):
                    return res
                pre = res.get("simple_request", {}).get("pre")
                if isinstance(pre, dict):
                    simple_preflight = {
                        "source": "simple_strategy",
                        "choice": pre.get("strategy"),
                        "native": bool(pre.get("native")),
                        "cost": float(pre.get("cost") or 0.0),
                    }
                # A pre-generation deferred request falls through to its
                # conversation lane without changing the caller's capability
                # flags. The request-specific route is still visible to Jev.
            if self._live_jev_available():
                return self.run_hourglass_request(
                    prompt,
                    session_id=sid,
                    cancel_check=cancel_check,
                    web=web,
                    request_preflight=simple_preflight,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    token_budget=token_budget,
                )
            return self._run_fallback_conversation(
                prompt,
                sid,
                cancel_check=cancel_check,
                web=web,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                token_budget=token_budget,
            )
        elif intent == "audit":
            return self._handle_audit(prompt, sid, cancel_check)
        else:
            return self._handle_edit(
                prompt,
                sid,
                auto_apply,
                cancel_check,
                use_jev_completion=(auto_apply and self._live_jev_available()),
            )
