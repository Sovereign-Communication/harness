"""site_aggregate: the Proof Bench metrics (SITE-3).

ONE owner of every number the public site shows. Pure functions over
``site-bundle-v1`` runs — no ledger access, no network, no clock (the only
timestamps read are the runs' own). Hermetic tests pin each metric's math.

Honesty rules encoded here:
- Headline metrics ($/task, pass rate) count **gated runs only**. Ungated
  runs are self-reports; they appear in volume counts, never in proof rows.
- Failures are data: aborts, gate-waste, and fail outcomes ride alongside
  the wins in every rate the site displays.
- The always-frontier savings and Jev-leverage numbers are **modeled** (the
  ledger does not carry per-run token counts); each carries its ``basis``
  string and degrades to ``None`` with a reason when pricing is absent —
  never a fabricated ratio.
- Run depth (``deepest_tier_reached`` vs ``entry_tier``) is the hourglass
  story: base layers must carry the volume; frontier rungs must be rare
  AND warranted. Warrant-less frontier runs are flagged, not hidden.
- Across bundles, no session may exceed ``INFLUENCE_CAP`` of a metric's
  sample weight; excess sessions are down-weighted and the cap is disclosed.
"""
from statistics import median

from .route_pack import tier_rank

SNAPSHOT_SCHEMA = "site-snapshot-v1"

# A single session may never contribute more than this share of a metric's
# sample weight in a multi-bundle aggregate. Disclosed on the methodology page.
INFLUENCE_CAP = 0.10


def _tier_of(value):
    return value if isinstance(value, str) and value.startswith("T") else None


def _is_gated_pass(run):
    return bool(run.get("gated")) and run.get("outcome") == "pass"


