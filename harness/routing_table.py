"""Task-to-model routing table and OpenRouter floor price controls.

Provides explicit deterministic mapping from task classification to a curated
shortlist of cost-efficient models, gateway-level max_price caps, and the
canonical :floor provider selector.

EV-2 / #195 / #197:
* Loads ``packs/tiers.snapshot.json`` at import time with a bootstrap fallback
  so tier shortlists and price tables are data-driven rather than hardcoded to
  stale model generations (DF-EV-4).
* Exposes ``resolve_task_class`` for task-aware model class routing
  (``fast``, ``vote``, ``specialist``, ``implement``, ``cheap-capable``,
  ``judge``, ``plan``, ``frontier``).
* Supports reversible per-run ``trial_model`` overrides on ``get_tier_route``
  and ``resolve_task_class`` without mutating persistent config.
"""
from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional, Sequence

# Canonical tier identifiers
TIER_0 = "T0"  # Free models ($0)
TIER_1 = "T1"  # Ultra-cheap (:floor, $0.05-$0.30/M)
TIER_2 = "T2"  # Mid-tier reasoning ($0.50-$3.00/M)
TIER_3 = "T3"  # Frontier specialist ($2.00-$10.00/M)

TIER_ORDER: Sequence[str] = (TIER_0, TIER_1, TIER_2, TIER_3)

PACKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packs")
TIERS_SNAPSHOT_PATH = os.path.join(PACKS_DIR, "tiers.snapshot.json")
DEFAULT_SNAPSHOT_MAX_AGE_DAYS = 14

_BOOTSTRAP_TIER_MODELS: Dict[str, List[str]] = {
    TIER_0: [
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "cohere/north-mini-code:free",
    ],
    TIER_1: [
        "deepseek/deepseek-v4.1-flash",
        "deepseek/deepseek-v4-flash",
        "inclusionai/ling-3.0-flash",
        "openai/gpt-4o-mini",
    ],
    TIER_2: [
        "z-ai/glm-5.3-flash",
        "openai/gpt-5-mini",
        "openai/gpt-5.6-luna",
        "google/gemini-3.8-flash",
        "deepseek/deepseek-v4-pro",
    ],
    TIER_3: [
        "qwen/qwen3.8-max-0902",
        "openai/gpt-4.1",
        "openai/gpt-5.6-sol",
    ],
}

_BOOTSTRAP_TASK_CLASSES: Dict[str, str] = {
    "fast": TIER_1,
    "vote": TIER_1,
    "cheap-capable": TIER_1,
    "implement": TIER_1,
    "specialist": TIER_2,
    "judge": TIER_2,
    "plan": TIER_2,
    "frontier": TIER_3,
}


def load_tier_snapshot(path: Optional[str] = None) -> Dict[str, Any]:
    """Load ``packs/tiers.snapshot.json`` with a safe bootstrap fallback."""
    target = path or TIERS_SNAPSHOT_PATH
    try:
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("snapshot root must be a JSON object")
        tiers_raw = raw.get("tiers") or {}
        tiers = {
            t: list(tiers_raw.get(t) or _BOOTSTRAP_TIER_MODELS[t])
            for t in TIER_ORDER
        }
        task_classes = dict(_BOOTSTRAP_TASK_CLASSES)
        if isinstance(raw.get("task_classes"), dict):
            for k, v in raw["task_classes"].items():
                if v in TIER_ORDER:
                    task_classes[str(k)] = str(v)
        price_table = raw.get("price_table")
        price_table = dict(price_table) if isinstance(price_table, dict) else {}
        return {
            "schema": int(raw.get("schema") or 1),
            "captured_at": str(raw.get("captured_at") or ""),
            "max_age_days": int(raw.get("max_age_days") or DEFAULT_SNAPSHOT_MAX_AGE_DAYS),
            "tiers": tiers,
            "task_classes": task_classes,
            "price_table": price_table,
            "source": "snapshot",
        }
    except (OSError, ValueError, TypeError):
        return {
            "schema": 1,
            "captured_at": "",
            "max_age_days": DEFAULT_SNAPSHOT_MAX_AGE_DAYS,
            "tiers": {t: list(v) for t, v in _BOOTSTRAP_TIER_MODELS.items()},
            "task_classes": dict(_BOOTSTRAP_TASK_CLASSES),
            "price_table": {},
            "source": "bootstrap",
        }


