"""Conversation answer and continuation lifecycle.

The mixin owns no shared mutable state. AutonomousAgent remains the runtime
composition root; this module owns the chat route's bounded answer transitions.
ChatCheckpoint's persisted shape and disk I/O are owned by harness.history.
"""
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .chat import (
    assess_output,
    chat,
    extract_content_and_cost,
    governed_completion,
    looks_truncated,
)
from .condenser import distill_context
from .config import Settings, effective_lane_policy, resolve_api_key, resolve_hourglass
from .errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from .events import cancellation_context, emit
from .history import (
    ChatCheckpoint,
    clear_chat_checkpoint,
    load_chat_checkpoint,
    load_chat_history,
    save_chat_checkpoint,
    save_chat_turn,
)
from .jev_policy import jev_cost_ceiling, policy_for
from .orchestrator import keyword_fallback
from .prompts import CAPABILITY_MARKER
from .provider_errors import ProviderSpendLimitError
from .results import _http_error
from .repo_scope import (
    discover_target_files,
    enumerate_repo_files,
    workspace_review_candidates,
)
from .session import governor_for, ledger_for
from .simple_request import (
    answer_clock as simple_clock_answer,
    answer_prompt as simple_answer_prompt,
    is_comprehensive_workspace_inspection_request,
    is_current_information_request,
    is_simple_answer_request,
    is_workspace_inspection_request,
    relevant_history as simple_relevant_history,
)
from .token_budget import resolve_lane_max_tokens as _resolve_lane_max_tokens
from .web import DEFAULT_FETCH_HOSTS, gather_web_context
from .workspace import resolve_workspace_path


TRUNCATION_NOTICE = (
    "\n\n[Note: This response reached the maximum output token limit and was truncated.]"
)

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are Sovereign Harness, an autonomous, cost-bounded agentic software engineering AI "
    "running directly on the local host hardware. You ARE the agent on this machine with direct "
    "access to local compute, the repository workspace, execution engines, git worktrees, and safe verification gates. "
    "Be concise, clear, and direct. When answering technical questions, explain precisely "
    "and provide code snippets when helpful. Focus on correctness, zero bloat, and safety. "
    "Your exact capability for this run is stated in the capability note below, if any. "
    "Never claim you searched, checked a source, or verified current information unless "
    "web tool results for this turn are attached -- say plainly when you cannot know "
    "something. When asked to execute a plan, run tests, or see if something works, remember you "
    "are on the hardware: explain how to execute or verify it safely, or let Harness's plan/apply "
    "engine execute the verified changes. Never falsely claim 'I have no compute or shell' — you are "
    "the agent on the hardware. Deferral is a first-class outcome in this system: when a request exceeds "
    "what you can honestly do in this conversation lane (large or multi-step repository "
    "work, long autonomous tasks, anything needing tools this lane does not have), do "
    "NOT pretend, guess, or merely say 'I can't' -- end with a single line beginning "
    "exactly 'HARNESS_DEFER: ' followed by a short reason. Handing the decision back "
    "with a reason is the correct, respected move; overpromising is not. You have no "
    "tool-call syntax in this lane: never emit <tool_call>, <tool>, or function-call "
    "markup -- web evidence is pre-gathered into this prompt by the runtime; if you "
    "need a tool you do not have, defer."
)

_NO_WEB_DISCLOSURE = (
    "[Capability note] This run has NO web tools: no search and no fetch are "
    "attached, and you have NO internet access. If the user asks you to search "
    "or verify online, say you cannot in this run and answer from training "
    "knowledge, labeled as such."
)

_WEB_CAPABILITY_NOTE = (
    "[Capability note] Web tools ARE attached this run: one web search, plus "
    "page fetch restricted to exactly these hosts: {hosts}. Every other host "
    "is refused by policy -- say so when asked about it rather than claiming "
    "no internet access. Results for this turn follow the marker below; cite "
    "only those, never invent others, and when a source FAILED, tell the user "
    "exactly what failed. These sources were gathered by the runtime before "
    "this turn -- do not narrate tool calls or describe fetching as your own "
    "action."
)

_MAX_WEB_SOURCES = 3

_MAX_WEB_CONTEXT_CHARS = 6000

_MAX_HOURGLASS_CONTEXT_FILES = 8

_MAX_HOURGLASS_CONTEXT_CHARS = 6000

_MAX_WORKSPACE_REVIEW_FILES = 20

_MAX_WORKSPACE_REVIEW_TOKENS = 4800

_MAX_WORKSPACE_REVIEW_CHARS = 18000

_DEFAULT_HOURGLASS_CONFIDENCE = 0.70

_MAX_HOURGLASS_ANSWER_ROUNDS = 3

DEFAULT_RUN_WALL_SECONDS = 120.0

_RESEARCH_SUBJECTS = ("news", "research", "recent", "latest", "today",
                      "papers", "paper", "article", "articles", "study",
                      "studies", "release", "releases", "update", "updates")

_RESEARCH_VERBS = ("find", "search", "look up", "lookup", "google",
                   "what's new", "whats new", "tell me about", "catch me up",
                   "summary of", "summarize", "summarise")

_QUESTION_OPENERS = ("what", "who", "when", "where", "which", "why", "how",
                     "is ", "are ", "does ", "do ", "did ", "can you",
                     "could you", "any news", "has there")

def is_research_question(prompt: str) -> bool:
    """True when a prompt is asking what is currently true in the world.

    A research *subject* is required, and that is what keeps edits out:
    "find the bug in auth.py" has a verb but no subject, so it stays an edit.
    On top of that the prompt must read as a lookup -- an explicit research
    verb, or an interrogative opening -- so "update the README" mentions a
    subject but asks for nothing.
    """
    cleaned = (prompt or "").strip().lower()
    if not cleaned:
        return False
    if not any(subj in cleaned for subj in _RESEARCH_SUBJECTS):
        return False
    if any(v in cleaned for v in _RESEARCH_VERBS):
        return True
    if cleaned.endswith("?"):
        return True
    return cleaned.startswith(_QUESTION_OPENERS)