def _cost(run):
    try:
        return float(run.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _runs_by(runs, key_fn):
    grouped = {}
    for run in runs:
        key = key_fn(run)
        if key is None:
            continue
        grouped.setdefault(key, []).append(run)
    return grouped


def cost_per_gated_task(runs):
    """Median gated-pass cost per completion tier (the headline $/task)."""
    out = {}
    for tier, tier_runs in _runs_by(
            [r for r in runs if _is_gated_pass(r)],
            lambda r: _tier_of(r.get("tier"))).items():
        costs = sorted(_cost(r) for r in tier_runs)
        out[tier] = {
            "median_cost": round(median(costs), 9),
            "samples": len(costs),
        }
    return out


def gated_pass_rate(runs):
    """Gated passes / gated attempts per tier (and overall)."""
    out = {}
    gated = [r for r in runs if r.get("gated")]
    for tier, tier_runs in _runs_by(gated, lambda r: _tier_of(r.get("tier"))).items():
        passes = sum(1 for r in tier_runs if r.get("outcome") == "pass")
        out[tier] = {
            "pass_rate": round(passes / len(tier_runs), 4) if tier_runs else None,
            "samples": len(tier_runs),
        }
    out["overall"] = {
        "pass_rate": (round(sum(1 for r in gated if r.get("outcome") == "pass")
                            / len(gated), 4) if gated else None),
        "samples": len(gated),
    }
    return out


def escalation_escape_rate(runs):
    """Per entry tier: share of runs that had to climb above where they
    entered. Low at T0/T1 = the base is enough; every entry-tier cohort
    shows its n so thin cohorts can't masquerade as proof."""
    out = {}
    for entry, entry_runs in _runs_by(
            runs, lambda r: _tier_of(r.get("entry_tier"))).items():
        escapes = sum(1 for r in entry_runs
                      if _tier_of(r.get("deepest_tier_reached")) is not None
                      and tier_rank(r.get("deepest_tier_reached"))
                      > tier_rank(r.get("entry_tier")))
        out[entry] = {
            "escape_rate": round(escapes / len(entry_runs), 4),
            "samples": len(entry_runs),
        }
    return out


def run_depth_distribution(runs):
    """Histogram of deepest_tier_reached — the hourglass shape itself."""
    total = len(runs)
    hist = {}
    for run in runs:
        tier = _tier_of(run.get("deepest_tier_reached")) or "unknown"
        hist[tier] = hist.get(tier, 0) + 1
    return {
        tier: {"runs": count,
               "share": round(count / total, 4) if total else None}
        for tier, count in sorted(hist.items())
    }


def frontier_warrant_rate(runs):
    """Of frontier-reaching runs: share carrying a real warrant (cheaper
    rungs verifiably exhausted first). Warrant-less runs are counted and
    flagged — shown, never hidden."""
    frontier = [r for r in runs
                if _tier_of(r.get("deepest_tier_reached")) == "T3"]
    if not frontier:
        return {"frontier_runs": 0, "warranted": 0, "warrant_rate": None,
                "unwarranted_flagged": 0}
    warranted = 0
    for run in frontier:
        esc = run.get("escalation") or {}
        warrant = esc.get("warrant") or {}
        if (esc and int(warrant.get("lower_rung_verify_failures") or 0) >= 1):
            warranted += 1
    return {
        "frontier_runs": len(frontier),
        "warranted": warranted,
        "warrant_rate": round(warranted / len(frontier), 4),
        "unwarranted_flagged": len(frontier) - warranted,
    }


def _price_blend(pricing, model):
    row = (pricing or {}).get(model)
    if not isinstance(row, dict):
        return None
    try:
        inp = float(row.get("input_per_mtok") or 0.0)
        out = float(row.get("output_per_mtok") or 0.0)
    except (TypeError, ValueError):
        return None
    return inp + out / 2.0


def _frontier_blend(pricing):
    """The priciest priced model in the snapshot stands in for 'always
    frontier' — the baseline the hourglass is measured against."""
    blends = [(model, _price_blend(pricing, model))
              for model in (pricing or {})]
    priced = [(m, b) for m, b in blends if b is not None and b > 0]
    if not priced:
        return None
    return max(priced, key=lambda pair: pair[1])


def hourglass_savings(runs, pricing):
    """Actual spend vs the modeled always-frontier spend for the same runs.

    Modeled, and labeled as such: per-run cost is scaled by the ratio of the
    frontier rung's price blend to the run's own model blend (per-token
    prices from ``pricing_snapshot``; the ledger does not carry token
    counts). Returns ``modeled=False`` reason strings when pricing is absent.
    """
    actual = round(sum(_cost(r) for r in runs)
                   + sum(float((r.get("jev_evals") or {}).get("cost") or 0.0)
                         for r in runs), 9)
    frontier = _frontier_blend(pricing)
    if frontier is None:
        return {"actual_cost": actual, "modeled_frontier_cost": None,
                "savings": None, "modeled": True,
                "basis": "unavailable: no priced model in pricing_snapshot"}
    frontier_model, frontier_price = frontier
    modeled = 0.0
    priced_runs = 0
    for run in runs:
        blend = _price_blend(pricing, run.get("primary_model"))
        if blend is None or blend <= 0:
            continue
        modeled += _cost(run) * (frontier_price / blend)
        priced_runs += 1
    if priced_runs == 0:
        return {"actual_cost": actual, "modeled_frontier_cost": None,
                "savings": None, "modeled": True,
                "basis": "unavailable: no runs with priced primary models"}
    modeled = round(modeled, 9)
    return {
        "actual_cost": actual,
        "modeled_frontier_cost": modeled,
        "savings": round(modeled - actual, 9),
        "savings_multiple": round(modeled / actual, 4) if actual > 0 else None,
        "priced_runs": priced_runs,
        "frontier_model": frontier_model,
        "modeled": True,
        "basis": ("per-run cost scaled by frontier/model price blend from "
                  "pricing_snapshot; token counts are not ledgered"),
    }


def jev_leverage(runs, pricing):
    """Jev decision spend vs the modeled floor of the same decisions on a
    generative seat (input tokens priced at the cheapest priced model).
    ``None`` (with reason) when no priced model exists — never a made-up
    200x."""
    jev_cost = round(sum(float((r.get("jev_evals") or {}).get("cost") or 0.0)
                         for r in runs), 9)
    jev_tokens = sum(int((r.get("jev_evals") or {}).get("count") or 0)
                     for r in runs)
    blends = [(m, _price_blend(pricing, m)) for m in (pricing or {})]
    priced = [(m, b) for m, b in blends if b is not None and b > 0]
    if not priced or jev_tokens == 0:
        return {"jev_cost": jev_cost, "jev_evals": jev_tokens,
                "modeled_generative_floor": None, "ratio": None,
                "basis": "unavailable: no priced model or no jev evals"}
    # The cheapest priced model is the *floor* of what a generative seat
    # would have cost — the conservative end of the leverage claim.
    floor_model, floor_blend = min(priced, key=lambda pair: pair[1])
    # Jev input tokens are not ledgered per eval either; model one eval at
    # the pack bound (~1k input tokens) as the floor unit.
    JEVAL_INPUT_TOKEN_FLOOR = 1000
    modeled = round(jev_tokens * JEVAL_INPUT_TOKEN_FLOOR
                    * floor_blend / 1e6, 9)
    return {
        "jev_cost": jev_cost,
        "jev_evals": jev_tokens,
        "modeled_generative_floor": modeled,
        "ratio": round(modeled / jev_cost, 2) if jev_cost > 0 else None,
        "floor_model": floor_model,
        "basis": (f"one eval ≈ {JEVAL_INPUT_TOKEN_FLOOR} input tokens at the "
                  "cheapest priced model's input rate — a floor, not a peak"),
    }


def cohort_splits(runs):
    """Every metric is also bucketed per month + harness version, so model
    generations are compared against their own era, not blended forever."""
    cohorts = {}
    for run in runs:
        ts = str(run.get("ts") or "")
        month = ts[:7] if len(ts) >= 7 and ts[4] == "-" else None
        version = str(run.get("harness_version") or "unknown")
        key = f"{month or 'unknown-month'}|{version}"
        cohorts.setdefault(key, []).append(run)
    return {
        key: {
            "runs": len(bucket),
            "gated_pass_rate": gated_pass_rate(bucket)["overall"]["pass_rate"],
            "median_gated_cost": (cost_per_gated_task(bucket) or None),
            "run_depth": run_depth_distribution(bucket),
        }
        for key, bucket in sorted(cohorts.items())
    }


def compute_metrics(runs, pricing=None):
    """All eight metric families over one session's runs."""
    runs = list(runs or [])
    return {
        "runs": len(runs),
        "gated_runs": sum(1 for r in runs if r.get("gated")),
        "cost_per_gated_task": cost_per_gated_task(runs),
        "gated_pass_rate": gated_pass_rate(runs),
        "escalation_escape_rate": escalation_escape_rate(runs),
        "run_depth_distribution": run_depth_distribution(runs),
        "frontier_warrant_rate": frontier_warrant_rate(runs),
        "hourglass_savings": hourglass_savings(runs, pricing),
        "jev_leverage": jev_leverage(runs, pricing),
        "cohorts": cohort_splits(runs),
    }


def _downweight_by_cap(samples, cap=INFLUENCE_CAP):
    """Cap one session's contribution weight.

    Rule: a session's weight toward any pooled metric is capped at ``cap``
    times the pool's raw volume — no single contributor can own a headline
    number regardless of how much it submits. Sessions already below the
    bound are untouched. (In a tiny pool every session may exceed the cap
    as a *share* — that is arithmetically unavoidable; the absolute bound
    still holds, and the cap is disclosed, not hidden.)
    """
    if not samples:
        return []
    total = sum(samples)
    if total <= 0:
        return [0.0 for _ in samples]
    bound = cap * total
    return [float(min(count, bound)) for count in samples]


def _flag_anomalies(bundles):
    """Anomalies are surfaced, never silently cleaned."""
    flags = []
    for bundle in bundles:
        metrics = compute_metrics((bundle or {}).get("runs") or [])
        overall = metrics["gated_pass_rate"]["overall"]
        if overall["pass_rate"] is not None and overall["samples"] < 3:
            flags.append({"bundle_id": (bundle or {}).get("bundle_id"),
                          "kind": "low_samples",
                          "detail": f"only {overall['samples']} gated runs"})
        depth = metrics["run_depth_distribution"]
        frontier_share = (depth.get("T3") or {}).get("share") or 0
        if frontier_share > 0.5:
            flags.append({"bundle_id": (bundle or {}).get("bundle_id"),
                          "kind": "frontier_heavy",
                          "detail": f"T3 share {frontier_share:.0%} — "
                                    f"atypical for the hourglass"})
    return flags


def aggregate_bundles(bundles, cap=INFLUENCE_CAP):
    """Combine many sessions' bundles with the disclosed influence cap.

    Per-session metrics are computed independently, then combined with
    capped weights; the response carries contributor counts and anomaly
    flags so the site can show its own seams.
    """
    bundles = [b for b in (bundles or []) if isinstance(b, dict)]
    if not bundles:
        return {"contributors": 0, "metrics": None, "influence_cap": cap,
                "anomalies": []}
    session_metrics = []
    for bundle in bundles:
        runs = bundle.get("runs") or []
        pricing = bundle.get("pricing_snapshot") or {}
        session_metrics.append({"bundle_id": bundle.get("bundle_id"),
                                "runs": len(runs),
                                "metrics": compute_metrics(runs, pricing)})
    weights = _downweight_by_cap([m["runs"] for m in session_metrics], cap)
    return {
        "contributors": len(bundles),
        "influence_cap": cap,
        "sessions": [
            {"bundle_id": m["bundle_id"], "runs": m["runs"],
             "weight": round(w, 4)}
            for m, w in zip(session_metrics, weights)],
        "metrics": [m["metrics"] for m in session_metrics],
        "anomalies": _flag_anomalies(bundles),
    }


def fold_rollup(previous, bundle):
    """Incremental rollup fold — the Python half of the worker's JS fold.

    The Cloudflare worker folds accepted bundles into a KV rollup with the
    SAME field-for-field logic (site/worker/index.js ``foldRollup``); shared
    fixture vectors (tests/fixtures/site_fold_vectors.json) pin the parity
    so the fast path and the authoritative nightly rebuild cannot drift.
    Pure function: same inputs, same outputs, no I/O.
    """
    import re
    rollup = previous if isinstance(previous, dict) else {}
    bundles = set(rollup.get("bundles") or [])
    bundle_id = bundle.get("bundle_id")
    if isinstance(bundle_id, str) and bundle_id:
        bundles.add(bundle_id)
    depth = dict(rollup.get("depth") or {})
    total_runs = int(rollup.get("total_runs") or 0)
    total_cost = float(rollup.get("total_cost") or 0.0)
    tier_re = re.compile(r"^T[0-3]$")
    for run in bundle.get("runs") or []:
        if not isinstance(run, dict):
            continue
        total_runs += 1
        try:
            total_cost += float(run.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
        tier = run.get("deepest_tier_reached")
        tier = tier if isinstance(tier, str) and tier_re.match(tier) else "unknown"
        depth[tier] = depth.get(tier, 0) + 1
    return {
        "schema": "site-rollup-v1",
        "contributors": len(bundles),
        "bundles": sorted(bundles),
        "total_runs": total_runs,
        "total_cost": round(total_cost, 9),
        "depth": depth,
    }


def build_snapshot(bundles, harness_version=None):
    """The static snapshot payload the site ships (SITE-3 output).

    Each session carries its own metrics block (computed over its runs with
    its own pricing) — the site renders per-session proofs and uses the
    weights only when pooling across contributors.
    """
    bundles = [b for b in (bundles or []) if isinstance(b, dict)]
    sessions = []
    for bundle in bundles:
        runs = bundle.get("runs") or []
        sessions.append({
            "bundle_id": bundle.get("bundle_id"),
            "runs": len(runs),
            "metrics": compute_metrics(runs,
                                       bundle.get("pricing_snapshot") or {}),
        })
    if sessions:
        weights = _downweight_by_cap([s["runs"] for s in sessions])
        for s, w in zip(sessions, weights):
            s["weight"] = round(w, 4)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_from_bundles": [b.get("bundle_id") for b in bundles],
        "harness_version": str(harness_version or ""),
        "contributors": len(bundles),
        "influence_cap": INFLUENCE_CAP,
        "anomalies": _flag_anomalies(bundles),
        "sessions": sessions,
    }
