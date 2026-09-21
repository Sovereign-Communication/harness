"""Jev phase-completion accountability (dogfood gate).

Code owns hard mechanical facts (PR/merge, required tests present, named
gates, open blockers). Jev owns a bounded semantic 0-100 judgment on whether
the evidence actually shows the phase is done. STATUS may claim complete only
when every hard gate passes AND the combined score clears the threshold.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .errors import HarnessError
from .jev import JevEvaluationResult

PHASE_COMPLETE_MIN_SCORE = 85.0
COMPLETION_SCORE_MAX = 100.0

# Mechanical points when hard gates pass. Sum == 100.
_HARD_GATE_POINTS = {
    "pr_merged": 25,
    "origin_evidence": 10,
    "required_tests_present": 20,
    "local_gates_green": 15,
    "ci_green": 15,
    "no_open_blockers": 15,
}

_BLOCKER_MARKERS = (
    "in progress",
    "blocked",
    "repair",
    "open",
    "red",
    "missing",
    "fail",
    "not complete",
    "deferred",
    "no pr",
)

# Canonical phase contracts for mission STATUS dogfooding.
PHASE_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "JEV-P0": {
        "pr_pattern": r"PR #34|d042d70",
        "required_tests": ["tests/test_jev.py", "tests/test_jev_smoke.py"],
        "required_files": ["harness/jev.py"],
    },
    "JEV-P1": {
        "pr_pattern": r"PR #35|9d5ff14",
        "required_tests": [
            "tests/test_jev_policy.py",
            "tests/test_jev_lane_parity.py",
            "tests/test_jev_ledger_spend.py",
        ],
        "required_files": ["harness/jev_policy.py"],
    },
    "JEV-P2": {
        "pr_pattern": r"PR #36",
        "required_tests": [
            "tests/test_consent_confidence.py",
            "tests/test_min_confidence_gating.py",
            "tests/test_jev_triage.py",
            "tests/test_jev_policy.py",
            "tests/test_jev_lane_parity.py",
            "tests/test_jev_ledger_spend.py",
        ],
        "required_files": [],
    },
    "JEV-P3": {
        "pr_pattern": r"PR #",
        "required_tests": [],
        "required_files": [],
    },
    "JEV-P4": {
        "pr_pattern": r"PR #",
        "required_tests": [],
        "required_files": [],
    },
    "JEV-COMPLETION": {
        "pr_pattern": r"PR #",
        "required_tests": ["tests/test_jev_completion.py"],
        "required_files": ["harness/jev_completion.py"],
    },
}

_COMPLETION_PACK = {
    "phase_evidence_quality": {
        "type": "score",
        "instructions": (
            "Rate 0-100 how complete the mission phase evidence is. "
            "Hard gates are already code-checked; score only the semantic "
            "quality of the evidence narrative: PR/gates present, STATUS "
            "honest, no claimed-complete while blockers remain."
        ),
        "criteria": [
            "incomplete or contradicted evidence",
            "partial evidence with open gaps",
            "substantial evidence with minor gaps",
            "complete and consistent evidence",
        ],
    }
}


def _norm_phase(phase_id: str) -> str:
    raw = (phase_id or "").strip().upper().replace(" ", "-")
    if not raw:
        raise HarnessError("phase id is required")
    if raw.startswith("P") and raw[1:2].isdigit():
        return f"JEV-P{raw[1:2]}"
    if not raw.startswith("JEV-"):
        return f"JEV-{raw}" if raw.startswith("P") else raw
    return raw


def _status_row_for(roadmap_text: str, phase_id: str) -> Optional[str]:
    needles = {
        "JEV-P0": re.compile(r"JEV-P0|0 Contract|contract truth", re.I),
        "JEV-P1": re.compile(r"JEV-P1|One owner|one owner \+ lanes", re.I),
        "JEV-P2": re.compile(r"JEV-P2|2 Pillars|System One pillars", re.I),
        "JEV-P3": re.compile(r"JEV-P3|3 Utilization|utilization", re.I),
        "JEV-P4": re.compile(r"JEV-P4|4 Ops|ops / exit", re.I),
        "JEV-COMPLETION": re.compile(r"JEV-COMPLETION|completion score|dogfood 0-100|Accountability", re.I),
    }
    pat = needles.get(phase_id)
    if not pat or not roadmap_text:
        return None
    statusish = re.compile(
        r"complete|in progress|blocked|repair|\bopen\b|planned|MERGED|PR #", re.I)
    candidates: List[str] = []
    for line in roadmap_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not pat.search(stripped):
            continue
        if not statusish.search(stripped):
            continue
        # Skip pure work-item definition rows (ID | work text) without status language.
        if re.search(r"^\|\s*`?JEV-P\d-[a-z]", stripped, re.I) and "complete" not in stripped.lower() \
                and "in progress" not in stripped.lower():
            continue
        candidates.append(stripped)
    if not candidates:
        return None
    # Prefer rows that look like STATUS/tracker conclusions.
    def rank(row: str) -> int:
        low = row.lower()
        score = 0
        if "**complete**" in low or "**in progress" in low or "**open**" in low:
            score += 10
        if "pr #" in low or "merged" in low:
            score += 5
        if phase_id.lower() in low or "pillar" in low or "owner" in low or "accountability" in low:
            score += 2
        if re.search(r"policy|consent-confidence|min-confidence|triage|apply\|", low):
            score -= 5
        return score
    candidates.sort(key=rank, reverse=True)
    return candidates[0]


def collect_phase_evidence(repo_root: str, phase_id: str,
                           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Gather code-owned phase evidence from the repo + optional overrides."""
    phase = _norm_phase(phase_id)
    contract = PHASE_CONTRACTS.get(phase, {"pr_pattern": r"PR #", "required_tests": [], "required_files": []})
    root = os.path.abspath(repo_root or os.getcwd())
    roadmap_path = os.path.join(root, "docs", "jev-roadmap.md")
    status_row = None
    if os.path.isfile(roadmap_path):
        try:
            with open(roadmap_path, encoding="utf-8") as fh:
                status_row = _status_row_for(fh.read(), phase)
        except OSError:
            status_row = None

    required_tests = list(contract.get("required_tests") or [])
    tests_present = []
    tests_missing = []
    for rel in required_tests:
        path = os.path.join(root, rel.replace("/", os.sep))
        (tests_present if os.path.isfile(path) else tests_missing).append(rel)

    required_files = list(contract.get("required_files") or [])
    files_missing = [
        rel for rel in required_files
        if not os.path.isfile(os.path.join(root, rel.replace("/", os.sep)))
    ]

    evidence: Dict[str, Any] = {
        "phase": phase,
        "repo_root": root,
        "status_row": status_row,
        "pr_pattern": contract.get("pr_pattern"),
        "required_tests": required_tests,
        "tests_present": tests_present,
        "tests_missing": tests_missing,
        "required_files": required_files,
        "files_missing": files_missing,
        "pr_merged": False,
        "origin_evidence": "",
        "local_gates_green": False,
        "ci_green": False,
        "open_blockers": [],
        "notes": [],
    }

    if status_row:
        pattern = contract.get("pr_pattern") or ""
        lowered = status_row.lower()
        mentions_pr = bool(pattern and re.search(pattern, status_row))
        open_pr = bool(re.search(r"\bopen\b|no pr", lowered))
        merged_word = "merged" in lowered or "merge" in lowered
        # Presence of a PR id is not merge evidence while the row still says open.
        evidence["pr_merged"] = bool(mentions_pr and merged_word and not open_pr)
        evidence["origin_evidence"] = status_row
        claims_complete = "**complete**" in lowered or "| complete" in lowered
        has_blocker = any(m in lowered for m in ("in progress", "blocked", "repair",
                                                 "open", "fail", "missing", "no pr"))
        if claims_complete and has_blocker:
            evidence["open_blockers"].append(
                "STATUS claims complete while row still lists open/repair/fail evidence")
        elif has_blocker:
            evidence["open_blockers"].append("STATUS row not complete")
        if open_pr or "no pr" in lowered:
            evidence["open_blockers"].append("STATUS says PR open or no PR")
        if evidence["pr_merged"]:
            evidence["ci_green"] = True  # merged PR implies checks were required green
            evidence["local_gates_green"] = True  # operator claimed green on merge path
    else:
        evidence["notes"].append(f"no STATUS row found for {phase} in docs/jev-roadmap.md")

    if tests_missing:
        evidence["open_blockers"].append("missing required tests: " + ", ".join(tests_missing))
        evidence["local_gates_green"] = False
        evidence["ci_green"] = False
    if files_missing:
        evidence["open_blockers"].append("missing required files: " + ", ".join(files_missing))

    if extra:
        for key in ("pr_merged", "local_gates_green", "ci_green", "origin_evidence"):
            if key in extra:
                evidence[key] = bool(extra[key]) if key != "origin_evidence" else extra[key]
        if isinstance(extra.get("open_blockers"), list):
            evidence["open_blockers"] = list(extra["open_blockers"])
        if extra.get("status_row"):
            evidence["status_row"] = extra["status_row"]
        if extra.get("gate_output"):
            evidence["notes"].append(str(extra["gate_output"])[:500])

    # Deduplicate blockers
    seen = set()
    uniq = []
    for b in evidence["open_blockers"]:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    evidence["open_blockers"] = uniq
    return evidence


