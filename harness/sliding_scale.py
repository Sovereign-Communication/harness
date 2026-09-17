"""Dynamic sliding-scale task complexity classification and model routing ladder.

Inherits the "free lanes first, always" and "cheapest capable model" discipline:
  - Tier 0 (Scout / AST / Fast): Syntax checking, docstrings, formatting, single-file
    scaffolding, and candidate symbol discovery. Free / sub-cent models.
  - Tier 1 (Distiller / Standard): Structured business logic, test implementation,
    context-condensed micro-brief reasoning, and multi-round bug fixing.
  - Tier 2 (Frontier / Specialist): Architectural refactoring, concurrency,
    cryptographic/security invariants, cross-module protocols, and consensus mechanisms.
    Reserved for frontier-class models (Fable 5.1 / GPT-6 / Claude Opus tier / top deep thinkers).

Provides upfront task classification before spend occurs, ensuring expensive
models are dispatched only when warranted and paired with dense micro-briefs.
"""
from dataclasses import dataclass
import re
from typing import Sequence

from .config import (
    DEFAULT_APPLY_POOL_PAID,
    DEFAULT_JUDGE_PAID,
    DEFAULT_JUDGE_PAID_TOP,
    DEFAULT_PANEL_PAID,
    ESCALATION_POOL_FREE,
    ESCALATION_POOL_PAID,
    FREE_APPLY_POOL,
    FREE_JUDGE,
    FREE_PANEL_POOL,
    HARD_MAX_COST,
)

# Canonical tier constants
TIER_0_SCOUT = 0
TIER_1_DISTILLER = 1
TIER_2_FRONTIER = 2

# Curated frontier aliases for operator and user convenience
FRONTIER_ALIASES: dict[str, str] = {
    "fable-5.1": "fable/fable-5.1",
    "fable/fable-5.1": "fable/fable-5.1",
    "gpt-6": "openai/gpt-6",
    "openai/gpt-6": "openai/gpt-6",
    "claude-opus": "anthropic/claude-3-opus",
    "anthropic/claude-3-opus": "anthropic/claude-3-opus",
    "claude-3.5-sonnet": "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-sonnet": "anthropic/claude-3.5-sonnet",
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "sol": "openai/gpt-5.6-sol",
    "gpt-4.1": "openai/gpt-4.1",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
}

# Linguistic keyword markers for complexity heuristics
_TIER_0_MARKERS = (
    "comment", "docstring", "lint", "format", "typo", "rename",
    "type hint", "annotation", "whitespace", "style", "signature",
    "ast extract", "view", "find", "search", "probe",
)

_TIER_2_MARKERS = (
    "architect", "concurrency", "deadlock", "race condition", "mutex",
    "lock", "thread", "parallel", "cryptograph", "security", "exploit",
    "vulnerability", "consensus", "protocol", "invariant", "migration",
    "re-architect", "state machine", "distributed", "zero-copy",
    "memory leak", "tamper", "hash chain", "byok",
)

_TIER_1_MARKERS = (
    "implement", "refactor", "update", "fix", "add test", "handler",
    "endpoint", "dispatch", "parse", "validate", "convert", "transform",
    "serialize", "deserialize", "cache", "retry",
)


@dataclass(frozen=True)
class TaskClassification:
    """Immutable classification result for a task or DAG node."""

    tier: int
    score: float
    reasons: tuple[str, ...]
    recommended_model: str
    estimated_cost_tier: str


@dataclass(frozen=True)
class SlidingScaleRoute:
    """Full sliding-scale route including model ladder and cost ceiling."""

    classification: TaskClassification
    ladder: tuple[str, ...]
    cost_ceiling: float


def resolve_frontier_model(custom_frontier: str | None = None, use_free: bool = False) -> str:
    """Resolve a user or config frontier model, mapping aliases when present."""
    if custom_frontier:
        stripped = custom_frontier.strip()
        if stripped in FRONTIER_ALIASES:
            return FRONTIER_ALIASES[stripped]
        return stripped
    if use_free:
        return FREE_JUDGE
    return DEFAULT_JUDGE_PAID_TOP


