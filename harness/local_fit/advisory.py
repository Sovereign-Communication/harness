"""Advisory scoring entrypoint.

Callers use this to attach local-fit scores to candidate seats without
changing any existing Harness guarantee.
"""

from typing import Any, Dict, List, Optional

from .config import (
    is_enabled,
    load_scorer,
    seat_scores,
    advisory_key,
    order_candidates_with_advisory,
)


def maybe_score_candidates(
    candidates: List[Dict[str, Any]],
    use_tiebreak: bool = True,
) -> List[Dict[str, Any]]:
    """Attach advisory scores when the feature is enabled, else no-op."""
    if not is_enabled():
        for c in candidates:
            c["local_fit_advisory"] = {}
        return candidates
    scorer = load_scorer()
    return order_candidates_with_advisory(candidates, scorer, use_advisory_as_tiebreak=use_tiebreak)


def score_one(features: Dict[str, Any]) -> Dict[str, Any]:
    """Score a single candidate seat feature dict."""
    if not is_enabled():
        return {}
    scorer = load_scorer()
    if scorer is None:
        return {}
    return seat_scores(scorer, features)