class ChatLifecycleMixin:
    """Conversation lifecycle behavior supplied by AutonomousAgent."""

    @staticmethod
    def _is_continue_command(prompt: str) -> bool:
        return bool(re.fullmatch(
            r"\s*(?:continue|resume|pick\s+up|keep\s+going|carry\s+on)[.!]?\s*",
            str(prompt or ""), flags=re.IGNORECASE))

    def _workspace_context_deferred(self, prompt: str, session_id: str):
        checkpoint = None
        try:
            checkpoint = load_chat_checkpoint(session_id, self.history_dir)
        except HarnessError:
            pass
        if checkpoint is None:
            checkpoint = {
                "route": "hourglass", "status": "interrupted",
                "stage": "workspace_selection", "prompt": prompt,
                "intent": "conversation", "web_requested": False,
                "workspace_root": "", "workspace_context_used": True,
                "known_cost": 0.0, "unknown_liability": 0.0,
                "usage_unknown": False, "next_round": 1, "rounds": [],
            }
        else:
            checkpoint["status"] = "interrupted"
            checkpoint.setdefault("stage", "workspace_selection")
        self._persist_chat_checkpoint(session_id, checkpoint)
        result = {
            "status": "deferred", "intent": "conversation",
            "workflow_tier": "workspace-inspection", "prompt": prompt,
            "response": ("Select a workspace folder so I can inspect the "
                         "repository you mean."),
            "model": None, "cost": 0.0,
            "defer_reason": "workspace context is not selected",
            "next_step": "select the repository folder and continue",
        }
        save_chat_turn(session_id, result, self.history_dir)
        return result

    def _live_jev_available(self) -> bool:
        return bool(getattr(self.settings, "jev_api_key", None)
                    and not getattr(self.settings, "jev_disabled", False))

    def run_simple_request(
            self, prompt: str, *, session_id: Optional[str] = None,
            cancel_check=None, max_tokens: Optional[int] = None,
            reasoning_effort: Optional[str] = None,
            token_budget: Optional[Any] = None,
            max_wall_seconds: float = 60.0,
            resume_state: Optional[ChatCheckpoint] = None) -> Dict[str, Any]:
        """Answer one stable no-tool request with one governed completion.

        Deterministic request-kind checks own the boundary.  Jev supplies one
        pre-answer Choice and one post-answer sufficiency assessment when
        available; either Jev outage degrades honestly without adding a model
        judge or turning the request into repo work.
        """
        sid = session_id or "default"
        started = time.monotonic()
        deadline = started + max(0.0, float(max_wall_seconds))
        ledger = ledger_for(self.settings, caller="agent")
        generation_attempts = 0
        pre_cost = 0.0
        generation_cost = 0.0
        post_cost = 0.0
        answer = None
        model = None
        completion = None
        checkpoint = dict(resume_state or {})
        resuming = bool(resume_state)
        known_cost_total = max(
            0.0, float(checkpoint.get("known_cost") or 0.0)) if resuming else 0.0
        if resuming:
            pre = checkpoint.get("pre")
            pre_cost = max(0.0, float((pre or {}).get("cost") or 0.0))
            answer = checkpoint.get("answer")
            model = checkpoint.get("answer_model")
            generation_cost = max(
                0.0, float(checkpoint.get("answer_cost") or 0.0))
            generation_attempts = int(checkpoint.get("generation_calls") or 0)
        else:
            pre = None

        def combined_cancel():
            return bool((cancel_check and cancel_check())
                        or time.monotonic() >= deadline)

        def cancelled_or_late(stage: str):
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            if time.monotonic() >= deadline:
                raise HarnessError(
                    f"simple request deadline exhausted before {stage}")

        def receipt(status: str, **fields):
            return ledger.append(
                "simple_request_result", task_id=sid, status=status,
                workflow_tier="simple-answer", elapsed_ms=round(
                    (time.monotonic() - started) * 1000), **fields)

        emit("simple_request_start", prompt=prompt, session_id=sid)
        try:
            # Public callers cannot bypass the deterministic boundary by
            # invoking this method directly.  Jev Choice is advisory routing
            # evidence; it never grants file, tool, execution, or write scope.
            intent = self._classify_prompt_intent(prompt)
            if not is_simple_answer_request(prompt, intent=intent):
                reason = "request is outside the stable no-tool answer lane"
                result = {
                    "status": "deferred", "intent": intent,
                    "workflow_tier": "simple-answer", "prompt": prompt,
                    "response": reason.capitalize() + ".",
                    "model": None, "cost": 0.0,
                    "defer_reason": reason,
                    "next_step": "use the existing specialized or clarification lane",
                    "simple_request": {"generation_calls": 0},
                }
                receipt("deferred", reason=reason, generation_calls=0,
                        input_tokens=0, output_tokens=0)
                save_chat_turn(sid, result, self.history_dir)
                return result
            # Clock questions are answered from the machine clock with zero
            # model or Jev calls -- no lookup exists to run.  The answer
            # names its source so it never poses as verified world time.
            clock_answer = simple_clock_answer(prompt)
            if clock_answer is not None:
                result = {
                    "status": "ok", "intent": intent,
                    "workflow_tier": "simple-answer", "prompt": prompt,
                    "response": clock_answer, "model": None,
                    "cost": 0.0,
                    "simple_request": {"generation_calls": 0,
                                       "clock": True},
                }
                receipt("ok", reason="machine-clock answer",
                        generation_calls=0, input_tokens=0, output_tokens=0,
                        cost=0.0)
                save_chat_turn(sid, result, self.history_dir)
                emit("simple_request_complete", status="ok", model=None,
                     cost=0.0)
                return result
            if not resuming:
                checkpoint = {
                    "route": "simple-answer", "status": "running",
                    "stage": "strategy", "prompt": prompt,
                    "known_cost": 0.0, "unknown_liability": 0.0,
                    "usage_unknown": False,
                }
                self._persist_chat_checkpoint(sid, checkpoint)
            cancelled_or_late("governor setup")
            with cancellation_context(combined_cancel):
                api_key, gov = governor_for(self.settings)
            if resuming:
                if known_cost_total:
                    gov.record_actual(known_cost_total,
                                      "chat-continuation", lane="answer")
                unknown = max(
                    0.0, float(checkpoint.get("unknown_liability") or 0.0))
                if unknown:
                    gov.retain_unknown(unknown,
                                       "cancelled-chat-provider-call")
            policy = policy_for(
                self.settings, transport=self.transport, governor=gov,
                ledger=ledger)
            turns = simple_relevant_history(
                prompt, load_chat_history(sid, self.history_dir))
            if resuming:
                turns = [turn for turn in turns
                         if turn.get("prompt") != prompt]
            context_text = "\n".join(
                f"User: {turn['prompt']}\nAssistant: {turn['response']}"
                for turn in turns)

            if not isinstance(pre, dict):
                checkpoint.update({
                    "stage": "strategy", "known_cost": round(
                        known_cost_total, 8),
                })
                self._persist_chat_checkpoint(sid, checkpoint)
                cancelled_or_late("Jev strategy")
                with cancellation_context(combined_cancel):
                    _pre_result, pre = policy.evaluate_simple_strategy(
                        prompt, context_text, task_id=sid)
                pre_cost = float(pre.get("cost") or 0.0)
                known_cost_total += pre_cost
                checkpoint.update({
                    "pre": pre, "stage": "answer_generation",
                    "known_cost": round(known_cost_total, 8),
                })
                self._persist_chat_checkpoint(sid, checkpoint)
            strategy = pre.get("strategy") or "direct_answer"
            if bool(pre.get("native")) and strategy != "direct_answer":
                if strategy in {
                        "needs_tool", "web_research",
                        "workspace_inspection"}:
                    if (strategy == "workspace_inspection"
                            and self.root_dir is None):
                        return self._workspace_context_deferred(prompt, sid)
                    needs_web = bool(
                        strategy in {"needs_tool", "web_research"}
                        or is_research_question(prompt)
                        or is_current_information_request(prompt))
                    if self._live_jev_available():
                        return self.run_hourglass_request(
                            prompt, session_id=sid, cancel_check=cancel_check,
                            web=needs_web,
                            request_preflight={
                                "source": "simple_strategy",
                                "choice": strategy,
                                "native": bool(pre.get("native")),
                                "cost": pre_cost,
                            },
                            max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                            token_budget=token_budget)
                    return self._handle_conversation(
                        prompt, sid, cancel_check,
                        web=needs_web,
                        max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                        token_budget=token_budget)
                reason = "request needs clarification"
                result = {
                    "status": "deferred", "intent": "conversation",
                    "workflow_tier": "simple-answer", "prompt": prompt,
                    "response": reason.capitalize() + ".",
                    "model": None, "cost": round(pre_cost, 6),
                    "defer_reason": reason,
                    "next_step": "clarify the request or use the appropriate tool lane",
                    "simple_request": {"pre": pre, "post": None,
                                       "generation_calls": 0},
                }
                receipt("deferred", reason=reason, cost=round(pre_cost, 6),
                        generation_calls=0, input_tokens=0, output_tokens=0)
                save_chat_turn(sid, result, self.history_dir)
                self._clear_chat_checkpoint(sid)
                return result

            pending_review = bool(resuming and answer
                                  and checkpoint.get("stage") == "answer_review")
            if not pending_review:
                checkpoint.update({
                    "stage": "answer_generation",
                    "known_cost": round(known_cost_total, 8),
                })
                self._persist_chat_checkpoint(sid, checkpoint)
                cancelled_or_late("answer generation")
                ladder = list(getattr(self.settings, "panel_pool", ()) or [])
                if not ladder:
                    ladder = self._chat_ladder(self.settings)
                if not ladder:
                    raise HarnessError("simple answer model pool is empty")
                model = ladder[0]
                target_tokens, lane_effort = effective_lane_policy(
                    "answer", max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort)
                generation_prompt = simple_answer_prompt(prompt, turns)
                allocated = _resolve_lane_max_tokens(
                    gov, model, generation_prompt, target_tokens,
                    token_budget=token_budget)
                if allocated < 1:
                    raise HarnessError("shared budget leaves no answer tokens")
                generation_attempts = max(1, generation_attempts + 1)
                with cancellation_context(combined_cancel):
                    completion = governed_completion(
                        self.transport, api_key, gov, model, generation_prompt,
                        allocated, label="simple-answer",
                        reasoning_effort=lane_effort)
                answer = str(completion["content"]).strip()
                generation_cost = float(completion.get("cost") or 0.0)
                known_cost_total += generation_cost
                checkpoint.update({
                    "stage": "answer_review", "answer": answer,
                    "answer_model": model, "answer_cost": generation_cost,
                    "generation_calls": generation_attempts,
                    "known_cost": round(known_cost_total, 8),
                })
                self._persist_chat_checkpoint(sid, checkpoint)
            else:
                completion = None

            post = None
            post_cost = 0.0
            post_skipped = None
            if time.monotonic() < deadline:
                with cancellation_context(combined_cancel):
                    _post_result, post = policy.evaluate_answer(
                        prompt, answer, context_text,
                        site="simple-answer-post", task_id=sid)
                post_cost = float(post.get("cost") or 0.0)
                known_cost_total += post_cost
            else:
                post_skipped = "deadline exhausted after answer generation"

            refusal = self._refusal_reason(answer)
            status = "deferred" if refusal else "ok"
            total_cost = known_cost_total
            review_available = bool(post and post.get("native"))
            review_passed = bool(
                review_available
                and post.get("verdict") == "pass"
                and isinstance(post.get("answer_sufficient"), (int, float))
                and float(post["answer_sufficient"]) >= float(
                    getattr(self.settings, "min_confidence", 0.70))
                and not bool(post.get("plan_required"))
                and not bool(post.get("iteration_required")))
            result = {
                "status": status, "intent": "conversation",
                "workflow_tier": "simple-answer", "prompt": prompt,
                "response": answer, "model": model,
                "cost": round(total_cost, 6),
                "simple_request": {
                    "pre": pre, "post": post,
                    "post_skipped": post_skipped,
                    "post_review_available": review_available,
                    "post_review_passed": review_passed,
                    "generation_calls": 1,
                    "history_turns": len(turns),
                    "deadline_seconds": float(max_wall_seconds),
                },
                **({"defer_reason": refusal,
                    "next_step": "review the refusal or clarify the request"}
                   if refusal else {}),
            }
            receipt(
                status, model=model, cost=round(total_cost, 6),
                generation_cost=round(generation_cost, 6),
                generation_calls=1,
                input_tokens=(completion or {}).get("input_tokens"),
                output_tokens=(completion or {}).get("output_tokens"),
                usage_source=("estimated" if (completion or {}).get("cost_estimated")
                              else "provider"),
                jev_pre_native=bool(pre.get("native")),
                jev_post_native=review_available,
                jev_post_passed=review_passed,
                refusal=bool(refusal), deadline_exhausted=bool(post_skipped))
            save_chat_turn(sid, result, self.history_dir)
            if post_skipped:
                checkpoint["stage"] = "answer_review"
                checkpoint["status"] = "interrupted"
                checkpoint["known_cost"] = round(known_cost_total, 8)
                self._persist_chat_checkpoint(sid, checkpoint)
            else:
                self._clear_chat_checkpoint(sid)
            emit("simple_request_complete", status=status, model=model,
                 cost=total_cost)
            return result
        except ToolCancelled as exc:
            self._checkpoint_interruption(checkpoint, exc, known_cost_total)
            checkpoint["generation_calls"] = generation_attempts
            self._persist_chat_checkpoint(sid, checkpoint)
            if time.monotonic() < deadline:
                receipt("cancelled", generation_calls=generation_attempts)
                raise
            reason = "simple request deadline exhausted during provider call"
            known_cost = float(getattr(exc, "known_cost", 0.0) or 0.0)
            usage_unknown = bool(getattr(exc, "usage_unknown", False))
            unknown_reserve = max(
                float(getattr(exc, "reserved_cost", 0.0) or 0.0),
                float(checkpoint.get("unknown_liability") or 0.0))
            if answer:
                total_cost = known_cost_total + known_cost
                receipt(
                    "ok", reason=reason, model=model,
                    generation_calls=generation_attempts,
                    cost=round(total_cost, 6),
                    input_tokens=(completion or {}).get("input_tokens"),
                    output_tokens=(completion or {}).get("output_tokens"),
                    deadline_exhausted=True, jev_post_native=False)
                result = {
                    "status": "ok", "intent": "conversation",
                    "workflow_tier": "simple-answer", "prompt": prompt,
                    "response": answer, "model": model,
                    "cost": round(total_cost, 6),
                    "simple_request": {
                        "generation_calls": generation_attempts,
                        "post": None, "post_review_available": False,
                        "post_skipped": reason,
                    },
                    **({"usage_unknown": True,
                        "unknown_cost_reserve": round(unknown_reserve, 6)}
                       if usage_unknown else {}),
                }
                save_chat_turn(sid, result, self.history_dir)
                if usage_unknown:
                    self._persist_chat_checkpoint(sid, checkpoint)
                else:
                    self._clear_chat_checkpoint(sid)
                return result
            total_cost = known_cost_total + known_cost
            receipt("deferred", reason=reason,
                    generation_calls=generation_attempts,
                    known_cost=total_cost,
                    usage_unknown=usage_unknown,
                    reserved_cost=unknown_reserve)
            result = {
                "status": "deferred", "intent": "conversation",
                "workflow_tier": "simple-answer", "prompt": prompt,
                "response": ("The bounded answer deadline expired. No "
                             "repository or write action was attempted."),
                "model": None,
                "cost": round(total_cost, 6),
                "defer_reason": reason,
                "next_step": "retry the ordinary answer",
                "simple_request": {"generation_calls": generation_attempts},
                **({"usage_unknown": True,
                    "unknown_cost_reserve": round(unknown_reserve, 6)}
                   if usage_unknown else {}),
            }
            save_chat_turn(sid, result, self.history_dir)
            if usage_unknown:
                self._persist_chat_checkpoint(sid, checkpoint)
            else:
                self._clear_chat_checkpoint(sid)
            return result
        except HarnessError as exc:
            reason = str(exc)
            known_cost = float(getattr(exc, "known_cost", 0.0) or 0.0)
            self._checkpoint_interruption(checkpoint, exc, known_cost_total)
            checkpoint["generation_calls"] = generation_attempts
            self._persist_chat_checkpoint(sid, checkpoint)
            receipt(
                "deferred", reason=reason,
                generation_calls=generation_attempts,
                known_cost=known_cost,
                reserved_cost=float(getattr(exc, "reserved_cost", 0.0) or 0.0),
                usage_unknown=bool(getattr(exc, "usage_unknown", False)))
            result = {
                "status": "deferred", "intent": "conversation",
                "workflow_tier": "simple-answer", "prompt": prompt,
                "response": ("I could not complete the bounded answer call. "
                             "No repository or write action was attempted."),
                "model": None,
                "cost": round(known_cost_total + known_cost, 6),
                "defer_reason": reason,
                "next_step": "retry the ordinary answer when the model service is available",
                "simple_request": {"generation_calls": generation_attempts},
                **({"usage_unknown": True,
                    "unknown_cost_reserve": round(float(
                        getattr(exc, "reserved_cost", 0.0) or 0.0), 6)}
                   if bool(getattr(exc, "usage_unknown", False)) else {}),
            }
            save_chat_turn(sid, result, self.history_dir)
            return result

    def _handle_conversation(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
        web: bool = False,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        token_budget: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # Handle informational or technical questions with conversational routing
        api_key, gov = governor_for(self.settings)
        target_tokens, lane_effort = effective_lane_policy(
            "chat", max_tokens=max_tokens, reasoning_effort=reasoning_effort)

        system_prompt = DEFAULT_CHAT_SYSTEM_PROMPT
        web_sources: List[Dict[str, Any]] = []
        if web:
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            try:
                web_sources = self._gather_web_context(prompt)
            except ToolCancelled:
                raise
            except Exception as e:  # web tools must never kill the chat lane
                web_sources = [{"kind": "web", "ok": False, "note": f"web tools error: {e}"}]
            hosts = ", ".join(sorted(DEFAULT_FETCH_HOSTS)) or "(none configured)"
            system_prompt += "\n\n" + _WEB_CAPABILITY_NOTE.format(hosts=hosts)
            context_lines = []
            for s in web_sources:
                if s.get("ok"):
                    body = (s.get("text") or "")[:_MAX_WEB_CONTEXT_CHARS]
                    context_lines.append(f"SOURCE ({s['kind']}): {s.get('title') or ''} {s['url']}\n{body}")
                else:
                    context_lines.append(f"SOURCE ({s['kind']}) FAILED: {s.get('url') or ''} {s.get('note')}")
            if context_lines:
                system_prompt += "\n\n[Web tool results for this turn -- cite only these; do not invent others]\n" + "\n\n".join(context_lines)
        else:
            system_prompt += "\n\n" + _NO_WEB_DISCLOSURE

        messages = [{"role": "system", "content": system_prompt}]
        past_turns = simple_relevant_history(
            prompt, load_chat_history(session_id, self.history_dir),
            max_turns=3, max_chars=2400)
        for turn in past_turns:
            p_text = turn.get("prompt")
            r_text = turn.get("response")
            if p_text:
                messages.append({"role": "user", "content": p_text})
            if r_text:
                messages.append({"role": "assistant", "content": r_text})
        messages.append({"role": "user", "content": prompt})

        # One governed attempt per ladder rung (preflight -> chat -> bill, the
        # governed_text contract), advancing on HTTP failure or an unusable
        # body. The old code pinned one free model, ignored the status, and
        # rendered a fake apology on a 429; now every failure is recorded,
        # emitted as a rotation, and an all-rungs failure raises honestly.
        answered = None
        attempts: List[str] = []
        # Longest cut-off body seen while every rung was failing: the raw
        # material for the honest truncation deferral.
        best_truncated: Optional[tuple] = None
        for model in self._chat_ladder(self.settings):
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            note = None
            content, cost = None, 0.0
            allocated_tokens = _resolve_lane_max_tokens(
                gov, model, prompt, target_tokens, token_budget=token_budget)
            if allocated_tokens < 1:
                attempts.append(f"{model}: insufficient budget for output tokens")
                continue
            try:
                gov.preflight(prompt, [("chat", model, allocated_tokens, 0)])
                status, resp = chat(
                    transport=self.transport,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    max_tokens=allocated_tokens,
                    reasoning_effort=lane_effort,
                    governor=gov,
                )
            except HarnessError as e:
                note = str(e)
            else:
                if status != 200:
                    note = _http_error(status, resp)
                else:
                    content, finish_reason, cost, is_byok = \
                        extract_content_and_cost(resp)
                    if is_byok:
                        gov.record_byok(model)
                        note = (f"response for {model} was BYOK-routed; "
                                "spend is not tracked on this key")
                    else:
                        # A billable-but-unusable completion is still billed
                        # (the provider charged for it); only usable content
                        # ends the walk.
                        if cost:
                            gov.record_actual(cost, model)
                        usable, why = assess_output(content, finish_reason)
                        if usable and looks_truncated(content):
                            # Providers do not always report finish_reason
                            # "length" honestly; an unbalanced fence/bracket
                            # body is cut regardless of what they claim.
                            usable, why = False, ("response cut off mid-body "
                                                  "(unbalanced code fence "
                                                  "or brackets)")
                        if not usable:
                            note = why
                            if (content or "").strip() and (
                                    best_truncated is None
                                    or len(content) > len(best_truncated[1])):
                                best_truncated = (model, content, cost)
            if note is None:
                answered = (model, content or "", cost)
                break
            attempts.append(f"{model}: {note}")
            emit("rotation", model=model, reason="chat_ladder_advance",
                 note=note)
        if answered is None and best_truncated is not None:
            # Every rung failed, but one produced substantive cut-off
            # content: defer honestly, keeping the content. Rotating more
            # cannot finish a body that exceeds the output cap.
            model, response_text, cost = best_truncated
            defer_reason = (f"response truncated at the token cap on every "
                            f"ladder model (max_tokens={target_tokens})")
            marker_defer = False
            truncation_defer = True
        elif answered is None:
            raise HarnessError(
                "chat failed on every ladder model: " + "; ".join(attempts))
        else:
            model, response_text, cost = answered
            defer_reason = None
            marker_defer = CAPABILITY_MARKER in response_text
            truncation_defer = False

        # The chat lane honors the same deferral contract as apply: a
        # HARNESS_DEFER marker is the model handing the decision back instead
        # of guessing. Free models usually defer in prose instead, so a
        # refusal-shaped answer with no successful tool evidence is treated
        # as the deferral it is; the model's own words become the reason.
        if marker_defer:
            head, _, tail = response_text.partition(CAPABILITY_MARKER)
            first_line = tail.strip().splitlines()[0].strip() if tail.strip() else ""
            defer_reason = " ".join(first_line.split())[:200] \
                or "request exceeds the conversation lane's capability"
            # A bare marker (no prose before it) still owes the user visible
            # text in the saved turn; the UI banner carries the same reason.
            response_text = head.strip() or f"Deferred: {defer_reason}"
        elif defer_reason is None:
            did_work = bool(web_sources and any(s.get("ok") for s in web_sources))
            if not did_work:
                defer_reason = self._refusal_reason(response_text)
        if defer_reason is not None:
            next_step = ('route to the plan lane: harness plan --goal "..." '
                         "--execute (or the MCP plan_and_execute tool)")
            if best_truncated is not None and answered is None:
                next_step = ('say "continue" to keep this going in the chat '
                             "lane, or route long-generation work to the plan "
                             'lane: harness plan --goal "..." --execute '
                             "(it has multi-round continuation)")
            elif not marker_defer:
                next_step = ('reframe within this lane (Q&A, small lookups), '
                             "enable web or adjust the fetch allowlist for "
                             "live data, or route real work to the plan lane: "
                             'harness plan --goal "..." --execute')
            ledger_for(self.settings).append(
                "model_result", task_id=session_id or "(chat)",
                event_note="chat", status="deferred", category="capability",
                model=model, reason=defer_reason, cost=round(cost, 6))
            # AUTO-ESCALATION: a capability defer with the paid key armed does
            # not stop at a handoff note -- the request routes itself into the
            # hourglass plan lane (frontier-planned DAG, governed apply with
            # paid escalation rungs). Truncation defers keep their "continue"
            # resume instead: the content already exists, it needs more room.
            if not truncation_defer and self._auto_escalation_armed():
                emit("chat_escalated", reason=defer_reason, target="plan_lane")
                try:
                    return self._handle_edit(
                        prompt, session_id, auto_apply=True,
                        cancel_check=cancel_check,
                        escalation_note=defer_reason)
                except HarnessError as e:
                    # The handoff failed (no routable target files, plan
                    # refusal, ...): the honest defer stands with the failure
                    # recorded -- never a silent swallow.
                    response_text += (f"\n\n[auto-escalation to the plan lane "
                                      f"failed: {e}]")

        result = {
            "status": "deferred" if defer_reason else "ok",
            "intent": "conversation",
            "prompt": prompt,
            "response": response_text,
            "model": model,
            "cost": round(cost, 6),
            **({"truncated": True} if truncation_defer else {}),
            **({"defer_reason": defer_reason,
                # The resume hint a deferral owes the operator (render.py's
                # contract): what to do instead of this lane's refusal.
                "next_step": next_step}
               if defer_reason else {}),
            # Honest provenance: did this turn actually retrieve web evidence?
            "web_used": bool(web_sources and any(s.get("ok") for s in web_sources)),
            "web_sources": [
                {k: s[k] for k in ("kind", "ok", "url") if k in s}
                for s in web_sources
            ],
        }

        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="conversation", cost=cost, model=model)
        return result

    def _gather_web_context(self, prompt: str) -> List[Dict[str, Any]]:
        # Web owns the network seams; pass those module functions through so
        # policy and hermetic test patches remain at the single owner.
        import harness.web as _web
        return gather_web_context(
            prompt,
            allowed_hosts=DEFAULT_FETCH_HOSTS,
            fetch_url_fn=_web.fetch_url,
            search_web_fn=_web.search_web,
            find_urls_fn=_web.find_urls,
            extract_query_fn=_web.extract_query,
            max_sources=_MAX_WEB_SOURCES,
        )

    @staticmethod
    def _chat_ladder(settings: Settings) -> List[str]:
        # Single-owner ladder: delegate to router's chat-ladder helper
        # (harness/chat.py: chat_ladder) so panel/escalation policy isn't
        # duplicated between the agent and the router.
        from .chat import chat_ladder as _chat_ladder
        return _chat_ladder(settings)

    @classmethod
    def _refusal_reason(cls, response_text: str) -> Optional[str]:
        for sentence in re.split(r"(?<=[.!?])\s+", response_text.strip()):
            if cls._REFUSAL_RE.search(sentence):
                return " ".join(sentence.split())[:200]
        return None

    def _hourglass_workspace_candidates(
            self, prompt: str, *, repo_files: Optional[List[str]] = None) -> List[str]:
        if self.root_dir is None:
            return []
        candidates = discover_target_files(prompt, self.root_dir)
        if is_comprehensive_workspace_inspection_request(prompt):
            inventory = (repo_files if repo_files is not None else
                         enumerate_repo_files(self.root_dir, limit=1000))
            return workspace_review_candidates(
                prompt, inventory, max_files=_MAX_WORKSPACE_REVIEW_FILES,
                preferred=candidates)
        if not candidates:
            candidates = keyword_fallback(
                prompt, enumerate_repo_files(self.root_dir),
                max_files=_MAX_HOURGLASS_CONTEXT_FILES)
        return candidates[:_MAX_HOURGLASS_CONTEXT_FILES]

    def _hourglass_request_context(self, prompt: str, web: bool = False,
                                  workspace: bool = True):
        """Gather a bounded, cheap intake brief for a non-edit request.

        Intake is local and deterministic: explicit paths win, then filename
        overlap, then no context.  It never feeds an unbounded repository to a
        model.  Web evidence is added only when the caller explicitly enables
        the existing bounded web lane.
        """
        files = {}
        broad_review = bool(
            workspace
            and is_comprehensive_workspace_inspection_request(prompt))
        repo_inventory = None
        if broad_review and self.root_dir is not None:
            repo_inventory = enumerate_repo_files(self.root_dir, limit=1000)
        if self.root_dir is not None and workspace:
            candidates = self._hourglass_workspace_candidates(
                prompt, repo_files=repo_inventory)
            for rel in candidates:
                try:
                    path = resolve_workspace_path(
                        self.root_dir, rel, must_exist=True)
                    if path.is_file():
                        files[rel] = path.read_text(encoding="utf-8", errors="replace")
                except (OSError, HarnessError):
                    continue
        context_limit = (_MAX_WORKSPACE_REVIEW_CHARS if broad_review
                         else _MAX_HOURGLASS_CONTEXT_CHARS)
        context_tokens = (_MAX_WORKSPACE_REVIEW_TOKENS if broad_review else 1800)
        summary = prompt
        if broad_review:
            top_level_counts: Dict[str, int] = {}
            for rel in repo_inventory or []:
                top = rel.split("/", 1)[0]
                top_level_counts[top] = top_level_counts.get(top, 0) + 1
            inventory_note = ", ".join(
                f"{name}: {count}" for name, count in
                sorted(top_level_counts.items(), key=lambda item: (-item[1], item[0]))[:12])
            summary = (
                f"{prompt}\n\nRead-only repository review scope: the inventory "
                f"contains up to {len(repo_inventory or [])} eligible files; "
                f"{len(files)} files were selected using project guidance, "
                "design/roadmap documents, manifests, source entry points, and "
                "tests as selection priorities. Top-level inventory counts: "
                f"{inventory_note or 'no files found'}. Treat this as a bounded "
                "review sample: ground findings in the selected evidence and "
                "name important areas that were not inspected instead of "
                "claiming exhaustive coverage.")
        brief = distill_context(
            files=files, summary=summary, max_tokens=context_tokens)
        # HV-2 evidence: the condensation is a ledger event, not a private
        # step -- site=hourglass, schema-v2 pack attached to the MicroBrief.
        ledger_for(self.settings, caller="agent").append(
            "brief_built", site="hourglass", schema=2,
            **brief.ledger_fields())
        context = brief.to_prompt_context()[:context_limit]
        web_sources = []
        if web:
            try:
                web_sources = self._gather_web_context(prompt)
            except (ToolCancelled, ProviderSpendLimitError):
                raise
            except Exception as exc:  # context failure must not kill the lane
                web_sources = [{"kind": "web", "ok": False,
                                "note": f"web tools error: {type(exc).__name__}"}]
            for source in web_sources:
                if source.get("ok"):
                    body = str(source.get("text") or "")[:_MAX_WEB_CONTEXT_CHARS]
                    context += "\n\nWEB EVIDENCE:\n" + body
            context = context[:context_limit]
        emit("context_condensed", estimated_tokens=brief.estimated_tokens,
             files=list(files), web_sources=len(web_sources),
             scope=("repository-review" if broad_review else
                    "targeted-workspace" if workspace else "none"))
        return context, files, brief, web_sources

    def _hourglass_answer_once(self, prompt: str, context: str,
                               prior: Optional[Dict[str, Any]] = None,
                               model_offset: int = 0,
                               cancel_check=None, governor=None,
                               api_key=None, past_turns=None,
                               web: bool = False,
                               web_sources=None,
                               max_tokens: Optional[int] = None,
                               reasoning_effort: Optional[str] = None,
                               token_budget: Optional[Any] = None) -> Dict[str, Any]:
        """Run one cheap-to-capable answer attempt through the shared ladder."""
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        if governor is None:
            api_key, governor = governor_for(self.settings)
        if api_key is None:
            api_key = resolve_api_key()
        gov = governor
        target_tokens, lane_effort = effective_lane_policy(
            "answer", max_tokens=max_tokens, reasoning_effort=reasoning_effort)
        # The ordinary chat helper puts the judge first because it optimizes
        # structured chat.  The all-request answer loop instead starts with
        # the configured panel/scout pool, then adds judge and escalation
        # rungs; this keeps the first paid attempt cheap and capability grows
        # only when Jev asks for another iteration.
        ladder = []
        for source in (getattr(self.settings, "panel_pool", ()),
                       (getattr(self.settings, "judge", None),),
                       (getattr(self.settings, "convergence_model", None),),
                       getattr(self.settings, "escalation_pool", ())):
            for model in source:
                if model and model not in ladder:
                    ladder.append(model)
        if not ladder:
            raise HarnessError("answer ladder is empty")
        start = max(0, min(int(model_offset), len(ladder) - 1))
        ordered = ladder[start:] + ladder[:start]
        system = DEFAULT_CHAT_SYSTEM_PROMPT
        if web:
            hosts = ", ".join(sorted(DEFAULT_FETCH_HOSTS)) or "(none configured)"
            system += "\n\n" + _WEB_CAPABILITY_NOTE.format(hosts=hosts)
            lines = []
            for source in (web_sources or []):
                if source.get("ok"):
                    lines.append(f"SOURCE ({source.get('kind')}): {source.get('title') or ''} {source.get('url')}\n" + str(source.get("text") or "")[:_MAX_WEB_CONTEXT_CHARS])
                else:
                    lines.append(f"SOURCE ({source.get('kind')}) FAILED: {source.get('url') or ''} {source.get('note')}")
            if lines:
                system += "\n\n[Web tool results for this turn -- cite only these; do not invent others]\n" + "\n\n".join(lines)
        else:
            system += "\n\n" + _NO_WEB_DISCLOSURE
        if context:
            system += ("\n\n[Retained local context — use it as evidence, "
                       "but do not claim facts that it does not contain]\n"
                       + context)
        user = prompt
        if prior:
            user += ("\n\n[Previous candidate and bounded Jev review]\n"
                     "Candidate answer:\n"
                     + str(prior.get("answer") or "")[:2400]
                     + "\nJev review:\n"
                     + str(prior.get("feedback") or "")[:1200]
                     + "\nProduce a fresh, corrected answer. Do not mention the "
                       "review process unless it helps the user.")
        messages = [{"role": "system", "content": system}]
        for turn in (past_turns or []):
            if turn.get("prompt"):
                messages.append({"role": "user", "content": turn["prompt"]})
            if turn.get("response"):
                messages.append({"role": "assistant", "content": turn["response"]})
        messages.append({"role": "user", "content": user})
        prompt_text = user
        attempts = []
        best_truncated_content = None
        best_truncated_model = None
        best_truncated_cost = 0.0
        for model in (self._rate_circuit.available(ordered) or ordered):
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            allocated_tokens = _resolve_lane_max_tokens(
                gov, model, prompt_text, target_tokens, token_budget=token_budget)
            if allocated_tokens < 1:
                attempts.append(f"{model}: insufficient budget for output tokens")
                continue
            try:
                gov.preflight(prompt_text, [("answer", model, allocated_tokens, 0)])
                status, response = chat(
                    transport=self.transport, api_key=api_key, model=model,
                    messages=messages, max_tokens=allocated_tokens,
                    reasoning_effort=lane_effort, governor=gov)
            except ProviderSpendLimitError:
                raise
            except HarnessError as exc:
                attempts.append(f"{model}: {exc}")
                if "429" in str(exc) or "rate limit" in str(exc).lower():
                    self._rate_circuit.note_rate_limited(model)
                continue
            if status != 200:
                attempts.append(f"{model}: HTTP {status}")
                if status == 429:
                    self._rate_circuit.note_rate_limited(model)
                emit("rotation", model=model, reason="answer_ladder_advance",
                     note=f"HTTP {status}")
                continue
            content, finish_reason, cost, is_byok = \
                extract_content_and_cost(response)
            if is_byok:
                gov.record_byok(model)
                attempts.append(f"{model}: BYOK route refused")
                continue
            if cost:
                gov.record_actual(cost, model)
            usable, why = assess_output(content, finish_reason)
            if usable and looks_truncated(content):
                usable, why = False, "response truncated mid-body"
            if not usable:
                attempts.append(f"{model}: {why}")
                if content and len(content) > len(best_truncated_content or ""):
                    best_truncated_content = content
                    best_truncated_model = model
                    best_truncated_cost = cost
                emit("rotation", model=model, reason="answer_ladder_advance",
                     note=why)
                continue
            return {"model": model, "answer": content.strip(), "cost": cost, "truncated": False}
        if best_truncated_content:
            return {
                "model": best_truncated_model,
                "answer": best_truncated_content.strip() + TRUNCATION_NOTICE,
                "cost": best_truncated_cost,
                "truncated": True,
            }
        raise HarnessError("answer failed on every ladder model: "
                           + "; ".join(attempts))

    @staticmethod
    def _jev_answer_envelope(result, structural: Dict[str, Any],
                             threshold: float) -> Dict[str, Any]:
        answers = result.answers if isinstance(result.answers, dict) else {}
        sufficient = structural.get("answer_sufficient")
        native = bool(structural.get("native"))
        passed = bool(native and isinstance(sufficient, (int, float))
                      and float(sufficient) >= threshold
                      and not bool(structural.get("iteration_required")))
        return {
            "native": native,
            "is_fallback": bool(result.is_fallback),
            "fallback_reason": result.fallback_reason,
            "verdict": result.verdict,
            "supported": result.supported,
            "answers": answers,
            "reasons": list(result.reasons or []),
            "cost": float(structural.get("cost") or 0.0),
            "input_tokens": int(structural.get("input_tokens") or 0),
            "output_tokens": int(structural.get("output_tokens") or 0),
            "model": result.model,
            "pack_version": structural.get("pack_version"),
            "confidence": {
                "threshold": threshold,
                "observed": sufficient,
                "passed": passed,
                "source": "jev.noul.answer_sufficient",
            },
        }

    def _persist_chat_checkpoint(self, session_id: str,
                                 checkpoint: ChatCheckpoint) -> None:
        try:
            save_chat_checkpoint(session_id, checkpoint, self.history_dir)
        except (HarnessError, OSError, TypeError, ValueError) as exc:
            emit("chat_checkpoint_unavailable",
                 error_type=type(exc).__name__)

    def _clear_chat_checkpoint(self, session_id: str) -> None:
        try:
            clear_chat_checkpoint(session_id, self.history_dir)
        except (HarnessError, OSError):
            emit("chat_checkpoint_clear_failed", session_id=session_id)

    @staticmethod
    def _checkpoint_interruption(checkpoint: ChatCheckpoint, exc: Exception,
                                 known_cost: float) -> None:
        checkpoint["status"] = "interrupted"
        checkpoint["known_cost"] = round(
            max(0.0, float(known_cost or 0.0)
                + float(getattr(exc, "known_cost", 0.0) or 0.0)), 8)
        reserved = max(0.0, float(getattr(exc, "reserved_cost", 0.0) or 0.0))
        if getattr(exc, "usage_unknown", False) and reserved:
            checkpoint["unknown_liability"] = round(
                max(0.0, float(checkpoint.get("unknown_liability") or 0.0))
                + reserved, 8)
        checkpoint["usage_unknown"] = bool(
            checkpoint.get("usage_unknown") or getattr(exc, "usage_unknown", False))

    def _hourglass_context_matches(self, checkpoint: ChatCheckpoint) -> bool:
        expected_root = str(checkpoint.get("workspace_root") or "")
        actual_root = str(self.root_dir or "")
        if expected_root != actual_root:
            # Temporary directories on macOS are commonly exposed through
            # /var while Path.resolve() stores /private/var. Treat those as
            # the same selected workspace, but fail closed for missing or
            # malformed checkpoint paths.
            if not expected_root or self.root_dir is None:
                return False
            try:
                if Path(expected_root).resolve(strict=True) != \
                        self.root_dir.resolve(strict=True):
                    return False
            except (OSError, RuntimeError, ValueError):
                return False
        if (self.root_dir is not None
                and bool(checkpoint.get("workspace_context_used"))):
            prompt = checkpoint.get("prompt")
            if not isinstance(prompt, str):
                return False
            current_files = self._hourglass_workspace_candidates(prompt)
            saved_files = checkpoint.get("context_files") or []
            if (not isinstance(saved_files, list)
                    or sorted(current_files) != sorted(saved_files)):
                return False
        hashes = checkpoint.get("file_hashes") or {}
        if not isinstance(hashes, dict):
            return False
        for rel, expected in hashes.items():
            try:
                path = resolve_workspace_path(
                    self.root_dir, rel, must_exist=True)
                actual = hashlib.sha256(
                    path.read_bytes()).hexdigest()
            except (OSError, HarnessError, TypeError):
                return False
            if actual != expected:
                return False
        return True

    def run_hourglass_request(
            self, prompt: str, *, session_id: Optional[str] = None,
            cancel_check=None, web: bool = False,
            confidence_threshold: Optional[float] = None,
            max_rounds: int = _MAX_HOURGLASS_ANSWER_ROUNDS,
            auto_apply: bool = False,
            max_tokens: Optional[int] = None,
            reasoning_effort: Optional[str] = None,
            token_budget: Optional[Any] = None,
            max_wall_seconds: Optional[float] = None,
            request_preflight: Optional[Dict[str, Any]] = None,
            resume_state: Optional[ChatCheckpoint] = None) -> Dict[str, Any]:
        """Run the all-request composition used by external local drivers.

        Edit requests enter the existing hourglass plan/executor lane. Direct
        answers use local context intake, the cheapest configured answer rung,
        and native Jev sufficiency/iteration signals in a bounded escalation
        loop.  A plan signal is advisory: only an explicit edit/action request
        can cross the write boundary.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise HarnessError("Prompt cannot be empty")
        threshold = float(
            getattr(self.settings, "min_confidence",
                    _DEFAULT_HOURGLASS_CONFIDENCE)
            if confidence_threshold is None else confidence_threshold)
        if threshold < 0.0 or threshold > 1.0:
            raise HarnessError("confidence_threshold must be between 0 and 1")
        rounds = max(1, min(int(max_rounds), _MAX_HOURGLASS_ANSWER_ROUNDS))
        sid = session_id or "default"
        intent = self._classify_prompt_intent(prompt)
        hourglass_policy = resolve_hourglass(self.settings)
        emit("hourglass_request", intent=intent, confidence_threshold=threshold,
             max_rounds=rounds)
        if intent == "audit" and not is_workspace_inspection_request(prompt):
            return self._handle_audit(prompt, sid, cancel_check)
        if intent == "simple-action":
            return self.run_simple_action(prompt, session_id=sid,
                                          cancel_check=cancel_check)
        if intent == "edit":
            result = self._handle_edit(
                prompt, sid, auto_apply, cancel_check,
                use_jev_completion=True)
            result.setdefault("hourglass", {
                "stages": ["context_intake", "planning_waist", "execution",
                           "completion_jev"],
                "source": "agent_edit_lane",
            })
            return result

        # Current-data requests earn web retrieval; workspace references are
        # handled from the selected local tree and never become web searches.
        web = (bool(web) or is_research_question(prompt)
               or is_current_information_request(prompt))
        wall_budget = (DEFAULT_RUN_WALL_SECONDS if max_wall_seconds is None
                       else float(max_wall_seconds))
        deadline = time.monotonic() + max(0.0, wall_budget)
        checkpoint = dict(resume_state or {})
        resuming = bool(resume_state)
        if not resuming:
            initial_cost = max(
                0.0, float((request_preflight or {}).get("cost") or 0.0))
            checkpoint = {
                "route": "hourglass", "status": "running", "stage": "route",
                "prompt": prompt, "intent": intent,
                "web_requested": web, "workspace_root": str(self.root_dir or ""),
                "workspace_context_used": False,
                "known_cost": round(initial_cost, 8),
                "unknown_liability": 0.0,
                "usage_unknown": False, "next_round": 1, "rounds": [],
            }
            if isinstance(request_preflight, dict):
                checkpoint["route_preflight"] = request_preflight
            self._persist_chat_checkpoint(sid, checkpoint)
        api_key, gov = governor_for(self.settings)
        policy = policy_for(
            self.settings, transport=self.transport, governor=gov,
            ledger=ledger_for(self.settings, caller="agent"))
        if resuming:
            prior_known = max(0.0, float(checkpoint.get("known_cost") or 0.0))
            prior_unknown = max(
                0.0, float(checkpoint.get("unknown_liability") or 0.0))
            if prior_known:
                gov.record_actual(prior_known, "chat-continuation", lane="answer")
            if prior_unknown:
                gov.retain_unknown(prior_unknown, "cancelled-chat-provider-call")
            total_cost = prior_known
        else:
            total_cost = max(0.0, float((request_preflight or {}).get("cost") or 0.0))
            if total_cost:
                gov.record_actual(total_cost, "chat-route-preflight", lane="answer")
            checkpoint["known_cost"] = round(total_cost, 8)

        route_preflight = (request_preflight
                           or checkpoint.get("route_preflight"))
        if (not isinstance(route_preflight, dict)
                and not callable(getattr(policy,
                                         "evaluate_request_workflow", None))):
            route_preflight = {
                "source": "request-workflow-unavailable",
                "choice": None, "native": False, "cost": 0.0,
            }
        if not isinstance(route_preflight, dict):
            try:
                route_evaluation = policy.evaluate_request_workflow(
                    {"request": prompt}, task_id=sid, max_input_tokens=512)
            except (ToolCancelled, ProviderUsageUnknown) as exc:
                self._checkpoint_interruption(checkpoint, exc, total_cost)
                self._persist_chat_checkpoint(sid, checkpoint)
                raise
            if (not isinstance(route_evaluation, (tuple, list))
                    or len(route_evaluation) != 2
                    or not isinstance(route_evaluation[1], dict)
                    or not hasattr(route_evaluation[0], "answers")):
                route_preflight = {
                    "source": "jev_request_workflow_unusable",
                    "choice": None, "native": False, "cost": 0.0,
                }
                emit("request_route_unusable",
                     reason_code="invalid_judgment_shape")
            else:
                route_result, route_structural = route_evaluation
                route_answer = (route_result.answers.get("workflow_tier")
                                if isinstance(route_result.answers, dict) else None)
                route_cost = route_structural.get("cost")
                route_preflight = {
                    "source": "jev_request_workflow",
                    "choice": (route_answer.get("choice")
                               if isinstance(route_answer, dict) else None),
                    "native": not bool(getattr(
                        route_result, "is_fallback", True)),
                    "is_fallback": bool(getattr(
                        route_result, "is_fallback", True)),
                    "cost": (float(route_cost) if isinstance(
                        route_cost, (int, float)) else 0.0),
                }
            total_cost += float(route_preflight.get("cost") or 0.0)
        checkpoint["route_preflight"] = route_preflight
        checkpoint["known_cost"] = round(total_cost, 8)
        selected_workspace = (
            is_workspace_inspection_request(prompt)
            or route_preflight.get("choice") in {
                "workspace-inspection", "workspace_inspection"})
        if selected_workspace:
            # Explicit local-workspace intent is stronger evidence than an
            # advisory Jev route: never send a repo audit to web search.
            web = False
        elif (bool(route_preflight.get("native"))
              and route_preflight.get("choice") == "web-research"):
            web = True
        elif (route_preflight.get("source") == "simple_strategy"
              and route_preflight.get("choice") == "web_research"):
            web = True
        if (route_preflight.get("choice") in {
                "workspace-inspection", "workspace_inspection"}
                and self.root_dir is None):
            return self._workspace_context_deferred(prompt, sid)
        checkpoint["web_requested"] = web
        checkpoint["intent"] = intent
        effective_route = (
            "workspace-inspection" if (
                is_workspace_inspection_request(prompt)
                or route_preflight.get("choice") == "workspace-inspection"
                or route_preflight.get("choice") == "workspace_inspection")
            else "web-research" if web
            else "answer")
        workspace_needed = effective_route == "workspace-inspection"
        checkpoint["effective_route"] = effective_route
        checkpoint["workspace_context_used"] = workspace_needed
        emit("request_route_selected",
             selected=route_preflight.get("choice"),
             effective=effective_route,
             source=route_preflight.get("source"),
             native=bool(route_preflight.get("native")))
        if (route_preflight.get("choice") == "clarify"
                and bool(route_preflight.get("native"))):
            result = {
                "status": "deferred", "intent": intent,
                "workflow_tier": "clarification", "prompt": prompt,
                "response": "I need one clarification before I can answer safely.",
                "model": None, "cost": round(total_cost, 6),
                "defer_reason": "Jev selected clarification",
                "next_step": "clarify the missing request detail",
                "jev": {"workflow_tier": "clarify",
                        "native": True},
            }
            save_chat_turn(sid, result, self.history_dir)
            self._clear_chat_checkpoint(sid)
            return result

        saved_context = checkpoint.get("context")
        context_current = (resuming and isinstance(saved_context, str)
                           and self._hourglass_context_matches(checkpoint))
        web_fresh = (
            not web or time.time() - float(
                checkpoint.get("context_updated_at") or 0.0) <= 300.0)
        if context_current and web_fresh:
            context = saved_context
            context_files = list(checkpoint.get("context_files") or [])
            context_tokens = int(checkpoint.get("context_tokens") or 0)
            web_sources = list(checkpoint.get("web_sources") or [])
        else:
            context, file_contents, brief, web_sources = \
                self._hourglass_request_context(
                    prompt, web=web, workspace=workspace_needed)
            context_files = list(file_contents)
            context_tokens = int(brief.estimated_tokens)
            file_hashes = {
                rel: hashlib.sha256(resolve_workspace_path(
                    self.root_dir, rel, must_exist=True).read_bytes()).hexdigest()
                for rel in file_contents}
            remaining_source_chars = _MAX_HOURGLASS_CONTEXT_CHARS
            bounded_sources = []
            for source in web_sources:
                item = {key: source[key] for key in
                        ("kind", "ok", "url", "title", "note")
                        if key in source}
                if source.get("text") and remaining_source_chars > 0:
                    text = str(source.get("text"))[:min(
                        1600, remaining_source_chars)]
                    item["text"] = text
                    remaining_source_chars -= len(text)
                bounded_sources.append(item)
            web_sources = bounded_sources
            checkpoint.update({
                "context": context,
                "context_files": context_files,
                "context_tokens": context_tokens,
                "context_scope": (
                    "repository-review" if (
                        workspace_needed and
                        is_comprehensive_workspace_inspection_request(prompt))
                    else "targeted-workspace" if workspace_needed else "none"),
                "file_hashes": file_hashes,
                "web_sources": web_sources,
                "context_updated_at": time.time(),
                "workspace_root": str(self.root_dir or ""),
                "workspace_context_used": workspace_needed,
            })
            if resuming and not (context_current and web_fresh):
                checkpoint["answer"] = None
                checkpoint["prior"] = None
                checkpoint["rounds"] = []
                checkpoint["next_round"] = 1
                checkpoint["stage"] = "answer"
            self._persist_chat_checkpoint(sid, checkpoint)

        turns = load_chat_history(sid, self.history_dir)
        if resuming:
            turns = [turn for turn in turns if turn.get("prompt") != prompt]
        past_turns = simple_relevant_history(
            prompt, turns,
            max_turns=3, max_chars=2400)
        prior = checkpoint.get("prior") if resuming else None
        history = list(checkpoint.get("rounds") or []) if resuming else []
        answer = str(checkpoint.get("answer") or "") if resuming else ""
        model = checkpoint.get("answer_model") if answer else None
        pending_review = bool(resuming and answer
                              and checkpoint.get("stage") == "jev_review")
        first_round = max(1, int(checkpoint.get("next_round") or 1)) \
            if resuming else 1
        jev_result = None
        jev_structural = None
        previous_envelopes = [
            entry.get("jev") for entry in history
            if isinstance(entry, dict) and isinstance(entry.get("jev"), dict)]
        last_envelope = previous_envelopes[-1] if previous_envelopes else None
        best_envelope = max(
            (item for item in previous_envelopes if item.get("native")),
            key=lambda item: (item.get("confidence") or {}).get("observed") or 0.0,
            default=None)
        stop_reason = None
        status = "needs_iteration"
        attempt = ({"answer": answer, "model": model,
                    "cost": float(checkpoint.get("answer_cost") or 0.0),
                    "truncated": bool(checkpoint.get("truncated"))}
                   if pending_review else {})
        for round_no in range(first_round, rounds + 1):
            if cancel_check and cancel_check():
                if not (pending_review and round_no == first_round):
                    checkpoint["stage"] = "answer"
                    checkpoint["next_round"] = round_no
                    checkpoint["prior"] = prior
                checkpoint["known_cost"] = round(total_cost, 8)
                self._persist_chat_checkpoint(sid, checkpoint)
                raise ToolCancelled("Prompt execution was cancelled by user")
            if time.monotonic() >= deadline:
                # Stop iterating, but keep whatever was answered so far: the
                # standing rule is that a run returns a real answer with an
                # honest disclosure, never a bare dead end.
                status = "needs_iteration"
                stop_reason = (
                    f"wall-clock budget of {wall_budget:.0f}s exhausted "
                    f"after round {round_no - 1}")
                break
            if round_no > 1:
                remaining = (gov.working_remaining()
                             if callable(getattr(gov, "working_remaining", None))
                             else None)
                if isinstance(remaining, (int, float)) and remaining < jev_cost_ceiling():
                    status = "deferred"
                    stop_reason = (
                        "Jev budget cannot safely reserve another answer review")
                    break
            if not (pending_review and round_no == first_round):
                checkpoint.update({
                    "stage": "answer", "next_round": round_no,
                    "prior": prior, "known_cost": round(total_cost, 8),
                })
                self._persist_chat_checkpoint(sid, checkpoint)
                try:
                    attempt = self._hourglass_answer_once(
                        prompt, context, prior=prior, model_offset=round_no - 1,
                        cancel_check=cancel_check, governor=gov, api_key=api_key,
                        past_turns=past_turns, web=web, web_sources=web_sources,
                        max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                        token_budget=token_budget)
                except (ToolCancelled, ProviderUsageUnknown) as exc:
                    self._checkpoint_interruption(checkpoint, exc, total_cost)
                    self._persist_chat_checkpoint(sid, checkpoint)
                    raise
                answer = attempt["answer"]
                model = attempt["model"]
                answer_cost = float(attempt.get("cost") or 0.0)
                total_cost += answer_cost
                checkpoint.update({
                    "stage": "jev_review", "next_round": round_no,
                    "answer": answer, "answer_model": model,
                    "answer_cost": answer_cost,
                    "truncated": bool(attempt.get("truncated")),
                    "known_cost": round(total_cost, 8),
                })
                self._persist_chat_checkpoint(sid, checkpoint)
                pending_review = False
            try:
                jev_result, jev_structural = policy.evaluate_answer(
                    prompt, answer, context, site="answer", task_id=sid)
            except (ToolCancelled, ProviderUsageUnknown) as exc:
                self._checkpoint_interruption(checkpoint, exc, total_cost)
                self._persist_chat_checkpoint(sid, checkpoint)
                raise
            total_cost += float(jev_structural.get("cost") or 0.0)
            envelope = self._jev_answer_envelope(
                jev_result, jev_structural, threshold)
            last_envelope = envelope
            observed = envelope["confidence"].get("observed")
            if (envelope["native"]
                    and (best_envelope is None
                         or not isinstance(observed, (int, float))
                         or float(observed) > float(
                             best_envelope["confidence"].get("observed") or 0.0))):
                best_envelope = envelope
            history.append({"round": round_no, "model": model,
                            "jev": envelope})
            emit("answer_judged", round=round_no, model=model,
                 native=envelope["native"],
                 confidence=envelope["confidence"]["observed"],
                 confidence_threshold=threshold,
                 iteration_required=bool(jev_structural.get("iteration_required")),
                 status=("pass" if envelope["confidence"]["passed"] else
                         "unverified" if envelope["is_fallback"] else
                         "needs_iteration" if bool(
                             jev_structural.get("iteration_required"))
                         else "stopped"))
            checkpoint["known_cost"] = round(total_cost, 8)
            checkpoint["rounds"] = history
            checkpoint["last_jev"] = envelope
            checkpoint["answer"] = answer
            checkpoint["answer_model"] = model
            checkpoint["answer_cost"] = float(attempt.get("cost") or 0.0)
            checkpoint["next_round"] = round_no
            checkpoint["stage"] = "jev_review"
            self._persist_chat_checkpoint(sid, checkpoint)
            if jev_structural.get("plan_required"):
                # A direct-answer request must not turn an advisory Jev signal
                # into a write.  The edit lane owns plan/execute; callers can
                # explicitly resubmit this request as an edit when appropriate.
                status = "plan_required"
                break
            if not envelope["native"] and not envelope["is_fallback"]:
                # A rejected, malformed, or preflight-refused live request is
                # not an allowed local fallback and cannot use the independent
                # fulfillment check to turn it into approval.
                status = "deferred"
                stop_reason = "; ".join(envelope["reasons"] or []) or (
                    "Jev could not produce a usable live judgment")
                break
            if not envelope["native"] and envelope["is_fallback"]:
                # Keep the useful answer without pretending it was semantically
                # reviewed or paying for a second generic completion model.
                status = "ok"
                stop_reason = (
                    "Jev unavailable; response returned without semantic review")
                break
            if envelope["confidence"]["passed"]:
                status = "ok"
                break
            if not bool(jev_structural.get("iteration_required")):
                status = "needs_iteration"
                stop_reason = (
                    "Jev did not meet the configured sufficiency threshold and "
                    "did not request another answer attempt")
                break
            prior = {
                "answer": answer,
                "feedback": "Jev says another answer attempt is needed: "
                            + ("; ".join(envelope["reasons"])
                               or "improve sufficiency against the retained context"),
            }
            if round_no == rounds:
                status = "needs_iteration"
                stop_reason = (
                    f"Jev requested another answer attempt; the bounded limit "
                    f"of {rounds} rounds was reached")
            else:
                checkpoint.update({
                    "stage": "answer", "next_round": round_no + 1,
                    "prior": prior, "answer": None,
                    "answer_model": None, "answer_cost": 0.0,
                })
                self._persist_chat_checkpoint(sid, checkpoint)

        is_truncated = bool(attempt.get("truncated"))
        result = {
            "status": status,
            "intent": intent,
            "workflow_tier": effective_route,
            "prompt": prompt,
            "response": answer,
            "model": model,
            "cost": round(total_cost, 6),
            "request_workflow": {
                "selected": route_preflight.get("choice"),
                "effective": effective_route,
                "source": route_preflight.get("source"),
                "native": bool(route_preflight.get("native")),
            },
            "verification": {
                "status": ("jev_verified" if status == "ok"
                           and bool((best_envelope or last_envelope or {}).get("confidence", {}).get("passed"))
                           else "unverified" if not bool((best_envelope or last_envelope or {}).get("native"))
                           else "needs_review"),
                "source": "jev.answer_sufficiency",
                **({"reason": stop_reason} if stop_reason else {}),
            },
            **({"usage_unknown": True,
                "unknown_cost_reserve": round(
                    float(checkpoint.get("unknown_liability") or 0.0), 6)}
               if checkpoint.get("usage_unknown") else {}),
            **({"truncated": True} if is_truncated else {}),
            # Every stop_reason above -- the Jev budget, the deferred branch
            # and the new wall-clock ceiling -- was computed and then dropped
            # on the floor here, so a caller could only see "needs_iteration"
            # with no reason. A run that stops must say why.
            **({"stop_reason": stop_reason} if stop_reason else {}),
            "web_used": bool(web_sources and any(s.get("ok") for s in web_sources)),
            "web_sources": [
                {k: s[k] for k in ("kind", "ok", "url") if k in s}
                for s in web_sources],
            "confidence": ((best_envelope or last_envelope or {}).get(
                "confidence", {"threshold": threshold, "observed": None,
                               "passed": False,
                               "source": "jev.noul.answer_sufficient"})),
            "jev": (best_envelope or last_envelope),
            **({"jev_last": last_envelope}
               if last_envelope is not best_envelope else {}),
            "hourglass": {
                "stages": ["context_intake", "answer", "jev_review"],
                "skipped": ["planning_waist", "execution"],
                "settings": hourglass_policy,
                "context_files": context_files,
                "context_tokens": context_tokens,
                "context_scope": checkpoint.get("context_scope", "none"),
                "rounds": history,
                "request_workflow": {
                    "selected": route_preflight.get("choice"),
                    "effective": effective_route,
                    "source": route_preflight.get("source"),
                    "native": bool(route_preflight.get("native")),
                },
                "web_sources": [
                    {k: s[k] for k in ("kind", "ok", "url") if k in s}
                    for s in web_sources],
            },
        }
        marker_defer = CAPABILITY_MARKER in answer
        if self._escalation_authorized(prompt, marker_defer, status):
            defer_reason = None
            if marker_defer:
                head, _, tail = answer.partition(CAPABILITY_MARKER)
                defer_reason = tail.strip().splitlines()[0].strip() if tail.strip() else ""
            else:
                defer_reason = "execution request routed to plan & execute lane"
            emit("chat_escalated", reason=defer_reason, target="plan_lane")
            try:
                escalated = self._handle_edit(
                    prompt, sid, auto_apply=True,
                    cancel_check=cancel_check,
                    escalation_note=defer_reason,
                    use_jev_completion=True)
                if isinstance(escalated, dict):
                    escalated.setdefault("hourglass", {
                        "stages": ["context_intake", "planning_waist", "execution",
                                   "completion_jev"],
                        "source": "hourglass_auto_escalation",
                    })
                    save_chat_turn(sid, escalated, self.history_dir)
                    self._clear_chat_checkpoint(sid)
                    return escalated
            except ToolCancelled:
                raise
            except Exception:
                pass

        if status == "needs_iteration":
            result["remaining_scope"] = (
                "Jev requested or retained another review, but the bounded "
                "answer path stopped without claiming completion")
        elif stop_reason:
            result["remaining_scope"] = stop_reason
        save_chat_turn(sid, result, self.history_dir)
        self._clear_chat_checkpoint(sid)
        emit("hourglass_complete", status=status,
             confidence=result["confidence"].get("observed"))
        return result

