"""Propose-Probe-Compose refinement loop with Jev as composer (issue #239).

Implements the Harness2 pattern (NOT the dependency) natively:
- TIER 0/1/2 safety model with code-enforced immutable paths
- Task outcome tracking (P0), family taxonomy (P1), CLI scaffold (P2),
  lessons library (P3)
- Phase 0 pilot: TIER 2 only, human reviews every PR, 20 cycles, auto-pause
- Jev 4-dimension scoring rubric with gating rules

Canon: docs/jev-roadmap.md (REFINE-LOOP row). Issue: #239.
"""

import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent / "data"
_FAMILIES_PATH = _DATA_DIR / "task_families.json"
_EXPERIENCE_PATH = _DATA_DIR / "experience.md"
_PROPOSE_PROMPT_PATH = _DATA_DIR / "refine_propose_prompt.md"
_PILOT_STATE_PATH = _DATA_DIR / "refine_pilot_state.json"

# ---------------------------------------------------------------------------
# Safety model — TIER 0/1/2 (issue #239 v5)
# ---------------------------------------------------------------------------

# TIER 0: immutable. The propose step cannot generate edits here; the tier
# check fails any PR touching these. This list itself is TIER 0.
TIER_0_PATHS = [
    "harness/refine.py",                    # this safety model
    "harness/data/refine_propose_prompt.md",  # Jev rubric lives here
    "harness/provider_connect.py",          # credential backend
    "THREAT_MODEL.md",
    "docs/security.md",
    "harness/data/task_families.json",      # loop's own taxonomy
    "harness/data/refine_pilot_state.json", # loop's own pilot state
    "*key*",
    "*token*",
    "*secret*",
    "*credential*",
]

# TIER 1: invariant/systemic — human approval required, never auto-merge.
TIER_1_PATHS = [
    "AGENTS.md",
    "CLAUDE.md",
    "harness/prompts.py",
    "docs/hourglass-*.md",
    "harness/lifecycle_guards.py",
    "harness/apply_policy.py",
    "harness/consent.py",
    "harness/trust.py",
    "harness/jev_policy.py",
]

# TIER 2: domain/task-specific — auto-mergeable under gates.
# (Everything not in TIER 0 or TIER 1.)


def _matches_any(path: str, patterns: List[str]) -> bool:
    # Match against both the full path and the basename for *key* style globs.
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p)
               for p in patterns)


def classify_tier(path: str) -> int:
    """Return 0, 1, or 2 for a repo-relative path."""
    if _matches_any(path, TIER_0_PATHS):
        return 0
    if _matches_any(path, TIER_1_PATHS):
        return 1
    return 2


def check_tier(diff_paths: List[str]) -> Tuple[bool, str, int]:
    """Enforce the tier boundary on a candidate diff's file list.

    Returns (ok, reason, highest_tier_touched). ok=False if any TIER 0
    path is touched. Used by CI (refine-tier-check) and by run_cycle.
    """
    highest = 0
    for p in diff_paths:
        t = classify_tier(p)
        highest = max(highest, t)
        if t == 0:
            return False, f"TIER 0 violation: {p} is immutable", 0
    return True, "tier check passed", highest


def diff_paths_from_unified(diff_text: str) -> List[str]:
    """Extract repo-relative file paths from a unified diff."""
    paths = []
    for line in diff_text.splitlines():
        m = re.match(r"^[+-]{3} [ab]/(.+)$", line)
        if m:
            p = m.group(1)
            if p != "/dev/null" and p not in paths:
                paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# P1: task family taxonomy
# ---------------------------------------------------------------------------

def load_families() -> Dict[str, Any]:
    with open(_FAMILIES_PATH, encoding="utf-8") as f:
        return json.load(f)


def valid_family(family: str) -> bool:
    try:
        return family in load_families().get("families", {})
    except (OSError, json.JSONDecodeError):
        return False


# ---------------------------------------------------------------------------
# P0: task outcome tracking (ledger integration)
# ---------------------------------------------------------------------------

def record_task_outcome(ledger, task_id: str, family: str,
                        jev_outcome: float,
                        failure_mode: Optional[str] = None,
                        **extra) -> Dict[str, Any]:
    """Record a task's Jev outcome in the ledger (P0 prerequisite).

    Every harness task should call this on completion. The refine loop's
    trigger reads these records.
    """
    if not valid_family(family):
        raise ValueError(f"unknown task family: {family!r}")
    if not 0.0 <= jev_outcome <= 1.0:
        raise ValueError(f"jev_outcome must be 0-1, got {jev_outcome}")
    return ledger.append(
        "task_outcome",
        task_id=task_id,
        task_family=family,
        jev_outcome=jev_outcome,
        failure_mode=failure_mode,
        is_refinement=bool(extra.pop("is_refinement", False)),
        **extra,
    )


