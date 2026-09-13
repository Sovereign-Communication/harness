"""Feature canonicalization for the local-fit advisory layer (pure stdlib).

This module must stay dependency-free: it is imported by the runtime
inference path, and the Harness package promises zero runtime dependencies.

It owns the single canonical mapping from a seat feature dict to the float
vector the model consumes:

    categoricals -> one-hot (fixed vocabularies)
    numerics     -> z-scored with stats persisted in model metadata
    model_id_hash -> z-scored the same way (it is a numeric here)

``build_feature_vector`` is shared by training (train.py) and inference
(infer.py), so the two paths cannot drift.
"""

from typing import Any, Dict, List

from .schema import (
    TASK_TYPE_VOCAB,
    SEAT_ROLE_VOCAB,
    REASONING_EFFORT_VOCAB,
    one_hot,
)

# The exact ordered list of numeric (non-one-hot) features the vector adds,
# in order. Shared by the vector builder, the stats computation, and the
# expected-length helper so all three can never disagree.
NUMERIC_FEATURES = [
    "structured_output_required", "max_tokens_requested", "prompt_chars",
    "source_window_attached", "claims_count", "convergence_expected", "is_iterative",
    "model_id_hash", "free_tier", "declared_context_length",
    "declared_structured_json", "declared_reasoning",
    "observed_usable_rate", "observed_truncation_rate", "observed_unusable_rate",
    "observed_mean_resp_chars", "observed_median_resp_chars", "observed_max_resp_chars",
    "observed_sample_count",
    "prompt_tokens_est_over_context", "max_tokens_over_mean_resp",
    "structured_need_vs_declared_json", "iterative_vs_truncation_rate",
]

# Numeric defaults used when a feature is missing. These mirror the
# train.py append_* defaults so a partially-built dispatch-time feature dict
# degrades the same way a training row would.
NUMERIC_DEFAULTS = {
    "structured_output_required": 0.0,
    "max_tokens_requested": 2048.0,
    "prompt_chars": 0.0,
    "source_window_attached": 0.0,
    "claims_count": 0.0,
    "convergence_expected": 0.0,
    "is_iterative": 0.0,
    "model_id_hash": 0.0,
    "free_tier": 0.0,
    "declared_context_length": 0.0,
    "declared_structured_json": 0.0,
    "declared_reasoning": 0.0,
    "observed_usable_rate": 0.0,
    "observed_truncation_rate": 0.0,
    "observed_unusable_rate": 0.0,
    "observed_mean_resp_chars": 0.0,
    "observed_median_resp_chars": 0.0,
    "observed_max_resp_chars": 0.0,
    "observed_sample_count": 0.0,
    "prompt_tokens_est_over_context": 0.0,
    "max_tokens_over_mean_resp": 0.0,
    "structured_need_vs_declared_json": 0.0,
    "iterative_vs_truncation_rate": 0.0,
}


def build_feature_vector(row_features: Dict[str, Any], stats: Dict[str, Dict[str, float]]) -> List[float]:
    """Map a seat feature dict to the model input vector.

    ``stats`` maps feature name -> {"mean": float, "stdev": float} and is
    persisted in model metadata; at inference time the same stats are used so
    normalization is identical between train and serve.
    """
    vec: List[float] = []

    def append_num(key: str, default: float = 0.0) -> None:
        raw = row_features.get(key, default)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = default
        if key in stats:
            m = stats[key].get("mean", 0.0)
            s = stats[key].get("stdev", 1.0)
            if s == 0:
                s = 1.0
            val = (val - m) / s
        vec.append(val)

    def append_vocab(key: str, vocab: List[str]) -> None:
        value = row_features.get(key, "")
        vec.extend(one_hot(value, vocab))

    append_vocab("task_type", TASK_TYPE_VOCAB)
    append_vocab("seat_role", SEAT_ROLE_VOCAB)
    append_vocab("reasoning_effort", REASONING_EFFORT_VOCAB)

    for key in NUMERIC_FEATURES:
        append_num(key, NUMERIC_DEFAULTS.get(key, 0.0))

    return vec


def expected_vector_length() -> int:
    return (
        len(TASK_TYPE_VOCAB)
        + len(SEAT_ROLE_VOCAB)
        + len(REASONING_EFFORT_VOCAB)
        + len(NUMERIC_FEATURES)
    )


# ---------------------------------------------------------------------------
# Shared math primitives (used by both the extractor and the dispatch builder)
# ---------------------------------------------------------------------------

def hash_model_id(model: str) -> int:
    """Stable in-process hash of a model id (no PYTHONHASHSEED dependence)."""
    h = 0
    for ch in str(model):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return float(a) / float(b)


