"""Demo data for the Proof Bench site (SITE-4).

Generates a synthetic-but-realistic ledger THROUGH THE REAL AutonomyLedger
(hash-chained events, real event vocabulary), then exports it through the
real ``site_export`` and aggregates through the real ``site_aggregate``.
Nothing here bypasses the production pipeline — the demo bundle is exactly
as sanitized, consent-bound, and chain-claimed as a real one, and every
consumer (site, worker, tests) treats it identically.

The bundle is honestly labeled: ``demo`` is true on the snapshot, and the
site renders a visible DEMO banner whenever it serves this data.
"""
import os

from harness.ledger import AutonomyLedger
from harness.site_aggregate import build_snapshot
from harness.site_export import export_bundle

_DEMO_MODELS = {
    "scout": {"id": "demo/scout-free:free", "tier": "T0",
              "input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "flash": {"id": "demo/helper-lite", "tier": "T1",
              "input_per_mtok": 0.10, "output_per_mtok": 0.0},
    "reasoner": {"id": "demo/reasoner-mid", "tier": "T2",
                 "input_per_mtok": 0.80, "output_per_mtok": 0.0},
    "frontier": {"id": "demo/frontier-max", "tier": "T3",
                 "input_per_mtok": 10.0, "output_per_mtok": 0.0},
}

# (bench task, entry tier, passed at entry, escalate_to, passed after
#  escalation, rounds, per-model costs) — shaped so the run-depth histogram
# shows the intended hourglass: base layers carry the volume, frontier is
# rare and warranted.
_TASKS = [
    # Base layers dominate: 20 of 26 runs enter at T0/T1 and 18 of those
    # finish there. Frontier appears twice — one warranted T2->T3 climb and
    # one direct dispatch the site flags as unwarranted.
    ("typo-string", "T0", True, None, 0.0001),
    ("typo-string", "T0", True, None, 0.0001),
    ("docstring", "T0", True, None, 0.0001),
    ("docstring", "T0", True, None, 0.0002),
    ("import-order", "T0", True, None, 0.0001),
    ("import-order", "T0", True, None, 0.0002),
    ("name-repair", "T0", True, None, 0.0002),
    ("name-repair", "T0", True, None, 0.0001),
    ("name-repair", "T0", False, "flash", 0.0009),
    ("unpack-args", "T0", True, None, 0.0002),
    ("unpack-args", "T0", True, None, 0.0001),
    ("typo-string", "T0", True, None, 0.0001),
    ("arg-default", "T1", True, None, 0.0006),
    ("dict-group", "T1", True, None, 0.0007),
    ("dict-group", "T1", True, None, 0.0006),
    ("multi-round-tokens", "T1", True, None, 0.0018),
    ("multi-round-tokens", "T1", True, None, 0.0016),
    ("multi-round-tokens", "T1", False, "flash", 0.0034),
    ("lru-cache", "T1", True, None, 0.0012),
    ("lru-cache", "T1", True, None, 0.0011),
    ("arg-default", "T1", True, None, 0.0007),
    ("race-guard", "T2", True, None, 0.0091),
    ("race-guard", "T2", True, None, 0.0088),
    ("invariant-ledger", "T2", True, None, 0.0102),
    ("retry-backoff", "T2", False, "frontier", 0.0620),
    ("retry-backoff", "T3", True, None, 0.0871),
]


