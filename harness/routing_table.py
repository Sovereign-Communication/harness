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
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from .validation import optional_float

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
        "nvidia/nemotron-3-ultra-550b-a55b:free",
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
            f"(exceeds max_age_days={limit_days:g}); refresh via "
            f"`harness economics --refresh-snapshot`."
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


class SnapshotRefreshError(ValueError):
    """A catalog read that must not be written over the committed snapshot.

    Every refusal path leaves the committed file untouched: a stale stamp is
    a visible red gate, while a snapshot refreshed from a catalog that has
    gaps (or from no catalog at all) would silently retire routing data the
    fleet still depends on.
    """


def _catalog_prices(catalog: Sequence[Any]) -> Dict[str, Dict[str, float]]:
    """Map model id -> published per-token prices from a ``/models`` read.

    The rows are free-form JSON, so the annotation is deliberately wide: a
    string, a list, or an object with no usable ``pricing`` is skipped here
    rather than refused (a refusal would mean one junk row could block the
    refresh). What the refresh does refuse is a *tier* id it cannot price.
    """
    prices: Dict[str, Dict[str, float]] = {}
    for entry in catalog or ():
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        pricing = entry.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        prompt = optional_float(pricing.get("prompt"))
        completion = optional_float(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        prices[model_id] = {"prompt_per_token": prompt,
                           "completion_per_token": completion}
    return prices


def refresh_tier_snapshot(
    catalog: Sequence[Any],
    *,
    path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Rewrite ``packs/tiers.snapshot.json`` from a live catalog read (EV-5).

    The gate's advertised cure existed only in prose: nothing in the repo could
    refresh this artifact, so once it aged out (2026-11-01) every head's
    freshness check and the scheduled rankings job could only ever be red
    (#446, DF-EV-5 drift). This is the missing writer.

    Contract, in the order the checks run -- each refusal raises
    :class:`SnapshotRefreshError` and writes nothing:

    * the committed artifact must be readable, so a corrupt file is repaired by
      review rather than silently replaced with the bootstrap literal;
    * the catalog read must be non-empty;
    * **every** id named by a tier must still be offered by the catalog -- a
      catalog gap is a refusal, never a silent drop, matching
      ``capabilities --check-shipped``;
    * every id already priced in ``price_table`` must still be catalogued and
      still publish both per-token rates, and no published rate may be
      negative.

    What it rewrites: ``captured_at`` (now), ``price_table`` values to the
    catalog's published rates, and each price entry's ``tier`` to the tier that
    actually lists it. Tier membership, ``task_classes``, ``schema`` and
    ``max_age_days`` are carried over unchanged, so the diff is a single
    reviewable JSON file and no routing decision moves. Free (``:free``) ids are
    deliberately not added to ``price_table``: they are zero by definition and
    adding keys would change the savings math in ``ledger_analytics`` -- that is
    a rotation decision, not a currency refresh.
    """
    target = path or TIERS_SNAPSHOT_PATH
    try:
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SnapshotRefreshError(
            f"committed snapshot {target} is unreadable ({exc}); refusing to "
            "replace it with a guess -- fix or restore it first") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tiers"), dict):
        raise SnapshotRefreshError(
            f"committed snapshot {target} has no tiers object; refusing to "
            "write over it")

    prices = _catalog_prices(catalog)
    if not prices:
        raise SnapshotRefreshError(
            "the catalog read returned no usable prices; refusing to stamp a "
            "currency claim on data that was never checked")

    tiers_raw = raw["tiers"]
    tiers_out: Dict[str, List[str]] = {}
    listed_in: Dict[str, str] = {}
    missing: List[str] = []
    for tier in TIER_ORDER:
        ids = list(tiers_raw.get(tier) or _BOOTSTRAP_TIER_MODELS[tier])
        tiers_out[tier] = ids
        for model_id in ids:
            listed_in.setdefault(model_id, tier)
            if model_id not in prices:
                missing.append(model_id)
    if missing:
        raise SnapshotRefreshError(
            "these tier ids are absent from the live catalog (no silent drop): "
            + ", ".join(sorted(missing)))

    current_pt = raw.get("price_table")
    current_pt = current_pt if isinstance(current_pt, dict) else {}
    price_table: Dict[str, Dict[str, Any]] = {}
    unpriced: List[str] = []
    negative: List[str] = []
    for model_id in sorted(current_pt):
        rates = prices.get(model_id)
        if rates is None:
            unpriced.append(model_id)
            continue
        if rates["prompt_per_token"] < 0 or rates["completion_per_token"] < 0:
            negative.append(model_id)
            continue
        price_table[model_id] = {
            "prompt_per_token": rates["prompt_per_token"],
            "completion_per_token": rates["completion_per_token"],
            "tier": listed_in.get(model_id)
                    or str((current_pt.get(model_id) or {}).get("tier") or TIER_1),
        }
    if unpriced:
        raise SnapshotRefreshError(
            "these priced ids are absent from the live catalog or publish no "
            "per-token rate: " + ", ".join(sorted(unpriced)))
    if negative:
        raise SnapshotRefreshError(
            "the catalog publishes a negative rate for: " + ", ".join(sorted(negative)))

    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    captured_at = ref_now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema": int(raw.get("schema") or 1),
        "captured_at": captured_at,
        "max_age_days": int(raw.get("max_age_days") or DEFAULT_SNAPSHOT_MAX_AGE_DAYS),
        "tiers": tiers_out,
        "task_classes": dict(raw.get("task_classes") or _BOOTSTRAP_TASK_CLASSES),
        "price_table": price_table,
    }

    previous = str(raw.get("captured_at") or "")
    changed = sorted(
        key for key in payload
        if payload[key] != raw.get(key)
    )

    # Atomic replace: a reader (or an import of this module) never sees a
    # half-written snapshot, and a refused or interrupted run leaves the
    # committed artifact exactly as it was.
    directory = os.path.dirname(os.path.abspath(target)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tiers.snapshot.", suffix=".tmp",
                                    dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "path": target,
        "captured_at": captured_at,
        "previous_captured_at": previous,
        "max_age_days": payload["max_age_days"],
        "catalog_size": len(prices),
        "tier_ids_validated": len(listed_in),
        "price_table_entries": len(price_table),
        "changed_fields": changed,
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


# ---- Provider backends (#447 / EV-7) -------------------------------------
# The table names MODELS; this registry names the PROVIDERS those models are
# dispatched to, so "which backend" is a table concept instead of a branch
# inside chat(). The three backends are the ones ``fireworks.resolve_route``
# already implements, and that function stays the single owner of the actual
# provider decision (offer resolution and fail-closed behaviour included).
OPENROUTER_BACKEND = "openrouter"


def backend_for_model(model_id: str) -> str:
    """The provider a model id belongs to, by the id's own shape.

    ``muse-`` ids are Meta's, confirmed Fireworks paths are Fireworks', and
    everything else is OpenRouter -- the same precedence ``resolve_route``
    applies, exposed as data so the table can report its coverage.
    """
    from .config import (FIREWORKS_PROVIDER, META_PROVIDER,
                         is_fireworks_model_path, is_meta_model_id)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("a model id is required to resolve a backend")
    if is_meta_model_id(model_id):
        return META_PROVIDER
    if is_fireworks_model_path(model_id):
        return FIREWORKS_PROVIDER
    return OPENROUTER_BACKEND


def backend_registry() -> Dict[str, str]:
    """Every registered backend and the key surface that serves it.

    One list, so adding a provider is a registry entry rather than another
    special case. The key text is a pointer for diagnostics, never a value.
    """
    from .config import FIREWORKS_PROVIDER, META_PROVIDER
    return {
        META_PROVIDER: "META_API_KEY/MUSE_API_KEY (~/.config/harness/meta.env)",
        FIREWORKS_PROVIDER: "FIREWORKS_API_KEY (~/.config/harness/fireworks.env)",
        OPENROUTER_BACKEND: "OPENROUTER_API_KEY",
    }


def route_for_model(model_id: str, *, settings: Any = None,
                    openrouter_enabled: Optional[bool] = None,
                    fireworks_enabled: Optional[bool] = None,
                    meta_enabled: Optional[bool] = None):
    """Resolve one model id to its ``Route`` through the backend registry.

    Flag precedence: an explicit argument wins; else the settings surface when
    one is supplied; else the backend the id itself names. A Muse id is only
    ever placed in a pool under the Muse preset, so its own identity is the
    honest default -- the fail-closed gates stay where they belong, at the
    uncommitted-offer lookup and the missing-key check in dispatch.
    """
    from .config import is_meta_model_id
    from .fireworks import resolve_route
    if openrouter_enabled is None:
        openrouter_enabled = bool(
            getattr(settings, "openrouter_enabled", True))
    if fireworks_enabled is None:
        fireworks_enabled = bool(
            getattr(settings, "fireworks_enabled", False))
    if meta_enabled is None:
        if settings is not None:
            meta_enabled = bool(getattr(settings, "meta_enabled", False))
        else:
            meta_enabled = is_meta_model_id(model_id)
    return resolve_route(model_id, openrouter_enabled=bool(openrouter_enabled),
                         fireworks_enabled=bool(fireworks_enabled),
                         meta_enabled=bool(meta_enabled))


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



