"""Advisory scoring entrypoint (thin back-compat re-export).

The canonical hook API lives in :mod:`hook`; this module re-exports it so
historical imports keep working. New code should import from ``hook`` (or use
:mod:`dispatch`, the live order_pool integration) directly.
"""

from typing import Any, Dict, List

from .hook import score_candidates, apply_advisory_tiebreak, score_one  # noqa: F401


def maybe_score_candidates(
    candidates: List[Dict[str, Any]],
    use_tiebreak: bool = True,
) -> List[Dict[str, Any]]:
    """Attach advisory scores when the feature is enabled, else no-op.

    With ``use_tiebreak=True`` (default) the returned list may be re-sorted by
    the small advisory tiebreak when the ordering flag is also on; with
    ``use_tiebreak=False`` the input order is always preserved.
    """
    scored = score_candidates(candidates)
    if use_tiebreak:
        return apply_advisory_tiebreak(scored)
    return scored
