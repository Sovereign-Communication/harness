"""JEV-P3 utilization packs and pure helpers (imported by jev_policy).

ONE vocabulary owner for typed route choice and the decision-relevant
question packs. Code owns paths, listing validation, artifact existence,
and keyword fallbacks; Jev owns only bounded semantic judgment when keyed.
Route values are execution-route vocabulary (``free-distill`` / ``diff`` /
``frontier``) — never provider brand ids.
"""
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROUTE_VOCABULARY = ("free-distill", "diff", "frontier")
ROUTE_TIER_HINT = {
    "free-distill": 0,
    "diff": 1,
    "frontier": 2,
}
MAX_FILE_TRIAGE = 15
MAX_CLAIM_SUPPORT_PACK = 8
MAX_CONTEXT_PACK_CHARS = 1200

_ARTIFACT_RE = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml|rs|go)\b")
_WORD_RE = re.compile(r"[a-z_0-9]+")
_ITERATIVE_MARKERS = (
    "iterat", "loop", "branch", "recur", "algorithm", "architect",
    "concurr", "state machine", "distributed", "protocol", "dag",
)


def _noul(question: str, yes: str, no: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": question,
            "criteria": {"true": yes, "false": no}}


def route_question_pack() -> Dict[str, Dict[str, Any]]:
    """Typed route choice pack for JEV-P3-route — vocabulary, not brands."""
    return {
        "route": {
            "type": "choice",
            "instructions": (
                "Choose the least capable execution route that can safely "
                "complete this task."),
            "criteria": {
                "free-distill": "bounded single-step or low-risk edit",
                "diff": "mechanical or multi-file diff-shaped edit",
                "frontier": "iterative, architectural, or high-dependency task",
            },
        },
        "requires_iteration": _noul(
            "Does the task require iterative control flow or dependent steps?",
            "The task requires iteration or dependent steps.",
            "The task is a bounded single-step edit."),
    }


