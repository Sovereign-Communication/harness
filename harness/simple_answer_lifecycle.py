"""Simple-answer generation, review, and continuation behavior."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .chat_policy import is_research_question
from .chat import governed_completion
from .config import effective_lane_policy
from .errors import HarnessError, ToolCancelled
from .events import cancellation_context, emit
from .history import ChatResumePayload, load_chat_history, save_chat_turn
from .lifecycle import review_probability
from .jev_policy import policy_for
from .session import governor_for, ledger_for
from .simple_request import (
    answer_clock as simple_clock_answer,
    answer_prompt as simple_answer_prompt,
    is_current_information_request,
    is_simple_answer_request,
    relevant_history as simple_relevant_history,
    resolve_answer_retry as resolve_simple_answer_retry,
)
from .token_budget import resolve_lane_max_tokens as _resolve_lane_max_tokens


class SimpleAnswerLifecycleMixin:
    """Owns the short-answer lane; routing and checkpoint I/O stay shared."""

    def run_simple_request(
            self, prompt: str, *, session_id: Optional[str] = None,
            cancel_check=None, max_tokens: Optional[int] = None,
            reasoning_effort: Optional[str] = None,
            token_budget: Optional[Any] = None,
            max_wall_seconds: float = 60.0,
            resume_state: Optional[ChatResumePayload] = None) -> Dict[str, Any]:
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
                review_threshold = float(getattr(
                    self.settings, "min_confidence", 0.70) or 0.70)
                self.lifecycle.begin(
                    "chat", sid, prompt, stage="strategy",
                    max_review_rounds=1,
                    data={"route": "simple-answer"})
                checkpoint = {
                    "route": "simple-answer", "status": "running",
                    "stage": "strategy", "prompt": prompt,
                    "max_review_rounds": 1,
                    "confidence_threshold": review_threshold,
                    "known_cost": 0.0, "unknown_liability": 0.0,
                    "usage_unknown": False,
                }
                self._persist_chat_checkpoint(sid, checkpoint)
            try:
                lifecycle_state = self.lifecycle.load(sid)
            except HarnessError:
                if not resuming:
                    raise
                self.lifecycle.clear(sid)
                lifecycle_state = None
            if resuming and lifecycle_state is None:
                lifecycle_state = self.lifecycle.adopt_chat_payload(sid, checkpoint)
            if lifecycle_state is None or lifecycle_state["lane"] != "chat":
                raise HarnessError("chat request lifecycle could not be restored")
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
            saved_turns = load_chat_history(sid, self.history_dir)
            effective_prompt = resolve_simple_answer_retry(prompt, saved_turns)
            turns = simple_relevant_history(effective_prompt, saved_turns)
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
                    def evaluate_strategy_once():
                        _result, structural = policy.evaluate_simple_strategy(
                            effective_prompt, context_text, task_id=sid)
                        return dict(structural or {})

                    pre = self.lifecycle.provider_call(
                        lifecycle_state,
                        lifecycle_state["request_id"] + ":simple-strategy",
                        evaluate_strategy_once,
                        cost=lambda value: float(value.get("cost") or 0.0),
                        retry_unknown=True)
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
                # The caller may hand this pre-answer route judgment to the
                # hourglass lane; retain the same lifecycle for that handoff.
                checkpoint.update({"stage": "route", "status": "running"})
                self._persist_chat_checkpoint(sid, checkpoint)
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
                generation_prompt = simple_answer_prompt(effective_prompt, turns)
                allocated = _resolve_lane_max_tokens(
                    gov, model, generation_prompt, target_tokens,
                    token_budget=token_budget)
                if allocated < 1:
                    raise HarnessError("shared budget leaves no answer tokens")
                generation_attempts = max(1, generation_attempts + 1)
                with cancellation_context(combined_cancel):
                    completion = self.lifecycle.provider_call(
                        lifecycle_state,
                        lifecycle_state["request_id"] + ":simple-answer",
                        lambda: governed_completion(
                            self.transport, api_key, gov, model,
                            generation_prompt, allocated,
                            label="simple-answer",
                            reasoning_effort=lane_effort,
                            preserve_billed_overrun=True),
                        cost=lambda value: float(value.get("cost") or 0.0),
                        retry_unknown=True)
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
            budget_overrun = (completion or {}).get("budget_overrun")
            if budget_overrun:
                post_skipped = ("answer was returned after provider cost"
                                " exceeded the ceiling")
            elif time.monotonic() < deadline:
                with cancellation_context(combined_cancel):
                    def evaluate_post_once():
                        post_result, structural = policy.evaluate_answer(
                            effective_prompt, answer, context_text,
                            site="simple-answer-post", task_id=sid)
                        saved = dict(structural or {})
                        verdict = saved.get(
                            "verdict", getattr(post_result, "verdict", "fail"))
                        saved.update({
                            "verdict": (verdict if verdict in ("pass", "fail")
                                        else "fail"),
                            "native": bool(saved.get(
                                "native", not bool(getattr(
                                    post_result, "is_fallback", True)))),
                            "answer_sufficient": review_probability(saved.get(
                                "answer_sufficient", getattr(
                                    post_result, "supported", None))),
                            "confidence": review_probability(getattr(
                                post_result, "confidence", None)),
                        })
                        return saved

                    post = self.lifecycle.provider_call(
                        lifecycle_state,
                        lifecycle_state["request_id"] + ":simple-review",
                        evaluate_post_once,
                        cost=lambda value: float(value.get("cost") or 0.0),
                        retry_unknown=True)
                post_cost = float(post.get("cost") or 0.0)
                known_cost_total += post_cost
            else:
                post_skipped = "deadline exhausted after answer generation"

            refusal = self._refusal_reason(answer)
            status = "deferred" if refusal else "ok"
            total_cost = known_cost_total
            review_available = bool(post and post.get("native"))
            transition = None
            if post is not None:
                transition = self.lifecycle.review(
                    lifecycle_state,
                    lifecycle_state["request_id"] + ":simple-review", {
                        "verdict": post.get("verdict", "fail"),
                        "native": bool(post.get("native")),
                        "supported": post.get("answer_sufficient"),
                        "confidence": post.get("confidence"),
                        "iteration_required": bool(
                            post.get("iteration_required")),
                        "plan_required": bool(post.get("plan_required")),
                        "cost": post.get("cost", 0.0),
                    }, confidence_threshold=float(getattr(
                        self.settings, "min_confidence", 0.70) or 0.70),
                    final=True)
            review_passed = bool(transition and transition.terminal_pass)
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
                    **({"resolved_prompt": effective_prompt}
                       if effective_prompt != prompt else {}),
                    **({"budget_overrun": budget_overrun}
                       if budget_overrun else {}),
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
                self._finish_chat_request(sid, result, candidate=answer)
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
                    self._finish_chat_request(sid, result, candidate=answer)
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
                self._finish_chat_request(sid, result)
            return result
        except HarnessError as exc:
            reason = str(exc)
            known_cost = float(getattr(exc, "known_cost", 0.0) or 0.0)
            self._checkpoint_interruption(checkpoint, exc, known_cost_total)
            checkpoint["generation_calls"] = generation_attempts
            usage_unknown = bool(
                getattr(exc, "usage_unknown", False)
                or checkpoint.get("usage_unknown"))
            unknown_reserve = max(
                float(getattr(exc, "reserved_cost", 0.0) or 0.0),
                float(checkpoint.get("unknown_liability") or 0.0))
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
                    "unknown_cost_reserve": round(unknown_reserve, 6)}
                   if usage_unknown else {}),
            }
            if "unknown completion" in reason:
                result["response"] = (
                    "The saved provider call has no recoverable result, so I did "
                    "not replay it. Restate the request to start a new attempt.")
                result["next_step"] = "restate the request as a new task"
            save_chat_turn(sid, result, self.history_dir)
            if "unknown completion" in reason:
                self._finish_chat_request(sid, result)
            return result
