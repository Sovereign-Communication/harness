"""Task-to-model routing table and OpenRouter floor price controls.

Provides explicit deterministic mapping from task classification to a curated
shortlist of cost-efficient models, gateway-level max_price caps, and the
canonical :floor provider selector.
"""
from typing import Any, Dict, List, Optional, Sequence

# Canonical tier identifiers
TIER_0 = "T0"  # Free models ($0)
TIER_1 = "T1"  # Ultra-cheap (:floor, $0.05-$0.30/M)
TIER_2 = "T2"  # Mid-tier reasoning ($0.50-$3.00/M)
TIER_3 = "T3"  # Frontier specialist ($2.00-$10.00/M)

TIER_ORDER: Sequence[str] = (TIER_0, TIER_1, TIER_2, TIER_3)

# Curated model shortlists per tier
TIER_MODELS: Dict[str, List[str]] = {
    TIER_0: [
        "deepseek/deepseek-chat:free",
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.3-70b-instruct:free",
    ],
    TIER_1: [
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-coder-32b-instruct",
    ],
    TIER_2: [
        "google/gemini-2.5-flash",
        "deepseek/deepseek-reasoner",
    ],
    TIER_3: [
        "qwen/qwen3.8-max-0902",
        "anthropic/claude-3.7-sonnet",
        "openai/o3-mini",
    ],
}

# Gateway-level price ceilings (USD per 1M tokens) for OpenRouter provider.max_price
TIER_MAX_PRICE: Dict[str, Dict[str, float]] = {
    TIER_0: {"prompt": 0.0, "completion": 0.0},
    TIER_1: {"prompt": 0.35, "completion": 1.50},
    TIER_2: {"prompt": 1.50, "completion": 5.00},
    TIER_3: {"prompt": 5.00, "completion": 20.00},
}

# Cost band labels for telemetry and reporting
TIER_COST_BANDS: Dict[str, str] = {
    TIER_0: "$0",
    TIER_1: "$0.05-$0.30/M",
    TIER_2: "$0.50-$3.00/M",
    TIER_3: "$2.00-$10.00/M",
}

# Escalation triggers documenting when to transition up the ladder
TIER_ESCALATION_TRIGGERS: Dict[str, str] = {
    TIER_0: "Output fails syntax or structural verification",
    TIER_1: "Verification confidence < threshold (calibrated abstention)",
    TIER_2: "Multi-step planning or complex debugging failure",
    TIER_3: "Terminal tier (manual operator escalation only)",
}

# Suffixes that already specify a variant endpoint and should not take :floor
_NON_FLOOR_SUFFIXES = (":free", ":floor", ":nitro", ":extended", ":exact")

# Complexity classification keywords
_T0_KEYWORDS = (
    "comment", "docstring", "lint", "format", "typo", "rename",
    "whitespace", "style", "signature", "explain", "summarize",
)

_T3_KEYWORDS = (
    "architect", "concurrency", "deadlock", "race condition", "mutex",
    "lock", "thread", "parallel", "cryptograph", "security", "exploit",
    "vulnerability", "consensus", "protocol", "invariant", "migration",
    "distributed", "zero-copy", "memory leak", "tamper", "hash chain",
)

_T2_KEYWORDS = (
    "plan", "dag", "multi-file", "refactor", "debug", "investigate",
    "failing test", "traceback", "exception", "state machine", "rollback",
)


def floor_model(model_id: str, enable_floor: bool = True) -> str:
    """Format an OpenRouter model identifier with :floor unless already variant-tagged.

    If enable_floor is False or model_id is empty or already ends with a known
    variant tag (:free, :floor, :nitro, etc.), returns model_id as-is.
    """
    if not enable_floor or not model_id:
        return model_id
    if any(model_id.endswith(s) for s in _NON_FLOOR_SUFFIXES):
        return model_id
    return f"{model_id}:floor"


def strip_variant_suffix(model_id: str) -> str:
    """Strip variant tags like :floor or :free for canonical pricing or BYOK lookups."""
    if not model_id:
        return model_id
    for s in _NON_FLOOR_SUFFIXES:
        if model_id.endswith(s):
            return model_id[:-len(s)]
    return model_id


def classify_task_tier(prompt: str, target_files: Optional[Sequence[str]] = None) -> str:
    """Classify a task or prompt into an explicit tier (T0, T1, T2, T3)."""
    p_lower = prompt.lower()
    files = list(target_files or [])

    # Multi-file edits immediately qualify for at least Tier 2
    if len(files) > 2:
        if any(kw in p_lower for kw in _T3_KEYWORDS):
            return TIER_3
        return TIER_2

    # Check high-complexity markers
    if any(kw in p_lower for kw in _T3_KEYWORDS):
        return TIER_3

    # Check mid-complexity markers
    if any(kw in p_lower for kw in _T2_KEYWORDS) or len(files) >= 2:
        return TIER_2

    # Check ultra-simple T0 markers
    if any(kw in p_lower for kw in _T0_KEYWORDS) and len(files) <= 1:
        return TIER_0

    # Default workhorse tier is T1 (ultra-cheap draft)
    return TIER_1


def next_tier(current_tier: str) -> Optional[str]:
    """Return the next escalated tier, or None if already at T3."""
    try:
        idx = TIER_ORDER.index(current_tier)
        if idx + 1 < len(TIER_ORDER):
            return TIER_ORDER[idx + 1]
    except ValueError:
        pass
    return None


def get_tier_route(tier: str, enable_floor: bool = True) -> Dict[str, Any]:
    """Return complete routing metadata for a given tier."""
    t = tier if tier in TIER_MODELS else TIER_1
    models = [floor_model(m, enable_floor) for m in TIER_MODELS[t]]
    return {
        "tier": t,
        "models": models,
        "canonical_models": list(TIER_MODELS[t]),
        "max_price": dict(TIER_MAX_PRICE[t]),
        "cost_band": TIER_COST_BANDS[t],
        "escalation_trigger": TIER_ESCALATION_TRIGGERS[t],
    }


def classify_model_tier(model_id: str) -> str:
    """Classify an OpenRouter model ID into its cost tier (T0, T1, T2, T3)."""
    if not model_id:
        return TIER_0
    m = str(model_id).lower()
    if m.endswith(":free") or ":free" in m:
        return TIER_0
    clean = strip_variant_suffix(m)
    for tier in (TIER_1, TIER_2, TIER_3):
        if any(clean == strip_variant_suffix(x.lower()) for x in TIER_MODELS[tier]):
            return tier
    # Heuristics for models outside the default curated list
    if any(k in clean for k in ("max", "sonnet", "opus", "o3", "o1", "gpt-4", "gpt-5", "sol", "r1")):
        return TIER_3
    if any(k in clean for k in ("flash", "reasoner", "mini", "medium")):
        return TIER_2
    if any(k in clean for k in ("coder", "small", "lite", "8b", "7b", "chat")):
        return TIER_1
    return TIER_2


