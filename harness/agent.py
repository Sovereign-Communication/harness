# Autonomous agent orchestrator (#PR-Chat-1)
# Drives natural language prompts to conclusion automatically with zero UI clutter.
import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ._http import HttpTransport
from .chat import (governed_text)
from .condenser import distill_context, condense_error_log
from .config import (
    Settings, load_settings, resolve_api_key,
    resolve_hourglass,
)
from .dag import TaskDAG, DAGNode, node_apply_kwargs
from .prompts import MAX_FILE_LINES
from .errors import HarnessError, ToolCancelled
from .events import emit
from .orchestrator import drive, keyword_fallback, triage_files
from .escalation import escalation_evidence, escalation_evidence_fields
from .executor import PlanExecutor
from .history import (
    load_chat_checkpoint, load_chat_history,
    save_chat_turn,
)
from .simple_request import (
    is_simple_answer_request,
    is_supplied_text_rewrite,
    is_workspace_inspection_request,
)
from .repo_scope import (
    discover_target_files,
    discover_verification_gate,
    enumerate_repo_files,
)
from .workspace import require_workspace
from .results import SUCCESS_STATUSES, model_envelope
from .session import (
    apply_session, attest_model_for, governor_for, jev_face_governor,
    jev_for, ledger_for,
)
from .jev_policy import (
    JevPolicy, aggregate_structural, policy_for,
)
from .jev_packs import (
    HOURGLASS_STAGE_SITE,
    declared_restart_targets,
    validate_restart_request,
)
from .token_budget import budget_from_settings
from .waist import (
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STATE_COMPLETED,
    STATE_PENDING,
    compose_arguments,
    compose_plan,
    resolve_scout_ladder,
)
from .chat_lifecycle import (
    ChatLifecycleMixin,
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_RUN_WALL_SECONDS,
    TRUNCATION_NOTICE,
    is_research_question,
)

# Consumers import history/repo_scope/web helpers from their owners
# (harness.history, harness.repo_scope, harness.web), not from this module.
__all__ = [
    "AutonomousAgent",
    "CONVERSATION_STARTERS",
    "DEFAULT_CHAT_SYSTEM_PROMPT",
    "DEFAULT_RUN_WALL_SECONDS",
    "EXECUTION_PHRASES",
    "MUTATION_KEYWORDS",
    "classify_prompt_intent",
    "discover_target_files",
    "discover_verification_gate",
    "enumerate_repo_files",
    "load_chat_history",
    "save_chat_turn",
    "TRUNCATION_NOTICE",
    "is_research_question",
]






# Appended to the system prompt when a run did NOT opt into web tools, so the
# model cannot roleplay a lookup it never performed (the Riemann failure mode).

# Replaces the no-web note when a run DID opt in: the model must know what is
# actually attached (and what fetch will refuse) so it answers "can you access
# X?" truthfully instead of denying the capability or inventing a lookup.


MUTATION_KEYWORDS = frozenset({
    "fix", "implement", "add", "refactor", "update", "change", "write",
    "modify", "create", "delete", "remove", "clean", "patch", "repair",
    "correct", "optimize", "rewrite", "replace", "build", "execute", "apply",
})

# Request-lifecycle #200: action verbs and uptime phrasings that mark a
# site-availability check. Question form alone must not outrank an explicit
# action verb plus a URL/host target.
_SITE_CHECK_VERBS = ("check", "verify", "test", "confirm", "look up",
                      "lookup", "see")
_SITE_UP_PHRASES = ("is up", "is down", "up right now", "still up",
                     "loads", "loading", "reachable", "resolves",
                     "online", "responding")


def is_site_check_prompt(prompt: str) -> bool:
    """True when a prompt asks whether a site/host is up (issue #200).

    Requires a URL/host target plus an action verb or an uptime phrasing.
    Genuine questions ("what is...?", "explain...") without a target, and
    repo-centered directives (code extensions, mutation verbs on code,
    plan-execution phrases, audit/driver keywords) never match."""
    from .web import extract_site_target as _extract_target
    cleaned = (prompt or "").strip().lower()
    if not cleaned:
        return False
    if any(p in cleaned for p in EXECUTION_PHRASES):
        return False
    if _extract_target(prompt) is None:
        return False
    if re.search(r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt):
        return False
    words = re.findall(r"\b[a-z_0-9-]+\b", cleaned)
    if any(w in MUTATION_KEYWORDS for w in words):
        return False
    if any(v in cleaned for v in _SITE_CHECK_VERBS):
        return True
    if any(p in cleaned for p in _SITE_UP_PHRASES):
        return True
    # Question form with the verb and the state split around the host:
    # "is X up?", "is X down right now?", "does X resolve?".
    return bool(re.search(
        r"\b(is|are|does|did|has)\b.*\b(up|down|loading|loads|reachable|resolves|online|responding|working)\b",
        cleaned))

EXECUTION_PHRASES = frozenset({
    "execute the plan", "execute plan", "run the plan", "run plan",
    "apply the plan", "apply plan", "test the plan", "test plan",
    "see if it works", "test if it works", "check if it works",
    "run the test", "run the tests", "run tests", "run test",
    "execute tests", "execute test", "execute it", "run it", "apply it",
    "proceed with plan", "proceed with the plan", "execute this", "run this",
    "test this", "verify the plan", "test the implementation",
    "execute implementation", "run the implementation",
    "execute the code", "run the code", "test the code",
})

CONVERSATION_STARTERS = frozenset({
    "what", "how", "why", "explain", "describe", "tell", "can", "is", "are",
    "does", "who", "when", "where", "list", "show", "help",
})


# A run that asks a model to think, judge and re-answer must be bounded in
# wall time as well as rounds. The dogfood ran a single "find news" prompt for
# about six minutes with no answer: every round was individually legal, and
# nothing stopped the sequence. This is the ceiling that ends it.

# Asking what the world currently says is a research question, not an edit.
# The dogfood prompt ("can you find news about ...") was routed to the
# conversation lane but never gathered sources, so the run iterated on a
# question it had no evidence for.
_LITERAL_GREETING_RE = re.compile(
    r"^(?:test\s*[-:,]\s*)?(?:please\s+)?(?:just\s+)?"
    r"(?:say|reply(?:\s+with)?|respond(?:\s+with)?)\s+"
    r"(?:hello|hi|hey)(?:\s+and\s+(?:stop|nothing else))?[.!]?$",
    re.IGNORECASE)


def is_literal_greeting_request(prompt: str) -> bool:
    """Recognize a command whose complete requested output is a greeting."""
    cleaned = " ".join((prompt or "").split())
    return bool(_LITERAL_GREETING_RE.fullmatch(cleaned))




def classify_prompt_intent(prompt: str) -> str:
    # Classify a natural language prompt into conversation, edit, or audit
    cleaned = prompt.strip().lower()
    words = re.findall(r"\b[a-z_0-9-]+\b", cleaned)
    first_word = words[0] if words else ""

    if any(w in cleaned for w in ("verify chain", "audit ledger", "ledger status", "check ledger")):
        return "audit"

    if any(w in cleaned for w in ("driver task", "drive request", "run driver", "machine drive", "perceive dom", "perceive cli", "driver perception", "perception step")):
        return "driver"

    if any(w in cleaned for w in ("provision host", "probe host", "plan provision", "setup host", "provision environment", "host provisioning")):
        return "provision"

    # Execution directives: commands to execute/test a plan, code, or tests on local hardware
    if any(p in cleaned for p in EXECUTION_PHRASES) or any(
        re.search(rf"\b{action}\b.*?\b(?:plan|code|test|tests|implementation|script)\b", cleaned)
        for action in ("execute", "run", "apply")
    ):
        return "edit"

    # Check for explicit file extension occurrences
    has_file_ext = bool(re.search(r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt))

    # Request-lifecycle #200: an action verb plus a URL/host target is a
    # site-availability check, even in question form. Ordinary questions
    # without a target keep the conversation lane below.
    if is_site_check_prompt(prompt):
        return "simple-action"

    # Prompts asking conversational questions (or ending with ?) take precedence unless explicit files/paths are given
    if first_word in CONVERSATION_STARTERS or cleaned.endswith("?"):
        if not has_file_ext and not any(w in cleaned for w in ("refactor ", "implement ", "fix bug ", "add test", "execute ", "run ")):
            return "conversation"

    # Transforming prose supplied in the prompt is conversational work.  A
    # named source file (or a second repo mutation verb) remains an edit.
    if is_supplied_text_rewrite(prompt):
        return "conversation"

    has_mutation_verb = any(w in MUTATION_KEYWORDS for w in words)

    if has_mutation_verb or has_file_ext:
        return "edit"

    return "conversation"




