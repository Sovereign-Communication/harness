"""Interruption-safe, Jev-reviewed answer lifecycle.

This mixin owns the bounded answer/review/resume route. Provider and checkpoint
effects are recorded through the shared lifecycle coordinator.
"""
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import script as _script
from .chat_policy import (
    DEFAULT_CHAT_SYSTEM_PROMPT, DEFAULT_RUN_WALL_SECONDS, TRUNCATION_NOTICE,
    is_research_question,
    _SCRIPT_CAPABILITY_NOTE,
    _DEFAULT_HOURGLASS_CONFIDENCE,
    _MAX_HOURGLASS_ANSWER_ROUNDS,
    _MAX_HOURGLASS_CONTEXT_CHARS,
    _MAX_HOURGLASS_CONTEXT_FILES,
    _MAX_WEB_CONTEXT_CHARS,
    _MAX_WORKSPACE_REVIEW_CHARS,
    _MAX_WORKSPACE_REVIEW_FILES,
    _MAX_WORKSPACE_REVIEW_TOKENS,
    _NO_WEB_DISCLOSURE,
    _WEB_CAPABILITY_NOTE
)
from .chat import (
    assess_output,
    chat,
    extract_content_and_cost,
    looks_truncated,
)
from .condenser import distill_context
from .config import (
    effective_lane_policy,
    resolve_api_key,
    resolve_hourglass,
)
from .errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from .events import emit
from .history import (
    ChatResumePayload,
    clear_chat_checkpoint,
    load_chat_history,
    save_chat_checkpoint,
    save_chat_turn,
)
from .jev_policy import jev_cost_ceiling, policy_for
from .orchestrator import keyword_fallback
from .prompts import CAPABILITY_MARKER
from .provider_errors import ProviderSpendLimitError
from .repo_scope import (
    discover_target_files,
    enumerate_repo_files,
    workspace_review_candidates,
)
from .session import governor_for, ledger_for
from .simple_request import (
    is_comprehensive_workspace_inspection_request,
    is_current_information_request,
    is_workspace_inspection_request,
    relevant_history as simple_relevant_history,
)
from .token_budget import resolve_lane_max_tokens as _resolve_lane_max_tokens
from .web import resolve_fetch_allowlist
from .workspace import resolve_workspace_path


