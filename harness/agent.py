# Autonomous agent orchestrator (#PR-Chat-1)
# Drives natural language prompts to conclusion automatically with zero UI clutter.
import difflib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ._http import HttpTransport
from .chat import assess_output, chat, extract_content_and_cost, governed_text, looks_truncated
from .condenser import distill_context, condense_error_log
from .config import (
    Settings, effective_lane_policy, load_settings, resolve_api_key,
    resolve_hourglass,
)
from .tokens import estimate_prompt_tokens
from .dag import TaskDAG, DAGNode, node_apply_kwargs
from .prompts import MAX_FILE_LINES
from .errors import HarnessError, ToolCancelled
from .events import emit
from .orchestrator import assess_completion, drive, keyword_fallback, triage_files
from .prompts import CAPABILITY_MARKER
from .escalation import escalation_evidence, escalation_evidence_fields
from .executor import PlanExecutor
from .history import load_chat_history, save_chat_turn
from .repo_scope import (
    discover_target_files,
    discover_verification_gate,
    enumerate_repo_files,
)
from .results import SUCCESS_STATUSES, _http_error, model_envelope
from .session import apply_session, attest_model_for, governor_for, jev_for, ledger_for
from .jev_policy import (
    JevPolicy, aggregate_structural, jev_cost_ceiling, policy_for,
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
from .web import DEFAULT_FETCH_HOSTS, gather_web_context

# Consumers import history/repo_scope/web helpers from their owners
# (harness.history, harness.repo_scope, harness.web), not from this module.
__all__ = [
    "AutonomousAgent",
    "CONVERSATION_STARTERS",
    "DEFAULT_CHAT_SYSTEM_PROMPT",
    "EXECUTION_PHRASES",
    "MUTATION_KEYWORDS",
    "classify_prompt_intent",
    "discover_target_files",
    "discover_verification_gate",
    "enumerate_repo_files",
    "load_chat_history",
    "save_chat_turn",
]

TRUNCATION_NOTICE = (
    "\n\n[Note: This response reached the maximum output token limit and was truncated.]"
)


def _resolve_lane_max_tokens(
    gov: Optional[Any],
    model: str,
    prompt_text: str,
    target_tokens: int,
    *,
    token_budget: Optional[Any] = None,
) -> int:
    """Dynamically allocate output tokens for a turn or stage.

    Starts with `target_tokens` (from :func:`effective_lane_policy`), then
    narrows:
    1. If `token_budget` is provided and has `remaining_output()`, bounds by
       available output tokens.
    2. If `gov` is provided and has pricing with a non-zero completion price,
       bounds by remaining dollar budget under current phase:
       max_output_tokens = max(0, int((rem_dollar - prompt_cost) / completion_price)).
    3. Never inflates above `target_tokens`.
    """
    allocated = int(target_tokens)
    if token_budget is not None and callable(getattr(token_budget, "remaining_output", None)):
        rem_out = token_budget.remaining_output()
        if isinstance(rem_out, (int, float)):
            allocated = min(allocated, int(rem_out))
    if gov is not None and callable(getattr(gov, "fetch_pricing", None)):
        try:
            pricing = gov.fetch_pricing([model])
            if isinstance(pricing, dict) and model in pricing:
                pp, cp = pricing[model]
                if isinstance(cp, (int, float)) and cp > 0.0 and callable(getattr(gov, "remaining", None)):
                    rem_dollars = gov.remaining()
                    if isinstance(rem_dollars, (int, float)):
                        prompt_tok = estimate_prompt_tokens(prompt_text)
                        pp_val = float(pp) if isinstance(pp, (int, float)) else 0.0
                        avail_dollar = max(0.0, float(rem_dollars) - prompt_tok * pp_val)
                        dollar_tokens = int(avail_dollar / float(cp))
                        allocated = min(allocated, dollar_tokens)
        except Exception:
            pass
    return max(0, allocated)


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

# Appended to the system prompt when a run did NOT opt into web tools, so the
# model cannot roleplay a lookup it never performed (the Riemann failure mode).
_NO_WEB_DISCLOSURE = (
    "[Capability note] This run has NO web tools: no search and no fetch are "
    "attached, and you have NO internet access. If the user asks you to search "
    "or verify online, say you cannot in this run and answer from training "
    "knowledge, labeled as such."
)

# Replaces the no-web note when a run DID opt in: the model must know what is
# actually attached (and what fetch will refuse) so it answers "can you access
# X?" truthfully instead of denying the capability or inventing a lookup.
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
_DEFAULT_HOURGLASS_CONFIDENCE = 0.99
_MAX_HOURGLASS_ANSWER_ROUNDS = 3

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
DEFAULT_RUN_WALL_SECONDS = 120.0

# Asking what the world currently says is a research question, not an edit.
# The dogfood prompt ("can you find news about ...") was routed to the
# conversation lane but never gathered sources, so the run iterated on a
# question it had no evidence for.
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


def classify_prompt_intent(prompt: str) -> str:
    # Classify a natural language prompt into conversation, edit, or audit
    cleaned = prompt.strip().lower()
    words = re.findall(r"\b[a-z_0-9-]+\b", cleaned)
    first_word = words[0] if words else ""

    if any(w in cleaned for w in ("verify chain", "audit ledger", "ledger status", "check ledger")):
        return "audit"

    if any(w in cleaned for w in ("driver task", "drive request", "run driver", "machine drive", "perceive dom", "perceive cli", "driver perception", "perception step")):
        return "driver"

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

    has_mutation_verb = any(w in MUTATION_KEYWORDS for w in words)

    if has_mutation_verb or has_file_ext:
        return "edit"

    return "conversation"




class AutonomousAgent:
    # Self-orchestrating agent driving natural language prompts to conclusion
    def __init__(
        self,
        settings: Optional[Settings] = None,
        root_dir: Optional[Path] = None,
        history_dir: Optional[Path] = None,
        transport: Optional[HttpTransport] = None,
    ):
        self.settings = settings or load_settings()
        self.root_dir = (root_dir or Path.cwd()).resolve()
        self.history_dir = history_dir
        self.transport = transport or HttpTransport()

    def run_prompt(
        self,
        prompt: str,
        auto_apply: bool = True,
        session_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        force_conversation: bool = False,
        web: bool = False,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        token_budget: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # Process a natural language prompt from intent to verified conclusion.
        # When force_conversation=True (e.g. UI chat box), skip the intent
        # classifier entirely and route straight to conversational handling so
        # that session history is always loaded and submitted to the model.
        if not prompt or not prompt.strip():
            raise HarnessError("Prompt cannot be empty")

        sid = session_id or "default"
        emit("chat_turn_start", prompt=prompt, session_id=sid)

        if force_conversation:
            # UI chat is conversational by default -- but an edit-intent
            # prompt (mutation verbs / explicit files) in Auto mode is repo
            # work: it drives the orchestrator loop instead of the chat model
            # narrating code it will never write. Everything else keeps the
            # conversation lane (session history still loads); audit keywords
            # stay here too -- the classifier sees the same text.
            intent = classify_prompt_intent(prompt)
            emit("intent_classified", intent=intent, prompt=prompt)
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            if intent == "driver":
                return self._handle_driver_task(prompt, sid, cancel_check=cancel_check)
            if intent == "audit":
                return self._handle_audit(prompt, sid, cancel_check=cancel_check)
            if intent == "simple-action":
                return self.run_simple_action(prompt, session_id=sid,
                                              cancel_check=cancel_check)
            if intent == "edit" and auto_apply:
                emit("chat_escalated", reason="edit-intent in auto mode",
                     target="plan_lane")
                return self._handle_edit(prompt, sid, auto_apply, cancel_check,
                                         use_jev_completion=self._live_jev_available())
            if intent == "conversation" and self._live_jev_available():
                return self.run_hourglass_request(
                    prompt, session_id=sid, cancel_check=cancel_check, web=web,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
            return self._handle_conversation(
                prompt, sid, cancel_check, web=web,
                max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                token_budget=token_budget)

        intent = classify_prompt_intent(prompt)
        emit("intent_classified", intent=intent, prompt=prompt)

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        if intent == "driver":
            return self._handle_driver_task(prompt, sid, cancel_check=cancel_check)
        elif intent == "simple-action":
            return self.run_simple_action(prompt, session_id=sid,
                                          cancel_check=cancel_check)
        elif intent == "conversation":
            if self._live_jev_available():
                return self.run_hourglass_request(
                    prompt, session_id=sid, cancel_check=cancel_check, web=web,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    token_budget=token_budget)
            return self._handle_conversation(
                prompt, sid, cancel_check, web=web,
                max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                token_budget=token_budget)
        elif intent == "audit":
            return self._handle_audit(prompt, sid, cancel_check)
        else:
            return self._handle_edit(prompt, sid, auto_apply, cancel_check,
                                     use_jev_completion=(auto_apply
                                                         and self._live_jev_available()))

    def _live_jev_available(self) -> bool:
        return bool(getattr(self.settings, "jev_api_key", None)
                    and not getattr(self.settings, "jev_disabled", False))

    def run_simple_action(self, prompt: str, session_id: Optional[str] = None,
                          cancel_check=None, probe_fn=None,
                          resolve_fn=None) -> Dict[str, Any]:
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
            return result
        try:
            probe = (_probe(target, cancel_check=cancel_check,
                            resolve_fn=resolve_fn,
                            connect_fn=probe_fn) if probe_fn is not None
                     else _probe(target, cancel_check=cancel_check,
                                 resolve_fn=resolve_fn))
        except HarnessError as e:
            reason = str(e)
            try:
                ledger_for(self.settings, caller="agent").append(
                    "simple_action", task_id=sid, event_note="preflight",
                    status="refused", reason=reason, target=target,
                    cost=0.0)
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
            save_chat_turn(sid, result, self.history_dir)
            emit("simple_action_complete", status="deferred", reason=reason)
            return result
        verdict = probe.get("verdict")
        if verdict in ("up", "denied", "redirect"):
            status, opener = "ok", "Yes"
            if verdict == "denied":
                opener = "Yes -- reachable but access was denied"
            elif verdict == "redirect":
                opener = "Yes -- it answers (with a redirect)"
            response = f"{opener}: {probe.get('reason')}."
        else:
            status, response = "deferred", f"No: {probe.get('reason')}."
        try:
            ledger_for(self.settings, caller="agent").append(
                "simple_action", task_id=sid, event_note="probe",
                status=status, verdict=verdict, target=target,
                http_status=probe.get("http_status"),
                latency_s=probe.get("latency_s"), cost=0.0)
        except Exception:
            pass
        result = {
            "status": status, "intent": "simple-action",
            "workflow_tier": "simple-action", "prompt": prompt,
            "response": response, "model": None, "cost": 0.0,
            "target": target, "probe": probe,
            **({"defer_reason": probe.get("reason")} if status != "ok" else {}),
        }
        save_chat_turn(sid, result, self.history_dir)
        emit("simple_action_complete", status=status, verdict=verdict)
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

    # Free models defer in prose ("I can't do that") far more often than in
    # the marker contract. A refusal-shaped answer with no successful tool
    # evidence IS a deferral: the model handed the work back. The model's own
    # refusal sentence becomes the reason; a caveat inside a turn that DID
    # retrieve evidence is not a deferral.
    _REFUSAL_RE = re.compile(
        r"\b(?:i can(?:'t|not)|i'?m unable|unable to|i have no access|"
        r"i (?:do not|don't) have (?:access|tools|internet|a browser|file|compute))\b",
        re.IGNORECASE)

    @classmethod
    def _refusal_reason(cls, response_text: str) -> Optional[str]:
        for sentence in re.split(r"(?<=[.!?])\s+", response_text.strip()):
            if cls._REFUSAL_RE.search(sentence):
                return " ".join(sentence.split())[:200]
        return None

    def _hourglass_request_context(self, prompt: str, web: bool = False):
        """Gather a bounded, cheap intake brief for a non-edit request.

        Intake is local and deterministic: explicit paths win, then filename
        overlap, then no context.  It never feeds an unbounded repository to a
        model.  Web evidence is added only when the caller explicitly enables
        the existing bounded web lane.
        """
        candidates = discover_target_files(prompt, self.root_dir)
        if not candidates:
            candidates = keyword_fallback(
                prompt, enumerate_repo_files(self.root_dir),
                max_files=_MAX_HOURGLASS_CONTEXT_FILES)
        files = {}
        for rel in candidates[:_MAX_HOURGLASS_CONTEXT_FILES]:
            path = self.root_dir / rel
            try:
                if path.is_file():
                    files[rel] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        brief = distill_context(
            files=files, summary=prompt, max_tokens=1800)
        # HV-2 evidence: the condensation is a ledger event, not a private
        # step -- site=hourglass, schema-v2 pack attached to the MicroBrief.
        ledger_for(self.settings, caller="agent").append(
            "brief_built", site="hourglass", schema=2,
            **brief.ledger_fields())
        context = brief.to_prompt_context()[:_MAX_HOURGLASS_CONTEXT_CHARS]
        web_sources = []
        if web:
            try:
                web_sources = self._gather_web_context(prompt)
            except Exception as exc:  # context failure must not kill the lane
                web_sources = [{"kind": "web", "ok": False,
                                "note": f"web tools error: {type(exc).__name__}"}]
            for source in web_sources:
                if source.get("ok"):
                    body = str(source.get("text") or "")[:_MAX_WEB_CONTEXT_CHARS]
                    context += "\n\nWEB EVIDENCE:\n" + body
            context = context[:_MAX_HOURGLASS_CONTEXT_CHARS]
        emit("context_condensed", estimated_tokens=brief.estimated_tokens,
             files=list(files), web_sources=len(web_sources))
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
        for turn in (past_turns or [])[-10:]:
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
        for model in ordered:
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
            except HarnessError as exc:
                attempts.append(f"{model}: {exc}")
                continue
            if status != 200:
                attempts.append(f"{model}: HTTP {status}")
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

    def run_hourglass_request(
            self, prompt: str, *, session_id: Optional[str] = None,
            cancel_check=None, web: bool = False,
            confidence_threshold: float = _DEFAULT_HOURGLASS_CONFIDENCE,
            max_rounds: int = _MAX_HOURGLASS_ANSWER_ROUNDS,
            auto_apply: bool = True,
            max_tokens: Optional[int] = None,
            reasoning_effort: Optional[str] = None,
            token_budget: Optional[Any] = None,
            max_wall_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Run the all-request composition used by external local drivers.

        Edit requests enter the existing hourglass plan/executor lane. Direct
        answers use local context intake, the cheapest configured answer rung,
        and native Jev sufficiency/iteration signals in a bounded escalation
        loop.  A plan signal is advisory: only an explicit edit/action request
        can cross the write boundary.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise HarnessError("Prompt cannot be empty")
        threshold = float(confidence_threshold)
        if threshold < 0.0 or threshold > 1.0:
            raise HarnessError("confidence_threshold must be between 0 and 1")
        rounds = max(1, min(int(max_rounds), _MAX_HOURGLASS_ANSWER_ROUNDS))
        sid = session_id or "default"
        intent = classify_prompt_intent(prompt)
        hourglass_policy = resolve_hourglass(self.settings)
        emit("hourglass_request", intent=intent, confidence_threshold=threshold,
             max_rounds=rounds)
        if intent == "audit":
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

        # A research question earns the web lane whether or not the caller
        # asked: answering "what is the latest news on X" from model memory is
        # how a run iterates six times on a question it had no evidence for.
        web = bool(web) or is_research_question(prompt)
        wall_budget = (DEFAULT_RUN_WALL_SECONDS if max_wall_seconds is None
                       else float(max_wall_seconds))
        deadline = time.monotonic() + max(0.0, wall_budget)
        context, files, brief, web_sources = self._hourglass_request_context(
            prompt, web=web)
        past_turns = load_chat_history(sid, self.history_dir)[-10:]
        api_key, gov = governor_for(self.settings)
        policy = policy_for(
            self.settings, transport=self.transport, governor=gov,
            ledger=ledger_for(self.settings, caller="agent"))
        prior = None
        history = []
        total_cost = 0.0
        answer = ""
        model = None
        jev_result = None
        jev_structural = None
        last_envelope = None
        best_envelope = None
        stop_reason = None
        status = "needs_iteration"
        attempt = {}
        for round_no in range(1, rounds + 1):
            if cancel_check and cancel_check():
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
            attempt = self._hourglass_answer_once(
                prompt, context, prior=prior, model_offset=round_no - 1,
                cancel_check=cancel_check, governor=gov, api_key=api_key,
                past_turns=past_turns, web=web, web_sources=web_sources,
                max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                token_budget=token_budget)
            answer = attempt["answer"]
            model = attempt["model"]
            total_cost += float(attempt.get("cost") or 0.0)
            jev_result, jev_structural = policy.evaluate_answer(
                prompt, answer, "", site="answer", task_id=sid)
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
                 iteration_required=bool(jev_structural.get("iteration_required")))
            if jev_structural.get("plan_required"):
                # A direct-answer request must not turn an advisory Jev signal
                # into a write.  The edit lane owns plan/execute; callers can
                # explicitly resubmit this request as an edit when appropriate.
                status = "plan_required"
                break
            should_check_completion = (
                envelope["confidence"]["passed"]
                or (
                    envelope["native"]
                    and not bool(jev_structural.get("iteration_required"))
                    and isinstance(observed, (int, float))
                    and observed >= 0.50)
                or (
                    envelope["native"]
                    and web
                    and any(s.get("ok") for s in (web_sources or []))
                    and isinstance(observed, (int, float))
                    and observed >= 0.50)
            )
            if should_check_completion:
                completion = assess_completion(
                    prompt,
                    "Candidate answer:\n" + answer + "\n\nRetained context:\n" + context,
                    self._orchestrator_chat_fn(gov))
                history[-1]["completion"] = completion
                if completion and completion.get("complete") is True:
                    status = "ok"
                    break
                prior = {
                    "answer": answer,
                    "feedback": ((completion or {}).get("remaining")
                                 or (completion or {}).get("reason")
                                 or "Independent completion judge could not verify fulfillment"),
                }
                if round_no == rounds:
                    status = "needs_iteration"
                continue
            if not envelope["native"] and not envelope["is_fallback"]:
                # A rejected, malformed, or preflight-refused live request is
                # not an allowed local fallback and cannot use the independent
                # fulfillment check to turn it into approval.
                status = "deferred"
                stop_reason = "; ".join(envelope["reasons"] or []) or (
                    "Jev could not produce a usable live judgment")
                break
            if not envelope["native"] and envelope["is_fallback"]:
                # Live Jev is unavailable. Its local fallback remains visible,
                # while the independent fulfillment judge decides completion.
                completion = assess_completion(
                    prompt,
                    "Candidate answer:\n" + answer + "\n\nRetained context:\n" + context,
                    self._orchestrator_chat_fn(gov))
                history[-1]["completion"] = completion
                if completion and completion.get("complete") is True:
                    status = "ok"
                    break
                if round_no == rounds:
                    status = "deferred"
                    stop_reason = ((completion or {}).get("remaining")
                                   or "Live Jev unavailable; independent fulfillment was not established")
                else:
                    prior = {"answer": answer,
                             "feedback": ((completion or {}).get("remaining")
                                          or (completion or {}).get("reason")
                                          or "Independent completion judge could not verify fulfillment")}
                continue
            prior = {
                "answer": answer,
                "feedback": "Jev says another answer attempt is needed: "
                            + "; ".join(envelope["reasons"]),
            }
            if round_no == rounds:
                if envelope["native"]:
                    try:
                        completion = assess_completion(
                            prompt,
                            "Candidate answer:\n" + answer + "\n\nRetained context:\n" + context,
                            self._orchestrator_chat_fn(gov))
                        history[-1]["completion"] = completion
                        if completion and completion.get("complete") is True:
                            status = "ok"
                            break
                    except Exception:
                        pass
                status = "needs_iteration"

        is_truncated = bool(attempt.get("truncated"))
        result = {
            "status": status,
            "intent": intent,
            "prompt": prompt,
            "response": answer,
            "model": model,
            "cost": round(total_cost, 6),
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
                "context_files": list(files),
                "context_tokens": brief.estimated_tokens,
                "rounds": history,
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
            except Exception:
                pass

        if status == "needs_iteration":
            result["remaining_scope"] = (
                "Jev and the independent completion judge did not establish fulfillment")
        elif stop_reason:
            result["remaining_scope"] = stop_reason
        save_chat_turn(sid, result, self.history_dir)
        emit("hourglass_complete", status=status,
             confidence=result["confidence"].get("observed"))
        return result

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
        trigger = bool(marker_defer or status == "plan_required"
                       or explicit_directive)
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
        past_turns = load_chat_history(session_id, self.history_dir)
        for turn in past_turns[-10:]:
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
        ladder = resolve_scout_ladder(use_free=self.settings.use_free,
                                      custom_frontier=self.settings.frontier_model)
        api_key = resolve_api_key()

        def chat_fn(prompt_text):
            # governed_text returns (content, cost); every orchestration
            # consumer (decompose/triage/judge) wants the text alone.
            last = None
            for model in ladder:
                try:
                    content, _ = governed_text(self.transport, api_key, gov,
                                               model, prompt_text, 2048,
                                               label="orchestrate")
                    return content
                except HarnessError as e:
                    last = e
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
                    token_budget=None):
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
        plan = compose_plan(
            transport=self.transport, api_key=resolve_api_key(),
            governor=gov, ledger=ledger_for(self.settings),
            opts_goal=goal, candidate_files=candidate_files,
            frontier_model=self.settings.frontier_model,
            use_free=self.settings.use_free,
            decompose_llm=True, confirm=bool(confirm),
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
            dag = plan.get("dag") or {}
            nodes = dag.get("nodes") or []
            targets_str = ", ".join(f"`{t}`" for t in target_files) if target_files else "identified repository components"
            plan_summary_lines = []
            if nodes:
                plan_summary_lines.append("**Planned Execution Stages:**")
                for i, node in enumerate(nodes[:5], 1):
                    plan_summary_lines.append(f"{i}. **{node.get('name', 'Stage')}**: {node.get('summary', node.get('description', ''))}")
            elif plan.get("stages"):
                plan_summary_lines.append("**Planned Execution Stages:**")
                for s in plan.get("stages")[:5]:
                    plan_summary_lines.append(f"- `{s}`")
            stages_block = ("\n" + "\n".join(plan_summary_lines) + "\n\n") if plan_summary_lines else ""
            answer_text = (
                f"### Analysis & Proposed Plan for `{prompt}`\n\n"
                f"Harness evaluated your request across {targets_str}.{stages_block}"
                f"The planned changes were structured and verified by the waist planner."
            )

        response_text = (
            f"{answer_text}\n\n"
            f"---\n"
            f"**Autonomous Waist Gate Guard:** Automated file writes were held ({reason}). "
            f"Review the planned changes above or confirm execution to proceed."
        )

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
                repo=str(self.root_dir), run_ceiling=gov.max_cost,
                apply=apply_node,
                # HG-final-gate: default ON when a verify command was
                # discovered/declared; shared with CLI/MCP via PlanExecutor.
                final_gate=hourglass.get("final_gate"),
                run_gate=run_gate)
            return plan_exec.execute(dag)

        drive_kwargs = dict(
            goal=prompt, target_files=target_files, initial_plan=plan,
            root_dir=self.root_dir, plan_round=lambda next_goal: self._plan_round(
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
            # envelope already carried, and `auto_apply` DEFAULTS TO TRUE --
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
