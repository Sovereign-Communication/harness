# Autonomous agent orchestrator (#PR-Chat-1)
# Drives natural language prompts to conclusion automatically with zero UI clutter.
import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ._http import HttpTransport
from .chat import assess_output, chat, extract_content_and_cost, governed_text, looks_truncated
from .condenser import distill_context, condense_error_log
from .config import Settings, load_settings, resolve_api_key
from .dag import TaskDAG, DAGNode, decompose_via_llm, node_apply_kwargs, plan_task
from .prompts import MAX_FILE_LINES
from .errors import HarnessError, ToolCancelled
from .events import emit
from .orchestrator import assess_completion, build_state_summary, keyword_fallback, triage_files
from .prompts import CAPABILITY_MARKER
from .executor import ConcurrentExecutor
from .results import SUCCESS_STATUSES, _http_error
from .session import apply_session, governor_for, ledger_for
from .waist import resolve_scout_ladder
from .web import DEFAULT_FETCH_HOSTS, extract_query, fetch_url, find_urls, search_web

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are Sovereign Harness, an autonomous, cost-bounded software engineering AI. "
    "Be concise, clear, and direct. When answering technical questions, explain precisely "
    "and provide code snippets when helpful. Focus on correctness, zero bloat, and safety. "
    "Your exact capability for this run is stated in the capability note below, if any. "
    "Never claim you searched, checked a source, or verified current information unless "
    "web tool results for this turn are attached -- say plainly when you cannot know "
    "something. Deferral is a first-class outcome in this system: when a request exceeds "
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

MUTATION_KEYWORDS = frozenset({
    "fix", "implement", "add", "refactor", "update", "change", "write",
    "modify", "create", "delete", "remove", "clean", "patch", "repair",
    "correct", "optimize", "rewrite", "replace", "build",
})

CONVERSATION_STARTERS = frozenset({
    "what", "how", "why", "explain", "describe", "tell", "can", "is", "are",
    "does", "who", "when", "where", "list", "show", "help",
})


def classify_prompt_intent(prompt: str) -> str:
    # Classify a natural language prompt into conversation, edit, or audit
    cleaned = prompt.strip().lower()
    words = re.findall(r"\b[a-z_0-9-]+\b", cleaned)
    first_word = words[0] if words else ""

    if any(w in cleaned for w in ("verify chain", "audit ledger", "ledger status", "check ledger")):
        return "audit"

    # Check for explicit file extension occurrences
    has_file_ext = bool(re.search(r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt))

    # Prompts asking conversational questions (or ending with ?) take precedence unless explicit files/paths are given
    if first_word in CONVERSATION_STARTERS or cleaned.endswith("?"):
        if not has_file_ext and not any(w in cleaned for w in ("refactor ", "implement ", "fix bug ", "add test")):
            return "conversation"

    has_mutation_verb = any(w in MUTATION_KEYWORDS for w in words)

    if has_mutation_verb or has_file_ext:
        return "edit"

    return "conversation"


def discover_target_files(prompt: str, root_dir: Optional[Path] = None) -> List[str]:
    # Autonomously identify candidate target files from prompt or repository
    root = root_dir or Path.cwd()
    candidates: List[str] = []

    # 1. Regex search for explicit filenames in prompt
    matches = re.findall(r"\b[a-zA-Z0-9_./-]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt)
    for m in matches:
        clean_path = m.strip("`'\" \t\r\n")
        full_path = root / clean_path
        if full_path.exists() and clean_path not in candidates:
            candidates.append(clean_path.replace("\\", "/"))

    if candidates:
        return candidates

    # 2. Heuristic search: check if prompt mentions existing python module names
    words = set(re.findall(r"\b[a-zA-Z_0-9-]+\b", prompt.lower()))
    harness_dir = root / "harness"
    if harness_dir.is_dir():
        for p in sorted(harness_dir.glob("*.py")):
            stem = p.stem.lower()
            if stem in words and stem != "__init__":
                rel = p.relative_to(root).as_posix()
                if rel not in candidates:
                    candidates.append(rel)

    return candidates


_REPO_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                   "dist", "build", "audits", ".ruff_cache", "chat_history"}
_REPO_SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".jsonl"}


