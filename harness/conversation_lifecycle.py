"""Conversational fallback generation and web evidence handling."""

import re
from typing import Any, Callable, Dict, List, Optional

from .chat import assess_output, chat, extract_content_and_cost, looks_truncated
from .chat_policy import (
    DEFAULT_CHAT_SYSTEM_PROMPT,
    _MAX_WEB_CONTEXT_CHARS,
    _MAX_WEB_SOURCES,
    _NO_WEB_DISCLOSURE,
    _WEB_CAPABILITY_NOTE,
)
from .config import Settings, effective_lane_policy
from .errors import HarnessError, ToolCancelled
from .events import emit
from .history import load_chat_history, save_chat_turn
from .lifecycle_types import LifecycleState
from .prompts import CAPABILITY_MARKER
from .results import _http_error
from .session import governor_for, ledger_for
from .simple_request import relevant_history as simple_relevant_history
from .token_budget import resolve_lane_max_tokens as _resolve_lane_max_tokens
from .web import gather_web_context, named_target_hosts, resolve_fetch_allowlist


class ConversationLifecycleMixin:
    """Own one-shot conversational fallback behavior."""

    _REFUSAL_RE = re.compile(
        r"\b(?:i can(?:'t|not)|i'?m unable|unable to|i have no access|"
        r"i (?:do not|don't) have (?:access|tools|internet|a browser|file|compute))\b",
        re.IGNORECASE,
    )

    def _handle_conversation(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
        web: bool = False,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        token_budget: Optional[Any] = None,
        lifecycle_state: Optional[LifecycleState] = None,
    ) -> Dict[str, Any]:
        # Handle informational or technical questions with conversational routing
        api_key, gov = governor_for(self.settings)
        target_tokens, lane_effort = effective_lane_policy(
            "chat", max_tokens=max_tokens, reasoning_effort=reasoning_effort
        )

        system_prompt = DEFAULT_CHAT_SYSTEM_PROMPT
        web_sources: List[Dict[str, Any]] = []
        if web:
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            named_hosts = named_target_hosts(prompt)
            try:
                web_sources = self._gather_web_context(prompt, named_hosts)
            except ToolCancelled:
                raise
            except Exception as e:  # web tools must never kill the chat lane
                web_sources = [{"kind": "web", "ok": False, "note": f"web tools error: {e}"}]
            _allowed, _any = resolve_fetch_allowlist(self.settings)
            if _any:
                hosts = "any public website (SSRF-guarded)"
            else:
                hosts = ", ".join(sorted(_allowed)) or "(none configured)"
            system_prompt += "\n\n" + _WEB_CAPABILITY_NOTE.format(hosts=hosts)
            context_lines = []
            for s in web_sources:
                if s.get("ok"):
                    body = (s.get("text") or "")[:_MAX_WEB_CONTEXT_CHARS]
                    context_lines.append(
                        f"SOURCE ({s['kind']}): {s.get('title') or ''} {s['url']}\n{body}"
                    )
                else:
                    context_lines.append(
                        f"SOURCE ({s['kind']}) FAILED: {s.get('url') or ''} {s.get('note')}"
                    )
            if context_lines:
                system_prompt += (
                    "\n\n[Web tool results for this turn -- cite only these; do not invent others]\n"
                    + "\n\n".join(context_lines)
                )
        else:
            system_prompt += "\n\n" + _NO_WEB_DISCLOSURE

        messages = [{"role": "system", "content": system_prompt}]
        past_turns = simple_relevant_history(
            prompt, load_chat_history(session_id, self.history_dir), max_turns=3, max_chars=2400
        )
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
        if lifecycle_state is not None:
            usage = lifecycle_state["usage"]
            known_cost = max(0.0, float(usage.get("known_cost") or 0.0))
            unknown_liability = max(
                0.0, float(usage.get("unknown_liability") or 0.0))
            if known_cost:
                gov.record_actual(
                    known_cost, "chat-continuation", lane="answer")
            if unknown_liability:
                gov.retain_unknown(
                    unknown_liability, "cancelled-chat-provider-call")
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
                gov, model, prompt, target_tokens, token_budget=token_budget
            )
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
                    content, finish_reason, cost, is_byok = extract_content_and_cost(resp)
                    if is_byok:
                        gov.record_byok(model)
                        note = (
                            f"response for {model} was BYOK-routed; "
                            "spend is not tracked on this key"
                        )
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
                            usable, why = (
                                False,
                                ("response cut off mid-body (unbalanced code fence or brackets)"),
                            )
                        if not usable:
                            note = why
                            if (content or "").strip() and (
                                best_truncated is None or len(content) > len(best_truncated[1])
                            ):
                                best_truncated = (model, content, cost)
            if note is None:
                answered = (model, content or "", cost)
                break
            attempts.append(f"{model}: {note}")
            emit("rotation", model=model, reason="chat_ladder_advance", note=note)
        if answered is None and best_truncated is not None:
            # Every rung failed, but one produced substantive cut-off
            # content: defer honestly, keeping the content. Rotating more
            # cannot finish a body that exceeds the output cap.
            model, response_text, cost = best_truncated
            defer_reason = (
                f"response truncated at the token cap on every "
                f"ladder model (max_tokens={target_tokens})"
            )
            marker_defer = False
            truncation_defer = True
        elif answered is None:
            raise HarnessError("chat failed on every ladder model: " + "; ".join(attempts))
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
            defer_reason = (
                " ".join(first_line.split())[:200]
                or "request exceeds the conversation lane's capability"
            )
            # A bare marker (no prose before it) still owes the user visible
            # text in the saved turn; the UI banner carries the same reason.
            response_text = head.strip() or f"Deferred: {defer_reason}"
        elif defer_reason is None:
            did_work = bool(web_sources and any(s.get("ok") for s in web_sources))
            if not did_work:
                defer_reason = self._refusal_reason(response_text)
        if defer_reason is not None:
            next_step = (
                'route to the plan lane: harness plan --goal "..." '
                "--execute (or the MCP plan_and_execute tool)"
            )
            if best_truncated is not None and answered is None:
                next_step = (
                    'say "continue" to keep this going in the chat '
                    "lane, or route long-generation work to the plan "
                    'lane: harness plan --goal "..." --execute '
                    "(it has multi-round continuation)"
                )
            elif not marker_defer:
                next_step = (
                    "reframe within this lane (Q&A, small lookups), "
                    "enable web or adjust the fetch allowlist for "
                    "live data, or route real work to the plan lane: "
                    'harness plan --goal "..." --execute'
                )
            ledger_for(self.settings).append(
                "model_result",
                task_id=session_id or "(chat)",
                event_note="chat",
                status="deferred",
                category="capability",
                model=model,
                reason=defer_reason,
                cost=round(cost, 6),
            )
            # AUTO-ESCALATION: a capability defer with the paid key armed does
            # not stop at a handoff note -- the request routes itself into the
            # hourglass plan lane (frontier-planned DAG, governed apply with
            # paid escalation rungs). Truncation defers keep their "continue"
            # resume instead: the content already exists, it needs more room.
            if not truncation_defer and self._auto_escalation_armed():
                emit("chat_escalated", reason=defer_reason, target="plan_lane")
                try:
                    return self._handle_edit(
                        prompt,
                        session_id,
                        auto_apply=True,
                        cancel_check=cancel_check,
                        escalation_note=defer_reason,
                    )
                except HarnessError as e:
                    # The handoff failed (no routable target files, plan
                    # refusal, ...): the honest defer stands with the failure
                    # recorded -- never a silent swallow.
                    response_text += f"\n\n[auto-escalation to the plan lane failed: {e}]"

        result = {
            "status": "deferred" if defer_reason else "ok",
            "intent": "conversation",
            "prompt": prompt,
            "response": response_text,
            "model": model,
            "cost": round(cost, 6),
            **({"truncated": True} if truncation_defer else {}),
            **(
                {
                    "defer_reason": defer_reason,
                    # The resume hint a deferral owes the operator (render.py's
                    # contract): what to do instead of this lane's refusal.
                    "next_step": next_step,
                }
                if defer_reason
                else {}
            ),
            # Honest provenance: did this turn actually retrieve web evidence?
            "web_used": bool(web_sources and any(s.get("ok") for s in web_sources)),
            "web_sources": [
                {k: s[k] for k in ("kind", "ok", "url") if k in s} for s in web_sources
            ],
        }

        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="conversation", cost=cost, model=model)
        return result

    def _gather_web_context(self, prompt: str,
                            named_hosts=None) -> List[Dict[str, Any]]:
        # Web owns the network seams; pass those module functions through so
        # policy and hermetic test patches remain at the single owner.
        import harness.web as _web

        allowed, any_public = resolve_fetch_allowlist(self.settings)
        return gather_web_context(
            prompt,
            allowed_hosts=allowed,
            allow_any_public=any_public,
            named_hosts=named_hosts or named_target_hosts(prompt),
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