def classify_task_tier(
    instruction: str,
    target_files: Sequence[str] | None = None,
    diff_size: int | None = None,
    dependency_depth: int = 0,
    is_leaf: bool = True,
    previous_failures: int = 0,
    use_free: bool = True,
    custom_frontier: str | None = None,
) -> TaskClassification:
    """Classify a task into Tier 0 (Scout), Tier 1 (Distiller), or Tier 2 (Frontier).

    Pure, deterministic evaluation based on instruction semantics, target scope,
    dependency depth, and retry history.
    """
    reasons: list[str] = []
    score = 0.20  # neutral starting score

    instr_lower = instruction.lower()

    # 1. Semantic keyword matching
    t0_matches = [m for m in _TIER_0_MARKERS if re.search(r"\b" + re.escape(m), instr_lower)]
    t2_matches = [m for m in _TIER_2_MARKERS if re.search(r"\b" + re.escape(m), instr_lower)]
    t1_matches = [m for m in _TIER_1_MARKERS if re.search(r"\b" + re.escape(m), instr_lower)]

    if t2_matches:
        boost = min(0.50, 0.25 * len(t2_matches))
        score += boost
        reasons.append(f"frontier keywords detected (+{boost:.2f}): {', '.join(t2_matches[:3])}")
    elif t0_matches:
        penalty = min(0.30, 0.15 * len(t0_matches))
        score -= penalty
        reasons.append(f"scout/formatting keywords detected (-{penalty:.2f}): {', '.join(t0_matches[:3])}")
    elif t1_matches:
        boost = min(0.30, 0.10 * len(t1_matches))
        score += boost
        reasons.append(f"standard implementation keywords (+{boost:.2f}): {', '.join(t1_matches[:3])}")

    # 2. Scope of target files
    files = list(target_files or [])
    if len(files) > 3:
        score += 0.25
        reasons.append(f"broad target file scope ({len(files)} files, +0.25)")
    elif len(files) in (2, 3):
        score += 0.10
        reasons.append(f"multi-file scope ({len(files)} files, +0.10)")
    elif len(files) == 1:
        reasons.append("single target file scope")

    # 3. Estimated diff size / line churn
    if diff_size is not None:
        if diff_size > 150:
            score += 0.25
            reasons.append(f"large churn estimated ({diff_size} lines, +0.25)")
        elif diff_size > 50:
            score += 0.10
            reasons.append(f"moderate churn estimated ({diff_size} lines, +0.10)")

    # 4. DAG dependency depth & topological position
    if dependency_depth > 2:
        score += 0.20
        reasons.append(f"deep DAG dependency depth ({dependency_depth}, +0.20)")
    elif dependency_depth > 0:
        score += 0.10
        reasons.append(f"DAG dependency depth ({dependency_depth}, +0.10)")

    if not is_leaf:
        score += 0.05
        reasons.append("intermediate non-leaf node (+0.05)")

    # 5. Dynamic retry / escalation history
    if previous_failures > 0:
        esc_boost = min(0.60, 0.30 * previous_failures)
        score += esc_boost
        reasons.append(f"retry escalation ({previous_failures} previous failure(s), +{esc_boost:.2f})")

    # Clamp score to [0.0, 1.0]
    final_score = max(0.0, min(1.0, score))

    # Tier mapping thresholds
    if previous_failures >= 2 or final_score >= 0.65:
        tier = TIER_2_FRONTIER
        cost_tier = "free" if use_free else "frontier"
    elif final_score >= 0.35:
        tier = TIER_1_DISTILLER
        cost_tier = "free" if use_free else "budget"
    else:
        tier = TIER_0_SCOUT
        cost_tier = "free" if use_free else "budget"

    # Select recommended model for the tier
    rec_model = resolve_tier_recommended_model(tier, use_free=use_free, custom_frontier=custom_frontier)

    return TaskClassification(
        tier=tier,
        score=round(final_score, 3),
        reasons=tuple(reasons),
        recommended_model=rec_model,
        estimated_cost_tier=cost_tier,
    )