def enumerate_repo_files(root_dir: Optional[Path] = None, limit: int = 300) -> List[str]:
    """The whole-repo listing for the relevance first pass (bounded, junk-free)."""
    root = (root_dir or Path.cwd()).resolve()
    files: List[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _REPO_SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _REPO_SKIP_SUFFIXES:
            continue
        files.append(p.relative_to(root).as_posix())
        if len(files) >= limit:
            break
    return files


def discover_verification_gate(target_files: Sequence[str], root_dir: Optional[Path] = None) -> Optional[str]:
    # Autonomously resolve a verification gate command for targeted files
    root = root_dir or Path.cwd()
    if not target_files:
        return None

    # Check primary target file
    primary = target_files[0].replace("\\", "/")
    if primary.startswith("harness/") and primary.endswith(".py"):
        mod_name = Path(primary).stem
        test_path = root / "tests" / f"test_{mod_name}.py"
        if test_path.exists():
            # Absolute: the gate runner's CWD is the server's, not the
            # agent's chosen root -- a relative path compiles/tests the
            # wrong tree (or nothing) for GUI runs with a workDir.
            return f"python -m unittest {test_path}"

    # Generic check: syntax compile
    if primary.endswith(".py") and (root / primary).exists():
        return f"python -m py_compile \"{root / primary}\""

    return None


def get_default_history_dir() -> Path:
    # Resolve directory for persisted conversation histories
    base = Path(os.environ.get("HARNESS_CONFIG_DIR", Path.home() / ".config" / "harness"))
    history_dir = base / "chat_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def save_chat_turn(session_id: str, turn: Dict[str, Any], history_dir: Optional[Path] = None) -> Path:
    # Append a chat turn to the persisted JSONL history file
    hdir = history_dir or get_default_history_dir()
    file_path = hdir / f"{session_id}.jsonl"
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn, ensure_ascii=False) + "\n")
    return file_path


