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
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import HarnessError

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


def escalation_decision_pack() -> Dict[str, Dict[str, Any]]:
    """Typed Jev signals for the Decide->Probe->Verify->Escalate pipeline
    (JEV-P2-dead-code). One decision noul for ``decide_probe_verify_escalate``
    -- calibrated confidence that ANOTHER ATTEMPT AT THE CURRENT TIER will
    pass verification (the exact signal that function's ``confidence``
    parameter consumes: low confidence means escalate) -- and one budget
    noul for ``should_abstain`` (is remaining capability budget worth
    another same-tier attempt?). Both feed the escalation driver via the
    policy directive; the generative verify lane stays the escalated
    executor.
    """
    return {
        "escalation_decision": _noul(
            "Would another attempt at the CURRENT tier pass verification "
            "for this edit?",
            "A retry at the current tier will very likely pass; the failure "
            "looks transient or marginal.",
            "A retry at the current tier is unlikely to pass verification; "
            "escalation is warranted."),
        "capability_budget": _noul(
            "Does the remaining attempt budget justify another attempt at "
            "the current tier?",
            "There is meaningful progress to harvest from another same-tier "
            "attempt; do not abstain yet.",
            "The budget is better spent escalating or stopping; abstain from "
            "another same-tier attempt."),
    }


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


# --- JEV-LOG operator log-factor packs (site=log_factor) ---

LOG_FACTOR_SITE = "log_factor"


def validate_log_pack(pack: Any) -> Dict[str, Any]:
    """Validate an operator log-factor pack; return a clean copy.

    Extends the JEV-P5 operator-pack shape with ONE operator-declared score
    dimension. Buckets keep every P5 rule (declared labels, kinds, path_id,
    keywords); the score block declares the level strings and code maps them
    to the score-question criteria in declared order. The model never invents
    buckets, path ids, actions, or score levels.
    """
    doc = validate_operator_pack(pack)
    score = pack.get("score")
    if not isinstance(score, dict):
        raise ValueError("log pack requires a score block object")
    score_id = score.get("id")
    if not isinstance(score_id, str) or not score_id or score_id == "bucket":
        raise ValueError(
            "log pack score requires a non-empty string id other than 'bucket'")
    instructions = score.get("instructions")
    if not isinstance(instructions, str) or not instructions:
        raise ValueError("log pack score requires non-empty instructions")
    levels = score.get("levels")
    if (not isinstance(levels, list) or len(levels) < 2
            or any(not isinstance(x, str) or not x for x in levels)):
        raise ValueError(
            "log pack score levels must be a list of at least two non-empty strings")
    if len(set(levels)) != len(levels):
        raise ValueError("log pack score levels must be unique")
    doc["score"] = {"id": score_id, "instructions": instructions,
                    "levels": list(levels)}
    return doc


def log_factor_question_pack(pack: Any) -> Dict[str, Dict[str, Any]]:
    """The TypeSafe question pack for one log item: bucket choice + score.

    Choice criteria keys are operator bucket ids, values their labels (the
    P5 parse contract). Score criteria are the operator level strings in
    declared order. Typed primitives only; nothing invented.
    """
    doc = validate_log_pack(pack)
    criteria = {
        bid: entry["label"] for bid, entry in doc["buckets"].items()
    }
    return {
        "bucket": {
            "type": "choice",
            "instructions": (
                "Choose the operator-declared bucket that best matches this "
                "log item. Select only from the declared criteria keys; do "
                "not invent categories."),
            "criteria": criteria,
        },
        doc["score"]["id"]: {
            "type": "score",
            "instructions": doc["score"]["instructions"],
            "criteria": list(doc["score"]["levels"]),
        },
    }


# --- JEV-P6 operator repo-summary pack (site=repo_summary) ---

REPO_SUMMARY_SITE = "repo_summary"


