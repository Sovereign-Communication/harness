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