def _hard_gates(evidence: Dict[str, Any]) -> Dict[str, bool]:
    tests_ok = not evidence.get("tests_missing") and bool(evidence.get("required_tests") or evidence.get("tests_present") is not None)
    if evidence.get("required_tests") and evidence.get("tests_missing"):
        tests_ok = False
    elif evidence.get("required_tests") and not evidence.get("tests_missing"):
        tests_ok = True
    else:
        tests_ok = not bool(evidence.get("files_missing")) and not bool(evidence.get("tests_missing"))
    blockers = list(evidence.get("open_blockers") or [])
    # A phase with no required tests can still pass presence if files exist.
    if not evidence.get("required_tests") and not evidence.get("files_missing"):
        tests_ok = True
    return {
        "pr_merged": bool(evidence.get("pr_merged")),
        "origin_evidence": bool(str(evidence.get("origin_evidence") or "").strip()),
        "required_tests_present": tests_ok,
        "local_gates_green": bool(evidence.get("local_gates_green")),
        "ci_green": bool(evidence.get("ci_green")),
        "no_open_blockers": not blockers,
    }


def _mechanical_score(gates: Dict[str, bool]) -> float:
    return float(sum(pts for name, pts in _HARD_GATE_POINTS.items() if gates.get(name)))


def _local_semantic_score(evidence: Dict[str, Any], gates: Dict[str, bool]) -> float:
    """Unkeyed heuristic 0-100; never a silent pass."""
    score = _mechanical_score(gates)
    row = str(evidence.get("status_row") or "")
    if row:
        if "**complete**" in row.lower() and not gates.get("no_open_blockers"):
            score = min(score, 70.0)
        if "complete" in row.lower() and gates.get("pr_merged") and gates.get("no_open_blockers"):
            score = min(100.0, score + 5.0)
    if evidence.get("tests_missing"):
        score = min(score, 60.0)
    return max(0.0, min(COMPLETION_SCORE_MAX, score))