def validate_repo_summary_pack(pack: Any) -> Dict[str, Any]:
    """Validate an operator repo-summary pack; return a clean copy.

    Required shape::

        {id: str,
         axes: {axis_id: {instructions: str,
                          criteria: {criterion_id: label}}},
         score: {id: str, instructions: str, levels: [str, ...]},
         nouls: {noul_id: {instructions: str, true: str, false: str}},
         keywords: {axis_id: {criterion_id: [str, ...]}}}

    ``nouls`` and ``keywords`` are optional. Question ids (axes + score id +
    noul ids) must be unique so typed answers never collide; keywords exist
    only for the code-owned unkeyed fallback and are never invented here.
    """
    if not isinstance(pack, dict):
        raise ValueError("repo pack must be an object")
    pack_id = pack.get("id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("repo pack requires a non-empty string id")
    axes = pack.get("axes")
    if not isinstance(axes, dict) or not axes:
        raise ValueError("repo pack requires a non-empty axes map")
    out_axes: Dict[str, Dict[str, Any]] = {}
    for axis, spec in axes.items():
        if not isinstance(axis, str) or not axis:
            raise ValueError("axis ids must be non-empty strings")
        if not isinstance(spec, dict):
            raise ValueError(f"axis {axis!r} must be an object")
        instructions = spec.get("instructions")
        if not isinstance(instructions, str) or not instructions:
            raise ValueError(f"axis {axis!r} requires non-empty instructions")
        criteria = spec.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError(f"axis {axis!r} requires a non-empty criteria map")
        out_criteria: Dict[str, str] = {}
        for criterion_id, label in criteria.items():
            if not isinstance(criterion_id, str) or not criterion_id:
                raise ValueError(f"axis {axis!r} criterion ids must be non-empty strings")
            if not isinstance(label, str) or not label:
                raise ValueError(f"axis {axis!r} criterion {criterion_id!r} requires a label")
            out_criteria[criterion_id] = label
        out_axes[axis] = {"instructions": instructions, "criteria": out_criteria}
    score = pack.get("score")
    if not isinstance(score, dict):
        raise ValueError("repo pack requires a score block object")
    score_id = score.get("id")
    if not isinstance(score_id, str) or not score_id:
        raise ValueError("repo pack score requires a non-empty string id")
    score_instructions = score.get("instructions")
    if not isinstance(score_instructions, str) or not score_instructions:
        raise ValueError("repo pack score requires non-empty instructions")
    levels = score.get("levels")
    if (not isinstance(levels, list) or len(levels) < 2
            or any(not isinstance(x, str) or not x for x in levels)):
        raise ValueError("repo pack score levels must be at least two non-empty strings")
    if len(set(levels)) != len(levels):
        raise ValueError("repo pack score levels must be unique")
    nouls_raw = pack.get("nouls")
    if nouls_raw is None:
        nouls_raw = {}
    if not isinstance(nouls_raw, dict):
        raise ValueError("repo pack nouls must be an object")
    out_nouls: Dict[str, Dict[str, str]] = {}
    for name, spec in nouls_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("noul ids must be non-empty strings")
        if not isinstance(spec, dict):
            raise ValueError(f"noul {name!r} must be an object")
        fields = {}
        for field in ("instructions", "true", "false"):
            value = spec.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"noul {name!r} requires non-empty {field}")
            fields[field] = value
        out_nouls[name] = fields
    keywords_raw = pack.get("keywords")
    if keywords_raw is None:
        keywords_raw = {}
    if not isinstance(keywords_raw, dict):
        raise ValueError("repo pack keywords must be an object")
    out_keywords: Dict[str, Dict[str, List[str]]] = {}
    for axis, by_criterion in keywords_raw.items():
        if axis not in out_axes:
            raise ValueError(f"keywords reference unknown axis {axis!r}")
        if not isinstance(by_criterion, dict):
            raise ValueError(f"keywords for axis {axis!r} must be an object")
        out_keywords[axis] = {}
        for criterion_id, words in by_criterion.items():
            if criterion_id not in out_axes[axis]["criteria"]:
                raise ValueError(
                    f"keywords reference unknown criterion {axis}.{criterion_id}")
            if (not isinstance(words, list)
                    or any(not isinstance(w, str) or not w for w in words)):
                raise ValueError(
                    f"keywords for {axis}.{criterion_id} must be non-empty strings")
            out_keywords[axis][criterion_id] = list(words)
    question_ids = list(out_axes) + [score_id] + list(out_nouls)
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("repo pack question ids (axes, score, nouls) must be unique")
    return {"id": pack_id, "axes": out_axes,
            "score": {"id": score_id, "instructions": score_instructions,
                      "levels": list(levels)},
            "nouls": out_nouls, "keywords": out_keywords}