class HourglassLifecycleMixin:
    """The Jev-reviewed answer route and its checkpoint helpers."""

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
        if getattr(self, "script_proposals", False):
            system += "\n\n" + _SCRIPT_CAPABILITY_NOTE
        if web:
            _allowed, _any = resolve_fetch_allowlist(self.settings)
            if _any:
                hosts = "any public website (SSRF-guarded)"
            else:
                hosts = ", ".join(sorted(_allowed)) or "(none configured)"
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
                                 checkpoint: ChatResumePayload) -> None:
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
    def _checkpoint_interruption(checkpoint: ChatResumePayload, exc: Exception,
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
    def _hourglass_context_matches(self, checkpoint: ChatResumePayload) -> bool:
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
            resume_state: Optional[ChatResumePayload] = None) -> Dict[str, Any]:
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
                "max_review_rounds": rounds,
                "confidence_threshold": threshold,
                "usage_unknown": False, "next_round": 1, "rounds": [],
            }
            if isinstance(request_preflight, dict):
                checkpoint["route_preflight"] = request_preflight
            try:
                existing = self.lifecycle.load(sid)
            except HarnessError:
                # A new request supersedes a stale pre-contract checkpoint.
                self.lifecycle.clear(sid)
                existing = None
            if (request_preflight is not None and existing is not None
                    and existing["lane"] == "chat"
                    and existing["status"] in ("running", "interrupted")
                    and existing["original_request"] == prompt):
                lifecycle_state = existing
                if lifecycle_state["review"]["rounds"] == 0:
                    lifecycle_state["review"]["max_rounds"] = rounds
            else:
                lifecycle_state = self.lifecycle.begin(
                    "chat", sid, prompt, stage="route",
                    max_review_rounds=rounds,
                    data={"route": "hourglass"})
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
        route_unknown_reason = None
        if not isinstance(route_preflight, dict):
            try:
                def evaluate_route_once():
                    route_evaluation = policy.evaluate_request_workflow(
                        {"request": prompt}, task_id=sid,
                        max_input_tokens=512)
                    if (not isinstance(route_evaluation, (tuple, list))
                            or len(route_evaluation) != 2
                            or not isinstance(route_evaluation[1], dict)
                            or not hasattr(route_evaluation[0], "answers")):
                        return {
                            "source": "jev_request_workflow_unusable",
                            "choice": None, "native": False, "cost": 0.0,
                        }
                    route_result, route_structural = route_evaluation
                    route_answer = (route_result.answers.get("workflow_tier")
                                    if isinstance(route_result.answers, dict)
                                    else None)
                    route_cost = route_structural.get("cost")
                    return {
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

                route_preflight = self.lifecycle.provider_call(
                    lifecycle_state,
                    lifecycle_state["request_id"] + ":chat-route-review",
                    evaluate_route_once,
                    cost=lambda value: float(value.get("cost") or 0.0),
                    retry_unknown=True)
            except (ToolCancelled, ProviderUsageUnknown) as exc:
                self._checkpoint_interruption(checkpoint, exc, total_cost)
                self._persist_chat_checkpoint(sid, checkpoint)
                raise
            except HarnessError as exc:
                if "unknown completion" not in str(exc):
                    raise
                route_preflight = {
                    "source": "jev_request_workflow_unknown",
                    "choice": None, "native": False, "cost": 0.0,
                }
                route_unknown_reason = str(exc)
                checkpoint["unknown_liability"] = lifecycle_state["usage"][
                    "unknown_liability"]
                checkpoint["usage_unknown"] = bool(
                    checkpoint["unknown_liability"] or any(
                        call.get("status") == "unknown"
                        for call in lifecycle_state["usage"][
                            "provider_calls"].values()))
            if route_preflight.get("source") == "jev_request_workflow_unusable":
                emit("request_route_unusable",
                     reason_code="invalid_judgment_shape")
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
            self._finish_chat_request(sid, result)
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
        jev_structural = None
        previous_envelopes = [
            entry.get("jev") for entry in history
            if isinstance(entry, dict) and isinstance(entry.get("jev"), dict)]
        last_envelope = previous_envelopes[-1] if previous_envelopes else None
        best_envelope = max(
            (item for item in previous_envelopes if item.get("native")),
            key=lambda item: (item.get("confidence") or {}).get("observed") or 0.0,
            default=None)
        stop_reason = route_unknown_reason
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
                    attempt = self.lifecycle.provider_call(
                        lifecycle_state,
                        lifecycle_state["request_id"] +
                        f":chat-answer:{round_no}",
                        lambda: self._hourglass_answer_once(
                            prompt, context, prior=prior,
                            model_offset=round_no - 1,
                            cancel_check=cancel_check, governor=gov,
                            api_key=api_key, past_turns=past_turns,
                            web=web, web_sources=web_sources,
                            max_tokens=max_tokens,
                            reasoning_effort=reasoning_effort,
                            token_budget=token_budget),
                        cost=lambda value: float(value.get("cost") or 0.0),
                        retry_unknown=True)
                except (ToolCancelled, ProviderUsageUnknown) as exc:
                    self._checkpoint_interruption(checkpoint, exc, total_cost)
                    self._persist_chat_checkpoint(sid, checkpoint)
                    raise
                except HarnessError as exc:
                    if "unknown completion" not in str(exc):
                        raise
                    checkpoint["unknown_liability"] = lifecycle_state["usage"][
                        "unknown_liability"]
                    checkpoint["usage_unknown"] = bool(
                        checkpoint["unknown_liability"] or any(
                            call.get("status") == "unknown"
                            for call in lifecycle_state["usage"][
                                "provider_calls"].values()))
                    status = "deferred"
                    stop_reason = str(exc)
                    break
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
            # A marked script proposal is a consent gate, not a candidate
            # answer: hand it back before Jev reviews or another round runs.
            if getattr(self, "script_proposals", False) and self.root_dir is not None:
                draft = {"response": answer, "model": model,
                         "cost": round(total_cost, 6)}
                proposal = _script.proposal_turn(
                    prompt, draft, root=self.root_dir,
                    policy=_script.resolve_script_policy(self.settings),
                    task_id=sid)
                answer = draft["response"]
                if proposal is not None:
                    checkpoint["known_cost"] = round(total_cost, 8)
                    self._persist_chat_checkpoint(sid, checkpoint)
                    save_chat_turn(sid, proposal, self.history_dir)
                    self._finish_chat_request(sid, proposal)
                    emit("hourglass_complete", status="awaiting_consent",
                         confidence=None)
                    return proposal
            try:
                jev_call_id = (lifecycle_state["request_id"] +
                               f":chat-review:{round_no}")

                def evaluate_answer_once():
                    result, structural = policy.evaluate_answer(
                        prompt, answer, context, site="answer", task_id=sid)
                    envelope = self._jev_answer_envelope(
                        result, structural, threshold)
                    return {"envelope": envelope,
                            "structural": dict(structural or {})}

                reviewed = self.lifecycle.provider_call(
                    lifecycle_state, jev_call_id, evaluate_answer_once,
                    cost=lambda value: float(
                        value.get("structural", {}).get("cost") or 0.0),
                    retry_unknown=True)
                envelope = reviewed["envelope"]
                jev_structural = reviewed["structural"]
            except (ToolCancelled, ProviderUsageUnknown) as exc:
                self._checkpoint_interruption(checkpoint, exc, total_cost)
                self._persist_chat_checkpoint(sid, checkpoint)
                raise
            except HarnessError as exc:
                if "unknown completion" not in str(exc):
                    raise
                checkpoint["unknown_liability"] = lifecycle_state["usage"][
                    "unknown_liability"]
                checkpoint["usage_unknown"] = bool(
                    checkpoint["unknown_liability"] or any(
                        call.get("status") == "unknown"
                        for call in lifecycle_state["usage"][
                            "provider_calls"].values()))
                status = "deferred"
                stop_reason = str(exc)
                break
            total_cost += float(jev_structural.get("cost") or 0.0)
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
            checkpoint["known_cost"] = round(total_cost, 8)
            checkpoint["rounds"] = history
            checkpoint["last_jev"] = envelope
            checkpoint["answer"] = answer
            checkpoint["answer_model"] = model
            checkpoint["answer_cost"] = float(attempt.get("cost") or 0.0)
            checkpoint["next_round"] = round_no
            checkpoint["stage"] = "jev_review"
            transition = self.lifecycle.review(
                lifecycle_state, jev_call_id, {
                    "verdict": ("pass" if envelope["native"]
                                and isinstance(observed, (int, float))
                                and float(observed) >= threshold
                                and not jev_structural.get("iteration_required")
                                and not jev_structural.get("plan_required")
                                else envelope.get("verdict", "fail")),
                    "native": bool(envelope.get("native")),
                    "supported": (envelope.get("confidence") or {}).get("observed"),
                    "confidence": envelope.get("supported"),
                    "iteration_required": bool(
                        jev_structural.get("iteration_required")),
                    "plan_required": bool(jev_structural.get("plan_required")),
                    "cost": jev_structural.get("cost", 0.0),
                }, confidence_threshold=threshold, final=True)
            self._persist_chat_checkpoint(sid, checkpoint)
            emit("answer_judged", round=round_no, model=model,
                 native=envelope["native"],
                 confidence=envelope["confidence"]["observed"],
                 confidence_threshold=threshold,
                 iteration_required=bool(jev_structural.get("iteration_required")),
                 status=transition.decision)
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
            if transition.terminal_pass:
                status = "ok"
                break
            if not transition.should_continue:
                status = "needs_iteration"
                stop_reason = (
                    "Jev review did not authorize another bounded answer attempt")
                break
            prior = {
                "answer": answer,
                "feedback": "Jev says another answer attempt is needed: "
                            + ("; ".join(envelope["reasons"])
                               or "improve sufficiency against the retained context"),
            }
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
            "response": (answer or "The answer provider did not return a recoverable response."),
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
        self._finish_chat_request(sid, result, candidate=answer)
        emit("hourglass_complete", status=status,
             confidence=result["confidence"].get("observed"))
        return result