def build_demo_ledger(path, tasks=8):
    """Write a hash-chained demo ledger with bench-shaped events."""
    ledger = AutonomyLedger(path)
    models = _DEMO_MODELS
    for i in range(tasks):
        name, entry_tier, passed, escalate_to, base_cost = _TASKS[
            i % len(_TASKS)]
        task_id = f"bench/demo-{name}-{i:03d}"
        entry_model = next(m for m in models.values() if m["tier"] == entry_tier)
        seq = [(entry_model, base_cost, passed)]
        if escalate_to and not passed:
            esc_model = models[escalate_to]
            esc_cost = base_cost * (4 if esc_model["tier"] == "T1" else
                                    12 if esc_model["tier"] == "T2" else 60)
            seq = [(entry_model, base_cost, False), (esc_model, esc_cost, True)]
        for model, cost, ok in seq:
            ledger.append("dispatch_start", task_id=task_id, model=model["id"])
            ledger.append("model_result", task_id=task_id, model=model["id"],
                          cost=round(cost, 9), status="ok")
            ledger.append("verify_round", task_id=task_id, round=1,
                          passed=ok, model=model["id"], readiness="confident")
            if ok:
                ledger.append("complete", task_id=task_id, model=model["id"],
                              rounds=1, status="ok")
            else:
                # The T2->T3 climb is Jev-directed (the low-confidence noul
                # handed the decision to the next rung); other escalations
                # stay verify-lane climbs. Same field names the production
                # apply path ledger-issues (apply_policy + EscalationDriver).
                directed = esc_model["tier"] == "T3"
                ledger.append(
                    "escalate", task_id=task_id,
                    from_model=model["id"], to_model="next",
                    directed_by="jev" if directed else "verify_lane",
                    jev_confidence=0.32 if directed else None,
                    target_rung=3 if directed else None,
                    condensed_context_chars=1840 if directed else None)
                ledger.append("jev_eval", task_id=task_id,
                              site="escalation-decision" if directed
                              else "model_route",
                              model="jev-latest", verdict="pass",
                              supported=0.4, confidence=0.38,
                              input_tokens=850, output_tokens=0,
                              cost=0.0000036, is_fallback=False)
        ledger.append("spend_check", task_id=task_id,
                      spent=round(sum(c for _, c, _ in seq), 9))
    return ledger


def write_demo_artifacts(out_dir, tasks=8, consent_path=None):
    """Produce bundle.json + snapshot.json via the REAL pipeline.

    ``consent_path``: an existing consent record to reuse (None = write the
    demo consent next to the bundle; it is still a real consent record —
    the demo operator consents to publishing the demo evidence).
    """
    os.makedirs(out_dir, exist_ok=True)
    ledger_path = os.path.join(out_dir, "demo-ledger.jsonl")
    # The ledger APPENDS (it is an evidence log); a demo rebuild must start
    # from an empty chain or task ids collide and runs merge.
    for stale in os.listdir(out_dir):
        if stale.startswith("demo-ledger.jsonl"):
            os.unlink(os.path.join(out_dir, stale))
    build_demo_ledger(ledger_path, tasks=tasks)
    if consent_path is None:
        consent_path = os.path.join(out_dir, "demo-consent.json")
        from harness.site_export import CONSENT_SCHEMA
        import json
        with open(consent_path, "w", encoding="utf-8") as f:
            json.dump({"schema": CONSENT_SCHEMA,
                       "accepted_at": "2026-09-21T00:00:00Z",
                       "surface": "demo_generator"}, f)
    # The demo's pricing snapshot is part of the generated evidence: the
    # savings/leverage metrics need prices, and the site must never ship a
    # fabricated ratio silently.
    import json
    pricing_path = os.path.join(out_dir, "demo-pricing.json")
    with open(pricing_path, "w", encoding="utf-8") as f:
        json.dump({m["id"]: {"input_per_mtok": m["input_per_mtok"],
                             "output_per_mtok": m["output_per_mtok"]}
                   for m in _DEMO_MODELS.values()}, f)
    bundle = export_bundle(ledger_path, consent_path,
                           pricing_path=pricing_path,
                           harness_version="demo")
    from harness.site_export import write_bundle
    bundle_path = os.path.join(out_dir, "bundle.json")
    write_bundle(bundle, bundle_path)
    snapshot = build_snapshot([bundle], harness_version="demo")
    snapshot["demo"] = True
    snapshot["demo_note"] = ("Synthetic evidence generated through the real "
                             "export + aggregate pipeline; labeled demo, "
                             "never blended silently with live data.")
    snapshot_path = os.path.join(out_dir, "snapshot.json")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    return {"bundle": bundle_path, "snapshot": snapshot_path,
            "bundle_id": bundle["bundle_id"], "runs": bundle["totals"]["runs"]}
