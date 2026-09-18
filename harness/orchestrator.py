"""The intermediary driver's judgment calls: completion verdicts and file triage.

The plan lane decomposes and executes; THIS module owns the two orchestration
decisions around it:

- ``assess_completion`` -- the judge round after execution: did the work
  actually satisfy the goal, or is there honest remaining scope? One owner
  of the completion verdict, the same strict-JSON schema-validated style as
  ``dag.decompose_via_llm`` (chat_fn injected, hermetically testable).
- ``triage_files`` -- the relevance first pass: which of the repo's files
  can plausibly bear on the goal. The model selects; the caller's real file
  listing validates the answer, so hallucinated paths are dropped.

Neither decision is ever invented: an unusable judge response returns
``None`` while a judge that fails outright raises ``HarnessError`` for the
caller's loud degradation note, and a triage that selects nothing falls
back to the caller's heuristic.
"""
import json
import re
from typing import List

from .errors import HarnessError

MAX_TRIAGE_FILES = 15

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
    """The judge round: is the goal complete?

    ``chat_fn(prompt) -> str`` is injected (governed upstream, the same seam
    as ``decompose_via_llm``). Returns the parsed verdict dict
    ``{"complete": bool, "remaining": str, "reason": str}`` or ``None`` when
    the response is unusable. A failed judge (``HarnessError`` from the
    ladder) propagates: the caller owns the loud degradation note.
    """
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
    """The relevance first pass: which listed files bear on the goal?

    The model's picks are validated against the real listing (case-exact
    subset; hallucinated paths dropped) and capped at ``max_files``. Returns
    the picked paths, or ``[]`` when the model is unavailable, unusable, or
    selects nothing -- the caller then applies its own heuristic fallback.
    """
    if not files:
        return []
    listing = "\n".join(files[:400])
    prompt = _TRIAGE_PROMPT.format(goal=goal.strip(), files=listing,
                                   max_n=max_files)
    try:
        data = _extract_json_blob(chat_fn(prompt))
    except (HarnessError, OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("files")
    if not isinstance(raw, list):
        return []
    known = set(files)
    picked: List[str] = []
    for item in raw:
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


def build_state_summary(goal, node_results, extra_notes=(), max_chars=8000):
    """Render execution state for the judge: honest, bounded, per-node."""
    # Notes first: they carry the orchestrator's own context (round, scope)
    # and must survive the cap even when per-node lines fill the budget.
    lines = [f"- note: {n}" for n in extra_notes]
    for res in node_results:
        if not isinstance(res, dict):
            continue
        lines.append(
            f"- subtask {res.get('node_id', '?')} target={res.get('file_path') or '?'} "
            f"status={res.get('status', '?')} error={str(res.get('error') or '')[:200]}")
    summary = "\n".join(lines)
    return summary[:max_chars]