def check_tier_snapshot_freshness(
    path: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Check whether ``packs/tiers.snapshot.json`` is within its freshness window."""
    snap = load_tier_snapshot(path)
    limit_days = float(
        max_age_days if max_age_days is not None
        else (snap.get("max_age_days") or DEFAULT_SNAPSHOT_MAX_AGE_DAYS)
    )
    captured_raw = snap.get("captured_at") or ""
    if not captured_raw:
        return {
            "ok": False,
            "fresh": False,
            "captured_at": "",
            "age_days": None,
            "max_age_days": limit_days,
            "warning": "tiers snapshot has no captured_at timestamp (using bootstrap)",
        }
    try:
        ts_text = captured_raw[:-1] + "+00:00" if captured_raw.endswith("Z") else captured_raw
        captured_dt = datetime.fromisoformat(ts_text)
        if captured_dt.tzinfo is None:
            captured_dt = captured_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return {
            "ok": False,
            "fresh": False,
            "captured_at": captured_raw,
            "age_days": None,
            "max_age_days": limit_days,
            "warning": f"tiers snapshot captured_at {captured_raw!r} is not valid ISO-8601",
        }

    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (ref_now - captured_dt).total_seconds() / 86400.0)
    fresh = age_days <= limit_days
    warning = None
    if not fresh:
        warning = (
            f"tiers.snapshot.json is {age_days:.1f} days old "
            f"(exceeds max_age_days={limit_days:g}); refresh via `harness economics --report`."
        )
    return {
        "ok": fresh,
        "fresh": fresh,
        "captured_at": captured_raw,
        "age_days": round(age_days, 3),
        "max_age_days": limit_days,
        "warning": warning,
    }


def generate_tier_snapshot(
    models_by_tier: Optional[Dict[str, Sequence[str]]] = None,
    *,
    captured_at: Optional[str] = None,
    max_age_days: int = DEFAULT_SNAPSHOT_MAX_AGE_DAYS,
    price_table: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a serializable ``tiers.snapshot.json`` payload (EV-2)."""
    base = load_tier_snapshot()
    tiers_in = models_by_tier or base["tiers"]
    tiers_out = {
        t: list(tiers_in.get(t) or _BOOTSTRAP_TIER_MODELS[t])
        for t in TIER_ORDER
    }
    ts = captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": 1,
        "captured_at": ts,
        "max_age_days": int(max_age_days),
        "tiers": tiers_out,
        "task_classes": dict(base["task_classes"]),
        "price_table": dict(price_table if price_table is not None else base["price_table"]),
    }


_INITIAL_SNAPSHOT = load_tier_snapshot()

# Curated model shortlists per tier (loaded from packs/tiers.snapshot.json)
TIER_MODELS: Dict[str, List[str]] = _INITIAL_SNAPSHOT["tiers"]
TASK_CLASS_TIERS: Dict[str, str] = _INITIAL_SNAPSHOT["task_classes"]
SNAPSHOT_PRICE_TABLE: Dict[str, Dict[str, Any]] = _INITIAL_SNAPSHOT["price_table"]

# Counter tracking models that required substring-heuristic tier classification
_UNLISTED_TIER_FALLBACKS: Dict[str, int] = {}


def unlisted_tier_fallbacks() -> Dict[str, int]:
    """Return a copy of the heuristic-fallback counter map (EV-2)."""
    return dict(_UNLISTED_TIER_FALLBACKS)


def reset_unlisted_tier_fallbacks() -> None:
    """Clear the heuristic-fallback counter map (for hermetic tests)."""
    _UNLISTED_TIER_FALLBACKS.clear()


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


