"""Illustrative, flag-gated dispatch hook prototype.

This module shows *where* a real dispatch path in Harness would consult the
local-fit advisory layer. It is deliberately separated from any existing
Harness routing math and is inert unless the feature flags are on.

It is a prototype for re-merge discussion, not a claim that it is wired into
live Harness behavior.

Usage shape (illustrative)
---------------------------
A real dispatch path that already builds a candidate list with an
`existing_order_key` per candidate could do something like::

    from harness.local_fit.dispatch_hook import maybe_score_and_order

    candidates = [...]            # each candidate has "features" + "existing_order_key"
    ordered = maybe_score_and_order(candidates)

This returns candidates annotated with `local_fit_advisory` and, when the
ordering flag is also on, re-sorted by a small advisory tiebreak on top of
the existing order key. The existing order is never discarded.
"""

from typing import Any, Dict, List

from .hook import score_candidates, apply_advisory_tiebreak, enabled, use_advisory_ordering


def maybe_score_and_order(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Score candidates and optionally nudge existing ordering, when enabled.

    When disabled, returns candidates unchanged except for an empty advisory
    dict attached.
    """
    if not enabled():
        for c in candidates:
            c.setdefault("local_fit_advisory", {})
        return list(candidates)

    scored = score_candidates(candidates)
    return apply_advisory_tiebreak(scored)


def maybe_score_only(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach advisory scores without touching any ordering.

    This is the safest integration point: the existing capability/reliability
    ordering is preserved exactly, and the advisory is only reported.
    """
    return score_candidates(list(candidates))


def explain_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach a short advisory explanation to each candidate."""
    from .hook import explain

    out = score_candidates(candidates)
    return explain(out)


def decision_snapshot(
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return a small snapshot useful for logging/reporting."""
    scored = score_candidates(list(candidates))
    nonzero = [c for c in scored if (c.get("local_fit_advisory") or {})]
    after = maybe_score_and_order(candidates)
    return {
        "enabled": enabled(),
        "advisory_ordering": use_advisory_ordering(),
        "candidate_count": len(candidates),
        "candidates_with_advisory": len(nonzero),
        "candidates": after,
    }
