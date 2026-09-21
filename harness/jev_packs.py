"""JEV pack helpers: P3 utilization packs + P5 operator issue-sort packs.

ONE vocabulary owner for typed route choice and the decision-relevant
question packs (JEV-P3). Code owns paths, listing validation, artifact
existence, and keyword fallbacks; Jev owns only bounded semantic judgment
when keyed. Route values are execution-route vocabulary (``free-distill`` /
``diff`` / ``frontier``) — never provider brand ids.

JEV-P5 issue-sort packs: the operator declares buckets; code owns matching.
TypeSafe/Jev choice criteria are exactly the operator bucket labels — this
module never invents buckets, path_ids, or suggested actions. Unkeyed /
transport-fail / out-of-pack paths use ``match_keywords`` against pack
keywords only.
"""
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --- JEV-P3 utilization vocabulary / limits ---
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


# --- JEV-P5 operator issue-sort packs (schema + matcher only) ---

BUCKET_KINDS = frozenset(
    ("trouble_area", "alternate_path", "orchestration_driver"))


def validate_operator_pack(pack: Any) -> Dict[str, Any]:
    """Validate an operator-declared issue-sort pack; return a clean copy.

    Required shape:
      {id: str, buckets: {bucket_id: {
          label: str,
          kind: trouble_area|alternate_path|orchestration_driver,
          path_id: str,
          keywords: [str, ...],
          suggested_next_action: str|None,
          attention: scalar|None,
      }}}
    """
    if not isinstance(pack, dict):
        raise ValueError("operator pack must be an object")
    pack_id = pack.get("id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("operator pack requires a non-empty string id")
    buckets = pack.get("buckets")
    if not isinstance(buckets, dict) or not buckets:
        raise ValueError("operator pack requires a non-empty buckets map")
    out_buckets: Dict[str, Dict[str, Any]] = {}
    for bid, bucket in buckets.items():
        if not isinstance(bid, str) or not bid:
            raise ValueError("bucket ids must be non-empty strings")
        if not isinstance(bucket, dict):
            raise ValueError(f"bucket {bid!r} must be an object")
        label = bucket.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(f"bucket {bid!r} requires a non-empty label")
        kind = bucket.get("kind")
        if kind not in BUCKET_KINDS:
            raise ValueError(
                f"bucket {bid!r} kind must be one of {sorted(BUCKET_KINDS)}")
        path_id = bucket.get("path_id")
        if not isinstance(path_id, str) or not path_id:
            raise ValueError(f"bucket {bid!r} requires a non-empty path_id")
        keywords = bucket.get("keywords", [])
        if not isinstance(keywords, list) or any(
                not isinstance(k, str) or not k for k in keywords):
            raise ValueError(
                f"bucket {bid!r} keywords must be a list of non-empty strings")
        action = bucket.get("suggested_next_action")
        if action is not None and not isinstance(action, str):
            raise ValueError(
                f"bucket {bid!r} suggested_next_action must be a string when set")
        attention = bucket.get("attention")
        if attention is not None and not isinstance(
                attention, (str, int, float, bool)):
            raise ValueError(
                f"bucket {bid!r} attention must be a scalar when set")
        out_buckets[bid] = {
            "label": label,
            "kind": kind,
            "path_id": path_id,
            "keywords": list(keywords),
            "suggested_next_action": action,
            "attention": attention,
        }
    return {"id": pack_id, "buckets": out_buckets}


def issue_sort_question_pack(pack: Any) -> Dict[str, Dict[str, Any]]:
    """Build the TypeSafe choice pack: criteria keys = operator bucket ids,
    criteria values = operator labels only. No invented keys."""
    pack_doc = validate_operator_pack(pack)
    criteria = {
        bid: entry["label"] for bid, entry in pack_doc["buckets"].items()
    }
    return {
        "bucket": {
            "type": "choice",
            "instructions": (
                "Choose the operator-declared bucket that best matches this "
                "issue. Select only from the declared criteria keys; do not "
                "invent categories."),
            "criteria": criteria,
        }
    }


def match_keywords(
    text: Any, pack: Any
) -> Tuple[Optional[str], int, List[str]]:
    """Match issue text against pack keywords only.

    Returns ``(bucket_id|None, score, evidence)``. Score is the hit count for
    the winning bucket; no match yields ``(None, 0, [])``. Deterministic:
    highest score wins; ties keep the lexicographically first bucket id.
    """
    pack_doc = validate_operator_pack(pack)
    lower = (text or "").lower() if isinstance(text, str) else ""
    best_id: Optional[str] = None
    best_score = 0
    best_ev: List[str] = []
    for bid in sorted(pack_doc["buckets"]):
        keywords = pack_doc["buckets"][bid].get("keywords") or []
        hits = [kw for kw in keywords if kw.lower() in lower]
        score = len(hits)
        if score > best_score:
            best_id, best_score, best_ev = bid, score, hits
    if best_score <= 0 or best_id is None:
        return None, 0, []
    return best_id, best_score, best_ev


def sort_notes_into_buckets(
    notes: Any, pack: Any, policy: Any, *, site: str = "issue_sort"
) -> List[Dict[str, Any]]:
    """Thin orchestrator/agent helper: sort open issues / deferral notes into
    declared attention buckets. ``path_id`` always comes from the pack via
    ``evaluate_issue_sort`` — never invented here."""
    if not notes:
        return []
    items = notes if isinstance(notes, (list, tuple)) else [notes]
    out: List[Dict[str, Any]] = []
    for note in items:
        if isinstance(note, str):
            text = note
        elif isinstance(note, dict):
            text = (note.get("text") or note.get("reason")
                    or note.get("issue") or note.get("note") or "")
        else:
            text = str(note)
        _result, _structural, combo = policy.evaluate_issue_sort(
            {"issue": text}, pack, site=site)
        out.append(combo)
    return out


# --- HUL-C mission scope packs (site=hul_scope) ---

HUL_SCOPE_SITE = "hul_scope"
SCOPE_COMPLEXITY_VOCABULARY = ("bounded", "iterative", "architectural")
# Code-owned hold thresholds for scope determination (not model brands).
SCOPE_COVERAGE_HOLD = 0.5
SCOPE_NOUL_HOLD = 0.5


def hul_scope_question_pack() -> Dict[str, Dict[str, Any]]:
    """HUL-C scope question pack consumed by ``JevPolicy.evaluate_scope``.

    Typed questions only: one score, three nouls, one choice. Criteria are
    fixed vocabulary — never provider brands or invented mission facts.
    """
    return {
        "scope_coverage": {
            "type": "score",
            "instructions": (
                "Rate how completely the attempt evidence covers the "
                "mission scope.in_scope entries."),
            "criteria": [
                "little or no in-scope work is evidenced",
                "some in-scope items evidenced, others missing",
                "all in-scope items are evidenced",
            ],
        },
        "success_definition_met": _noul(
            "Does the attempt evidence show the mission success_definition "
            "is met?",
            "Evidence demonstrates the stated success definition.",
            "Evidence does not demonstrate the stated success definition."),
        "claims_supported": _noul(
            "Are the mission claims supported by the attempt evidence?",
            "Claims are backed by artifacts or receipts.",
            "Claims are unsupported or contradicted by evidence."),
        "needs_human": _noul(
            "Does this mission require human intervention beyond the driver?",
            "Human action is required before the mission can complete.",
            "No human intervention is required beyond automated attempts."),
        "complexity_class": {
            "type": "choice",
            "instructions": (
                "Choose the complexity class that matches this mission."),
            "criteria": {
                "bounded": "single-step or low-dependency mission work",
                "iterative": "dependent steps or loop-shaped work",
                "architectural": "multi-component or high-dependency work",
            },
        },
    }


def scope_in_scope_holds(scope: Any) -> bool:
    """Code-owned: declared scope must be non-empty for a complete claim."""
    if not isinstance(scope, dict):
        return False
    in_scope = scope.get("in_scope")
    if not isinstance(in_scope, (list, tuple)):
        return False
    return any(isinstance(item, str) and item.strip() for item in in_scope)


def normalize_complexity_class(value: Any) -> Optional[str]:
    """Return a declared complexity vocabulary id, or None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("_", "-").replace(" ", "-")
    if cleaned in SCOPE_COMPLEXITY_VOCABULARY:
        return cleaned
    return None
