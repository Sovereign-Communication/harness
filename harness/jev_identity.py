"""Authoritative Jev self-knowledge for chat lanes.

Jev is part of this product, not a generic UX heuristic.  Before the model is
asked to perform a "Jev assessment" it must see the repository's own definition
and gate policy, so it cannot ask the operator what Jev means or invent an
acronym.  The block is small, data-only, and bounded; the live skill and policy
remain the source of truth.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_JEV_MENTION_RE = re.compile(r"\bjev\b|\bsystem\s+one\b|\btypesafe\b", re.IGNORECASE)

_HEADER = """\
JEV IDENTITY (authoritative; this repository owns Jev):
Jev is Harness's typed-judgment component (TypeSafe System One). It returns
typed answers and Noul probabilities for named questions; code owns the
workflow and combines the judgments. It is not a generic usability framework,
not an acronym to expand, and not something to ask the operator to define.
The canonical gate policy is docs/JEV_GATE_POLICY_V1.md. For a site audit,
every Jev judgment must be grounded in fetched or driven evidence, and a
capability that is not available (browser driving, multi-region tests) must be
named as unavailable rather than simulated or replaced with invented scores.
Never assign a numeric score to an aspect that was not actually exercised:
static fetch text is not interaction evidence. Mark such an aspect
"unassessed (no driven evidence)" and do not average it into a 99/100 verdict.
When the operator supplies a bar (for example 99/100 in every aspect), any
scored aspect below that bar is a FAIL for the requested bar, even when the
core flow works; unassessed aspects also prevent a blanket pass. State the
below-bar aspects and do not call the run a pass.
"""

_POLICY_FALLBACK = """\
Gate on risk, not on numbers: no signal, malformed answers, destructive intent,
an explicit escalate disposition, or a weak disposition lead all fail closed.
A low score starts investigation; only actual risk blocks. A disposition's
confidence is telemetry, not permission."""

_SKILL_FALLBACK = """\
TypeSafe makes units of AI intelligence usable like programming primitives.
Jev is TypeSafe's flagship System One model: it understands natural language
and returns typed answers and probabilities rather than prose. Code owns the
workflow; the model supplies programmable common sense where ordinary code
needs semantic understanding."""


def jev_mentioned(prompt: str) -> bool:
    """True when the request names Jev or its TypeSafe/System One component."""
    return bool(_JEV_MENTION_RE.search(str(prompt or "")))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_bounded(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit].strip()
    except OSError:
        return ""


@lru_cache(maxsize=1)
def jev_identity_block() -> str:
    """The authoritative block, with the repository's own policy and skill.

    The cache is process-local and keyed by nothing: the files are static for
    the life of a run.  Missing files degrade to the short fallback rather than
    making a chat turn fail.
    """
    root = _repo_root()
    policy = _read_bounded(root / "docs" / "JEV_GATE_POLICY_V1.md", 5000)
    skill = _read_bounded(root / ".agents" / "skills" / "typesafe-ai" / "SKILL.md", 5000)
    chunks = [_HEADER]
    if policy:
        chunks.append("GATE POLICY EXCERPT:\n" + policy)
    elif _POLICY_FALLBACK:
        chunks.append("GATE POLICY:\n" + _POLICY_FALLBACK)
    if skill:
        chunks.append("TYPESAFE SKILL EXCERPT:\n" + skill)
    else:
        chunks.append("TYPESAFE SKILL:\n" + _SKILL_FALLBACK)
    return "\n\n".join(chunks)


__all__ = ["jev_identity_block", "jev_mentioned"]
