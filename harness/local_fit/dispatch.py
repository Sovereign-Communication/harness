"""The live dispatch integration for the local-fit advisory layer.

This is the module ``capability.order_pool`` calls. It is flag-gated with
three graduated stages and is inert (and invisible) by default:

OFF      — HARNESS_LOCAL_FIT_ENABLE unset: nothing runs, nothing loads.
OBSERVE  — ENABLE + MODEL_DIR set: candidates are scored and returned on the
           result for caller logging; the returned order is identical to the
           baseline order.
INFLUENCE— additionally HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER=1: models the
           scorer flags as likely-unusable (p_unusable >= threshold, default
           0.6) sort AFTER their peers within the same demotion tier. Ties
           inside a group fall back to the baseline sort key, so the baseline
           order is preserved exactly among all non-flagged models.

Design invariants (each pinned by tests):
- OFF/OBSERVE never reorder: the output is a copy of the baseline order.
- INFLUENCE only demotes within a demotion tier; it never promotes a model
  above its tier peers and never crosses the demotion boundary itself.
- Every input path is fail-closed: any scoring error degrades to OBSERVE
  behavior (scores dropped, order untouched). This function never raises.
- A degenerate/saturated artifact (one that cannot distinguish candidates:
  near-constant scores or top-class probability saturated on every input)
  must NOT be trusted for ordering. It is detected and the layer stands
  down to baseline ordering with an observable reason on the result. A
  model that legitimately scores everyone healthy is NOT degenerate: its
  score spread is real and "no reorder" is the correct outcome.
"""

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .features import build_dispatch_features

# Stage names (informational; returned in the result dict for logging).
STAGE_OFF = "off"
STAGE_OBSERVE = "observe"
STAGE_INFLUENCE = "influence"

DEFAULT_UNUSABLE_THRESHOLD = 0.6

# Degenerate-artifact guard (dispatch-side; see also train.DEGENERATE_SPREAD).
# These bound what a *usable* artifact can look like across a pool of
# candidates. An artifact whose score structure cannot distinguish candidates
# must not order them; standing down is the honest behavior. Tuned so that:
# - a genuinely health-scored pool (no reorder is CORRECT) still shows real
#   probability separation (measured spread on the real corpus: >= 0.6
#   p_unusable between healthy and failing models after export calibration),
# - the pre-fix saturated artifact (spread ~2.8e-2 raw, ~1e-4 after naive
#   sharpening with a collapsed top class) fails both checks below.
MIN_SPREAD = 0.01
MIN_TOP_DIFF = 0.02  # a pinned top-1 leaves < 2% probability unassigned


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def stage() -> str:
    """Which stage the layer is operating in right now."""
    from .config import is_enabled

    if not is_enabled():
        return STAGE_OFF
    if _env_bool("HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"):
        return STAGE_INFLUENCE
    return STAGE_OBSERVE


def unusable_threshold() -> float:
    return _env_float("HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD",
                      DEFAULT_UNUSABLE_THRESHOLD)