def resolve_tier_recommended_model(tier: int, use_free: bool = True, custom_frontier: str | None = None) -> str:
    """Return the primary recommended model for a given tier."""
    if use_free:
        if tier == TIER_0_SCOUT:
            return FREE_PANEL_POOL[1] if len(FREE_PANEL_POOL) > 1 else FREE_PANEL_POOL[0]
        if tier == TIER_1_DISTILLER:
            return FREE_APPLY_POOL[0]
        return FREE_JUDGE
    else:
        if tier == TIER_0_SCOUT:
            return DEFAULT_PANEL_PAID[0]
        if tier == TIER_1_DISTILLER:
            return DEFAULT_APPLY_POOL_PAID[0]
        return resolve_frontier_model(custom_frontier, use_free=False)


def tier_model_ladder(tier: int, use_free: bool = True, custom_frontier: str | None = None) -> list[str]:
    """Return an ordered candidate ladder for the given tier, cheapest first."""
    out: list[str] = []
    if use_free:
        if tier == TIER_0_SCOUT:
            for m in [FREE_PANEL_POOL[1], FREE_PANEL_POOL[2], FREE_PANEL_POOL[0], "openrouter/free"]:
                if m not in out:
                    out.append(m)
        elif tier == TIER_1_DISTILLER:
            for m in FREE_APPLY_POOL:
                if m not in out:
                    out.append(m)
        else:  # TIER_2_FRONTIER
            for m in [FREE_JUDGE] + ESCALATION_POOL_FREE:
                if m not in out:
                    out.append(m)
    else:
        if tier == TIER_0_SCOUT:
            candidates = [DEFAULT_PANEL_PAID[0], DEFAULT_PANEL_PAID[1], DEFAULT_PANEL_PAID[2]]
            for m in candidates:
                if m not in out:
                    out.append(m)
        elif tier == TIER_1_DISTILLER:
            candidates = list(DEFAULT_APPLY_POOL_PAID) + [DEFAULT_JUDGE_PAID]
            for m in candidates:
                if m not in out:
                    out.append(m)
        else:  # TIER_2_FRONTIER
            frontier = resolve_frontier_model(custom_frontier, use_free=False)
            out.append(frontier)
            for m in ESCALATION_POOL_PAID:
                if m not in out:
                    out.append(m)
    return out


def tier_cost_ceiling(tier: int, use_free: bool = True) -> float:
    """Return the preflight spend ceiling for a given tier in USD."""
    if use_free:
        return 0.0
    if tier == TIER_0_SCOUT:
        return 0.01
    if tier == TIER_1_DISTILLER:
        return 0.04
    # Tier 2 frontier hard cap
    return min(0.10, HARD_MAX_COST)


def resolve_sliding_scale_route(
    instruction: str,
    target_files: Sequence[str] | None = None,
    diff_size: int | None = None,
    dependency_depth: int = 0,
    is_leaf: bool = True,
    previous_failures: int = 0,
    use_free: bool = True,
    custom_frontier: str | None = None,
) -> SlidingScaleRoute:
    """Classify and resolve full routing ladder and budget ceiling in one call."""
    classification = classify_task_tier(
        instruction=instruction,
        target_files=target_files,
        diff_size=diff_size,
        dependency_depth=dependency_depth,
        is_leaf=is_leaf,
        previous_failures=previous_failures,
        use_free=use_free,
        custom_frontier=custom_frontier,
    )
    ladder = tier_model_ladder(
        tier=classification.tier,
        use_free=use_free,
        custom_frontier=custom_frontier,
    )
    ceiling = tier_cost_ceiling(
        tier=classification.tier,
        use_free=use_free,
    )
    return SlidingScaleRoute(
        classification=classification,
        ladder=tuple(ladder),
        cost_ceiling=ceiling,
    )