def get_tier_route(
    tier: str,
    enable_floor: bool = True,
    *,
    trial_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Return complete routing metadata for a given tier.

    When ``trial_model`` (#197) is supplied, it is placed at the head of the
    run's candidate list while preserving the rest of the tier's fallback
    ladder and without mutating ``TIER_MODELS``.
    """
    t = tier if tier in TIER_MODELS else TIER_1
    canonical = list(TIER_MODELS[t])
    trial_active = False
    if trial_model and str(trial_model).strip():
        tm_clean = strip_variant_suffix(str(trial_model).strip())
        canonical = [tm_clean] + [m for m in canonical if strip_variant_suffix(m) != tm_clean]
        trial_active = True
    models = [floor_model(m, enable_floor) for m in canonical]
    if trial_active and str(trial_model).strip().endswith(_NON_FLOOR_SUFFIXES):
        models[0] = str(trial_model).strip()
    out = {
        "tier": t,
        "models": models,
        "canonical_models": canonical,
        "max_price": dict(TIER_MAX_PRICE[t]),
        "cost_band": TIER_COST_BANDS[t],
        "escalation_trigger": TIER_ESCALATION_TRIGGERS[t],
    }
    if trial_active:
        out["trial_model"] = str(trial_model).strip()
        out["reversible_trial"] = True
    return out


def resolve_task_class(
    task_class: str,
    *,
    enable_floor: bool = True,
    trial_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a named task class (#195) into its tier route and candidate ladder.

    Supported task classes: ``fast``, ``vote``, ``cheap-capable``, ``implement``,
    ``specialist``, ``judge``, ``plan``, ``frontier`` (plus direct tier names
    ``T0``..``T3``). Unknown classes default safely to ``T1``.
    """
    key = str(task_class or "implement").strip().lower()
    if key.upper() in TIER_MODELS:
        tier = key.upper()
    else:
        tier = TASK_CLASS_TIERS.get(key, TIER_1)
    route = get_tier_route(tier, enable_floor=enable_floor, trial_model=trial_model)
    route["task_class"] = key
    return route


def classify_model_tier(model_id: str, *, profile: Any = None) -> str:
    """Classify an OpenRouter model ID into its cost tier (T0, T1, T2, T3).

    Resolution order (EV-2):
    1. Free variant tag (``:free``) -> ``T0``.
    2. Snapshot ``TIER_MODELS`` membership.
    3. ``SNAPSHOT_PRICE_TABLE`` or supplied ``profile`` per-token pricing.
    4. Substring heuristics (increments ``unlisted_tier_fallbacks`` counter).
    """
    if not model_id:
        return TIER_0
    m = str(model_id).lower()
    if m.endswith(":free") or ":free" in m:
        return TIER_0
    clean = strip_variant_suffix(m)
    for tier in (TIER_0, TIER_1, TIER_2, TIER_3):
        if any(clean == strip_variant_suffix(x.lower()) for x in TIER_MODELS[tier]):
            return tier
    pt_entry = SNAPSHOT_PRICE_TABLE.get(clean)
    if isinstance(pt_entry, dict) and pt_entry.get("tier") in TIER_ORDER:
        return str(pt_entry["tier"])
    if profile is not None:
        if getattr(profile, "free", False):
            return TIER_0
        cp = float(getattr(profile, "completion_price", 0.0) or 0.0)
        if cp > 0.0:
            if cp <= 1.0e-6:
                return TIER_1
            if cp <= 4.5e-6:
                return TIER_2
            return TIER_3
    _UNLISTED_TIER_FALLBACKS[clean] = _UNLISTED_TIER_FALLBACKS.get(clean, 0) + 1
    # Heuristics for models outside the default curated list
    if any(k in clean for k in ("max", "o3", "o1", "gpt-4", "gpt-5", "sol", "r1")):
        return TIER_3
    if any(k in clean for k in ("flash", "reasoner", "mini", "medium")):
        return TIER_2
    if any(k in clean for k in ("coder", "small", "lite", "8b", "7b", "chat")):
        return TIER_1
    return TIER_2



