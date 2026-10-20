# Autonomous agent orchestrator (#PR-Chat-1)
# Drives natural language prompts to conclusion automatically with zero UI clutter.
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ._http import HttpTransport
from .chat import (governed_text)
from .config import (
    Settings, load_settings, resolve_api_key,
)
from .errors import HarnessError, ToolCancelled
from .events import emit
from .orchestrator import keyword_fallback, triage_files
from .history import (
    load_chat_checkpoint,
    save_chat_turn,
)
from .lifecycle import LifecycleCoordinator
from .simple_request import (
    is_simple_answer_request,
    is_supplied_text_rewrite,
    is_workspace_inspection_request,
)
from .repo_scope import (
    discover_target_files,
    enumerate_repo_files,
)
from .session import (
    governor_for, jev_face_governor,
    jev_for, ledger_for,
)
from .jev_policy import (
    policy_for,
)
from .jev_packs import (
    HOURGLASS_STAGE_SITE,
    declared_restart_targets,
    validate_restart_request,
)
from .waist import (
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STATE_COMPLETED,
    STATE_PENDING,
    compose_arguments,
    compose_plan,
    resolve_scout_ladder,
)
from .edit_lifecycle import AutoEditLifecycleMixin, EXECUTION_PHRASES
from .chat_lifecycle import ChatLifecycleMixin
from .task_lifecycle import TaskLaneLifecycleMixin

# Consumers import history/repo_scope/web helpers from their owners
# (harness.history, harness.repo_scope, harness.web), not from this module.
__all__ = [
    "AutonomousAgent",
    "CONVERSATION_STARTERS",
    "MUTATION_KEYWORDS",
    "classify_prompt_intent",
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




class AutonomousAgent(
    ChatLifecycleMixin, AutoEditLifecycleMixin, TaskLaneLifecycleMixin,
):
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
        self.lifecycle = LifecycleCoordinator(history_dir=history_dir)
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
                resumed = self.lifecycle.resume(
                    sid, followup=followup or None)
            except HarnessError:
                emit("lifecycle_resume_unavailable", session_id=sid,
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
            if resumed is not None:
                emit("chat_resume", session_id=sid, lane=resumed["lane"],
                     route=resumed["data"].get("route"),
                     stage=resumed["stage"])
                if resumed["status"] == "deferred":
                    reason = resumed["data"].get(
                        "defer_reason", "an interrupted side effect needs reconciliation")
                    return {
                        "status": "deferred", "intent": resumed["lane"],
                        "workflow_tier": "continuation",
                        "prompt": self.lifecycle.active_request(resumed),
                        "response": ("I restored the request state, but will not "
                                     "replay an operation whose completion is unknown. "
                                     "Reconcile the saved operation before continuing."),
                        "model": None,
                        "cost": resumed["usage"]["known_cost"],
                        "defer_reason": reason,
                        "lifecycle": {
                            "request_id": resumed["request_id"],
                            "lane": resumed["lane"],
                            "stage": resumed["stage"],
                            "unknown_liability": resumed["usage"]["unknown_liability"],
                            "requires_reconciliation": list(
                                resumed["execution"]["requires_reconciliation"]),
                        },
                    }
                prompt = self.lifecycle.active_request(resumed)
                lane = resumed["lane"]
                if lane == "chat":
                    resume_state = self.lifecycle.chat_payload(sid) or {}
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
                elif lane == "audit":
                    return self._handle_audit(
                        prompt, sid, cancel_check=cancel_check,
                        lifecycle_state=resumed)
                elif lane == "provisioning":
                    return self._handle_provision(
                        prompt, sid, cancel_check=cancel_check,
                        lifecycle_state=resumed)
                elif lane == "driver":
                    return self._handle_driver_task(
                        prompt, sid, cancel_check=cancel_check,
                        lifecycle_state=resumed)
                elif lane == "auto_edit":
                    return self._handle_edit(
                        prompt, sid, bool(resumed["data"].get("auto_apply")),
                        cancel_check, use_jev_completion=self._live_jev_available(),
                        lifecycle_state=resumed)
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
            # A new request supersedes the session lifecycle; only an explicit
            # continuation carries request state forward.
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