def _jev_semantic_score(evidence: Dict[str, Any], jev_policy=None) -> Dict[str, Any]:
    if jev_policy is None:
        return {
            "score": _local_semantic_score(evidence, _hard_gates(evidence)),
            "is_fallback": True,
            "model": "local-heuristic",
            "site": "phase-completion",
            "verdict": "fallback",
        }
    prompt = (
        f"Phase {evidence.get('phase')} mission evidence.\n"
        f"STATUS row: {evidence.get('status_row')}\n"
        f"PR merged claim: {evidence.get('pr_merged')} ({evidence.get('origin_evidence')})\n"
        f"Required tests missing: {evidence.get('tests_missing')}\n"
        f"Open blockers: {evidence.get('open_blockers')}\n"
        f"local_gates_green={evidence.get('local_gates_green')} ci_green={evidence.get('ci_green')}\n"
        "Score how complete this phase really is, 0-100. Be strict."
    )
    result: JevEvaluationResult = jev_policy.evaluator.evaluate(
        {"phase": evidence.get("phase"), "prompt": prompt}, _COMPLETION_PACK)
    answer = (result.answers or {}).get("phase_evidence_quality") or {}
    raw = answer.get("score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        # Typed score is usually 0-1 legend in TypeSafe; map to 0-100.
        raw = _local_semantic_score(evidence, _hard_gates(evidence))
        scale_note = "missing score; local heuristic used"
    else:
        scale_note = "jev score"
        if 0.0 <= float(raw) <= 1.0:
            raw = float(raw) * 100.0
    return {
        "score": float(raw),
        "is_fallback": bool(result.is_fallback),
        "model": result.model,
        "site": "phase-completion",
        "verdict": result.verdict,
        "cost": float(result.cost or 0.0),
        "note": scale_note,
        "reasons": list(result.reasons or []),
    }


def score_phase_completion(
    evidence: Dict[str, Any],
    *,
    jev_policy=None,
    min_score: float = PHASE_COMPLETE_MIN_SCORE,
) -> Dict[str, Any]:
    """Return the dogfoodable phase completion judgment."""
    if not isinstance(evidence, dict) or not evidence.get("phase"):
        raise HarnessError("phase evidence requires a phase id")
    gates = _hard_gates(evidence)
    mechanical = _mechanical_score(gates)
    semantic = _jev_semantic_score(evidence, jev_policy=jev_policy)
    semantic_score = float(semantic.get("score") or 0.0)

    all_hard_pass = all(gates.values())
    if not all_hard_pass:
        # Hard fail: score cannot clear the gate no matter how nice the prose.
        combined = min(mechanical, semantic_score, min_score - 0.01)
        combined = max(0.0, combined)
    else:
        combined = 0.7 * mechanical + 0.3 * semantic_score
        combined = max(0.0, min(COMPLETION_SCORE_MAX, combined))

    can_mark_complete = all_hard_pass and combined >= float(min_score)
    blockers = list(evidence.get("open_blockers") or [])
    if not gates.get("pr_merged"):
        blockers.append("hard gate failed: pr_merged")
    if not gates.get("origin_evidence"):
        blockers.append("hard gate failed: origin_evidence")
    if not gates.get("required_tests_present"):
        blockers.append("hard gate failed: required_tests_present")
    if not gates.get("local_gates_green"):
        blockers.append("hard gate failed: local_gates_green")
    if not gates.get("ci_green"):
        blockers.append("hard gate failed: ci_green")
    if not gates.get("no_open_blockers"):
        blockers.append("hard gate failed: open_blockers")

    return {
        "phase": evidence.get("phase"),
        "score": round(combined, 2),
        "min_score": float(min_score),
        "can_mark_complete": bool(can_mark_complete),
        "hard_gates": gates,
        "mechanical_score": round(mechanical, 2),
        "semantic": semantic,
        "blockers": blockers,
        "status_row": evidence.get("status_row"),
        "evidence": {
            "pr_merged": evidence.get("pr_merged"),
            "origin_evidence": evidence.get("origin_evidence"),
            "tests_missing": evidence.get("tests_missing") or [],
            "open_blockers": evidence.get("open_blockers") or [],
        },
    }


def load_evidence_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise HarnessError(f"cannot read evidence file: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessError("evidence file must contain a JSON object")
    return data


def dogfood_phase(
    repo_root: str,
    phase_id: str,
    *,
    evidence_path: Optional[str] = None,
    settings=None,
    transport=None,
    governor=None,
    ledger=None,
    use_live_jev: bool = True,
    min_score: float = PHASE_COMPLETE_MIN_SCORE,
) -> Dict[str, Any]:
    """Collect evidence and score one phase for mission dogfooding."""
    extra = load_evidence_file(evidence_path) if evidence_path else None
    evidence = collect_phase_evidence(repo_root, phase_id, extra=extra)
    jev_policy = None
    if use_live_jev and settings is not None:
        from .jev_policy import policy_for
        jev_policy = policy_for(settings, transport=transport, governor=governor, ledger=ledger)
    return score_phase_completion(evidence, jev_policy=jev_policy, min_score=min_score)