def file_relevance_question_pack(paths: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Noul relevance pack over orchestrator file candidates (JEV-P3-triage-files)."""
    pack: Dict[str, Dict[str, Any]] = {}
    for index, path in enumerate(list(paths)[:MAX_FILE_TRIAGE]):
        pack[f"file_{index}_relevant"] = _noul(
            f"Is the file {path!r} relevant to this goal?",
            "The file plausibly needs to be read or modified to serve the goal.",
            "The file is unrelated to the goal.")
    return pack


def claim_support_question_pack(n_claims: int) -> Dict[str, Dict[str, Any]]:
    """Lean claim-support noul pack for JEV-P3-claims (optional, flag-gated)."""
    pack: Dict[str, Dict[str, Any]] = {}
    for index in range(min(max(int(n_claims), 0), MAX_CLAIM_SUPPORT_PACK)):
        pack[f"claim_{index}_supported"] = _noul(
            f"Is claim_{index} supported by the quoted evidence?",
            "The quoted evidence justifies the claim text.",
            "The quoted evidence does not justify the claim text.")
    return pack


def completion_question_pack() -> Dict[str, Dict[str, Any]]:
    """Artifact/goal nouls for JEV-P3-completion, before any generative judge."""
    return {
        "named_artifacts_present": _noul(
            "Are all named artifacts present and non-empty in the repository facts?",
            "Every named artifact listed in state is present.",
            "At least one named artifact is missing or empty."),
        "goal_achieved": _noul(
            "Does the execution state show the stated goal is achieved?",
            "Execution state demonstrates the goal is complete.",
            "Execution state leaves the goal incomplete or unproven."),
    }


def normalize_route(value: Any) -> Optional[str]:
    """Return a vocabulary route id, or None when the value is not a route."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("_", "-").replace(" ", "-")
    if cleaned in ROUTE_VOCABULARY:
        return cleaned
    return None


def route_tier_hint(route: Optional[str]) -> Optional[int]:
    """Map a typed route to a sliding-scale tier hint, or None if unusable."""
    return ROUTE_TIER_HINT.get(normalize_route(route) or "")


def heuristic_route(prompt: str, target_files: Optional[Sequence[str]] = None) -> str:
    """Unkeyed route heuristic — honest fallback, never live judgment."""
    lower = (prompt or "").lower()
    if any(word in lower for word in _ITERATIVE_MARKERS):
        return "frontier"
    if len(list(target_files or [])) > 1:
        return "diff"
    return "free-distill"


def heuristic_requires_iteration(prompt: str) -> bool:
    lower = (prompt or "").lower()
    return any(word in lower for word in _ITERATIVE_MARKERS)


def heuristic_file_relevance(goal: str, files: Sequence[str],
                             max_files: int = MAX_FILE_TRIAGE) -> List[str]:
    """Keyword overlap fallback over real listing names (JEV-P3-triage-files)."""
    words = set(_WORD_RE.findall((goal or "").lower()))
    scored: List[tuple] = []
    for f in files:
        stem = re.sub(r"\.[^.]+$", "", str(f).rsplit("/", 1)[-1]).lower()
        stem_words = set(stem.split("_")) | {stem}
        score = len(words & stem_words)
        if score:
            scored.append((-score, f))
    scored.sort()
    return [f for _, f in scored[:max_files]]


def validate_candidates(candidates: Iterable[str],
                        known_files: Optional[Sequence[str]]) -> List[str]:
    """Keep only paths that exist in the real listing (when one is supplied)."""
    out: List[str] = []
    if known_files is None:
        known = None
    else:
        known = {str(p).replace("\\", "/") for p in known_files}
    for raw in candidates or []:
        path = str(raw).strip().replace("\\", "/")
        if not path:
            continue
        if known is not None and path not in known:
            continue
        if path not in out:
            out.append(path)
    return out


def build_context_pack(
    goal: str,
    candidate_files: Optional[Sequence[str]] = None,
    repo_context: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    max_chars: int = MAX_CONTEXT_PACK_CHARS,
) -> str:
    """Condensed decision-relevant state for generative seats (JEV-P3-context-pack).

    Filters to goal + candidate scope + named extras; never invents facts.
    When a richer ``repo_context`` already exists it is preserved (trimmed).
    """
    if repo_context and str(repo_context).strip():
        text = str(repo_context).strip()
        return text[:max_chars] if len(text) > max_chars else text
    lines: List[str] = ["DECISION CONTEXT (distilled):"]
    goal_line = (goal or "").strip().replace("\n", " ")
    lines.append(f"goal: {goal_line[:400]}")
    files = [str(p).replace("\\", "/") for p in (candidate_files or [])]
    if files:
        shown = files[:12]
        more = len(files) - len(shown)
        lines.append("candidate_files: " + ", ".join(shown)
                     + (f" (+{more} more)" if more > 0 else ""))
    else:
        lines.append("candidate_files: (none declared)")
    for key, value in (extra or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(v) for v in list(value)[:8])
        else:
            rendered = str(value).replace("\n", " ")[:200]
        lines.append(f"{key}: {rendered}")
    pack = "\n".join(lines)
    return pack[:max_chars]


def named_artifact_status(
    goal: str,
    target_files: Optional[Sequence[str]] = None,
    root_dir=None,
    extra_names: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Code-owned artifact facts for completion (JEV-P3-completion)."""
    root = Path(root_dir) if root_dir is not None else Path.cwd()
    names: List[str] = []
    seen = set()
    for source in (list(target_files or []), list(extra_names or []),
                   _ARTIFACT_RE.findall(goal or "")):
        for raw in source:
            rel = str(raw).replace("\\", "/").strip("`'\" .")
            if not rel or rel in seen or ".." in rel:
                continue
            seen.add(rel)
            names.append(rel)
            if len(names) >= 8:
                break
        if len(names) >= 8:
            break
    out: List[Dict[str, Any]] = []
    for rel in names:
        path = root / rel
        present = path.is_file()
        lines = 0
        if present:
            try:
                lines = len(path.read_text(encoding="utf-8",
                                           errors="replace").splitlines())
            except OSError:
                lines = 0
        out.append({"path": rel, "present": present, "lines": lines})
    return out


def missing_named_artifacts(artifact_status: Sequence[Dict[str, Any]]) -> List[str]:
    return [item["path"] for item in artifact_status
            if isinstance(item, dict) and not item.get("present")]


def claims_from_payload(claims: Any) -> List[Dict[str, str]]:
    """Normalize claim payloads to [{id, text}] without owning claims lint."""
    out: List[Dict[str, str]] = []
    if claims is None:
        return out
    if isinstance(claims, dict) and "claims" in claims:
        claims = claims.get("claims")
    if not isinstance(claims, (list, tuple)):
        return out
    for i, item in enumerate(claims):
        if isinstance(item, dict):
            cid = str(item.get("id") or item.get("claim_id") or f"claim_{i}")
            text = str(item.get("text") or item.get("claim") or "").strip()
        else:
            cid = f"claim_{i}"
            text = str(item or "").strip()
        if text:
            out.append({"id": cid, "text": text})
    return out[:MAX_CLAIM_SUPPORT_PACK]