class AutonomousAgent(ChatLifecycleMixin):
    # Self-orchestrating agent driving natural language prompts to conclusion
    def __init__(
        self,
        settings: Optional[Settings] = None,
        root_dir: Optional[Path] = None,
        history_dir: Optional[Path] = None,
        transport: Optional[HttpTransport] = None,
    ):
        self.settings = settings or load_settings()
        self.root_dir = Path(root_dir).resolve() if root_dir is not None else None
        self.history_dir = history_dir
        self.transport = transport or HttpTransport()
        from .lifecycle_guards import RateCircuit
        self._rate_circuit = RateCircuit()

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
        resume_state = None
        if self._is_continue_command(prompt):
            try:
                resume_state = load_chat_checkpoint(sid, self.history_dir)
            except HarnessError:
                emit("chat_resume_unavailable", session_id=sid,
                     reason_code="checkpoint_invalid")
                return {
                    "status": "deferred", "intent": "conversation",
                    "workflow_tier": "continuation", "prompt": prompt,
                    "response": ("The saved continuation state is unreadable. "
                                 "Paste or restate the task to start again."),
                    "model": None, "cost": 0.0,
                    "defer_reason": "invalid chat checkpoint",
                    "next_step": "restate the original task",
                }
            if resume_state is not None:
                prompt = resume_state["prompt"]
                emit("chat_resume", session_id=sid,
                     route=resume_state.get("route"),
                     stage=resume_state.get("stage"))
                if resume_state.get("route") == "hourglass":
                    return self.run_hourglass_request(
                        prompt, session_id=sid, cancel_check=cancel_check,
                        web=bool(resume_state.get("web_requested")),
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                        token_budget=token_budget,
                        resume_state=resume_state)
                if resume_state.get("route") == "simple-answer":
                    return self.run_simple_request(
                        prompt, session_id=sid, cancel_check=cancel_check,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                        token_budget=token_budget,
                        resume_state=resume_state)
            emit("chat_resume_unavailable", session_id=sid)
            return {
                "status": "deferred", "intent": "conversation",
                "workflow_tier": "continuation", "prompt": prompt,
                "response": ("There is no interrupted request saved for this "
                             "chat. Paste or restate the task to continue."),
                "model": None, "cost": 0.0,
                "defer_reason": "no resumable chat checkpoint",
                "next_step": "restate the original task",
            }
        else:
            # A new request supersedes the single pending conversational
            # checkpoint for this session; only an explicit continue resumes it.
            self._clear_chat_checkpoint(sid)
        emit("chat_turn_start", prompt=prompt, session_id=sid)

        if is_literal_greeting_request(prompt):
            result = {
                "status": "ok", "intent": "conversation",
                "workflow_tier": "answer", "prompt": prompt,
                "answer": "Hello.", "response": "Hello.",
                "model": None, "cost": 0.0,
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
            needs_workspace = (intent == "provision"
                               or (intent == "edit" and auto_apply))
            if needs_workspace and self.root_dir is None:
                raise HarnessError(
                    "Select a workspace folder before requesting file or provisioning work")
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            if is_workspace_inspection_request(prompt):
                if self.root_dir is None:
                    return self._workspace_context_deferred(prompt, sid)
                return self.run_hourglass_request(
                    prompt, session_id=sid, cancel_check=cancel_check,
                    web=False, max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
            if intent == "driver":
                return self._handle_driver_task(prompt, sid, cancel_check=cancel_check)
            if intent == "audit":
                return self._handle_audit(prompt, sid, cancel_check=cancel_check)
            if intent == "provision":
                return self._handle_provision(prompt, sid, cancel_check=cancel_check)
            if intent == "simple-action":
                return self._run_simple_action_lifecycle(
                    prompt, session_id=sid, cancel_check=cancel_check)
            if intent == "edit" and auto_apply:
                emit("chat_escalated", reason="edit-intent in auto mode",
                     target="plan_lane")
                return self._handle_edit(prompt, sid, auto_apply, cancel_check,
                                         use_jev_completion=self._live_jev_available())
            simple_preflight = None
            if is_simple_answer_request(prompt, intent=intent):
                res = self.run_simple_request(
                    prompt, session_id=sid, cancel_check=cancel_check,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
                if res.get("status") == "ok" or (res.get("status") == "deferred" and res.get("simple_request", {}).get("generation_calls", 0) > 0):
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
                    prompt, session_id=sid, cancel_check=cancel_check, web=web,
                    request_preflight=simple_preflight,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
            return self._handle_conversation(
                prompt, sid, cancel_check,
                web=web,
                max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                token_budget=token_budget)

        intent = classify_prompt_intent(prompt)
        emit("intent_classified", intent=intent, prompt=prompt)
        if intent in ("edit", "provision") and self.root_dir is None:
            raise HarnessError(
                "Select a workspace folder before requesting file or provisioning work")

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        if is_workspace_inspection_request(prompt):
            if self.root_dir is None:
                return self._workspace_context_deferred(prompt, sid)
            return self.run_hourglass_request(
                prompt, session_id=sid, cancel_check=cancel_check,
                web=False, max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                token_budget=token_budget)

        if intent == "driver":
            return self._handle_driver_task(prompt, sid, cancel_check=cancel_check)
        elif intent == "simple-action":
            return self._run_simple_action_lifecycle(
                prompt, session_id=sid, cancel_check=cancel_check)
        elif intent == "provision":
            return self._handle_provision(prompt, sid, cancel_check=cancel_check)
        elif intent == "conversation":
            simple_preflight = None
            if is_simple_answer_request(prompt, intent=intent):
                res = self.run_simple_request(
                    prompt, session_id=sid, cancel_check=cancel_check,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
                if res.get("status") == "ok" or (res.get("status") == "deferred" and res.get("simple_request", {}).get("generation_calls", 0) > 0):
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
                    prompt, session_id=sid, cancel_check=cancel_check,
                    web=web, request_preflight=simple_preflight,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
            return self._handle_conversation(
                prompt, sid, cancel_check,
                web=web,
                max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                token_budget=token_budget)
        elif intent == "audit":
            return self._handle_audit(prompt, sid, cancel_check)
        else:
            return self._handle_edit(prompt, sid, auto_apply, cancel_check,
                                     use_jev_completion=(auto_apply
                                                         and self._live_jev_available()))




    def _run_simple_action_lifecycle(self, prompt: str,
                                     session_id: Optional[str] = None,
                                     cancel_check=None) -> Dict[str, Any]:
        """Use one typed Choice before the probe, then one evidence Noul.

        The deterministic classifier identifies a candidate site-check
        request; Jev selects whether it really belongs in the one-probe
        simple-action tier. Other Jev choices do not trigger an OpenRouter
        or plan escalation from this bounded lane.
        """
        sid = session_id or "default"
        if not self._live_jev_available():
            return self.run_simple_action(
                prompt, session_id=sid, cancel_check=cancel_check)

        from .web import extract_site_target as _extract_target
        from .web import preflight_public_site as _preflight
        target = _extract_target(prompt)
        if target is None:
            return self.run_simple_action(
                prompt, session_id=sid, cancel_check=cancel_check)
        emit("simple_action_start", prompt=prompt, session_id=sid)
        emit("simple_action_preflight_start", target=target)
        try:
            preflight_plan = _preflight(target, cancel_check=cancel_check)
        except HarnessError as exc:
            return self._simple_action_preflight_refusal(
                prompt, sid, target, str(exc))
        emit("simple_action_preflight_complete", target=target)

        policy = jev_for(
            self.settings, transport=self.transport,
            governor=jev_face_governor(self.settings),
            ledger=ledger_for(self.settings, caller="agent"),
            request_timeout=10.0, single_attempt=True)
        emit("simple_action_route_start", session_id=sid)
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        try:
            route_result, _route_structural = policy.evaluate_request_workflow(
                {"request": prompt}, task_id=sid, max_input_tokens=512)
        except ToolCancelled:
            raise
        except Exception as exc:
            emit("simple_action_route_unavailable",
                 error_type=type(exc).__name__)
            result = {
                "status": "deferred", "intent": "simple-action",
                "workflow_tier": "simple-action", "prompt": prompt,
                "response": ("Inconclusive -- Jev could not select the "
                             "site-check workflow; no request was sent."),
                "model": None, "cost": 0.0,
                "defer_reason": "Jev workflow selection was unavailable",
                "next_step": "retry the bounded site-check request",
                "jev": {"workflow_tier": None, "is_fallback": True},
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("simple_action_complete", status="deferred",
                 reason_code="jev_route_unavailable")
            return result

        route_answer = (route_result.answers.get("workflow_tier")
                        if isinstance(route_result.answers, dict) else None)
        choice = (route_answer.get("choice")
                  if isinstance(route_answer, dict) else None)
        if choice not in {
                "answer", "simple-action", "workspace-inspection",
                "web-research", "plan", "clarify"}:
            choice = None
        fallback = bool(getattr(route_result, "is_fallback", True))
        emit("simple_action_route_complete", selected=choice,
             fallback=fallback)
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        if fallback or choice != "simple-action":
            reason = ("Jev did not select the bounded site-check workflow"
                      if not fallback else
                      "Jev did not provide a usable workflow choice")
            result = {
                "status": "deferred", "intent": "simple-action",
                "workflow_tier": "simple-action", "prompt": prompt,
                "response": ("Inconclusive -- " + reason +
                             "; no request was sent."),
                "model": getattr(route_result, "model", None),
                "cost": float(getattr(route_result, "cost", 0.0) or 0.0),
                "defer_reason": reason,
                "next_step": "rephrase as a single current site-availability check",
                "jev": {"workflow_tier": choice,
                        "is_fallback": fallback},
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("simple_action_complete", status="deferred",
                 reason_code="jev_route_not_simple_action")
            return result
        return self.run_simple_action(
            prompt, session_id=sid, cancel_check=cancel_check,
            jev_policy=policy, workflow_choice=choice,
            prior_jev_cost=float(getattr(route_result, "cost", 0.0) or 0.0),
            preflight_plan=preflight_plan, started=True)

    def _simple_action_preflight_refusal(self, prompt, session_id, target,
                                         reason):
        try:
            ledger_for(self.settings, caller="agent").append(
                "simple_action", task_id=session_id, event_note="preflight",
                status="refused", reason=reason, target=target, cost=0.0)
        except Exception:
            pass
        result = {
            "status": "deferred", "intent": "simple-action",
            "workflow_tier": "simple-action", "prompt": prompt,
            "response": f"I can't check that site: {reason}",
            "model": None, "cost": 0.0, "target": target,
            "defer_reason": reason,
            "next_step": "provide a public https URL without credentials",
        }
        save_chat_turn(session_id, result, self.history_dir)
        emit("simple_action_probe_refused", target=target,
             reason_code="preflight_refused")
        emit("simple_action_complete", status="deferred",
             reason_code="preflight_refused")
        return result


    def run_simple_action(self, prompt: str, session_id: Optional[str] = None,
                          cancel_check=None, probe_fn=None,
                          resolve_fn=None, jev_policy=None,
                          workflow_choice=None, prior_jev_cost: float = 0.0,
                          preflight_plan=None, started=False
                          ) -> Dict[str, Any]:
        """Execute one bounded read-only site-availability check (#202/#203).

        Deterministic capability preflight runs before any model call; a
        known mismatch returns an honest terminal result plus a ledger event
        with zero model or decomposition calls. On success exactly one
        bounded HTTPS probe runs -- no repo triage, DAG, or waist work --
        and the verdict comes from observed probe evidence, never from
        search snippets. ``probe_fn``/``resolve_fn`` are hermetic seams."""
        from .web import extract_site_target as _extract_target
        from .web import probe_public_site as _probe
        sid = session_id or "default"
        if not started:
            emit("simple_action_start", prompt=prompt, session_id=sid)
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        target = _extract_target(prompt)
        if target is None:
            result = {
                "status": "deferred", "intent": "simple-action",
                "workflow_tier": "simple-action", "prompt": prompt,
                "response": ("I could not find a site to check in that request. "
                             "Name a URL or host such as https://example.com."),
                "model": None, "cost": 0.0,
                "defer_reason": "no site target in prompt",
                "next_step": "rephrase with a URL or host to check",
            }
            save_chat_turn(sid, result, self.history_dir)
            emit("simple_action_complete", status="deferred",
                 reason_code="no_site_target")
            return result
        emit("simple_action_probe_start", target=target)
        try:
            probe_kwargs = {"cancel_check": cancel_check}
            if probe_fn is not None:
                probe_kwargs.update(resolve_fn=resolve_fn,
                                    connect_fn=probe_fn)
            elif resolve_fn is not None:
                probe_kwargs["resolve_fn"] = resolve_fn
            if preflight_plan is not None:
                probe_kwargs["preflight_plan"] = preflight_plan
            probe = _probe(target, **probe_kwargs)
        except HarnessError as e:
            return self._simple_action_preflight_refusal(
                prompt, sid, target, str(e))
        emit("simple_action_probe_complete", target=target,
             verdict=probe.get("verdict"),
             http_status=probe.get("http_status"),
             latency_s=probe.get("latency_s"))
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        verdict = probe.get("verdict")
        http_status = probe.get("http_status")
        response_received = (
            isinstance(http_status, int) and not isinstance(http_status, bool)
            and 100 <= http_status <= 599)
        latency = probe.get("latency_s")
        if (isinstance(latency, bool)
                or not isinstance(latency, (int, float))
                or latency < 0):
            latency = None
        status_class = (f"{http_status // 100}xx"
                        if response_received else None)
        if response_received:
            if verdict == "denied":
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status}. This confirms endpoint "
                    "reachability; page access was denied.")
            elif verdict == "redirect":
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status} redirect; it is reachable, "
                    "but the destination was not checked.")
            elif verdict in ("server_error", "rate_limited", "http_error"):
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status}; it is reachable but returned "
                    "an HTTP error.")
            else:
                evidence_summary = (
                    f"The requested HTTPS endpoint responded to this probe "
                    f"with HTTP {http_status}; it is reachable.")
        else:
            evidence_summary = (
                f"No HTTP response was received; the probe ended with "
                f"{verdict or 'an unknown transport result'}.")
        probe_evidence = {
            "host": target,
            "probe_outcome": "http_response" if response_received else (
                verdict if verdict in ("timeout", "network") else "no_http_response"),
            "http_status": http_status if response_received else None,
            "http_status_received": response_received,
            "http_status_class": status_class,
            "probe_verdict": verdict,
            "evidence_summary": evidence_summary,
            "latency_s": latency,
        }
        action_ledger = getattr(jev_policy, "ledger", None)
        jev_result = None
        try:
            if action_ledger is None:
                action_ledger = ledger_for(self.settings, caller="agent")
            # This route only resolves the local OpenRouter key for spend
            # accounting; JevEvaluator sends the sole model request to
            # System One. The site check never calls OpenRouter.
            policy = jev_policy or jev_for(
                self.settings, transport=self.transport,
                governor=jev_face_governor(self.settings),
                ledger=action_ledger, request_timeout=10.0,
                single_attempt=True)
            emit("simple_action_jev_start", target=target)
            jev_result, _structural = policy.evaluate_site_reachability(
                probe_evidence, task_id=sid, max_input_tokens=512)
        except Exception as exc:
            emit("simple_action_jev_unavailable",
                 error_type=type(exc).__name__)
        answer = (jev_result.answers.get("http_response_received")
                  if jev_result and isinstance(jev_result.answers, dict)
                  else None)
        probability = answer.get("noul") if isinstance(answer, dict) else None
        if (jev_result is None or jev_result.is_fallback
                or isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not 0.0 <= float(probability) <= 1.0):
            probability = None
        else:
            probability = float(probability)
        emit("simple_action_jev_complete", target=target,
             http_response_received=probability,
             fallback=bool(getattr(jev_result, "is_fallback", True)))

        answer_text = None
        if response_received and probability is not None and probability >= 0.99:
            status = "ok"
            answer_text = "yes"
            response = "Yes."
        elif (not response_received and probability is not None
              and probability <= 0.01):
            status = "ok"
            answer_text = "no"
            response = "No."
        else:
            status = "deferred"
            if response_received:
                reason = (f"HTTPS {http_status} was received, but Jev's "
                          "reachability judgment did not meet the 99% yes threshold")
            else:
                reason = (f"the probe returned no HTTPS status ({verdict or 'unknown'}), "
                          "so it cannot establish that the site is up")
            if probability is None:
                reason += "; Jev did not provide a usable Noul judgment"
            else:
                reason += f" (Jev yes probability: {probability:.3f})"
            response = f"Inconclusive -- {reason}."
        try:
            if action_ledger is None:
                action_ledger = ledger_for(self.settings, caller="agent")
            action_ledger.append(
                "simple_action", task_id=sid, event_note="probe",
                status=status, verdict=verdict, target=target,
                http_status=probe.get("http_status"),
                latency_s=probe.get("latency_s"), jev_noul=probability,
                jev_model=getattr(jev_result, "model", None),
                jev_fallback=bool(getattr(jev_result, "is_fallback", True)),
                cost=0.0)
        except Exception:
            pass
        result = {
            "status": status, "intent": "simple-action",
            "workflow_tier": "simple-action", "prompt": prompt,
            "answer": answer_text, "response": response,
            "model": getattr(jev_result, "model", None),
            "cost": (float(prior_jev_cost or 0.0)
                     + float(getattr(jev_result, "cost", 0.0) or 0.0)),
            "target": target, "probe": probe,
            "jev": {"workflow_tier": workflow_choice,
                    "http_response_received": probability,
                    "threshold_yes": 0.99, "threshold_no": 0.01,
                    "is_fallback": bool(getattr(jev_result,
                                                  "is_fallback", True))},
            **({"defer_reason": response} if status != "ok" else {}),
        }
        save_chat_turn(sid, result, self.history_dir)
        emit("simple_action_complete", status=status, answer=answer_text,
             verdict=verdict)
        return result



    # Free models defer in prose ("I can't do that") far more often than in
    # the marker contract. A refusal-shaped answer with no successful tool
    # evidence IS a deferral: the model handed the work back. The model's own
    # refusal sentence becomes the reason; a caveat inside a turn that DID
    # retrieve evidence is not a deferral.
    _REFUSAL_RE = re.compile(
        r"\b(?:i can(?:'t|not)|i'?m unable|unable to|i have no access|"
        r"i (?:do not|don't) have (?:access|tools|internet|a browser|file|compute))\b",
        re.IGNORECASE)











    def _escalation_authorized(self, prompt: str, marker_defer: bool,
                               status: Optional[str]) -> bool:
        """Whether the hourglass answer lane may escalate to plan (issue #201).

        An explicit execution directive may constrain the route, but
        ``auto_apply=True`` alone never authorizes escalation -- the old
        gate was vacuous because auto_apply defaults to true. Each clause
        is independently testable; capability, scope, consent, and policy
        checks still apply on the plan lane itself."""
        lowered = (prompt or "").lower()
        explicit_directive = any(p in lowered for p in EXECUTION_PHRASES)
        intent = classify_prompt_intent(prompt)
        plan_trigger = (status == "plan_required" and intent == "edit")
        trigger = bool(marker_defer or plan_trigger or explicit_directive)
        return bool(trigger and (self._auto_escalation_armed()
                                 or explicit_directive))

    def _auto_escalation_armed(self) -> bool:
        # Auto-escalation routes a chat defer into paid-capable plan
        # execution: it requires the operator's escalation gate AND a
        # resolved paid key. No key, no auto-escalation -- the honest defer
        # with the manual resume path stands.
        if not self.settings.allow_escalation:
            return False
        return bool(resolve_api_key())

    @staticmethod
    def _classify_prompt_intent(prompt: str) -> str:
        """Expose the router to the chat lifecycle without duplicating policy."""
        return classify_prompt_intent(prompt)


    def _handle_audit(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        # Handle audit and cryptographic ledger verification requests
        ledger = ledger_for(self.settings)
        ok, bad_seq = ledger.verify()
        chain_info = ledger.chain_status()

        if ok:
            msg = f"Cryptographic ledger chain is clean and verified (0 quarantined entries, latest seq {chain_info.get('latest_seq', 0)})."
        else:
            msg = f"Ledger verification failed at record sequence {bad_seq}. Check quarantined entries."

        result = {
            "status": "ok" if ok else "audit_failed",
            "intent": "audit",
            "prompt": prompt,
            "response": msg,
            "chain_status": chain_info,
            "cost": 0.0,
        }

        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="audit", verified=ok)
        return result

    def _handle_provision(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Plan host provisioning setup safely without automatic execution."""
        from . import provision as pv
        probe = pv.probe_host()
        root = str(require_workspace(self.root_dir))
        policy = pv.policy_for_probe(probe, [root])
        plan = pv.plan_provision(prompt, probe, policy=policy)
        rendered = pv.render_plan(plan)
        result = {
            "status": "planned",
            "intent": "provision",
            "prompt": prompt,
            "response": rendered,
            "plan": plan.to_dict(),
            "cost": 0.0,
        }
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="provision", planned=True)
        return result

    def _handle_driver_task(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Drive machine requests autonomously through Jev Driver across perception aspects."""
        from .server import run_driver_task
        task_id = f"drv/{session_id or 'default'}"
        res = run_driver_task(
            task_id,
            {"goal": prompt, "max_steps": 5, "target": "cli"},
            cancel_check=cancel_check,
        )
        total_steps = res.get("total_steps", 0)
        summary = res.get("summary") or f"Jev driver completed {total_steps} step(s)."
        steps = res.get("steps") or []
        step_lines = []
        for s in steps:
            env = s.get("envelope") or {}
            step_lines.append(
                f"- **Step #{s.get('step_number', 1)}** ({s.get('aspect', 'cli')} ➔ {s.get('target', 'cli')}): "
                f"stopped at `{env.get('stopped_at', 'complete')}`, "
                f"status `{'ok' if env.get('ok') else env.get('reason')}`"
            )
        body_text = f"{summary}\n\n" + "\n".join(step_lines) if step_lines else summary
        audit = res.get("audit") or {}
        if audit.get("ok"):
            body_text += (
                f"\n\n---\n**Cryptographic Audit:** Verified clean (0 quarantined entries, "
                f"status `{'ok' if audit.get('ok') else 'unverified'}`)."
            )
        result = {
            "status": res.get("status", "done"),
            "intent": "driver",
            "prompt": prompt,
            "response": body_text,
            "total_steps": total_steps,
            "steps": steps,
            "cost": res.get("total_cost_usd", 0.0),
        }
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="driver", status=result["status"], cost=result["cost"])
        return result

    def _orchestrator_chat_fn(self, gov):
        """Injected chat_fn for orchestration calls (decompose/triage/judge):
        governed, tier-0 scout head -- the cheapest rung of the same sliding
        scale that classifies nodes. Raises HarnessError on failure; callers
        degrade loudly."""
        allow_escalation = bool(getattr(self.settings, "allow_escalation", False))
        ladder = resolve_scout_ladder(use_free=self.settings.use_free,
                                      custom_frontier=self.settings.frontier_model,
                                      allow_escalation=allow_escalation)
        api_key = resolve_api_key()

        def chat_fn(prompt_text):
            # governed_text returns (content, cost); every orchestration
            # consumer (decompose/triage/judge) wants the text alone.
            last = None
            for model in (self._rate_circuit.available(ladder) or ladder):
                try:
                    content, _ = governed_text(self.transport, api_key, gov,
                                               model, prompt_text, 2048,
                                               label="orchestrate")
                    return content
                except HarnessError as e:
                    last = e
                    if "429" in str(e) or "rate limit" in str(e).lower():
                        self._rate_circuit.note_rate_limited(model)
            raise last or HarnessError("orchestration ladder empty")

        return chat_fn

    def _run_execution_stage(self, goal, plan, *, jev_policy,
                             completed_stages=()):
        """HV-5: judge the work package, then let CODE decide any restart.

        ``HV-1`` published five typed stage dimensions and, until now, no
        production path asked for any of them: the single call site sat inside
        the planning stage, which only asks its question when the brief is
        NOT already sufficient -- so on a clean run the dimension never
        executed. This is the execution stage's own call, and it is the one
        place the ``execution`` and ``restart_target`` dimensions mean
        anything: the package about to be handed to a worker, and the
        decision to walk back to an earlier stage instead.

        The restart split is ``HV-1``'s, and this caller keeps it: Jev may
        only RECOMMEND a declared target, and
        :func:`~harness.jev_packs.validate_restart_request` -- code -- decides
        whether that transition is allowed, preserving completed work and
        forcing consent renewal for a changed assignment. When no native
        answer arrives the recommendation is derived from the execution
        signals, and the same code-owned guard decides it, so the decision
        path exists whether or not a model was reachable.

        Returns the judgment as evidence. It deliberately does NOT mark the
        stage ``completed``: judging a package is not dispatching it, and
        dispatch is the next ``HV-5`` slice. Reporting ``completed`` here
        would claim work this code does not do.
        """
        judgment: Dict[str, Any] = {
            "dimension": "execution",
            "state": STATE_PENDING,
            "dispatched": False,
            "signals": {},
            "native": False,
            "restart": None,
        }
        nodes = [
            {"node_id": n.get("node_id"), "instruction": n.get("instruction"),
             "target": (n.get("target_files") or [""])[0]}
            for n in (plan.get("nodes") or [])
        ]
        package = {
            "goal": goal,
            "status": plan.get("status"),
            "decomposition": plan.get("decomposition"),
            "total_nodes": plan.get("total_nodes") or len(nodes),
            "nodes": nodes,
        }
        if jev_policy is not None:
            judgment.update(self._ask_stage_dimension(
                "execution", package, jev_policy, task_id=None))
        judgment["restart"] = self._restart_decision(
            goal, jev_policy, judgment, completed_stages=completed_stages)
        return judgment

    def _ask_stage_dimension(self, dimension, state, jev_policy, *,
                             task_id=None):
        """Ask one declared ``HV-1`` dimension and read its signals.

        The dimension name, the question pack and the signal vocabulary are
        all ``HV-1``'s (``harness/jev_packs.py``); this only calls the owner
        and reports what came back. A fallback, a transport failure or a
        malformed answer is never promoted to a native signal -- that is the
        owner's contract, and this caller does not second-guess it.
        """
        from .jev_packs import HOURGLASS_STAGE_DIMENSIONS
        try:
            eval_res = jev_policy.evaluate_hourglass_stage(
                dimension, state, site=HOURGLASS_STAGE_SITE, task_id=task_id)
            if isinstance(eval_res, tuple) and len(eval_res) >= 2:
                _result, structural = eval_res[0], eval_res[1]
            else:
                _result, structural = None, getattr(eval_res, "structural", {}) or {}
        except (HarnessError, TypeError, ValueError) as exc:
            return {"signals": {}, "native": False,
                    "error": "{0}: {1}".format(type(exc).__name__, exc)}
        structural = structural or {}
        declared = HOURGLASS_STAGE_DIMENSIONS[dimension]["signals"]
        signals = {name: structural.get(name) for name in declared
                   if structural.get(name) is not None}
        return {"signals": signals,
                "native": bool(structural.get("native"))}

    def _restart_decision(self, goal, jev_policy, judgment, *,
                          completed_stages=()):
        """Ask for a restart target, then let code rule on the transition."""
        recommendation = None
        source = "none"
        if jev_policy is not None:
            asked = self._ask_stage_dimension(
                "restart_target",
                {"goal": goal, "completed_stages": list(completed_stages)},
                jev_policy)
            choice = asked["signals"].get("restart_target")
            if isinstance(choice, dict) and choice.get("target"):
                recommendation = choice["target"]
                source = "jev"
        if recommendation is None:
            # No native answer. The recommendation is then DERIVED from the
            # execution signals -- an underspecified package or a required
            # checkpoint both mean the work is not ready where it is -- and
            # it is still only a DECLARED stage: the walk-back target is read
            # out of `HV-1`'s own vocabulary rather than hardcoded here, so
            # this caller and the pack cannot disagree about what a restart
            # may even name. The decision below is code either way.
            walk_back = self._declared_walk_back()
            signals = judgment.get("signals") or {}
            unsuitable = signals.get("execution_suitable")
            checkpoint = signals.get("checkpoint_required")
            if walk_back is not None and (
                    (unsuitable is not None and unsuitable < 0.5)
                    or (checkpoint is not None and checkpoint >= 0.5)):
                recommendation, source = walk_back, "derived"
        if recommendation is not None and \
                recommendation not in declared_restart_targets():
            # Belt and braces: a recommendation that is not in the declared
            # vocabulary is discarded here rather than handed to the guard as
            # something it would have to reject.
            recommendation, source = None, "undeclared_discarded"

        consent_fresh = None
        if jev_policy is not None:
            consent_eval = self._ask_stage_dimension(
                "consent",
                {"assignment": goal, "stages": list(completed_stages)},
                jev_policy)
            judgment["consent"] = consent_eval
            fresh_sig = (consent_eval.get("signals") or {}).get("consent_fresh")
            if isinstance(fresh_sig, (int, float)) and not isinstance(fresh_sig, bool):
                consent_fresh = fresh_sig >= 0.70
            elif consent_eval.get("native") is False:
                consent_fresh = None

        decision = validate_restart_request(
            STAGE_EXECUTION, recommendation,
            completed_stages=completed_stages, consent_fresh=consent_fresh)
        decision["recommendation_source"] = source
        return decision

    @staticmethod
    def _declared_walk_back():
        """The declared stage a restart from ``execution`` should name.

        Read from `HV-1`'s declared vocabulary, not hardcoded: a restart may
        only ever name a declared stage, and the stage immediately preceding
        execution in that vocabulary is the furthest-back legal target. If
        execution is not in the vocabulary, or nothing precedes it, there is
        no legal walk-back and the caller reports none.
        """
        declared = declared_restart_targets()
        if STAGE_EXECUTION not in declared:
            return None
        index = declared.index(STAGE_EXECUTION)
        return declared[index - 1] if index > 0 else None

    def _compose_run_stages(self, goal, candidate_files, jev_policy=None,
                            plan=None):
        """Attach execution-stage judgments to the composition from waist.

        ``compose_plan`` already performs planning when selected. This agent
        hook must consume that result rather than composing or planning again.
        """
        envelope = (dict(plan.get("composition") or {})
                    if isinstance(plan, dict) else {})
        stages = envelope.get("stages") or []
        selected_execution = any(isinstance(item, dict)
                                 and item.get("stage") == STAGE_EXECUTION
                                 and item.get("state") != "skipped"
                                 for item in stages)
        selected_context = any(isinstance(item, dict)
                               and item.get("stage") == STAGE_CONTEXT
                               and item.get("state") != "skipped"
                               for item in stages)
        judgments: Dict[str, Any] = {}
        if selected_context and jev_policy is not None:
            judgments["context"] = self._ask_stage_dimension(
                "context_intake",
                {"request": goal, "files": list(candidate_files or [])},
                jev_policy)
        if selected_execution and isinstance(plan, dict):
            completed = tuple(
                item.get("stage") for item in stages
                if isinstance(item, dict) and item.get("state") == STATE_COMPLETED)
            judgments["execution"] = self._run_execution_stage(
                goal, plan, jev_policy=jev_policy, completed_stages=completed)
        return (envelope, None, judgments)
    def _evidence_reader(self):
        """A reader for brief intake, bound to this run's own root.

        Brief intake is allowed to report a read failure as a read failure --
        it must not curate a different evidence set to work around one -- so
        this raises :class:`HarnessError` rather than letting an ``OSError``
        escape as an unhandled crash. The caller's refusal path is already
        fail-soft: it emits a visible note and leaves the gated plan intact.
        """
        def read(rel):
            try:
                return (self.root_dir / rel).read_text(
                    encoding="utf-8", errors="replace")
            except OSError as exc:
                raise HarnessError(
                    "could not read evidence {0!r}: {1}".format(rel, exc)
                ) from exc
        return read

    def _plan_round(self, goal, candidate_files, gov, confirm=None,
                    token_budget=None, decompose_llm=None):
        """One planning pass through the ONE plan composer
        (harness/waist.py:compose_plan): cheap-LLM decomposition when the
        orchestration ladder answers, tier classification, then (hourglass)
        waist confirmation against the resolved frontier rung.

        The GUI lane plans with the same composer the CLI and MCP lanes use,
        so there is no second plan lane to keep in sync. A confirmation
        failure raises (fail-closed): an unconfirmed plan never executes.
        """
        jev_policy = policy_for(
            self.settings, transport=self.transport, governor=gov,
            ledger=ledger_for(self.settings, caller="agent"))
        compose_args = compose_arguments(
            self.settings, goal=goal, files=candidate_files,
            root=self.root_dir)
        if token_budget is not None:
            compose_args["token_budget"] = token_budget
        decompose_setting = getattr(self.settings, "hourglass_decompose", None)
        decompose_flag = (bool(decompose_setting) if decompose_setting is not None
                          else bool(confirm)) if decompose_llm is None else bool(decompose_llm)
        plan = compose_plan(
            transport=self.transport, api_key=resolve_api_key(),
            governor=gov, ledger=ledger_for(self.settings),
            opts_goal=goal, candidate_files=candidate_files,
            frontier_model=self.settings.frontier_model,
            use_free=self.settings.use_free,
            decompose_llm=decompose_flag, confirm=bool(confirm),
            plan_consensus=bool(getattr(self.settings, "hourglass_plan_consensus", False)),
            # The lane's tree: a node's target size is measured against the
            # files this run actually edits, not the server's CWD.
            root=str(self.root_dir),
            chat_fn=lambda prompt_text: (
                self._orchestrator_chat_fn(gov)(prompt_text), 0.0),
            execute=True,
            allow_escalation=bool(getattr(self.settings, "allow_escalation", False)),
            jev_policy=jev_policy,
            # HV-4/HV-2-use: the GUI lane composes through the same owner the
            # CLI and MCP lanes use, so "which stages ran, what each was
            # allowed, and what evidence it started from" is answerable from
            # the envelope here too instead of only from the caller's head.
            **compose_args)
        if str(plan.get("decomposition", "")).startswith("heuristic"):
            # compose_plan degrades to the heuristic only after the LLM
            # decomposition failed (execute=True); the GUI needs that on the
            # event stream, not just on stderr.
            emit("orchestration_note",
                 note="LLM decomposition unavailable; heuristic plan in use")

        try:
            _, _, judgments = self._compose_run_stages(
                goal, candidate_files, jev_policy=jev_policy, plan=plan)
            if judgments:
                plan["stage_judgments"] = {**plan.get("stage_judgments", {}), **judgments}
                restart = (judgments.get("execution") or {}).get("restart") or {}
                emit("execution_stage",
                     signals=dict((judgments.get("execution") or {}).get("signals") or {}),
                     native=bool((judgments.get("execution") or {}).get("native")),
                     restart=restart.get("allowed"))
                if restart.get("target") is not None:
                    plan["status"] = "refused"
                    plan["confirmation"] = {
                        "verdict": "refused",
                        "model": "restart-guard",
                        "reason": ("validated restart to {0} requires a resumable "
                                   "stage runner: {1}".format(
                                       restart.get("target"),
                                       "; ".join(restart.get("reasons") or []))),
                    }
        except HarnessError:
            pass
        return plan

    def _refused_edit(self, plan, prompt, target_files, session_id):
        """Terminal envelope for a waist refusal: nothing dispatched."""
        confirmation = plan.get("confirmation") or {}
        reason = confirmation.get("reason") or "unspecified"

        # Always provide a prompt response to the user's inquiry whenever possible,
        # using conversational / Jev-driven handling rather than returning only a bare refusal.
        answer_text = ""
        try:
            conv = self._handle_conversation(prompt, session_id)
            if isinstance(conv, dict) and conv.get("response"):
                cand = str(conv["response"]).strip()
                if not cand.startswith("The waist confirmation gate refused"):
                    answer_text = cand
        except (Exception, AssertionError):
            pass

        if not answer_text:
            # #206: refused/empty renders as refused, never as verified.
            from .lifecycle_guards import render_refused_plan
            dag = plan.get("dag") or {}
            answer_text = render_refused_plan(
                prompt, reason,
                stages=plan.get("stages"),
                nodes=dag.get("nodes"),
            ).rstrip("\n")

        # #206: guard speaks only to the real write surface; deduped to one.
        from .lifecycle_guards import (
            dedupe_notices,
            has_write_surface,
            render_gate_guard,
        )
        guard_block = render_gate_guard(
            reason,
            has_write_surface(target_files, plan.get("dag")),
        )
        notice_blocks = dedupe_notices([guard_block] if guard_block else [])
        if notice_blocks:
            response_text = (
                f"{answer_text}\n\n"
                f"---\n"
                + "\n".join(notice_blocks)
            )
        else:
            response_text = answer_text

        result = {
            "status": "refused",
            "intent": "edit",
            "prompt": prompt,
            "response": response_text,
            "target_files": target_files,
            "dag": plan.get("dag"),
            "confirmation": confirmation,
            # A refused run still composed, still ran planning and still
            # judged execution; the refusal is about the plan, not about
            # erasing what the run did. Same three keys, same reason.
            **({"composition": dict(plan["composition"])}
               if isinstance(plan.get("composition"), dict) else {}),
            **({"planning": dict(plan["planning"])}
               if isinstance(plan.get("planning"), dict) else {}),
            **({"stage_judgments": dict(plan["stage_judgments"])}
               if isinstance(plan.get("stage_judgments"), dict) else {}),
            "cost": float(confirmation.get("cost") or 0.0),
            **({"structural": plan["structural"]}
               if isinstance(plan.get("structural"), dict) else {}),
        }
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="edit", status="refused")
        return result


    def _triage_scope(self, prompt):
        """The relevance first pass over the whole repo: explicit prompt
        files win; else the model triages the bounded repo listing (paid-key
        arming decided by the ladder itself failing); else keyword overlap."""
        explicit = discover_target_files(prompt, self.root_dir)
        if explicit:
            return explicit
        repo_files = enumerate_repo_files(self.root_dir)
        if not repo_files:
            return []
        try:
            gov = governor_for(self.settings)[1]
            picked = triage_files(prompt, repo_files,
                                  self._orchestrator_chat_fn(gov))
        except HarnessError:
            picked = []
        if not picked:
            picked = keyword_fallback(prompt, repo_files)
        if picked:
            emit("files_discovered", target_files=picked, triage="orchestrated")
        return picked

    def _handle_edit(
        self,
        prompt: str,
        session_id: str,
        auto_apply: bool,
        cancel_check: Optional[Callable[[], bool]] = None,
        escalation_note: Optional[str] = None,
        use_jev_completion: bool = False,
    ) -> Dict[str, Any]:
        # Handle code edit/refactor requests: the orchestrator drives the
        # hourglass -- relevance triage over the whole repo, DAG decomposition
        # and waist confirmation (the same composer the CLI/MCP lanes use),
        # governed execution through the shared PlanExecutor (parallel
        # workers, per-node cost reservations, worktree isolation, diff
        # attestation), then a completion judge round that re-plans remaining
        # scope until the goal is met or the round budget is spent.
        root_dir = require_workspace(self.root_dir)
        hourglass = resolve_hourglass(self.settings)
        target_files = []

        # Context-aware plan continuation & execution:
        # If prompt is an execution directive or target_files is empty, inspect
        # past session turns to resolve a prior formulated plan or discussed targets.
        past_turns = load_chat_history(session_id, self.history_dir)
        is_exec_directive = (
            any(p in prompt.lower() for p in EXECUTION_PHRASES)
            or any(re.search(rf"\b{action}\b.*?\b(?:plan|code|test|tests|implementation|script)\b", prompt.lower())
                   for action in ("execute", "run", "apply"))
        )
        if is_exec_directive:
            auto_apply = True

        prior_plan = None
        if past_turns and is_exec_directive:
            for turn in reversed(past_turns):
                if isinstance(turn, dict):
                    if turn.get("dag") and turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break
                    elif turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break

        if not target_files:
            target_files = self._triage_scope(prompt)

        if past_turns and not target_files:
            for turn in reversed(past_turns):
                if isinstance(turn, dict):
                    if turn.get("dag") and turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break
                    elif turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break
                    elif turn.get("response"):
                        candidate_text = turn.get("response", "") + "\n" + turn.get("prompt", "")
                        discovered = self._triage_scope(candidate_text)
                        if discovered:
                            target_files = discovered
                            prior_plan = turn
                            break

        if prior_plan is not None:
            if not target_files and prior_plan.get("target_files"):
                target_files = list(prior_plan.get("target_files"))
            prior_goal = prior_plan.get("prompt", "")
            if prior_goal and prior_goal.lower() not in prompt.lower():
                prompt = f"{prior_goal}\n\n[EXECUTION DIRECTIVE]: {prompt}"

        if not target_files:
            # Fallback for execution requests without explicit files:
            # discover candidate test/verification targets in the workspace.
            repo_files = enumerate_repo_files(self.root_dir)
            test_files = [f for f in repo_files if "test" in f.lower() or f.endswith(".py")]
            if test_files:
                target_files = test_files[:3]

        emit("files_discovered", target_files=target_files)

        # Condense candidate file contexts
        file_contents: Dict[str, str] = {}
        original_contents: Dict[str, str] = {}
        for tf in target_files:
            fp = self.root_dir / tf
            if fp.exists():
                try:
                    text = fp.read_text(encoding="utf-8")
                    file_contents[tf] = text
                    original_contents[tf] = text
                except Exception:
                    pass

        brief = distill_context(files=file_contents, summary=prompt)
        ledger_for(self.settings, caller="agent").append(
            "brief_built", task_id=session_id, site="hourglass", schema=2,
            **brief.ledger_fields())
        emit("context_condensed", estimated_tokens=brief.estimated_tokens)

        # Formulate the FIRST-round DAG plan (LLM decomposition when the
        # orchestration ladder answers; heuristic fallback) and gate.
        # The generative plan lane still requires the normal Harness
        # governor. Jev's own key controls only the typed structural call;
        # an unkeyed Jev evaluator remains the explicit local fallback.
        _, gov = governor_for(self.settings)
        run_token_budget = budget_from_settings(self.settings, label="edit")
        plan_policy = policy_for(
            self.settings, transport=self.transport, governor=gov,
            ledger=ledger_for(self.settings, caller="agent"))
        plan_prompt = prompt
        plan_eval, plan_structural = plan_policy.evaluate_plan(
            prompt, target_files, site="agent-plan", task_id=session_id)
        if plan_eval.answers.get("requires_iteration"):
            emit("orchestration_note",
                 note="Jev structural analysis detected algorithmic iteration; injecting DAG loop directive")
            plan_prompt = (
                f"{prompt}\n\n[STRUCTURAL GUIDELINE]: This goal requires iterative "
                "control flow, conditional branching, or multi-step execution. "
                "Ensure the decomposed DAG explicitly breaks down the iterative "
                "loop and discrete steps into executable nodes.")
        try:
            plan = self._plan_round(plan_prompt, target_files, gov,
                                    confirm=hourglass["confirm"],
                                    token_budget=run_token_budget)
        except TypeError:
            plan = self._plan_round(plan_prompt, target_files, gov,
                                    confirm=hourglass["confirm"])
        if isinstance(plan_structural, dict):
            plan["structural"] = plan_structural
        if plan.get("status") == "refused" and (
            str(plan.get("confirmation", {}).get("reason", "")).startswith("validated restart to planning")
        ):
            # Iterate once with Jev stage guidance to refine the plan rather than immediately giving up
            emit("orchestration_note",
                 note="Iterating on plan with Jev stage-runner feedback")
            refined_prompt = (
                f"{plan_prompt}\n\n[JEV REVISION DIRECTIVE]: The initial work package required planning refinement: "
                f"{plan.get('confirmation', {}).get('reason')}. Provide a more specific, verifiable, and safe execution plan."
            )
            try:
                second_plan = self._plan_round(refined_prompt, target_files, gov,
                                              confirm=hourglass["confirm"],
                                              token_budget=run_token_budget)
                if second_plan.get("status") != "refused":
                    plan = second_plan
            except ToolCancelled:
                raise
            except Exception:
                pass
        if plan.get("status") == "refused":
            return self._refused_edit(plan, prompt, target_files, session_id)
        if not any(isinstance(entry, dict)
                   and entry.get("stage") == STAGE_EXECUTION
                   for entry in (plan.get("composition") or {}).get("stages", [])):
            plan["status"] = "refused"
            plan["confirmation"] = {
                "verdict": "refused", "model": "execution-budget",
                "reason": "execution stage is not selected; refusing dispatch",
            }
            return self._refused_edit(plan, prompt, target_files, session_id)
        emit("dag_planned", total_nodes=plan["total_nodes"],
             total_ceiling=plan["total_cost_ceiling"],
             nodes=[{"node_id": n["node_id"], "instruction": n["instruction"],
                     "target": (n.get("target_files") or [""])[0]}
                    for n in plan.get("nodes", [])])

        # Auto-discover local verification gate
        verification_gate = discover_verification_gate(target_files, self.root_dir)

        if not auto_apply:
            # Review-first mode: return proposed plan and candidate target files
            result = {
                "status": "preview_ready",
                "intent": "edit",
                "prompt": prompt,
                "response": f"Plan formulated for {len(target_files)} target file(s). Ready for execution.",
                "target_files": target_files,
                "dag": plan["dag"],
                "verification_gate": verification_gate,
                "cost_ceiling": plan["total_cost_ceiling"],
                "cost": 0.0,
                # HV-5: the composed stages and the planning outcome are part
                # of what the caller asked for, so they are part of the
                # answer. A run that reports a plan without saying which
                # stages it was allowed to spend, and how planning ended,
                # is asking the reader to take the composition on trust.
                **({"composition": dict(plan["composition"])}
                   if isinstance(plan.get("composition"), dict) else {}),
                **({"planning": dict(plan["planning"])}
                   if isinstance(plan.get("planning"), dict) else {}),
                **({"stage_judgments": dict(plan["stage_judgments"])}
                   if isinstance(plan.get("stage_judgments"), dict) else {}),
                **({"structural": dict(plan["structural"])}
                   if isinstance(plan.get("structural"), dict) else {}),
            }
            save_chat_turn(session_id, result, self.history_dir)
            return result

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        # Autonomous execution mode: the orchestrator drives rounds --
        # plan -> execute EVERY node (keep_going: failures are state for the
        # judge, not abort) -> completion judge -> re-plan remaining scope --
        # until the judge calls it complete or the round budget is spent.
        # Keep the injected transport on the actual apply engine as well as
        # planning/judging. This is the sole transport boundary for the agent
        # lane; otherwise a dogfood/test transport silently fell back to live
        # HTTP during node execution.
        engine = apply_session(self.settings, transport=self.transport)

        # Recreate the exact execution-stage ceiling reported by compose_plan,
        # as a child of the SAME run budget. The live child is shared by all
        # node calls, so concurrent reservations are accounted atomically.
        execution_record = next((entry for entry in
                                 (plan.get("composition") or {}).get("stages", [])
                                 if isinstance(entry, dict)
                                 and entry.get("stage") == STAGE_EXECUTION), None)
        execution_budget = None
        if execution_record:
            execution_budget = run_token_budget.stage(
                STAGE_EXECUTION,
                max_input_tokens=execution_record.get("max_input_tokens"),
                max_output_tokens=execution_record.get("max_output_tokens"))

        def apply_node(target, node: DAGNode, route_kwargs, task_runner):
            """One node's engine call for the agent lane: the absolute target
            (the engine resolves paths against process CWD, the server's
            directory, not the agent's chosen root), the gate fallback, the
            MAX_FILE_LINES -> diff-backend switch, and the self-healing
            retry. How the round runs -- worker count, reservations, worktree
            isolation, attestation -- is the shared PlanExecutor's."""
            if cancel_check and cancel_check():
                raise ToolCancelled("Subtask cancelled by user")
            if target is None and target_files:
                target = target_files[0]
            # Isolated nodes arrive absolute, inside their own worktree.
            engine_target = target
            if target and not Path(target).is_absolute():
                engine_target = str(self.root_dir / target)
            # The planner owns gate derivation. This lane deliberately does
            # not repair or invent a gate: a missing plan-time gate remains a
            # fail-closed trust refusal rather than becoming an ungated ok.
            gate = node.local_gate
            emit("subtask_start", node_id=node.node_id, instruction=node.instruction, target=target)

            # Whole-file rewrites cap at MAX_FILE_LINES by engine policy;
            # large-file nodes go straight to the diff backend (touched
            # hunks only) instead of dying as "out of scope" fatals.
            if engine_target and Path(engine_target).is_file():
                try:
                    line_count = len(Path(engine_target).read_text(
                        encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    line_count = 0
                if line_count > MAX_FILE_LINES:
                    route_kwargs.setdefault("backend", "diff")

            apply_kwargs = dict(route_kwargs)
            if execution_budget is not None:
                apply_kwargs["token_budget"] = execution_budget
            # Consent is bound to this exact node assignment and source pins.
            # Pin contents are represented by digests; no prior chat history
            # or web response bodies enter the consent prompt.
            source_pins = {}
            for rel in target_files:
                source = self.root_dir / rel
                if source.is_file():
                    try:
                        source_pins[str(rel)] = hashlib.sha256(
                            source.read_bytes()).hexdigest()
                    except OSError:
                        source_pins[str(rel)] = "unreadable"
            task_limit = route_kwargs.get(
                "task_max_cost", engine.default_task_max_cost)
            assignment = {
                "schema": "jev-work-package-v1",
                "request": prompt,
                "node_id": node.node_id,
                "instruction": node.instruction,
                "target": str(target or ""),
                "verification_gate": gate,
                "context_pins": source_pins,
                "model_policy": {
                    "pinned_model": route_kwargs.get("model"),
                    "apply_pool": list(route_kwargs.get("apply_pool") or
                                        engine.router.apply_pool),
                    "allow_escalation": bool(route_kwargs.get(
                        "allow_escalation", self.settings.allow_escalation)),
                },
                "limits": {
                    "task_max_cost": task_limit,
                    "run_max_cost": gov.max_cost,
                    "run_remaining_cost": gov.remaining(),
                    "max_input_tokens": (execution_budget.max_input_tokens
                                          if execution_budget else None),
                    "max_output_tokens": (execution_budget.max_output_tokens
                                           if execution_budget else None),
                },
            }
            encoded = json.dumps(assignment, sort_keys=True,
                                 separators=(",", ":"), default=str)
            assignment["assignment_id"] = "wp-" + hashlib.sha256(
                encoded.encode("utf-8")).hexdigest()[:24]
            apply_kwargs["assignment_context"] = assignment
            if task_runner is not None:
                apply_kwargs["task_runner"] = task_runner
            res = engine.apply_edit(
                file_path=engine_target,
                instruction=node.instruction,
                verify_cmd=gate,
                allow_verify=True,
                require_consent=True,
                require_diff_authorization=hourglass["require_diff_authorization"],
                **apply_kwargs,
            )
            if isinstance(res, dict):
                res.setdefault("assignment_id", assignment["assignment_id"])
                res.setdefault("assignment_digest", hashlib.sha256(
                    json.dumps(assignment, sort_keys=True, separators=(",", ":"),
                               default=str).encode("utf-8")).hexdigest())
                if execution_budget is not None:
                    res["token_usage"] = execution_budget.snapshot()

            # The shared engine gate owns Jev when it was composed normally.
            # A lightweight injected engine (used by library callers/tests)
            # has no policy, so this lane supplies the same policy owner as a
            # compatibility boundary rather than silently skipping the check.
            # JEV-P4: composition goes through session.jev_for → policy_for
            # (no raw JevEvaluator outside the policy owner).
            structural = res.get("structural") if isinstance(res, dict) else None
            policy = getattr(engine, "jev_policy", None)
            if (res.get("status") in SUCCESS_STATUSES and res.get("diff")
                    and not isinstance(policy, JevPolicy)):
                policy = jev_for(
                    self.settings, transport=self.transport, governor=gov,
                    ledger=ledger_for(self.settings, caller="agent"))
                jev_res, structural = policy.evaluate_diff(
                    diff=res["diff"], instruction=node.instruction,
                    file_path=engine_target or "", candidate=res.get("content"),
                    site="agent-apply", task_id=session_id,
                    node_id=node.node_id)
                emit("structural_eval", node_id=node.node_id,
                     verdict=structural["verdict"],
                     confidence=structural["confidence"],
                     supported=structural["supported"])
                if not jev_res.is_passing(min_confidence=self.settings.min_confidence):
                    emit("subtask_retry", node_id=node.node_id,
                         error="Structural check failed: " + "; ".join(jev_res.reasons))
                    healing_inst = (f"{node.instruction}\nSTRUCTURAL EVALUATION FAILED:\n"
                                    + "\n".join(jev_res.reasons))
                    prior_cost = float(res.get("cost", 0.0) or 0.0)
                    res = engine.apply_edit(
                        file_path=engine_target,
                        instruction=healing_inst,
                        verify_cmd=gate,
                        allow_verify=True,
                        require_consent=True,
                        require_diff_authorization=hourglass["require_diff_authorization"],
                        **apply_kwargs,
                    )
                    if isinstance(res, dict):
                        res["cost"] = round(prior_cost + float(res.get("cost", 0.0) or 0.0), 6)
                        res["structural"] = structural

            # Self-healing retry on verification failure
            if res.get("status") not in SUCCESS_STATUSES and res.get("error"):
                prior_cost = float(res.get("cost", 0.0) or 0.0)
                condensed_err = condense_error_log(str(res.get("error")))
                emit("subtask_retry", node_id=node.node_id, error=condensed_err[:120])
                # Attempt retry with healing instruction
                healing_inst = f"{node.instruction}\nPREVIOUS TEST FAILURE:\n{condensed_err}"
                res = engine.apply_edit(
                    file_path=engine_target,
                    instruction=healing_inst,
                    verify_cmd=gate,
                    allow_verify=True,
                    require_consent=True,
                    require_diff_authorization=hourglass["require_diff_authorization"],
                    **apply_kwargs,
                )
                if isinstance(res, dict):
                    res["cost"] = round(prior_cost + float(res.get("cost", 0.0) or 0.0), 6)

            emit("subtask_finish", node_id=node.node_id, status=res.get("status"))
            return res

        def execute_plan(plan_to_run):
            dag = TaskDAG.from_dict(plan_to_run["dag"])
            node_routes = {n.get("node_id"): n for n in plan_to_run["nodes"]}
            run_gate = None
            for n in plan_to_run.get("nodes") or ():
                if isinstance(n, dict) and n.get("local_gate"):
                    run_gate = n["local_gate"]
                    break
            plan_exec = PlanExecutor(
                engine, node_routes,
                parallel=hourglass["parallel"], isolate=hourglass["isolate"],
                keep_going=True,
                require_diff_authorization=hourglass["require_diff_authorization"],
                route_kwargs_fn=lambda route: node_apply_kwargs(
                    route,
                    allow_escalation=self.settings.allow_escalation,
                    attest_model=attest_model_for(self.settings)),
                repo=str(root_dir), run_ceiling=gov.max_cost,
                apply=apply_node,
                # HG-final-gate: default ON when a verify command was
                # discovered/declared; shared with CLI/MCP via PlanExecutor.
                final_gate=hourglass.get("final_gate"),
                run_gate=run_gate)
            return plan_exec.execute(dag)

        drive_kwargs = dict(
            goal=prompt, target_files=target_files, initial_plan=plan,
            root_dir=root_dir, plan_round=lambda next_goal: self._plan_round(
                next_goal, target_files, gov, confirm=hourglass["confirm"],
                token_budget=run_token_budget),
            execute_plan=execute_plan,
            completion_chat=lambda prompt_text: self._orchestrator_chat_fn(gov)(prompt_text),
            emit=emit, cancel_check=cancel_check,
            refused=lambda refused_plan: self._refused_edit(
                refused_plan, prompt, target_files, session_id))
        if use_jev_completion:
            drive_kwargs.update(jev_policy=plan_policy,
                                jev_completion_threshold=0.99)
        driven = drive(**drive_kwargs)
        if driven.get("status") == "refused":
            return driven
        all_results = driven["all_results"]
        total_cost = driven["total_cost"]
        rounds_history = driven["rounds_history"]
        final_all_ok = driven["final_all_ok"]
        remaining_scope = driven["remaining_scope"]
        handoff = driven.get("handoff")
        plan = driven["plan"]

        # Evidence-bound escalation. The deferral note is a HANDOFF, not proof
        # that a rung ran: a run that planned, handoff-routed, and executed
        # every node on the free lane used to report itself as "escalated".
        # Only a gate-passed escalation whose family differs from the primary
        # it replaced may carry the label (escalation.escalation_evidence is
        # the ONE definition of that evidence).
        escalation_ev = None
        for _node_result in all_results.values():
            escalation_ev = escalation_evidence(_node_result)
            if escalation_ev is not None:
                break

        # Generate unified diffs
        diffs: List[str] = []
        for tf in target_files:
            fp = self.root_dir / tf
            if fp.exists():
                try:
                    new_text = fp.read_text(encoding="utf-8")
                    old_text = original_contents.get(tf, "")
                    if old_text != new_text:
                        diff = difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{tf}",
                            tofile=f"b/{tf}",
                        )
                        diffs.append("".join(diff))
                except Exception:
                    pass

        unified_diff_str = "\n".join(diffs).strip()

        # Synthesize clear natural language conclusion
        if handoff:
            response_msg = ("Execution stopped at the consent boundary. "
                            "Completed node evidence is retained in the handoff.")
        elif final_all_ok:
            response_msg = (
                f"Orchestrator complete after {len(rounds_history)} round(s). "
                f"Modified {len(target_files)} file(s); the completion judge "
                f"confirmed the goal is met."
            )
        elif remaining_scope:
            response_msg = (f"Orchestrator stopped after {len(rounds_history)} "
                            f"round(s) with honest remaining scope: "
                            f"{remaining_scope}")
        else:
            response_msg = "Task encountered a verification failure during execution. Check diff and error details."
        if escalation_note:
            if escalation_ev is not None:
                response_msg += (
                    f"\n\n**Auto-escalated to the plan lane (hourglass)** after "
                    f"a capability defer: {escalation_note} -- escalation rung "
                    f"ran: {escalation_ev['from_family']} -> "
                    f"{escalation_ev['family']} on {escalation_ev['model']}.")
            else:
                response_msg += (
                    f"\n\n**Auto-escalation handoff ran, but no escalation rung "
                    f"actually ran** (capability defer: {escalation_note}): every "
                    f"node stayed on its configured lane, so this run is not "
                    f"escalated.")

        agent_structural = aggregate_structural(
            list(all_results.values()), site="agent")
        if agent_structural is None and isinstance(plan.get("structural"), dict):
            agent_structural = dict(plan["structural"])
            agent_structural["site"] = "agent"
        last_jev = next((r.get("jev") for r in reversed(rounds_history)
                         if isinstance(r, dict) and r.get("jev")), None)
        hourglass_evidence = {
            "stages": ["context_intake", "planning_waist", "execution",
                       "completion_jev"] if use_jev_completion else
                      ["context_intake", "planning_waist", "execution"],
            "rounds": rounds_history,
            "settings": hourglass,
            "token_usage": run_token_budget.snapshot(),
            "source": "agent_edit_lane",
        }
        result = {
            "status": "deferred" if handoff else
                      ("ok" if final_all_ok else "failed"),
            **({"escalated_from_defer":
                escalation_note or "free model capability defer"}
               if escalation_ev is not None else {}),
            **escalation_evidence_fields(escalation_ev),
            "intent": "edit",
            "prompt": prompt,
            "response": response_msg,
            "target_files": target_files,
            "diff": unified_diff_str,
            "verification_gate": verification_gate,
            # The waist gate's own verdict rides the envelope, so a reader
            # can see what confirmed this plan (and on which model).
            **({"confirmation": plan["confirmation"]}
               if plan.get("confirmation") else {}),
            **({"structural": agent_structural}
               if agent_structural is not None else {}),
            # MS envelope: requested = first child's requested primary;
            # observed = every model that served across the run.
            **model_envelope(
                model_requested=next((r.get("model_requested") for r in
                                      all_results.values()
                                      if isinstance(r, dict)
                                      and r.get("model_requested")), None),
                model_observed=[m for r in all_results.values()
                                if isinstance(r, dict)
                                for m in (r.get("model_observed") or [])]),
            "cost": round(total_cost, 6),
            # HV-5: the composed stages, the planning outcome and the
            # execution-stage judgment are the same evidence the review-first
            # envelope already carried, and `auto_apply` defaults to false --
            # so leaving them on the preview branch only would have made the
            # whole slice invisible on an ordinary autonomous run. They ride
            # the final plan, next to the `hourglass` block that already
            # carries stage evidence, because a reader of a completed run has
            # at least as much claim to them as a reader of a preview.
            **({"composition": dict(plan["composition"])}
               if isinstance(plan.get("composition"), dict) else {}),
            **({"planning": dict(plan["planning"])}
               if isinstance(plan.get("planning"), dict) else {}),
            **({"stage_judgments": dict(plan["stage_judgments"])}
               if isinstance(plan.get("stage_judgments"), dict) else {}),
            **engine.governor.snapshot(),
            "results": list(all_results.values()),
            "orchestrator_rounds": len(rounds_history),
            "orchestrator_history": rounds_history,
            "hourglass": hourglass_evidence,
            **({"handoff": handoff} if handoff else {}),
            **({"confidence": {
                "threshold": 0.99,
                "observed": (last_jev or {}).get("supported"),
                "passed": bool(
                    final_all_ok
                    and (last_jev or {}).get("native")
                    and isinstance((last_jev or {}).get("supported"), (int, float))
                    and float((last_jev or {}).get("supported")) >= 0.99),
                "source": "jev.noul.goal_achieved",
            }} if last_jev else {}),
            **({"remaining_scope": remaining_scope} if not final_all_ok else {}),
        }

        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="edit", status=result["status"], cost=total_cost)
        return result
