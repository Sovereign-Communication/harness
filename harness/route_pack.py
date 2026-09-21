"""Route marketplace pack: request → model-rung choice over declared evidence (SITE-2).

Extends the JEV-P3 route vocabulary for the Proof Bench track: where
``evaluate_route`` picks an execution lane (free-distill / diff / frontier),
``evaluate_model_route`` picks the cheapest capable *rung* for a user query
by comparing the request against the declared model ladder plus each rung's
observed evidence (cost class, capability, observed success, tier guidance).

0-hallucination contract (same class as ``evaluate_issue_sort``):
- The choice criteria are the DECLARED rung ids of the operator's ladder —
  the Jev evaluator can only answer with a rung that exists in the pack.
- Unkeyed / transport failure / out-of-ladder choice → the code-owned tier
  heuristic answers with ``is_fallback=True`` and the same honest vocabulary.
- No provider brand ids are invented here: the ladder comes from the caller
  (config / router pools), never from this module.

This module owns pack validation + normalization; ``jev_policy`` owns the
decision call and the ONE ledger ``jev_eval`` per request.
"""
from typing import Any, Dict, List, Optional, Tuple

ROUTE_QUERY_SITE = "model_route"

_TIER_ORDER = ("T0", "T1", "T2", "T3")

# The "belongs here" tags each rung may carry. They are the site's tier-guide
# vocabulary: T0 = mechanical edits; T1 = structured logic; T2 = hard local
# reasoning (concurrency/invariants/protocols); T3 = frontier-class planning
# (architecture, cross-module protocols, novel proofs) — and even there the
# frontier seat plans a tightly scoped implementation over pre-distilled
# research; it does not do what a cheaper rung can do.
_TIER_TAGS = {
    "T0": {"typo", "rename", "format", "docstring", "style"},
    "T1": {"implement", "fix", "test", "refactor", "parse", "validate"},
    "T2": {"concurrency", "race", "deadlock", "mutex", "thread",
           "invariant", "protocol", "performance", "migration"},
    "T3": {"architecture", "cross-module-protocol", "security-review",
           "novel-proof"},
}