def repo_summary_question_pack(pack: Any) -> Dict[str, Dict[str, Any]]:
    """The TypeSafe question pack for one repo element (JEV-P6).

    Choice criteria keys are operator axis ids, score criteria are the
    operator level strings in declared order, noul criteria are the declared
    true/false strings. Typed primitives only; nothing invented.
    """
    doc = validate_repo_summary_pack(pack)
    questions: Dict[str, Dict[str, Any]] = {}
    for axis, spec in doc["axes"].items():
        questions[axis] = {
            "type": "choice",
            "instructions": spec["instructions"],
            "criteria": dict(spec["criteria"]),
        }
    score = doc["score"]
    questions[score["id"]] = {
        "type": "score",
        "instructions": score["instructions"],
        "criteria": list(score["levels"]),
    }
    for name, spec in doc["nouls"].items():
        questions[name] = {
            "type": "noul",
            "instructions": spec["instructions"],
            "criteria": {"true": spec["true"], "false": spec["false"]},
        }
    return questions


def heuristic_repo_axes(text: Any, pack: Any) -> Dict[str, Optional[str]]:
    """Unkeyed fallback: operator-declared keywords only (JEV-P6).

    Per axis: the criterion with the most keyword hits wins (ties keep the
    lexicographically first criterion id); no hit yields ``None`` -- never a
    guessed axis. Accepts a raw or already-validated pack.
    """
    doc = validate_repo_summary_pack(pack)
    lowered = (text or "").lower() if isinstance(text, str) else ""
    keywords = doc.get("keywords") or {}
    out: Dict[str, Optional[str]] = {}
    for axis, spec in doc["axes"].items():
        axis_keywords = keywords.get(axis) or {}
        best_id: Optional[str] = None
        best_hits = 0
        for criterion_id in sorted(spec["criteria"]):
            hits = sum(1 for word in axis_keywords.get(criterion_id, [])
                       if word and word.lower() in lowered)
            if hits > best_hits:
                best_id, best_hits = criterion_id, hits
        out[axis] = best_id
    return out


# --- JEV-BAR phase-completion sentiment packs (site=phase_completion) ---

PHASE_COMPLETION_SITE = "phase_completion"
DEFAULT_PHASE_COMPLETION_PACK = "packs/phase_completion.pack.json"

_COMPLETION_BLOCKER_PATTERNS = (
    re.compile(r"in progress", re.I),
    re.compile(r"\bblocked\b", re.I),
    re.compile(r"\brepair\b", re.I),
    re.compile(r"\bopen\b", re.I),
    re.compile(r"\bfail(?:ed|ing|s)?\b", re.I),
    re.compile(r"\bmissing\b", re.I),
    re.compile(r"not complete", re.I),
    re.compile(r"\bno pr\b", re.I),
)
_COMPLETION_RESIDUAL_PATTERNS = (
    re.compile(r"\bresidual\b", re.I),
    re.compile(r"\bdeferred\b", re.I),
    re.compile(r"\bfollow-up\b", re.I),
    re.compile(r"\bremaining\b", re.I),
)
_GENERIC_PR_MENTION_RE = re.compile(r"PR #\d+", re.I)
_DOGFOOD_EVIDENCE_RE = re.compile(r"\b(?:dogfood|smoke|receipt|live)\b", re.I)


def phase_status_has_blocker(text: Any) -> bool:
    """Word-boundary blocker-marker match (JEV-BAR). Never a bare substring:
    ``\\bopen\\b`` does not match ``OpenRouter``/``reopened``; ``deferred`` is
    a residual marker, not a hard blocker (see ``phase_status_has_residual``).
    """
    if not isinstance(text, str) or not text:
        return False
    return any(p.search(text) for p in _COMPLETION_BLOCKER_PATTERNS)