def degenerate_reason(scores: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Classify a score map as degenerate, returning the reason when it is.

    Two distinct failure shapes are detected (and must be distinguished from
    a legitimate all-healthy pool):

    - "score_spread_too_small": every candidate's p_unusable is nearly
      identical — the artifact does not separate candidates, so any order it
      implies is noise (this is the saturation observed on the real corpus:
      p_unusable=0.0000 / p_stop=1.0000 for EVERY model).
    - "top_class_saturated": EVERY candidate's top-1 class probability is
      pinned to ~1.0 (all within MIN_TOP_DIFF of 1.0). Softmax always sums to
      1, so "everyone healthy" with real separation still leaves room below
      the max; every top-1 pinned means the artifact's logit range has
      collapsed and the probabilities carry no usable signal. A single
      confident candidate among mixed ones is NOT saturation.

    Scores that vary with meaningful spread, even if every best_guess is
    "healthy", are NOT degenerate: returning the same verdict for every
    candidate can be the correct answer when the candidates really are all
    healthy.
    """
    if len(scores) < 2:
        return None
    try:
        unusables = [float(s.get("unusable", 0.0)) for s in scores.values()]
    except (TypeError, ValueError):
        return "malformed_scores"
    if max(unusables) - min(unusables) < MIN_SPREAD:
        return "score_spread_too_small"
    try:
        tops = []
        for s in scores.values():
            top = max(float(s.get("unusable", 0.0)),
                      float(s.get("truncated", 0.0)),
                      float(s.get("usable_stop", 0.0)))
            tops.append(top)
    except (TypeError, ValueError):
        return "malformed_scores"
    if min(tops) >= 1.0 - MIN_TOP_DIFF:
        return "top_class_saturated"
    return None


def _warn(msg: str) -> None:
    """Observable stand-down notice. stderr keeps the advisory inert on stdout
    (machine-readable) while still surfacing WHY it did not act."""
    try:
        print(f"[local_fit] standing down: {msg}", file=sys.stderr)
    except Exception:
        pass


def maybe_order_pool(
    pool: List[str],
    *,
    task: str = "default",
    free_tier: Optional[bool] = None,
    profiles=None,
    calibration: Optional[Dict[str, Dict[str, Any]]] = None,
    call_lane: str = "apply",
    baseline_keys: Optional[Dict[str, Tuple]] = None,
) -> Dict[str, Any]:
    """Consult the advisory layer when ordering the live pool.

    Parameters mirror ``capability.order_pool``'s inputs so the call site
    passes them straight through. ``baseline_keys`` maps model id -> its
    baseline sort key tuple (from order_pool's ``scored`` list); it defines
    both the observed baseline order and the tier-preserving tiebreak.

    Returns a dict:
        stage      : "off" | "observe" | "influence"
        ordered    : the pool in advisory order (== baseline unless INFLUENCE
                     actually reorders)
        scores     : model id -> advisory score dict (empty when off/failed)
        reordered  : True only when INFLUENCE changed the order
    """
    result = {
        "stage": STAGE_OFF,
        "ordered": list(pool or []),
        "scores": {},
        "flagged": [],
        "reordered": False,
        "degenerate": None,
    }
    if not pool:
        return result

    try:
        if stage() == STAGE_OFF:
            return result

        from .config import load_scorer

        scorer = load_scorer()
        if scorer is None:
            # Enabled but no loadable artifact: fail closed to OFF semantics.
            result["stage"] = STAGE_OFF
            return result

        cal = calibration or {}
        scores: Dict[str, Dict[str, Any]] = {}
        for model in pool:
            try:
                feats = build_dispatch_features(
                    model,
                    task=task,
                    free_tier=bool(free_tier) if free_tier is not None else False,
                    profile=(profiles or {}).get(model),
                    calibration=cal.get(model),
                    call_lane=call_lane,
                )
                scores[model] = scorer.score(feats)
            except Exception:
                # One model's scoring failure must not affect the others.
                continue
        result["scores"] = scores
        result["stage"] = STAGE_OBSERVE

        if stage() != STAGE_INFLUENCE or not scores:
            return result

        threshold = unusable_threshold()
        flagged = {
            m for m, s in scores.items()
            if float(s.get("unusable", 0.0)) >= threshold
        }
        if not flagged and len(scores) >= 2:
            # Nobody crosses the threshold, but a pool-wide flag at a threshold
            # no candidate can plausibly reach is itself the saturation
            # signature (pre-fix real corpus: p_unusable==0.0000 for EVERY
            # candidate). Distinguish it from an honest all-clear so the
            # caller can tell the two apart; either way: no reorder.
            degenerate = degenerate_reason(scores)
            if degenerate:
                result["degenerate"] = degenerate
                result["flagged"] = []
                result["reordered"] = False
                result["ordered"] = list(pool)
                result["stage"] = STAGE_OBSERVE
                _warn(f"degenerate artifact ({degenerate}); "
                      f"advisory order suppressed (baseline kept, "
                      f"{len(scores)} candidates scored)")
            return result
        if not flagged:
            # Genuinely nobody is at risk: the correct no-op.
            return result

        # Degenerate-artifact guard, fail-closed: an artifact that cannot
        # distinguish candidates (or has collapsed its top class) must not
        # reorder. A flagged set computed from such scores is noise, so the
        # layer stands down to baseline ordering and says why. This check sits
        # AFTER the flag set so OBSERVE keeps reporting raw scores untouched.
        degenerate = degenerate_reason(scores)
        if degenerate:
            result["degenerate"] = degenerate
            result["flagged"] = []
            result["reordered"] = False
            result["ordered"] = list(pool)
            _warn(f"degenerate artifact ({degenerate}); "
                  f"INFLUENCE order suppressed (baseline kept, "
                  f"{len(scores)} candidates scored)")
            return result

        base = baseline_keys or {}

        def sort_key(model: str) -> Tuple:
            # The advisory flag applies WITHIN the demotion tier: baseline
            # key element 0 is the tier (0 healthy / 1 demoted), so compose
            # (tier, flagged-last, rest-of-baseline-key). This can reorder
            # models inside a tier but can never cross a tier boundary — a
            # flagged healthy model still sorts below every healthy unflagged
            # one and above every demoted model.
            bk = base.get(model)
            if not bk:
                return (1 if model in flagged else 0,)
            return (bk[0], 1 if model in flagged else 0) + tuple(bk[1:])

        ordered = sorted(pool, key=sort_key)
        result["ordered"] = ordered
        result["flagged"] = sorted(m for m in pool if m in flagged)
        result["stage"] = STAGE_INFLUENCE
        result["reordered"] = ordered != list(pool)
        return result
    except Exception:
        # Absolute fail-closed guarantee: any unexpected error returns the
        # untouched baseline order.
        result["ordered"] = list(pool or [])
        result["scores"] = {}
        result["reordered"] = False
        return result