def normalize_tier(value: Any) -> Optional[str]:
    """Map loose tier spellings onto the canonical T0..T3 vocabulary."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in _TIER_ORDER:
        return text
    if text in {"0", "TIER0", "TIER_0", "SCOUT"}:
        return "T0"
    if text in {"1", "TIER1", "TIER_1", "DISTILLER"}:
        return "T1"
    if text in {"2", "TIER2", "TIER_2", "SPECIALIST"}:
        return "T2"
    if text in {"3", "TIER3", "TIER_3", "FRONTIER"}:
        return "T3"
    return None


def tier_rank(tier: Any) -> int:
    """Ordering rank of a tier string; unknown tiers rank above T3 (safe)."""
    normalized = normalize_tier(tier)
    if normalized is None:
        return len(_TIER_ORDER)
    return _TIER_ORDER.index(normalized)


def cost_class_from_prices(input_per_mtok: Any, output_per_mtok: Any) -> str:
    """One owner of the price → cost-class bucket the site displays.

    Classes are coarse on purpose (the site compares rungs, not cents):
    free / cheap / moderate / expensive / premium.
    """
    def _num(v):
        try:
            amount = float(v)
        except (TypeError, ValueError):
            return 0.0
        return amount if amount == amount and amount >= 0 else 0.0

    inp, out = _num(input_per_mtok), _num(output_per_mtok)
    blended = inp + out / 2.0
    if blended <= 0.0:
        return "free"
    if blended < 0.5:
        return "cheap"
    if blended < 3.0:
        return "moderate"
    if blended < 10.0:
        return "expensive"
    return "premium"


def validate_route_pack(pack: Any) -> Dict[str, Any]:
    """Validate the operator route pack; raise ValueError on any defect.

    Shape (declared by the caller, never invented here)::

        {
          "id": "route-pack-v1",
          "rungs": [
            {
              "rung_id": "r0",                    # the choice vocabulary
              "tier": "T0"|"T1"|"T2"|"T3",
              "model": "<declared model id>",     # from config/ladder — no hardcoding
              "cost_class": "free|cheap|moderate|expensive|premium",
              "observed_success": 0.93,           # optional evidence (0..1)
              "samples": 12,                      # optional evidence count
              "guidance": ["typo", "rename"],     # optional tier tags
              "notes": "human guidance line",     # optional
            }, ...
          ]
        }
    """
    if not isinstance(pack, dict):
        raise ValueError("route pack must be an object")
    pack_id = pack.get("id")
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise ValueError("route pack requires a non-empty string id")
    rungs = pack.get("rungs")
    if not isinstance(rungs, list) or not rungs:
        raise ValueError("route pack requires a non-empty rungs list")
    seen: set = set()
    for rung in rungs:
        if not isinstance(rung, dict):
            raise ValueError("each route rung must be an object")
        rung_id = rung.get("rung_id")
        if not isinstance(rung_id, str) or not rung_id.strip():
            raise ValueError("each rung requires a non-empty string rung_id")
        if rung_id in seen:
            raise ValueError(f"duplicate rung_id: {rung_id!r}")
        seen.add(rung_id)
        if normalize_tier(rung.get("tier")) is None:
            raise ValueError(f"rung {rung_id!r} tier must be T0..T3")
        for field in ("model", "cost_class"):
            value = rung.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"rung {rung_id!r} requires non-empty {field}")
        observed = rung.get("observed_success")
        if observed is not None:
            if not isinstance(observed, (int, float)) or not 0.0 <= float(observed) <= 1.0:
                raise ValueError(
                    f"rung {rung_id!r} observed_success must be within 0..1")
        samples = rung.get("samples")
        if samples is not None and (not isinstance(samples, int) or samples < 0):
            raise ValueError(f"rung {rung_id!r} samples must be a non-negative int")
        guidance = rung.get("guidance")
        if guidance is not None:
            if not isinstance(guidance, list) or \
                    not all(isinstance(g, str) and g.strip() for g in guidance):
                raise ValueError(
                    f"rung {rung_id!r} guidance must be a list of tag strings")
        notes = rung.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValueError(f"rung {rung_id!r} notes must be a string")
    return pack


def route_question_pack(pack: Any) -> Dict[str, Dict[str, Any]]:
    """TypeSafe choice pack: criteria = declared rung ids, values = labels.

    The label carries tier + cost class + observed evidence so the choice is
    grounded in the same facts the site displays — not vibes.
    """
    pack_doc = validate_route_pack(pack)
    criteria: Dict[str, str] = {}
    for rung in pack_doc["rungs"]:
        observed = rung.get("observed_success")
        evidence = (f", observed success {float(observed):.2f} over "
                    f"{int(rung.get('samples') or 0)} runs") if observed is not None else ""
        criteria[rung["rung_id"]] = (
            f"tier {rung['tier']} ({rung['cost_class']} cost{evidence})")
    return {
        "rung": {
            "type": "choice",
            "instructions": (
                "Choose the cheapest capable rung from the declared criteria "
                "that can complete this request. Prefer the lowest tier that "
                "plausibly suffices; escalate only when the request genuinely "
                "needs a higher tier's capability. Select only from the "
                "declared criteria keys; do not invent rungs."),
            "criteria": criteria,
        },
    }


def tier_floor_for_goal(goal: Any) -> str:
    """Code-owned heuristic floor for a goal (the fallback vocabulary).

    Deterministic keyword floor over the shared tier-guide tags. Returns the
    suggested tier; the caller maps it onto the declared ladder's first rung
    at or above the floor. This is counting, not judgment — the Jev seat (or,
    unkeyed, this heuristic) only ever picks a DECLARED rung id.
    """
    text = (goal or "").lower() if isinstance(goal, str) else ""
    if not text:
        return "T0"
    # Scan high tiers first, then mechanical (T0) before generic-verb (T1):
    # "fix a typo" names the mechanical noun class, so it must stay T0 even
    # though "fix" is a T1 verb. Only an unclaimed goal defaults to T1's
    # implement/fix floor when nothing more specific matched.
    for tier in ("T3", "T2", "T0"):
        if any(tag in text for tag in _TIER_TAGS[tier]):
            return tier
    if any(tag in text for tag in _TIER_TAGS["T1"]):
        return "T1"
    return "T0"


def choose_rung_for_tier(pack: Any, tier: Any) -> Optional[str]:
    """First declared rung (cheapest-first order) at or above ``tier``."""
    pack_doc = validate_route_pack(pack)
    floor = tier_rank(tier)
    for rung in pack_doc["rungs"]:
        if tier_rank(rung.get("tier")) >= floor:
            return rung["rung_id"]
    return None


def fallback_route(goal: Any, pack: Any) -> Tuple[Optional[str], str, List[str]]:
    """Deterministic unkeyed route: (rung_id, tier, reasons).

    Returns ``rung_id=None`` when the declared ladder cannot satisfy the
    heuristic floor (the honest answer, never a guess).
    """
    pack_doc = validate_route_pack(pack)
    tier = tier_floor_for_goal(goal)
    rung_id = choose_rung_for_tier(pack_doc, tier)
    reasons = [f"heuristic tier floor {tier} (keyword match on goal)"]
    if rung_id is None:
        reasons.append("no declared rung at or above the floor; rung=null")
    return rung_id, tier, reasons


def route_combo(rung_id, pack_doc, *, tier, reasons, confidence=0.0,
                is_fallback, structural=None) -> Dict[str, Any]:
    """Bind the route decision to pack fields only — never invent a rung."""
    pack_doc = pack_doc or {}
    rungs = {r["rung_id"]: r for r in (pack_doc.get("rungs") or [])}
    entry = rungs.get(rung_id) if isinstance(rung_id, str) else None
    if entry is None:
        rung_id = None
        entry = {}
    return {
        "rung_id": rung_id,
        "tier": entry.get("tier") or (tier if rung_id is None else tier),
        "model": entry.get("model"),
        "cost_class": entry.get("cost_class"),
        "guidance": list(entry.get("guidance") or []),
        "notes": entry.get("notes"),
        "confidence": float(confidence or 0.0),
        "reasons": list(reasons or []),
        "is_fallback": bool(is_fallback),
        "pack_id": pack_doc.get("id"),
        "structural": structural,
    }
