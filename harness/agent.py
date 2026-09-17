# Autonomous agent orchestrator (#PR-Chat-1)
# Drives natural language prompts to conclusion automatically with zero UI clutter.
import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ._http import HttpTransport
from .chat import chat, extract_content_and_cost
from .condenser import distill_context, condense_error_log
from .config import Settings, load_settings
from .dag import TaskDAG, DAGNode, plan_task
from .errors import HarnessError, ToolCancelled
from .events import emit
from .executor import ConcurrentExecutor
from .results import SUCCESS_STATUSES
from .session import apply_session, governor_for, ledger_for

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are Sovereign Harness, an autonomous, cost-bounded software engineering AI. "
    "Be concise, clear, and direct. When answering technical questions, explain precisely "
    "and provide code snippets when helpful. Focus on correctness, zero bloat, and safety."
)

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

    # Check for file extension occurrences
    has_file_ext = bool(re.search(r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt))

    has_mutation_verb = any(w in MUTATION_KEYWORDS for w in words)

    if has_mutation_verb or has_file_ext:
        return "edit"

    if first_word in CONVERSATION_STARTERS or cleaned.endswith("?"):
        return "conversation"

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
            return f"python -m unittest tests/test_{mod_name}.py"

    # Generic check: syntax compile
    if primary.endswith(".py") and (root / primary).exists():
        return f"python -m py_compile {primary}"

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
    ) -> Dict[str, Any]:
        # Process a natural language prompt from intent to verified conclusion
        if not prompt or not prompt.strip():
            raise HarnessError("Prompt cannot be empty")

        sid = session_id or "default"
        emit("chat_turn_start", prompt=prompt, session_id=sid)

        intent = classify_prompt_intent(prompt)
        emit("intent_classified", intent=intent, prompt=prompt)

        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")

        if intent == "conversation":
            return self._handle_conversation(prompt, sid, cancel_check)
        elif intent == "audit":
            return self._handle_audit(prompt, sid, cancel_check)
        else:
            return self._handle_edit(prompt, sid, auto_apply, cancel_check)

    def _handle_conversation(
        self,
        prompt: str,
        session_id: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        # Handle informational or technical questions with conversational routing
        model = getattr(self.settings, "tier1_model", None) or self.settings.judge or "inclusionai/ling-3.0-flash-fin:free"
        api_key, gov = governor_for(self.settings)

        messages = [{"role": "system", "content": DEFAULT_CHAT_SYSTEM_PROMPT}]
        past_turns = load_chat_history(session_id, self.history_dir)
        for turn in past_turns[-10:]:
            p_text = turn.get("prompt")
            r_text = turn.get("response")
            if p_text:
                messages.append({"role": "user", "content": p_text})
            if r_text:
                messages.append({"role": "assistant", "content": r_text})
        messages.append({"role": "user", "content": prompt})

        status, resp = chat(
            transport=self.transport,
            api_key=api_key,
            model=model,
            messages=messages,
            max_tokens=4096,
            reasoning_effort="off",
            governor=gov,
        )

        content, _, cost, _ = extract_content_and_cost(resp)
        response_text = content or "I was unable to formulate a response."

        result = {
            "status": "ok",
            "intent": "conversation",
            "prompt": prompt,
            "response": response_text,
            "model": model,
            "cost": round(cost, 6),
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

    def _handle_edit(
        self,
        prompt: str,
        session_id: str,
        auto_apply: bool,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        # Handle code edit/refactor requests: autonomous discovery, DAG, execute, verify
        target_files = discover_target_files(prompt, self.root_dir)
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

        # Formulate DAG plan and sliding scale tiers
        plan = plan_task(
            goal=prompt,
            candidate_files=target_files,
            custom_frontier=self.settings.frontier_model,
            use_free=self.settings.use_free,
        )
        emit("dag_planned", total_nodes=plan["total_nodes"], total_ceiling=plan["total_cost_ceiling"])

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

        # Autonomous execution mode
        engine = apply_session(self.settings)
        dag = TaskDAG.from_dict(plan["dag"])
        executor = ConcurrentExecutor(max_workers=1)

        def run_node(node: DAGNode) -> Dict[str, Any]:
            if cancel_check and cancel_check():
                raise ToolCancelled("Subtask cancelled by user")
            target = node.target_files[0] if node.target_files else (target_files[0] if target_files else None)
            gate = node.local_gate or verification_gate
            emit("subtask_start", node_id=node.node_id, instruction=node.instruction, target=target)

            res = engine.apply_edit(
                file_path=target,
                instruction=node.instruction,
                verify_cmd=gate,
                allow_verify=True,
                require_consent=False,
            )

            # Self-healing retry on verification failure
            if res.get("status") not in SUCCESS_STATUSES and res.get("error"):
                prior_cost = float(res.get("cost", 0.0) or 0.0)
                condensed_err = condense_error_log(str(res.get("error")))
                emit("subtask_retry", node_id=node.node_id, error=condensed_err[:120])
                # Attempt retry with healing instruction
                healing_inst = f"{node.instruction}\nPREVIOUS TEST FAILURE:\n{condensed_err}"
                res = engine.apply_edit(
                    file_path=target,
                    instruction=healing_inst,
                    verify_cmd=gate,
                    allow_verify=True,
                    require_consent=False,
                )
                if isinstance(res, dict):
                    res["cost"] = round(prior_cost + float(res.get("cost", 0.0) or 0.0), 6)

            emit("subtask_finish", node_id=node.node_id, status=res.get("status"))
            return res

        all_results = executor.execute_dag(dag, run_node, keep_going=False)
        all_ok = all(r.get("status") in SUCCESS_STATUSES for r in all_results.values())
        total_cost = sum(float(r.get("cost", 0.0) or 0.0) for r in all_results.values())

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
        if all_ok:
            response_msg = (
                f"Successfully completed task. Modified {len(target_files)} file(s). "
                f"Verification gate passed cleanly."
            )
        else:
            response_msg = "Task encountered a verification failure during execution. Check diff and error details."

        result = {
            "status": "ok" if all_ok else "failed",
            "intent": "edit",
            "prompt": prompt,
            "response": response_msg,
            "target_files": target_files,
            "diff": unified_diff_str,
            "verification_gate": verification_gate,
            "cost": round(total_cost, 6),
            "results": list(all_results.values()),
        }

        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="edit", status=result["status"], cost=total_cost)
        return result