def load_chat_history(session_id: str, history_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    # Load past turns for a session
    hdir = history_dir or get_default_history_dir()
    file_path = hdir / f"{session_id}.jsonl"
    if not file_path.exists():
        return []
    history: List[Dict[str, Any]] = []
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return history


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
            if intent == "edit" and auto_apply:
                emit("chat_escalated", reason="edit-intent in auto mode",
                     target="plan_lane")
                return self._handle_edit(prompt, sid, auto_apply, cancel_check)
            return self._handle_conversation(prompt, sid, cancel_check, web=web)

        intent = classify_prompt_intent(prompt)
        emit("intent_classified", intent=intent, prompt=prompt)

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        if intent == "conversation":
            return self._handle_conversation(prompt, sid, cancel_check, web=web)
        elif intent == "audit":
            return self._handle_audit(prompt, sid, cancel_check)
        else:
            return self._handle_edit(prompt, sid, auto_apply, cancel_check)

    def _gather_web_context(self, prompt: str) -> List[Dict[str, Any]]:
        # Evidence for one conversation turn: allowlist fetches for any URL in
        # the prompt, one search otherwise. Every failure is recorded, never
        # hidden -- the model sees exactly what did and did not come back.
        sources: List[Dict[str, Any]] = []
        urls = find_urls(prompt)[:_MAX_WEB_SOURCES]
        if urls:
            for u in urls:
                try:
                    page = fetch_url(u, allowed_hosts=DEFAULT_FETCH_HOSTS)
                    sources.append({"kind": "fetch", "ok": True, "url": page["url"],
                                    "title": page["title"], "text": page["text"]})
                except HarnessError as e:
                    sources.append({"kind": "fetch", "ok": False, "url": u,
                                    "note": str(e)})
            if any(s["ok"] for s in sources):
                return sources
            # Every fetch failed (transient challenge, timeout, upstream 5xx).
            # Fall through to one search so the turn still carries evidence;
            # the FAILED fetch notes stay -- nothing is hidden.

        try:
            results = search_web(extract_query(prompt))
            for r in results[:_MAX_WEB_SOURCES]:
                sources.append({"kind": "search", "ok": True, "url": r["url"],
                                "title": r["title"], "text": r["snippet"]})
        except HarnessError as e:
            sources.append({"kind": "search", "ok": False, "note": str(e)})
        return sources

    @staticmethod
    def _chat_ladder(settings: Settings) -> List[str]:
        # The chat lane's rotation ladder: tier-1 head, then the free panel
        # pool (different providers, so upstream rate limits rarely line up),
        # then -- only when escalation is allowed -- the paid rungs. The same
        # settings-owned ladders every other lane walks; no new policy.
        models = [getattr(settings, "tier1_model", None)
                  or settings.judge or "inclusionai/ling-3.0-flash-fin:free"]
        for m in list(settings.panel_pool or []):
            if m not in models:
                models.append(m)
        if settings.allow_escalation and settings.escalation_pool:
            for m in settings.escalation_pool:
                if m not in models:
                    models.append(m)
        return models

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
    ) -> Dict[str, Any]:
        # Handle informational or technical questions with conversational routing
        api_key, gov = governor_for(self.settings)

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
            try:
                gov.preflight(prompt, [("chat", model, 4096, 0)])
                status, resp = chat(
                    transport=self.transport,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    max_tokens=4096,
                    reasoning_effort="off",
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
            defer_reason = ("response truncated at the token cap on every "
                            "ladder model (max_tokens=4096)")
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

    def _plan_round(self, goal, candidate_files, gov):
        """One planning pass: LLM decomposition when the orchestration ladder
        answers (the smarter model breaks the goal into realistic, bite-sized
        steps), the heuristic decomposition as the loud fallback."""
        repo_context = None
        decomposed = None
        decomposition = "heuristic"
        if candidate_files:
            repo_context = "repository files:\n" + "\n".join(candidate_files[:200])
        try:
            decomposed = decompose_via_llm(
                self._orchestrator_chat_fn(gov), goal,
                candidate_files=candidate_files, repo_context=repo_context)
            decomposition = "llm:orchestrator"
        except HarnessError as e:
            emit("orchestration_note", note=f"LLM decomposition failed: {e}")
        plan = plan_task(goal=goal, candidate_files=candidate_files,
                         custom_frontier=self.settings.frontier_model,
                         use_free=self.settings.use_free,
                         decomposed_dag=decomposed)
        plan["decomposition"] = decomposition
        return plan

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
    ) -> Dict[str, Any]:
        # Handle code edit/refactor requests: the orchestrator drives the
        # hourglass -- relevance triage over the whole repo, DAG decomposition
        # (LLM-authored when the ladder answers), governed execution, then a
        # completion judge round that re-plans remaining scope until the goal
        # is met or the round budget is spent.
        target_files = self._triage_scope(prompt)
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
        emit("context_condensed", estimated_tokens=brief.estimated_tokens)

        # Formulate the FIRST-round DAG plan (LLM decomposition when the
        # orchestration ladder answers; heuristic fallback) and gate.
        _, gov = governor_for(self.settings)
        plan = self._plan_round(prompt, target_files, gov)
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
            }
            save_chat_turn(session_id, result, self.history_dir)
            return result

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        # Autonomous execution mode: the orchestrator drives rounds --
        # plan -> execute EVERY node (keep_going: failures are state for the
        # judge, not abort) -> completion judge -> re-plan remaining scope --
        # until the judge calls it complete or the round budget is spent.
        engine = apply_session(self.settings)
        executor = ConcurrentExecutor(max_workers=1)

        def run_node(node: DAGNode, gate_round) -> Dict[str, Any]:
            if cancel_check and cancel_check():
                raise ToolCancelled("Subtask cancelled by user")
            target = node.target_files[0] if node.target_files else (target_files[0] if target_files else None)
            gate = node.local_gate or gate_round
            if gate is None and target and target.endswith(".py"):
                # A .py node is always verifiable after its file exists --
                # including a file this node CREATES. Gateless nodes are
                # refused at mutation time (unknown trust writes nothing
                # unreviewed), which would kill new-file subtasks outright.
                gate = f'python -m py_compile "{self.root_dir / target}"'
            route_kwargs = node_apply_kwargs(node_routes.get(node.node_id))
            emit("subtask_start", node_id=node.node_id, instruction=node.instruction, target=target)

            # The engine resolves paths against process CWD (the server's
            # directory, not the agent's chosen root): hand it the absolute
            # target so GUI runs with a workDir land in the right tree.
            engine_target = target
            if target and not Path(target).is_absolute():
                engine_target = str(self.root_dir / target)

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

            res = engine.apply_edit(
                file_path=engine_target,
                instruction=node.instruction,
                verify_cmd=gate,
                allow_verify=True,
                require_consent=False,
                **route_kwargs,
            )

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
                    require_consent=False,
                    **route_kwargs,
                )
                if isinstance(res, dict):
                    res["cost"] = round(prior_cost + float(res.get("cost", 0.0) or 0.0), 6)

            emit("subtask_finish", node_id=node.node_id, status=res.get("status"))
            return res

        MAX_ORCH_ROUNDS = 3
        all_results: Dict[str, Dict[str, Any]] = {}
        total_cost = 0.0
        rounds_history: List[Dict[str, Any]] = []
        final_all_ok = False
        remaining_scope = ""
        current_goal = prompt
        for round_no in range(1, MAX_ORCH_ROUNDS + 1):
            if cancel_check and cancel_check():
                raise ToolCancelled("Prompt execution was cancelled by user")
            if round_no > 1:
                emit("orchestration_round", round=round_no, goal=current_goal)
                plan = self._plan_round(current_goal, target_files, gov)
                emit("dag_planned", total_nodes=plan["total_nodes"],
                     total_ceiling=plan["total_cost_ceiling"],
                     nodes=[{"node_id": n["node_id"],
                             "instruction": n["instruction"],
                             "target": (n.get("target_files") or [""])[0]}
                            for n in plan.get("nodes", [])])
                verification_gate = discover_verification_gate(
                    target_files, self.root_dir)
            dag = TaskDAG.from_dict(plan["dag"])
            if not dag.nodes:
                # The planner had nothing executable: honest stop, not a
                # fake completion.
                remaining_scope = ("planner produced no executable nodes "
                                   f"for: {current_goal[:150]}")
                break
            node_routes = {n.get("node_id"): n for n in plan["nodes"]}
            round_results = executor.execute_dag(
                dag, lambda n, g=verification_gate: run_node(n, g),
                keep_going=True)
            for nid, r in round_results.items():
                if isinstance(r, dict):
                    r.setdefault("node_id", nid)
            all_results.update(round_results)
            round_cost = sum(float(r.get("cost", 0.0) or 0.0)
                             for r in round_results.values())
            total_cost = round(total_cost + round_cost, 6)
            round_ok = all(r.get("status") in SUCCESS_STATUSES
                           for r in round_results.values())
            rounds_history.append({
                "round": round_no, "goal": current_goal[:200],
                "nodes": len(dag.nodes), "all_nodes_ok": round_ok,
                "cost": round(round_cost, 6),
            })

            # The completion judge: one governed verdict on the round's state.
            # Artifact truth first: the judge sees node statuses, never the
            # repo -- a node that "succeeded" on the wrong scope would read
            # as completion. Name the real state of every file the goal
            # mentions (bounded), then let the judge weigh it.
            artifact_notes = []
            seen_paths = set()

            def _note_artifact(rel):
                rel = rel.replace("\\", "/").strip("`'\" .")
                if (not rel or rel in seen_paths
                        or len(seen_paths) >= 8 or ".." in rel):
                    return
                seen_paths.add(rel)
                fp = self.root_dir / rel
                if fp.is_file():
                    try:
                        nlines = len(fp.read_text(
                            encoding="utf-8",
                            errors="replace").splitlines())
                    except OSError:
                        nlines = 0
                    artifact_notes.append(
                        f"artifact truth: {rel} present ({nlines} lines)")
                else:
                    artifact_notes.append(
                        f"artifact truth: {rel} MISSING from the repository")

            for tf in target_files:
                _note_artifact(tf)
            for m in re.finditer(r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml)\b",
                                 prompt):
                _note_artifact(m.group(0))
            summary = build_state_summary(
                current_goal, list(round_results.values()),
                extra_notes=[f"orchestrator round {round_no} of {MAX_ORCH_ROUNDS}"]
                + artifact_notes)
            verdict = None
            try:
                verdict = assess_completion(prompt, summary,
                                            self._orchestrator_chat_fn(gov))
            except HarnessError as e:
                emit("orchestration_note", note=f"completion judge failed: {e}")
            if verdict is None:
                # Judge unavailable/unusable: degrade to node statuses --
                # honest about what we know, no invented completion.
                final_all_ok = round_ok and len(rounds_history) > 0
                if not final_all_ok:
                    remaining_scope = ("completion judge unavailable; "
                                       "see per-node failures")
                break
            missing = [n for n in artifact_notes if "MISSING" in n]
            if verdict["complete"] and missing:
                # Structural guard: a judge verdict cannot make a missing
                # artifact exist. The loop continues on the real remainder.
                emit("orchestration_note",
                     note=f"judge said complete but {len(missing)} named "
                          f"artifact(s) missing; overriding to incomplete")
                verdict = {"complete": False,
                           "remaining": "; ".join(missing),
                           "reason": "named artifacts missing despite verdict"}
            if verdict["complete"]:
                final_all_ok = True
                break
            remaining_scope = verdict["remaining"] or verdict["reason"]
            current_goal = remaining_scope or prompt

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
        if final_all_ok:
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
            response_msg += (f"\n\n**Auto-escalated to the plan lane (hourglass)** "
                             f"after a capability defer: {escalation_note}")

        result = {
            "status": "ok" if final_all_ok else "failed",
            **({"escalated_from_defer": escalation_note} if escalation_note else {}),
            "intent": "edit",
            "prompt": prompt,
            "response": response_msg,
            "target_files": target_files,
            "diff": unified_diff_str,
            "verification_gate": verification_gate,
            "cost": round(total_cost, 6),
            "results": list(all_results.values()),
            "orchestrator_rounds": len(rounds_history),
            "orchestrator_history": rounds_history,
            **({"remaining_scope": remaining_scope} if not final_all_ok else {}),
        }

        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="edit", status=result["status"], cost=total_cost)
        return result
