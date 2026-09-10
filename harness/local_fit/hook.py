"""Flag-gated advisory hook prototype for candidate ordering.

This is an illustrative / prototype integration point for the local-fit
advisory layer. It is deliberately additive and inert unless the feature flags
are turned on.

It does NOT replace or rewrite any existing Harness capability/reliability
ordering math. It only attaches advisory scores and, optionally, applies a
small tiebreak nudge on top of an existing order key.

Environment flags
-----------------
HARNESS_LOCAL_FIT_ENABLE=1|true|yes   — turn the advisory layer on
HARNESS_LOCAL_FIT_MODEL_DIR=path        — directory with model.onnx + model_meta.json
HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER=1|true|yes — also apply the advisory as a
                                              small tiebreak on top of the existing
                                              existing_order_key (still advisory only)
"""

from typing import Any, Dict, List

from .config import is_enabled, load_scorer, seat_scores, advisory_key


def enabled() -> bool:
    """Whether the advisory layer is on at all."""
    return is_enabled()


def use_advisory_ordering() -> bool:
    """Whether to apply the advisory as a small tiebreak on top of existing_order_key."""
    return _env_bool("HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER", False)


def _env_bool(name: str, default: bool) -> bool:
    v = __import__("os").environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no", ""):
        return default
    return default


def score_candidates(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach advisory scores to candidates when enabled, else no-op.

    This is the primary hook a dispatch path would call. It never changes
    existing ordering by itself; call apply_advisory_tiebreak afterwards if you
    want the advisory to nudge the existing order.
    """
    if not enabled():
        for c in candidates:
            c["local_fit_advisory"] = {}
        return candidates

    scorer = load_scorer()
    if scorer is None:
        for c in candidates:
            c["local_fit_advisory"] = {}
        return candidates

    return _attach_scores(scorer, candidates)


def _attach_scores(scorer: Any, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in candidates:
        c = dict(c)
        feats = c.get("features") or {}
        c["local_fit_advisory"] = seat_scores(scorer, feats)
        out.append(c)
    return out


def apply_advisory_tiebreak(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply a small advisory tiebreak on top of each candidate's existing_order_key.

    This is intentionally small and advisory-only. It does not discard the
    existing order; it nudges it.
    """
    if not enabled():
        return list(candidates)

    scorer = load_scorer()
    if scorer is None:
        return list(candidates)

    cands = _attach_scores(scorer, candidates)

    if not use_advisory_ordering():
        return cands

    weight = _env_float("HARNESS_LOCAL_FIT_ADVISORY_TIEBREAK_WEIGHT", 0.05)

    def key(c: Dict[str, Any]) -> float:
        base = float(c.get("existing_order_key") or 0.0)
        adv = advisory_key(c.get("local_fit_advisory") or {})
        return base + adv * weight

    return sorted(cands, key=key, reverse=True)


def score_one(feats: Dict[str, Any]) -> Dict[str, Any]:
    """Score a single candidate feature dict (convenience wrapper)."""
    if not enabled():
        return {}
    scorer = load_scorer()
    if scorer is None:
        return {}
    return seat_scores(scorer, feats)


def _env_float(name: str, default: float) -> float:
    v = __import__("os").environ.get(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def explain(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a copy of candidates annotated with a short advisory explanation.

    Useful for debugging / reporting. Not a routing primitive.
    """
    out = score_candidates(candidates)
    for c in out:
        adv = c.get("local_fit_advisory") or {}
        c["_local_fit_explanation"] = {
            "enabled": enabled(),
            "scorer_loaded": (load_scorer() is not None) if enabled() else False,
            "usable_stop": adv.get("usable_stop"),
            "truncated": adv.get("truncated"),
            "unusable": adv.get("unusable"),
            "best_guess": adv.get("best_guess"),
            "advisory_key": advisory_key(adv),
        }
    return out
