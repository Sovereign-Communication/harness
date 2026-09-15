"""Rankings-driven pool-candidate refresh (2026-09-13 operator ruling 7).

Rotation candidates should be evidence-driven, not folklore-driven. The
OpenRouter daily rankings API is authoritative and cheap (one GET with the
existing key): ``GET /api/v1/datasets/rankings-daily`` returns per-day rows
of ``{"date", "model_permaslug", "total_tokens"}`` covering the trailing 30
days. This module turns that traffic evidence into a report:

* top models by recent total tokens (climbers -- models whose traffic is
  rising across the window -- are surfaced separately),
* the intersection with the live /models catalog (a candidate that is not
  routable is dropped before it can waste a probe),
* which candidates are already covered by the shipped pools,
* the newly proposed candidates.

A candidate only becomes a *probed* candidate by passing the ONE-vote probe:
a single structured-vote call through the harness's own governed ``chat``
path with reasoning explicitly disabled, judged by parseable JSON. Nothing
here mutates configuration or pools; the report is advisory input for the
operator and the weekly CI job.
"""
from . import events as _events
from .chat import _extract_json, chat, extract_content_and_cost, _reported_cost
from .config import OPENROUTER_RANKINGS_URL, shipped_model_ids
from .errors import HarnessError
from .output import eprint

# How many ranked models to report by default.
RANKINGS_TOP_N = 15
# A climber must grow at least this fraction between the first and last
# fifth of the window to be surfaced as climbing.
CLIMBER_GROWTH_FRACTION = 0.15
# Default probe budget: the vote-lane minimum (structured votes are proven
# sufficient at 4096; a smaller probe would starve reasoning-capable ids).
PROBE_MAX_TOKENS = 4096