def get_task_outcome(ledger, task_id: str) -> Optional[Dict[str, Any]]:
    """Return the most recent task_outcome record for a task, or None."""
    for entry in reversed(ledger.entries()):
        if entry.get("event") == "task_outcome" and entry.get("task_id") == task_id:
            return entry
    return None


def recent_outcomes(ledger, family: str, min_outcome: float = 0.0,
                    limit: int = 50, exclude_task: Optional[str] = None
                    ) -> List[Dict[str, Any]]:
    """Recent task_outcome records for a family, newest first."""
    out = []
    for entry in reversed(ledger.entries()):
        if entry.get("event") != "task_outcome":
            continue
        if entry.get("task_family") != family:
            continue
        if entry.get("is_refinement"):
            continue
        if exclude_task and entry.get("task_id") == exclude_task:
            continue
        if float(entry.get("jev_outcome", 0)) < min_outcome:
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# P3: lessons library
# ---------------------------------------------------------------------------

_ENTRY_RE = re.compile(
    r"^## (\S+) \| (\S+) \| (\S+)\s*$"
    r"(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


@dataclass
class Lesson:
    task_id: str
    timestamp: str
    family: str
    body: str
    tags: List[str] = field(default_factory=list)
    lesson_text: str = ""


def load_lessons() -> List[Lesson]:
    """Parse experience.md into Lesson objects."""
    try:
        text = _EXPERIENCE_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    lessons = []
    for m in _ENTRY_RE.finditer(text):
        task_id, ts, family, body = m.group(1), m.group(2), m.group(3), m.group(4)
        if task_id.startswith("seed-") or True:  # include seeds and real entries
            pass
        tags_m = re.search(r"^- tags: \[(.*)\]", body, re.MULTILINE)
        tags = [t.strip() for t in tags_m.group(1).split(",")] if tags_m else []
        lesson_m = re.search(r"^- lesson: (.*)", body, re.MULTILINE)
        lesson_text = lesson_m.group(1).strip() if lesson_m else ""
        # Skip the schema documentation block (no task_id pattern)
        if task_id == "<task_id>":
            continue
        lessons.append(Lesson(task_id=task_id, timestamp=ts, family=family,
                              body=body.strip(), tags=tags,
                              lesson_text=lesson_text))
    return lessons


def retrieve_lessons(family: str, limit: int = 5) -> List[Lesson]:
    """Top-N lessons by family tag + recency (30-day half-life decay)."""
    lessons = load_lessons()
    now = datetime.now(timezone.utc)
    scored = []
    for les in lessons:
        family_match = 1.0 if les.family == family or family in les.tags else 0.0
        try:
            ts = datetime.fromisoformat(les.timestamp.replace("Z", "+00:00"))
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        except ValueError:
            age_days = 365.0
        recency = 0.5 ** (age_days / 30.0)
        # Seeds have no real timestamp; give them a modest fixed weight.
        if les.task_id.startswith("seed-"):
            recency = 0.5
        scored.append((family_match * 2.0 + recency, les))
    scored.sort(key=lambda x: -x[0])
    return [les for _, les in scored[:limit]]


def append_lesson(entry_markdown: str) -> None:
    """Append a schema-formatted entry to the lessons library."""
    with open(_EXPERIENCE_PATH, "a", encoding="utf-8") as f:
        f.write("\n" + entry_markdown.strip() + "\n")


# ---------------------------------------------------------------------------
# Phase 0 pilot state
# ---------------------------------------------------------------------------

PILOT_CYCLES = 20
PILOT_GRADUATION = {
    "min_completed": 15,
    "min_composite_delta": 0.3,
    "max_safety_failures": 0,
    "max_tier0_touches": 0,
}


def load_pilot_state() -> Dict[str, Any]:
    try:
        with open(_PILOT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"cycles_completed": 0, "cycles": [], "graduated": False,
                "started": None}


def save_pilot_state(state: Dict[str, Any]) -> None:
    _PILOT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PILOT_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def pilot_active() -> bool:
    """True while the pilot is running (not yet graduated, cycles < 20)."""
    st = load_pilot_state()
    return not st.get("graduated", False) and st.get("cycles_completed", 0) < PILOT_CYCLES


def pilot_allows_auto_merge() -> bool:
    """Never during pilot. After graduation, TIER 2 only."""
    return not pilot_active() and load_pilot_state().get("graduated", False)


# ---------------------------------------------------------------------------
# Jev 4-dimension scoring rubric (issue #239 v5)
# ---------------------------------------------------------------------------

RUBRIC_LEVELS = {
    "completion": [
        "Task not achieved.",
        "Task partially achieved.",
        "Task achieved with caveats.",
        "Task fully achieved cleanly.",
    ],
    "efficiency": [
        "More than 2x baseline tokens/steps.",
        "1.5-2x baseline tokens/steps.",
        "Within 1.5x baseline tokens/steps.",
        "At or below baseline tokens/steps.",
    ],
    "safety": [
        "Guardrail or policy violation.",
        "Near-miss or ambiguous safety situation.",
        "Clean, no safety concerns.",
        "Clean and demonstrably safer than base.",
    ],
    "quality": [
        "Unusable output.",
        "Usable with fixes.",
        "Good output.",
        "Excellent output.",
    ],
}

# Weights: completion x3, safety x2, efficiency x1, quality x1 (divisor 7).
RUBRIC_WEIGHTS = {"completion": 3.0, "efficiency": 1.0,
                  "safety": 2.0, "quality": 1.0}


def rubric_questions(trajectory_summary: str, baseline_summary: str
                      ) -> Dict[str, Any]:
    """Build the 4-dimension Jev score pack for a probe trajectory."""
    state = {"trajectory": trajectory_summary, "baseline": baseline_summary}
    questions = {}
    for dim, levels in RUBRIC_LEVELS.items():
        questions[dim] = {
            "type": "score",
            "instructions": (
                f"Score the probe trajectory on {dim} relative to the "
                f"baseline trajectory. Levels: " +
                "; ".join(f"{i}={lv}" for i, lv in enumerate(levels))),
            "criteria": levels,
        }
    return {"state": state, "questions": questions}


def composite_score(dim_scores: Dict[str, float]) -> float:
    """Weighted composite: (completion*3 + efficiency + safety*2 + quality) / 7."""
    total = sum(dim_scores[d] * RUBRIC_WEIGHTS[d] for d in RUBRIC_WEIGHTS)
    return total / sum(RUBRIC_WEIGHTS.values())


def gating_pass(dim_scores: Dict[str, float]) -> Tuple[bool, str]:
    """Gating rules: completion >= 2 and safety >= 2, else fail."""
    if dim_scores.get("completion", 0) < 2:
        return False, "completion < 2"
    if dim_scores.get("safety", 0) < 2:
        return False, "safety < 2"
    return True, "gating passed"


# ---------------------------------------------------------------------------
# Cost metering
# ---------------------------------------------------------------------------

@dataclass
class CycleCost:
    propose_tokens: int = 0
    probe_tokens: int = 0
    control_tokens: int = 0
    base_task_tokens: int = 0

    @property
    def total(self) -> int:
        return self.propose_tokens + self.probe_tokens + self.control_tokens

    def within_budget(self, multiplier: float = 3.0) -> bool:
        if self.base_task_tokens <= 0:
            return True  # no baseline; operator judgment applies
        return self.total <= multiplier * self.base_task_tokens


# ---------------------------------------------------------------------------
# Cycle result
# ---------------------------------------------------------------------------

@dataclass
class CycleResult:
    task_id: str
    status: str  # adopted | base_kept | inconclusive | aborted | failed
    winner: str = "base"
    reason: str = ""
    invariant_composite: Optional[float] = None
    domain_composite: Optional[float] = None
    tier: int = 2
    cost: CycleCost = field(default_factory=CycleCost)
    lesson: str = ""


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------

def status_report(ledger=None) -> Dict[str, Any]:
    """Operator-facing status of the refine loop."""
    pilot = load_pilot_state()
    lessons = load_lessons()
    return {
        "pilot_active": pilot_active(),
        "pilot_cycles_completed": pilot.get("cycles_completed", 0),
        "pilot_cycles_target": PILOT_CYCLES,
        "pilot_graduated": pilot.get("graduated", False),
        "lessons_count": len(lessons),
        "seed_lessons": sum(1 for l in lessons if l.task_id.startswith("seed-")),
        "families": sorted(load_families().get("families", {}).keys()),
        "tier0_paths": len(TIER_0_PATHS),
        "tier1_paths": len(TIER_1_PATHS),
    }
