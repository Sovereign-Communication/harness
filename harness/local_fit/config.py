"""Opt-in advisory hook for local model-fit scoring.

This module is guarded by HARNESS_LOCAL_FIT_ENABLE.

When disabled, it remains inert and does not affect existing behavior.
When enabled, it provides advisory scores that may influence candidate
ordering in the isolated integration path only.

The flag is read dynamically from the environment each call so that tests
can toggle it at runtime without reloading the module.
"""

import os
from typing import Any, Dict, List, Optional

_MODEL_FILE = "model.onnx"
_META_FILE = "model_meta.json"


def _env_enabled() -> bool:
    return os.environ.get("HARNESS_LOCAL_FIT_ENABLE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _env_model_dir() -> str:
    return os.environ.get("HARNESS_LOCAL_FIT_MODEL_DIR", "").strip()


def is_enabled() -> bool:
    return _env_enabled() and bool(_env_model_dir())


def load_scorer() -> Optional[Any]:
    if not is_enabled():
        return None
    try:
        from .model_loader import LocalScorer

        return LocalScorer(
            os.path.join(_env_model_dir(), _MODEL_FILE),
            os.path.join(_env_model_dir(), _META_FILE),
        )
    except Exception:
        return None


def seat_scores(scorer: Any, row_features: Dict[str, Any]) -> Dict[str, Any]:
    if scorer is None:
        return {}
    return scorer.score(row_features)


def advisory_key(scores: Dict[str, Any]) -> float:
    """Return a single advisory value: higher = better usability fit.

    Simple fallback if only raw probabilities are available.
    """
    if not scores:
        return 0.0
    usable = scores.get("usable_stop", 0.0)
    truncated = scores.get("truncated", 0.0)
    unusable = scores.get("unusable", 0.0)
    return float(usable) - float(truncated) - float(unusable)


def order_candidates_with_advisory(
    candidates: List[Dict[str, Any]],
    scorer: Optional[Any],
    use_advisory_as_tiebreak: bool = True,
) -> List[Dict[str, Any]]:
    """Return candidate list with optional advisory scores appended.

    This does NOT reorder existing capability/reliability ordering here.
    It attaches advisory scores for the caller to use as it sees fit.
    """
    if scorer is None:
        for c in candidates:
            c["local_fit_advisory"] = {}
        return candidates

    for c in candidates:
        c["local_fit_advisory"] = seat_scores(scorer, c.get("features", {}))
    if not use_advisory_as_tiebreak:
        return candidates

    def key(c: Dict[str, Any]) -> float:
        base = c.get("existing_order_key", 0.0)
        adv = advisory_key(c.get("local_fit_advisory", {}))
        return base + adv * 0.05  # small weight; advisory is a nudge

    return sorted(candidates, key=key, reverse=True)