def fetch_rankings(transport, api_key):
    """Fetch the daily rankings rows (one GET). Raises on transport failure."""
    resp = transport.get(OPENROUTER_RANKINGS_URL, api_key, timeout=30)
    rows = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(rows, list):
        raise HarnessError(
            f"rankings endpoint returned no data rows (got {type(resp).__name__})")
    clean = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        slug = r.get("model_permaslug")
        try:
            tokens = float(r.get("total_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if not slug:
            continue
        clean.append({"date": str(r.get("date") or ""), "slug": slug,
                      "total_tokens": tokens})
    if not clean:
        raise HarnessError("rankings endpoint returned only malformed rows")
    return clean


def _norm_row(r):
    """Accept a raw API row or a fetch_rankings-normalized row."""
    if "slug" in r:
        return r
    return {"date": str(r.get("date") or ""),
            "slug": r.get("model_permaslug"),
            "total_tokens": r.get("total_tokens")}


def aggregate_rankings(rows, top_n=RANKINGS_TOP_N):
    """Aggregate per-model token totals over the window.

    Accepts raw API rows (``model_permaslug``) or the normalized rows from
    :func:`fetch_rankings` (``slug``). Returns ``{"window": {"start",
    "end", "days"}, "totals": [...]}`` sorted by total descending.
    ``trend`` is "climbing" when the model's recent traffic grows across
    the window, "falling" when it shrinks, else "stable". Sorted
    deterministically (total, then slug) so reports diff cleanly.
    """
    rows = [_norm_row(r) for r in rows]
    by_date = {}
    for r in rows:
        if r.get("date"):
            by_date.setdefault(r["date"], set()).add(r["slug"])
    dates = sorted(by_date)
    by_slug = {}
    for r in rows:
        if not r.get("slug"):
            continue
        e = by_slug.setdefault(r["slug"], {"total": 0.0, "daily": {}})
        e["total"] += r["total_tokens"]
        e["daily"][r["date"]] = e["daily"].get(r["date"], 0.0) + r["total_tokens"]
    window_start, window_end = dates[0], dates[-1]

    def trend(daily):
        if len(dates) < 5:
            return "stable"
        chunk = max(1, len(dates) // 5)
        early = sum(daily.get(d, 0.0) for d in dates[:chunk])
        late = sum(daily.get(d, 0.0) for d in dates[-chunk:])
        if early <= 0:
            return "climbing" if late > 0 else "stable"
        growth = (late - early) / early
        if growth >= CLIMBER_GROWTH_FRACTION:
            return "climbing"
        if growth <= -CLIMBER_GROWTH_FRACTION:
            return "falling"
        return "stable"

    totals = [{"slug": slug, "total_tokens": int(e["total"]),
               "trend": trend(e["daily"])}
              for slug, e in by_slug.items()]
    totals.sort(key=lambda x: (-x["total_tokens"], x["slug"]))
    kept = totals[:max(1, int(top_n))]
    return {"window": {"start": window_start, "end": window_end,
                       "days": len(dates)},
            "totals": kept,
            "climbers": [t["slug"] for t in kept if t["trend"] == "climbing"]}


def _slug_matches_model(slug, model_id):
    """Rankings permaslug vs a live catalog model id.

    The rankings API identifies models by permaslug (e.g. ``deepseek-v4.1-
    flash``), while the catalog ids are ``vendor/model-name``. Match on the
    model-name tail, tokenized so ``deepseek-v4.1-flash`` matches
    ``deepseek/deepseek-v4.1-flash`` without matching an unrelated id that
    merely shares a substring.
    """
    s = str(slug or "").lower()
    m = str(model_id or "").lower().split("/", 1)[-1]
    if s == m:
        return True
    s_parts = [p for p in s.replace("_", "-").split("-") if p]
    m_parts = [p for p in m.replace("_", "-").split("-") if p]
    return s_parts == m_parts


def candidates_from_rankings(aggregates, catalog_ids, shipped_ids=None,
                             top_n=RANKINGS_TOP_N):
    """Intersect ranked slugs with the live catalog.

    Returns ``{"ranked_in_catalog": [...], "proposed": [...]}`` where
    ``proposed`` are catalog-known candidates not already covered by the
    shipped pools (``shipped_ids``).
    """
    shipped = set(shipped_ids or [])
    slug_to_catalog = {}
    for cid in catalog_ids or []:
        for a in aggregates:
            if _slug_matches_model(a["slug"], cid):
                slug_to_catalog.setdefault(a["slug"], cid)
                break
    ranked_in_catalog = []
    seen = set()
    for a in aggregates:
        cid = slug_to_catalog.get(a["slug"])
        if cid and cid not in seen:
            seen.add(cid)
            ranked_in_catalog.append({"slug": a["slug"], "model_id": cid,
                                      "total_tokens": a["total_tokens"],
                                      "trend": a["trend"]})
    proposed = [r for r in ranked_in_catalog if r["model_id"] not in shipped]
    return {"ranked_in_catalog": ranked_in_catalog, "proposed": proposed}


def _probe_vote(transport, api_key, governor, model, max_tokens=PROBE_MAX_TOKENS):
    """The ONE-vote probe: a single structured vote, reasoning disabled.

    Returns ``(ok, detail, cost)``. A candidate passes only with an HTTP 200
    whose body carries parseable JSON -- the exact bar a panel vote must
    clear (a reasoning-only or truncated body is a fail, per assess policy).
    Cost is billable either way and is reported back for the evidence file.
    """
    prompt = (
        "Respond with ONLY a JSON object and nothing else:\n"
        "{\"claim_1\": {\"real\": true, \"confidence\": 0.9}}\n"
        "This is a connectivity and JSON-emission probe. Do not add prose.")
    status, resp = chat(transport, api_key, model,
                        [{"role": "user", "content": prompt}],
                        max_tokens, "off", 0.4, governor)
    cost = _reported_cost(resp)
    if status != 200:
        err = (resp.get("error", {}).get("message", resp)
               if isinstance(resp, dict) else resp)
        return False, f"http_{status}: {str(err)[:200]}", cost
    content, _finish, cost, is_byok = extract_content_and_cost(resp)
    if is_byok and not governor.is_free(model):
        return False, "paid BYOK route; spend invisible to the tracked key", cost
    if not content or not str(content).strip():
        return False, "empty response body", cost
    parsed = _extract_json(content)
    if not isinstance(parsed, dict) or not parsed:
        return False, "no parseable JSON in the response body", cost
    return True, "parseable JSON vote", cost


def build_rankings_report(transport, api_key, governor, *,
                          top_n=RANKINGS_TOP_N, probe_candidates=False,
                          shipped_ids=None):
    """The full advisory report: rankings, catalog intersection, candidates.

    ``shipped_ids`` defaults to the config's shipped pools. Hermetic tests
    patch ``harness.rankings.shipped_model_ids`` (the module-level seam) or
    pass ``shipped_ids``; production resolves the config default.

    With ``probe_candidates`` set, every proposed candidate is gated through
    the one-vote probe (billable, spend-governed) and the result is attached
    per candidate. The report never mutates configuration.
    """
    rows = fetch_rankings(transport, api_key)
    aggregates = aggregate_rankings(rows, top_n=top_n)
    catalog_ids = [m.get("id") for m in governor.fetch_models() if m.get("id")]
    if shipped_ids is None:
        shipped_ids = shipped_model_ids()
    candidates = candidates_from_rankings(
        aggregates["totals"], catalog_ids, shipped_ids=shipped_ids)
    report = {
        "window": aggregates["window"],
        "top": aggregates["totals"],
        "climbers": aggregates["climbers"],
        "ranked_in_catalog": candidates["ranked_in_catalog"],
        "proposed_candidates": candidates["proposed"],
        "probed_candidates": [],
    }
    if probe_candidates and report["proposed_candidates"]:
        for cand in report["proposed_candidates"]:
            model = cand["model_id"]
            eprint(f"[rankings] probing candidate {model} (one-vote gate) ...")
            _events.emit("rankings_probe", model=model, phase="start")
            try:
                ok, detail, cost = _probe_vote(transport, api_key, governor, model)
            except HarnessError as exc:
                ok, detail, cost = False, str(exc), 0.0
            cand["probe"] = {"ok": ok, "detail": detail, "cost": cost}
            report["probed_candidates"].append(cand)
            eprint(f"[rankings] {model}: {'PASS' if ok else 'FAIL'} ({detail}); "
                   f"probe cost ${cost:.6f}")
            _events.emit("rankings_probe", model=model, phase="end",
                         ok=ok, cost=cost)
    return report
