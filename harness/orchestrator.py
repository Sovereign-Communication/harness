"""Autonomous orchestration policy.

This module owns the decisions around a planned edit: repository triage,
completion judgment, bounded round progression, and artifact-truth checks.
Execution mechanics remain injected through ``execute_plan`` so the shared
PlanExecutor stays the single owner of reservations, workers, isolation, and
gates.
"""
import json
import re
from pathlib import Path
from typing import Callable, Dict, List

from .errors import HarnessError, ToolCancelled

MAX_TRIAGE_FILES = 15
MAX_ORCH_ROUNDS = 3

_COMPLETION_PROMPT = """You are the completion judge for an autonomous coding orchestrator.
A goal was broken into subtasks and executed by cheaper models. Decide whether
the goal is now COMPLETE, using only the execution state below.

GOAL:
{goal}

EXECUTION STATE (per-subtask results, verification gates, errors):
{state}

Answer with ONE JSON object, no prose:
{{"complete": true|false, "remaining": "<what is still missing, empty when complete>", "reason": "<one line justification>"}}
Be strict: partial work, failed gates, or unfinished scope mean complete=false.
"""

_TRIAGE_PROMPT = """You are the file-triage pass for an autonomous coding orchestrator.
GOAL:
{goal}

REPOSITORY FILES:
{files}

Pick the files that plausibly need to be read or modified to serve this goal
(new files the goal should create are NOT in this list; name only existing ones).
Answer with ONE JSON object, no prose:
{{"files": ["<path>", ...]}}
At most {max_n} paths, all copied exactly from the repository list above.
"""