def phase_status_has_residual(text: Any) -> bool:
    """Word-boundary residual/deferred marker match (not a hard blocker)."""
    if not isinstance(text, str) or not text:
        return False
    return any(p.search(text) for p in _COMPLETION_RESIDUAL_PATTERNS)


def phase_status_claims_complete(text: Any) -> bool:
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return "**complete**" in lowered or "| complete" in lowered


def phase_status_mentions_pr(status_row: Any, pattern: Optional[str]) -> bool:
    """Does the row mention the phase's PR? A declared ``pattern`` is used
    verbatim; ``None`` falls back to the generic ``PR #<digits>`` rule (never
    the bare ``\"PR #\"`` substring that let any PR mention pass)."""
    if not isinstance(status_row, str) or not status_row:
        return False
    if pattern:
        return bool(re.search(pattern, status_row))
    return bool(_GENERIC_PR_MENTION_RE.search(status_row))


def validate_completion_pack(pack: Any) -> Dict[str, Any]:
    """Validate an operator phase-completion sentiment pack; return a clean
    copy (JEV-BAR). Required shape::

        {id: str,
         sentiment: {levels: [str, ...>=2 unique],
                     ordinals: [num, ...same length, non-decreasing, 0..100],
                     blocking_max_index: int, improve_below_index: int},
         axes: {axis_id: {authority: "code"|"jev", bucket: bucket_id,
                          instructions: str}},
         buckets: {bucket_id: {label, path_id, keywords: [str, ...],
                               suggested_next_action: str}}}

    Axis ids may not collide with the reserved question id ``primary_gap``;
    bucket ids may not collide with the reserved choice value ``none``. The
    model never invents buckets, path ids, actions, or levels.
    """
    if not isinstance(pack, dict):
        raise ValueError("completion pack must be an object")
    pack_id = pack.get("id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("completion pack requires a non-empty string id")

    sentiment = pack.get("sentiment")
    if not isinstance(sentiment, dict):
        raise ValueError("completion pack requires a sentiment block object")
    levels = sentiment.get("levels")
    if (not isinstance(levels, list) or len(levels) < 2
            or any(not isinstance(x, str) or not x for x in levels)):
        raise ValueError(
            "completion pack sentiment levels must be at least two non-empty strings")
    if len(set(levels)) != len(levels):
        raise ValueError("completion pack sentiment levels must be unique")
    ordinals = sentiment.get("ordinals")
    if (not isinstance(ordinals, list) or len(ordinals) != len(levels)
            or any(isinstance(o, bool) or not isinstance(o, (int, float))
                   for o in ordinals)):
        raise ValueError(
            "completion pack sentiment ordinals must be numbers matching levels length")
    if any(o < 0 or o > 100 for o in ordinals):
        raise ValueError("completion pack sentiment ordinals must be within 0..100")
    if any(ordinals[i] > ordinals[i + 1] for i in range(len(ordinals) - 1)):
        raise ValueError("completion pack sentiment ordinals must be non-decreasing")
    blocking_max_index = sentiment.get("blocking_max_index")
    if (isinstance(blocking_max_index, bool)
            or not isinstance(blocking_max_index, int)
            or not (0 <= blocking_max_index < len(levels))):
        raise ValueError(
            "completion pack sentiment blocking_max_index must be an in-range int")
    improve_below_index = sentiment.get("improve_below_index")
    if (isinstance(improve_below_index, bool)
            or not isinstance(improve_below_index, int)
            or not (0 <= improve_below_index <= len(levels))):
        raise ValueError(
            "completion pack sentiment improve_below_index must be an in-range int")

    buckets = pack.get("buckets")
    if not isinstance(buckets, dict) or not buckets:
        raise ValueError("completion pack requires a non-empty buckets map")
    out_buckets: Dict[str, Dict[str, Any]] = {}
    for bid, bucket in buckets.items():
        if not isinstance(bid, str) or not bid:
            raise ValueError("bucket ids must be non-empty strings")
        if bid == "none":
            raise ValueError("bucket id 'none' is reserved")
        if not isinstance(bucket, dict):
            raise ValueError(f"bucket {bid!r} must be an object")
        label = bucket.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(f"bucket {bid!r} requires a non-empty label")
        path_id = bucket.get("path_id")
        if not isinstance(path_id, str) or not path_id:
            raise ValueError(f"bucket {bid!r} requires a non-empty path_id")
        keywords = bucket.get("keywords", [])
        if not isinstance(keywords, list) or any(
                not isinstance(k, str) or not k for k in keywords):
            raise ValueError(
                f"bucket {bid!r} keywords must be a list of non-empty strings")
        action = bucket.get("suggested_next_action")
        if not isinstance(action, str) or not action:
            raise ValueError(
                f"bucket {bid!r} requires a non-empty suggested_next_action")
        out_buckets[bid] = {
            "label": label, "path_id": path_id, "keywords": list(keywords),
            "suggested_next_action": action,
        }

    axes = pack.get("axes")
    if not isinstance(axes, dict) or not axes:
        raise ValueError("completion pack requires a non-empty axes map")
    out_axes: Dict[str, Dict[str, Any]] = {}
    for axis, spec in axes.items():
        if not isinstance(axis, str) or not axis:
            raise ValueError("axis ids must be non-empty strings")
        if axis == "primary_gap":
            raise ValueError("axis id 'primary_gap' is reserved")
        if not isinstance(spec, dict):
            raise ValueError(f"axis {axis!r} must be an object")
        authority = spec.get("authority")
        if authority not in ("code", "jev"):
            raise ValueError(f"axis {axis!r} authority must be 'code' or 'jev'")
        bucket_id = spec.get("bucket")
        if not isinstance(bucket_id, str) or bucket_id not in out_buckets:
            raise ValueError(f"axis {axis!r} bucket must be a declared bucket id")
        instructions = spec.get("instructions")
        if not isinstance(instructions, str) or not instructions:
            raise ValueError(f"axis {axis!r} requires non-empty instructions")
        out_axes[axis] = {
            "authority": authority, "bucket": bucket_id, "instructions": instructions,
        }

    return {
        "id": pack_id,
        "sentiment": {
            "levels": list(levels),
            "ordinals": [float(o) for o in ordinals],
            "blocking_max_index": int(blocking_max_index),
            "improve_below_index": int(improve_below_index),
        },
        "axes": out_axes,
        "buckets": out_buckets,
    }


def load_completion_pack(repo_root: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Read + validate the operator completion pack from disk (utf-8-sig
    tolerant); ``HarnessError`` on unreadable/invalid JSON or shape."""
    root = os.path.abspath(repo_root or os.getcwd())
    rel = path or DEFAULT_PHASE_COMPLETION_PACK
    full = rel if os.path.isabs(rel) else os.path.join(root, rel.replace("/", os.sep))
    try:
        with open(full, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise HarnessError(f"cannot read completion pack: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid completion pack JSON: {exc}") from exc
    try:
        return validate_completion_pack(data)
    except ValueError as exc:
        raise HarnessError(f"invalid completion pack: {exc}") from exc


def completion_bar_question_pack(pack: Any) -> Dict[str, Dict[str, Any]]:
    """The TypeSafe question pack for the JEV-BAR phase-completion judgment:
    one ``score`` question per declared axis (criteria = the shared sentiment
    levels, in declared order) plus one ``primary_gap`` choice (criteria keys
    = declared bucket ids + the reserved ``none``). Typed primitives only;
    nothing invented."""
    doc = validate_completion_pack(pack)
    levels = doc["sentiment"]["levels"]
    questions: Dict[str, Dict[str, Any]] = {}
    for axis, spec in doc["axes"].items():
        questions[axis] = {
            "type": "score",
            "instructions": spec["instructions"],
            "criteria": list(levels),
        }
    criteria = {bid: entry["label"] for bid, entry in doc["buckets"].items()}
    criteria["none"] = "no improvement needed"
    questions["primary_gap"] = {
        "type": "choice",
        "instructions": (
            "Choose the single declared bucket that best represents the "
            "primary gap blocking this phase from a confident bar pass, or "
            "'none' when no improvement is needed. Select only from the "
            "declared criteria keys; do not invent categories."),
        "criteria": criteria,
    }
    return questions


def match_completion_keywords(
    text: Any, pack: Any
) -> Tuple[Optional[str], int, List[str]]:
    """Match phase evidence text against completion-bar bucket keywords only
    (JEV-BAR unkeyed/fallback ``primary_gap``; never invents a bucket).
    Deterministic: highest hit-count wins; ties keep the lexicographically
    first bucket id."""
    pack_doc = validate_completion_pack(pack)
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


def heuristic_completion_sentiment(evidence: Any, pack: Any) -> Dict[str, int]:
    """Code-owned deterministic sentiment fallback for JEV-BAR.

    Answers every declared axis from the pack's own level count -- never a
    live guess -- so an unkeyed run or a code-authority axis always has a
    grounded floor to compare the live judgment against. Recognized axis ids
    (``merge_evidence``, ``gate_tests``, ``verification``, ``status_honesty``,
    ``residual_scope``, ``dogfood``) use the operator-approved rules below;
    an operator-added axis this module does not recognize gets the neutral
    ``mixed`` tier (index 2 in the canonical 5-level pack).
    """
    doc = validate_completion_pack(pack)
    n_levels = len(doc["sentiment"]["levels"])

    def clamp(idx: int) -> int:
        return max(0, min(idx, n_levels - 1))

    ev = evidence if isinstance(evidence, dict) else {}
    status_row = ev.get("status_row")
    status_row = status_row if isinstance(status_row, str) else ""

    def axis_merge_evidence() -> int:
        if ev.get("pr_merged"):
            return 4
        if status_row and phase_status_mentions_pr(status_row, ev.get("pr_pattern")):
            return 1
        return 0

    def axis_gate_tests() -> int:
        if ev.get("required_tests"):
            return 0 if ev.get("tests_missing") else 4
        return 2

    def axis_verification() -> int:
        if ev.get("tests_missing"):
            return 0
        if ((ev.get("gate_output") or ev.get("ci_run"))
                and ev.get("local_gates_green") and ev.get("ci_green")):
            return 4
        if ev.get("local_gates_green") and ev.get("ci_green"):
            return 3
        return 1

    def axis_status_honesty() -> int:
        if not status_row:
            return 0
        if phase_status_claims_complete(status_row) and phase_status_has_blocker(status_row):
            return 0
        return 4

    def axis_residual_scope() -> int:
        if phase_status_has_residual(status_row):
            return 2 if "not blocking" in status_row.lower() else 1
        return 4

    def axis_dogfood() -> int:
        if not ev.get("user_facing"):
            return 4
        combined = " ".join(
            str(part) for part in (
                [status_row, ev.get("origin_evidence")] + list(ev.get("notes") or []))
            if part)
        return 3 if _DOGFOOD_EVIDENCE_RE.search(combined) else 1

    axis_fns = {
        "merge_evidence": axis_merge_evidence,
        "gate_tests": axis_gate_tests,
        "verification": axis_verification,
        "status_honesty": axis_status_honesty,
        "residual_scope": axis_residual_scope,
        "dogfood": axis_dogfood,
    }

    return {axis: clamp(axis_fns[axis]() if axis in axis_fns else 2)
            for axis in doc["axes"]}


# --- JEV 4-Dimension Self-Audit Question Pack ---
AUDIT_DIMENSIONS_SITE = "audit_dimensions"

DEFAULT_AUDIT_DIMENSIONS_PACK: Dict[str, Any] = {
    "id": "harness-audit-dimensions-v1",
    "sentiment": {
        "levels": [
            "failing — severe defect or broken guarantee (<7.0)",
            "at_risk — partial satisfaction with significant gaps (7.0-8.4)",
            "approaching — minor non-safety gap below threshold (8.5-9.4)",
            "bar_met — satisfies the 95%+ audit bar (9.5-9.9)",
            "exemplary — 100% defect-free across all checks (10.0)",
        ],
        "ordinals": [5.0, 7.5, 9.0, 9.6, 10.0],
        "bar_met_index": 3,
    },
    "dimensions": {
        "A": {
            "name": "Security",
            "instructions": (
                "Assess whether security controls, trust boundaries, fail-closed guarantees, "
                "spend preflights, and file mutation safety are fully satisfied without bypass."
            ),
        },
        "R": {
            "name": "Reliability",
            "instructions": (
                "Assess whether transient fault tolerance (429/5xx), rotation, output usability, "
                "concurrency guards, and ledger integrity hold under all conditions."
            ),
        },
        "SM": {
            "name": "Structural hygiene & maintainability",
            "instructions": (
                "Assess whether architectural layering, single-ownership of policies, "
                "no facade re-exports, and test-mirror symmetry are maintained."
            ),
        },
        "SD": {
            "name": "Documentation & release integrity",
            "instructions": (
                "Assess whether documentation, CLI surfaces, exit codes, MCP parity, versioning, "
                "and release changelog discipline are accurately preserved."
            ),
        },
    },
}


def validate_audit_pack(pack: Any) -> Dict[str, Any]:
    if pack is None:
        return DEFAULT_AUDIT_DIMENSIONS_PACK
    if isinstance(pack, str):
        pack = json.loads(pack)
    if not isinstance(pack, dict):
        raise HarnessError("audit pack must be a JSON object")
    if "dimensions" not in pack or "sentiment" not in pack:
        raise HarnessError("audit pack missing required keys 'dimensions' or 'sentiment'")
    return pack


def audit_dimensions_question_pack(pack: Any = None) -> Dict[str, Dict[str, Any]]:
    """The TypeSafe question pack for the 4-dimension audit judgment."""
    doc = validate_audit_pack(pack)
    levels = doc["sentiment"]["levels"]
    questions: Dict[str, Dict[str, Any]] = {}
    for dim, spec in doc["dimensions"].items():
        questions[f"dim_{dim}"] = {
            "type": "score",
            "instructions": spec["instructions"],
            "criteria": list(levels),
        }
    return questions


def heuristic_audit_dimensions(
    dimension_evidence: Any, pack: Any = None
) -> Dict[str, Any]:
    """Code-owned fallback/hermetic sentiment for the 4-dimension audit.

    A dimension key ABSENT from a real ``dimension_evidence`` mapping (a
    partial ``--dim`` run) is reported ``not_evaluated`` -- ``level_index``
    and ``score`` are ``None`` and ``bar_met`` is ``False`` -- never a false
    0.0/"failing" score standing in for a check that never ran. When no
    evidence mapping is supplied at all (``None`` / non-dict), every
    declared dimension keeps the historical all-zero fallback, since there
    is nothing in that shape to distinguish "not run" from "run with no
    evidence".
    """
    doc = validate_audit_pack(pack)
    levels = doc["sentiment"]["levels"]
    ordinals = doc["sentiment"]["ordinals"]
    evidence_is_dict = isinstance(dimension_evidence, dict)
    out = {}
    for dim in doc["dimensions"]:
        if evidence_is_dict and dim not in dimension_evidence:
            out[dim] = {
                "name": doc["dimensions"][dim]["name"],
                "level_index": None,
                "level": "not_evaluated",
                "score": None,
                "bar_met": False,
                "evaluated": False,
            }
            continue
        ev = dimension_evidence.get(dim, {}) if evidence_is_dict else {}
        score_val = float(ev.get("score", 0.0)) if isinstance(ev, dict) else 0.0
        if score_val >= 9.99:
            idx = 4
        elif score_val >= 9.5:
            idx = 3
        elif score_val >= 8.5:
            idx = 2
        elif score_val >= 7.0:
            idx = 1
        else:
            idx = 0
        out[dim] = {
            "name": doc["dimensions"][dim]["name"],
            "level_index": idx,
            "level": levels[idx],
            "score": round(score_val if 0.0 <= score_val <= 10.0 else ordinals[idx], 2),
            "bar_met": idx >= doc["sentiment"].get("bar_met_index", 3),
            "evaluated": True,
        }
    return out
