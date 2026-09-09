"""Seat-level schema for the local model-fit advisory layer.

Each extracted row is one model *seat* from an existing Harness run JSON:
a panel seat, judge seat, specialist seat, probe seat, etc.

Labels are mutually exclusive and severity-ordered:
    unusable > truncated > usable_stop

Features are pre-dispatch only. No outcome data leaks into the input vector.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Label definitions
# ---------------------------------------------------------------------------

LABEL_USABLE_STOP = "usable_stop"
LABEL_TRUNCATED = "truncated"
LABEL_UNUSABLE = "unusable"
LABEL_ORDER = [LABEL_UNUSABLE, LABEL_TRUNCATED, LABEL_USABLE_STOP]

# ---------------------------------------------------------------------------
# Label rule helpers (derived from existing run JSON fields)
# ---------------------------------------------------------------------------

def is_usable_stop(row: Dict[str, Any], structured_required: bool, parseable: bool) -> bool:
    """finish_reason == "stop", content present, and parseable when required."""
    if row.get("finish_reason") != "stop":
        return False
    if not row.get("content_present"):
        return False
    if structured_required and not parseable:
        return False
    return True

def is_truncated(row: Dict[str, Any], truncated_flag: bool = False) -> bool:
    """finish_reason == "length", or an explicit truncated flag on the seat.

    In this repo's run JSON, some seats finish with "stop" but still carry a
    boolean truncated marker. We treat that as truncation here too.
    """
    if row.get("finish_reason") == "length":
        return True
    if truncated_flag:
        return True
    return False

def is_unusable(row: Dict[str, Any]) -> bool:
    """error finish, or unusable status, or missing required content."""
    fr = row.get("finish_reason")
    status = row.get("status")
    if fr == "error":
        return True
    if status in ("error", "byok", "invalid_output"):
        return True
    if not row.get("content_present"):
        return True
    return False

def resolve_label(row: Dict[str, Any], structured_required: bool, parseable: bool, truncated_flag: bool = False) -> str:
    if is_unusable(row):
        return LABEL_UNUSABLE
    if is_truncated(row, truncated_flag):
        return LABEL_TRUNCATED
    if is_usable_stop(row, structured_required, parseable):
        return LABEL_USABLE_STOP
    # Fallback: if it does not cleanly fit, treat as unusable rather than guess.
    return LABEL_UNUSABLE

# ---------------------------------------------------------------------------
# Feature schema
# ---------------------------------------------------------------------------

# Pre-dispatch features only.

TASK_FEATURES = [
    "task_type",
    "seat_role",
    "structured_output_required",
    "max_tokens_requested",
    "reasoning_effort",
    "prompt_chars",
    "source_window_attached",
    "claims_count",
    "convergence_expected",
    "is_iterative",
]

MODEL_FEATURES = [
    "model_id_hash",
    "free_tier",
    "declared_context_length",
    "declared_structured_json",
    "declared_reasoning",
    "observed_usable_rate",
    "observed_truncation_rate",
    "observed_unusable_rate",
    "observed_mean_resp_chars",
    "observed_median_resp_chars",
    "observed_max_resp_chars",
    "observed_sample_count",
]

INTERACTION_FEATURES = [
    "prompt_tokens_est_over_context",
    "max_tokens_over_mean_resp",
    "structured_need_vs_declared_json",
    "iterative_vs_truncation_rate",
]

FEATURE_ORDER = TASK_FEATURES + MODEL_FEATURES + INTERACTION_FEATURES

# Extra extractor-only fields that are not part of the model input vector.
# Kept on SeatRow for debugging/audit but excluded from the feature vector.
EXTRACTOR_ONLY_FIELDS = {"truncated_flag"}

# Categorical vocabularies (fixed at extraction time for stability)
TASK_TYPE_VOCAB = [
    "verify_panel",
    "structured_claims",
    "apply",
    "bench",
    "probe",
    "consent",
]

SEAT_ROLE_VOCAB = [
    "panel",
    "judge",
    "specialist",
    "apply",
    "consent",
]

REASONING_EFFORT_VOCAB = [
    "off",
    "low",
    "medium",
    "high",
    "auto",
]

SOURCE_KIND_VOCAB = [
    "code",
    "prose",
    "mixed",
]

# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def one_hot(value: Any, vocab: List[str]) -> List[int]:
    idx = vocab.index(value) if value in vocab else -1
    out = [0] * len(vocab)
    if idx >= 0:
        out[idx] = 1
    # Unknown category is encoded as all-zero; callers must decide whether that
    # is acceptable for a given feature or whether to drop the row.
    return out

def scaler_stats() -> Dict[str, Dict[str, float]]:
    """Placeholders for numeric feature scaling means/stdevs.

    In practice these are computed from the extracted dataset and persisted
    alongside the model so inference uses identical normalization.
    """
    return {}

# ---------------------------------------------------------------------------
# Row contract
# ---------------------------------------------------------------------------

@dataclass
class SeatRow:
    run: str
    seat_index: int
    features: Dict[str, Any]
    label: str
    model: str
    task_type: str
    seat_role: str
    finish_reason: Optional[str] = None
    status: Optional[str] = None
    content_present: bool = False
    parseable: bool = False
    cost: float = 0.0
    prompt_chars: int = 0
    resp_chars: int = 0
    truncated_flag: bool = False