def _extract_json_blob(text):
    """The first balanced {...} block in the response, or None."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def assess_completion(goal, state_summary, chat_fn):
    """Return a strict completion verdict, or ``None`` for unusable output."""
    prompt = _COMPLETION_PROMPT.format(goal=goal.strip(),
                                       state=state_summary.strip())
    data = _extract_json_blob(chat_fn(prompt))
    if not isinstance(data, dict) or not isinstance(data.get("complete"), bool):
        return None
    return {
        "complete": data["complete"],
        "remaining": str(data.get("remaining") or "")[:2000],
        "reason": str(data.get("reason") or "")[:500],
    }


def triage_files(goal, files, chat_fn, max_files=MAX_TRIAGE_FILES):
    """Select relevant files, validating every model-selected path."""
    if not files:
        return []
    listing = "\n".join(files[:400])
    prompt = _TRIAGE_PROMPT.format(goal=goal.strip(), files=listing,
                                   max_n=max_files)
    try:
        data = _extract_json_blob(chat_fn(prompt))
    except (HarnessError, OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return []
    known = set(files)
    picked: List[str] = []
    for item in data["files"]:
        path = str(item).strip().replace("\\", "/")
        if path in known and path not in picked:
            picked.append(path)
        if len(picked) >= max_files:
            break
    return picked


def keyword_fallback(goal, files, max_files=MAX_TRIAGE_FILES):
    """The no-model triage fallback: keyword overlap with file names/stems."""
    words = set(re.findall(r"[a-z_0-9]+", goal.lower()))
    scored: List[tuple] = []
    for f in files:
        stem = re.sub(r"\.[^.]+$", "", f.rsplit("/", 1)[-1]).lower()
        stem_words = set(stem.split("_")) | {stem}
        score = len(words & stem_words)
        if score:
            scored.append((-score, f))
    scored.sort()
    return [f for _, f in scored[:max_files]]


def build_state_summary(node_results, extra_notes=(), max_chars=8000):
    """Render bounded execution state for the completion judge."""
    lines = [f"- note: {n}" for n in extra_notes]
    for res in node_results:
        if not isinstance(res, dict):
            continue
        lines.append(
            f"- subtask {res.get('node_id', '?')} target={res.get('file_path') or '?'} "
            f"status={res.get('status', '?')} error={str(res.get('error') or '')[:200]}")
    return "\n".join(lines)[:max_chars]


def drive(*, goal: str, target_files: List[str], initial_plan: Dict,
          root_dir, plan_round: Callable[[str], Dict],
          execute_plan: Callable[[Dict], Dict],
          completion_chat: Callable[[str], str], emit: Callable[..., None],
          cancel_check=None, refused=None, max_rounds=MAX_ORCH_ROUNDS):
    """Run plan -> execute -> judge until complete or the round budget ends.

    ``execute_plan`` is the only execution seam: the caller supplies the
    already-composed PlanExecutor invocation, while this function owns round
    state, re-planning, artifact truth, and completion semantics.
    """
    all_results: Dict[str, Dict] = {}
    total_cost = 0.0
    rounds_history: List[Dict] = []
    final_all_ok = False
    remaining_scope = ""
    current_goal = goal
    plan = initial_plan

    for round_no in range(1, max_rounds + 1):
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        if round_no > 1:
            emit("orchestration_round", round=round_no, goal=current_goal)
            plan = plan_round(current_goal)
            if plan.get("status") == "refused":
                if refused is not None:
                    return refused(plan)
                return {"status": "refused", "plan": plan}
            emit("dag_planned", total_nodes=plan["total_nodes"],
                 total_ceiling=plan["total_cost_ceiling"],
                 nodes=[{"node_id": n["node_id"],
                         "instruction": n["instruction"],
                         "target": (n.get("target_files") or [""])[0]}
                        for n in plan.get("nodes", [])])

        nodes = (plan.get("dag") or {}).get("nodes") or []
        if not nodes:
            remaining_scope = ("planner produced no executable nodes "
                               f"for: {current_goal[:150]}")
            break

        round_results = execute_plan(plan)
        for nid, result in round_results.items():
            if isinstance(result, dict):
                result.setdefault("node_id", nid)
        all_results.update({f"r{round_no}/{nid}": result
                            for nid, result in round_results.items()})
        round_cost = sum(float(result.get("cost", 0.0) or 0.0)
                         for result in round_results.values())
        total_cost = round(total_cost + round_cost, 6)
        round_ok = bool(round_results) and all(
            result.get("status") in {"ok", "changed", "completed"}
            for result in round_results.values())
        rounds_history.append({
            "round": round_no, "goal": current_goal[:200],
            "nodes": len(nodes), "all_nodes_ok": round_ok,
            "cost": round(round_cost, 6),
        })

        artifact_notes = []
        seen_paths = set()

        def note_artifact(rel):
            rel = str(rel).replace("\\", "/").strip("`'\" .")
            if (not rel or rel in seen_paths or len(seen_paths) >= 8
                    or ".." in rel):
                return
            seen_paths.add(rel)
            path = Path(root_dir) / rel
            if path.is_file():
                try:
                    lines = len(path.read_text(encoding="utf-8",
                                               errors="replace").splitlines())
                except OSError:
                    lines = 0
                artifact_notes.append(f"artifact truth: {rel} present ({lines} lines)")
            else:
                artifact_notes.append(f"artifact truth: {rel} MISSING from the repository")

        for target in target_files:
            note_artifact(target)
        for match in re.finditer(
                r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml)\b",
                goal):
            note_artifact(match.group(0))

        summary = build_state_summary(
            list(round_results.values()),
            extra_notes=[f"orchestrator round {round_no} of {max_rounds}"]
            + artifact_notes)
        try:
            verdict = assess_completion(goal, summary, completion_chat)
        except HarnessError as exc:
            emit("orchestration_note", note=f"completion judge failed: {exc}")
            verdict = None
        if verdict is None:
            final_all_ok = round_ok
            if not final_all_ok:
                remaining_scope = "completion judge unavailable; see per-node failures"
            break

        missing = [note for note in artifact_notes if "MISSING" in note]
        if verdict["complete"] and not round_ok:
            emit("orchestration_note",
                 note="judge said complete but one or more nodes did not succeed; "
                      "overriding to incomplete")
            verdict = {"complete": False,
                       "remaining": "one or more execution nodes did not complete",
                       "reason": "node failure despite completion verdict"}
        if verdict["complete"] and missing:
            emit("orchestration_note",
                 note=f"judge said complete but {len(missing)} named artifact(s) missing; "
                      "overriding to incomplete")
            verdict = {"complete": False,
                       "remaining": "; ".join(missing),
                       "reason": "named artifacts missing despite verdict"}
        if verdict["complete"]:
            final_all_ok = True
            break
        remaining_scope = verdict["remaining"] or verdict["reason"]
        current_goal = remaining_scope or goal

    return {
        "all_results": all_results,
        "total_cost": total_cost,
        "rounds_history": rounds_history,
        "final_all_ok": final_all_ok,
        "remaining_scope": remaining_scope,
        "plan": plan,
    }