def prompt_chars_estimate(chars: int) -> int:
    # Very rough tokenizer-free estimate: ~4 chars per token on average text.
    return max(1, int(chars) // 4)


# ---------------------------------------------------------------------------
# Dispatch-side feature builder (live pool ordering path)
# ---------------------------------------------------------------------------

# Key-by-key mapping from live dispatch inputs to the training feature dict.
# The parity pin test (tests/test_local_fit_features.py) proves that this
# builder and extract._build_features produce identical dicts for an
# equivalent seat, so the model consumes the same distribution it trained on
# for every deterministically-shared field.
#
# Sources at dispatch time (order_pool in harness/capability.py):
#   model_id   : pool entry (str)
#   task       : "code" | "structured" | "default" (str)
#   call_lane  : "apply" | "panel"
#   profile    : CapabilityProfile or None (declared context/json/reasoning/free)
#   calibration: ledger participation_report()["calibration"][model] or None
#   observed   : optional extract-side observed-stats dict (parity/testing);
#                derived from `calibration` when omitted.

def _task_type_for(task: str, call_lane: str) -> str:
    if task == "structured":
        return "structured_claims"
    if call_lane == "panel":
        return "verify_panel"
    return "apply"


def _observed_from_calibration(calibration) -> Dict[str, Any]:
    cal = calibration or {}
    samples = int(cal.get("samples") or 0)
    unusable_events = int(cal.get("unusable_outputs") or 0) + int(cal.get("consent_unusable") or 0)
    success_rate = cal.get("success_rate")
    return {
        "usable_rate": float(success_rate) if success_rate is not None else 0.0,
        # The ledger does not record truncation events today; see README
        # "Known skew" notes. Stays 0.0 until the ledger grows the field.
        "truncation_rate": 0.0,
        "unusable_rate": (unusable_events / samples) if samples > 0 else 0.0,
        "mean_resp_chars": 0.0,
        "median_resp_chars": 0.0,
        "max_resp_chars": 0.0,
        "n": samples,
    }


def build_dispatch_features(
    model_id: str,
    *,
    task: str = "default",
    free_tier: bool = False,
    profile=None,
    calibration=None,
    call_lane: str = "apply",
    observed=None,
    _extract_overrides=None,
) -> Dict[str, Any]:
    """Build the canonical pre-dispatch feature dict for one candidate model.

    Returns the same dict shape as the extractor's per-seat features so the
    shared vector builder consumes both identically. Missing dispatch-time
    knowledge (prompt text, max tokens, reasoning effort) falls back to the
    same defaults the extractor uses for unknown runs, keeping the two
    distributions aligned by construction.

    ``_extract_overrides`` is reserved for the extractor: run JSON knows
    richer per-seat context (prompt chars, max tokens, reasoning effort, the
    exact seat role) than live pool ordering does, and the extractor passes
    those fields through this hook so both paths still share one dict
    construction. It is not part of the public dispatch API.
    """
    task_type = _task_type_for(task, call_lane)
    seat_role = "panel" if call_lane == "panel" else "apply"

    obs = dict(observed) if observed else _observed_from_calibration(calibration)

    context_length = int(getattr(profile, "context_length", 0) or 0)
    declared_json = bool(
        getattr(profile, "supports_structured_json", False)
        or getattr(profile, "supports_json_schema", False)
        or getattr(profile, "supports_response_format", False)
    )
    declared_reasoning = bool(getattr(profile, "supports_reasoning", False))
    model_free = bool(getattr(profile, "free", free_tier))

    max_tokens_requested = 2048  # extractor default for unknown runs
    prompt_chars = 0             # prompt not available at pool-ordering time
    is_iterative = False

    features = {
        "task_type": task_type,
        "seat_role": seat_role,
        "structured_output_required": task_type == "structured_claims",
        "max_tokens_requested": max_tokens_requested,
        # "auto" mirrors the extractor's default for runs with no recorded
        # reasoning effort, so the one-hot matches the training distribution.
        "reasoning_effort": "auto",
        "prompt_chars": prompt_chars,
        "source_window_attached": False,
        "claims_count": 0,
        "convergence_expected": call_lane == "panel" and task == "structured",
        "is_iterative": is_iterative,
        "model_id_hash": hash_model_id(model_id),
        "free_tier": model_free,
        "declared_context_length": context_length,
        "declared_structured_json": declared_json,
        "declared_reasoning": declared_reasoning,
        "observed_usable_rate": obs.get("usable_rate", 0.0),
        "observed_truncation_rate": obs.get("truncation_rate", 0.0),
        "observed_unusable_rate": obs.get("unusable_rate", 0.0),
        "observed_mean_resp_chars": obs.get("mean_resp_chars", 0.0),
        "observed_median_resp_chars": obs.get("median_resp_chars", 0.0),
        "observed_max_resp_chars": obs.get("max_resp_chars", 0.0),
        "observed_sample_count": obs.get("n", 0),
        "prompt_tokens_est_over_context": safe_div(
            prompt_chars_estimate(prompt_chars), context_length or 1),
        "max_tokens_over_mean_resp": safe_div(
            max_tokens_requested, obs.get("mean_resp_chars", 1) or 1),
        "structured_need_vs_declared_json": int(
            task_type == "structured_claims" and not declared_json),
        "iterative_vs_truncation_rate": (
            safe_div(float(is_iterative), max(obs.get("truncation_rate", 0.0), 1e-6))
            if is_iterative else 0.0),
    }
    if _extract_overrides:
        # Extractor-only knowledge injection (see docstring). Keys are the
        # same feature names; the interaction features are recomputed after
        # the overrides so they stay internally consistent.
        features.update(_extract_overrides)
        pc = features.get("prompt_chars", 0)
        mt = features.get("max_tokens_requested", 2048)
        it = bool(features.get("is_iterative", False))
        ctx = features.get("declared_context_length", 0) or 1
        djson = features.get("declared_structured_json", False)
        ttr = features.get("observed_truncation_rate", 0.0)
        features["prompt_tokens_est_over_context"] = safe_div(
            prompt_chars_estimate(pc), ctx)
        features["max_tokens_over_mean_resp"] = safe_div(
            mt, features.get("observed_mean_resp_chars", 1) or 1)
        features["structured_need_vs_declared_json"] = int(
            features.get("task_type") == "structured_claims" and not djson)
        features["iterative_vs_truncation_rate"] = (
            safe_div(float(it), max(ttr, 1e-6)) if it else 0.0)
    return features
