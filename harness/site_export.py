"""site_export: sanitized evidence bundles for the Proof Bench site (SITE-1).

The public site proves real capability/$ from *verifiable* harness evidence.
This module is the ONE owner of the boundary between a local evidence ledger
and what may ever leave a machine: it rebuilds runs from ledger events,
applies a construction-from-allowlist sanitizer (fields are never copied --
only rebuilt from explicitly allowlisted values), demands a consent record,
refuses to run against a ledger whose hash chain does not verify, and scans
the serialized bundle for credential shapes before writing.

Fail-closed contract (same class as attest.py): missing consent, a broken
chain, an oversized/untrimmable bundle, or any secret-shaped substring in the
output raises :class:`HarnessError`. There is no redact-and-continue: a
bundle that needed redacting was built wrong.

Privacy posture: bundle-v1 carries no prompt text, no paths, no gate output,
no error text, no caller identity. Task identity appears only as a truncated
SHA-256 of the ledger task id; instruction text never enters the ledger and
therefore never enters a bundle. The worst thing a bundle can reveal is
"someone ran N tasks with these models, at these costs, with these outcomes"
-- which is exactly the site's point.

Trust posture (documented on the site's methodology page): the bundle carries
a chain *claim* (verified at export time on the contributor's machine, plus
the ledger head hash and entry count). A receiving server cannot re-verify a
chain it does not hold; deep verification stays local. The claim is honest
precisely because export refuses when ``ledger verify`` fails.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from .errors import HarnessError

BUNDLE_SCHEMA = "site-bundle-v1"
CONSENT_SCHEMA = "site-consent-v1"

# Evidence caps: a bundle is a curated sample, not a full dump. The oldest
# runs are dropped first and the truncation is recorded honestly in the
# bundle instead of silently shrinking the story.
MAX_RUNS = 5000
MAX_BUNDLE_BYTES = 512 * 1024

# Defense-in-depth scan over the SERIALIZED bundle. The builder never copies
# free-text fields, but a model id or a future field must never become the
# leak: any hit refuses the export outright.
_SECRET_PATTERNS = (
    re.compile(r"sk-or-v1-[0-9a-zA-Z]{16,}"),
    re.compile(r"apikey_[0-9a-zA-Z]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# Ledger event types that may contribute to a bundle. Everything else in the
# ledger is invisible to the exporter by construction.
_ALLOWED_EVENTS = frozenset({
    "dispatch_start", "complete", "abort", "defer_midtask", "escalate",
    "verify_round", "readiness", "model_result", "jev_eval", "spend_check",
})

_VALID_OUTCOMES = ("pass", "fail", "deferred", "aborted")


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _sha16(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _finite_number(value):
    """Best-effort float coercion; None when the ledger value is unusable."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount in (float("inf"), float("-inf")):
        return None
    return amount


def load_consent(path):
    """Load and validate the operator's consent record (fail-closed)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            consent = json.load(f)
    except OSError as exc:
        raise HarnessError(f"consent record not readable: {path} "
                           f"({getattr(exc, 'strerror', None) or exc})") from exc
    except ValueError as exc:
        raise HarnessError(f"consent record is not valid JSON: {path} ({exc})") from exc
    if not isinstance(consent, dict):
        raise HarnessError("consent record must be a JSON object")
    if consent.get("schema") != CONSENT_SCHEMA:
        raise HarnessError(
            f"consent record schema must be {CONSENT_SCHEMA!r}; "
            f"got {consent.get('schema')!r}")
    if not str(consent.get("accepted_at") or "").strip():
        raise HarnessError("consent record missing accepted_at timestamp")
    if not str(consent.get("surface") or "").strip():
        raise HarnessError("consent record missing consent surface")
    return consent


def load_pricing(path):
    """Load an optional pricing snapshot (``harness models --all`` dump).

    Prices travel only as USD per million tokens, keyed by model id. Absent
    pricing simply leaves the bundle's ``pricing_snapshot`` null -- the site
    then shows cost-per-task in absolute dollars only, never fabricates a
    price.
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except OSError as exc:
        raise HarnessError(f"pricing snapshot not readable: {path} "
                           f"({getattr(exc, 'strerror', None) or exc})") from exc
    except ValueError as exc:
        raise HarnessError(f"pricing snapshot is not valid JSON: {path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise HarnessError("pricing snapshot must be a JSON object keyed by model id")
    pricing = {}
    for model, row in raw.items():
        if not isinstance(model, str) or not model.strip():
            continue
        if isinstance(row, dict):
            inp = _finite_number(row.get("input_per_mtok"))
            out = _finite_number(row.get("output_per_mtok"))
        else:
            inp = out = None
        if inp is not None or out is not None:
            pricing[model] = {"input_per_mtok": inp, "output_per_mtok": out}
    if not pricing:
        raise HarnessError("pricing snapshot contains no usable rows")
    return pricing


def _read_ledger_entries(path):
    """Read raw JSONL entries; unparseable lines are counted, never hidden."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError as exc:
        raise HarnessError(f"ledger not readable: {path} "
                           f"({getattr(exc, 'strerror', None) or exc})") from exc
    entries = []
    skipped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(entry, dict):
            entries.append(entry)
        else:
            skipped += 1
    return entries, skipped


def verify_chain(path):
    """Re-verify the ledger's hash chain through its ONE owner.

    Delegates to :class:`harness.ledger.AutonomyLedger` (storage/integrity
    owner) rather than reimplementing chain math here. Export proceeds only
    on a clean verify.
    """
    from .ledger import AutonomyLedger  # local import: keeps site_export lean
    ledger = AutonomyLedger(path)
    ok, first_bad = ledger.verify()
    if not ok:
        raise HarnessError(
            f"ledger hash chain FAILED verification at seq {first_bad}; "
            f"refusing export (run 'harness ledger repair' or investigate)")
    chain = ledger.chain_status()
    entries = len(ledger.entries())
    if entries == 0:
        raise HarnessError("ledger contains no entries; nothing to export")
    return {"verified_claim": True, "head_hash": chain.get("head_hash") or "",
            "entries": entries}


def _allow(entries):
    return [e for e in entries if e.get("event") in _ALLOWED_EVENTS]


def _model_tier(model):
    """Tier of a model via the routing table's ONE classifier; None unknown."""
    try:
        from .routing_table import classify_model_tier
        return classify_model_tier(model)
    except Exception:  # pragma: no cover - classifier is stdlib and total
        return None


def _run_lane(task_id):
    return "bench" if str(task_id or "").startswith("bench/") else "task"


def build_runs(entries):
    """Rebuild per-task run records from allowlisted events.

    One run = one ledger task_id. Costs come only from ``model_result`` /
    ``jev_eval`` cost fields; outcomes only from ``complete`` status; escalation
    provenance only from ``escalate`` + ``escalation_rung`` markers. Every v2
    escalation field is additive and tolerated in absence (v1 ledgers render
    with ``directed_by: "verify_lane"`` and null confidence -- the site shows
    the same cascade, one column lighter).
    """
    by_task = {}
    order = []
    for e in entries:
        task_id = e.get("task_id")
        if not task_id:
            continue
        bucket = by_task.get(task_id)
        if bucket is None:
            bucket = {"escalates": [], "rung_models": [], "verify": [],
                      "jev": [], "generative_cost": 0.0, "jev_cost": 0.0,
                      "jev_fallback": 0, "models": set(), "deepest": None,
                      "entry_tier": None, "complete": None, "abort": False,
                      "deferred": False, "ts": None}
            by_task[task_id] = bucket
            order.append(task_id)
        ev = e.get("event")
        if bucket["ts"] is None and e.get("ts"):
            bucket["ts"] = e.get("ts")
        if ev == "dispatch_start":
            model = e.get("model")
            if model:
                bucket["models"].add(str(model))
                tier = _model_tier(model)
                if tier is not None:
                    if bucket["entry_tier"] is None:
                        bucket["entry_tier"] = tier
                    bucket["deepest"] = tier if bucket["deepest"] is None \
                        else max(bucket["deepest"], tier)
        elif ev == "model_result":
            model = e.get("model")
            cost = _finite_number(e.get("cost")) or 0.0
            bucket["generative_cost"] += max(0.0, cost)
            if model:
                bucket["models"].add(str(model))
                note = str(e.get("event_note") or "")
                if note.startswith("escalation_rung_"):
                    bucket["rung_models"].append(str(model))
                tier = _model_tier(model)
                if tier is not None:
                    if bucket["entry_tier"] is None and \
                            not note.startswith("escalation_rung_"):
                        bucket["entry_tier"] = tier
                    bucket["deepest"] = tier if bucket["deepest"] is None \
                        else max(bucket["deepest"], tier)
        elif ev == "jev_eval":
            cost = _finite_number(e.get("cost")) or 0.0
            bucket["jev_cost"] += max(0.0, cost)
            if e.get("is_fallback"):
                bucket["jev_fallback"] += 1
            conf = e.get("confidence")
            if isinstance(conf, (int, float)):
                bucket["jev"].append({"ts": e.get("ts"), "confidence": float(conf)})
        elif ev == "verify_round":
            bucket["verify"].append({
                "ts": e.get("ts"), "passed": bool(e.get("passed")),
                "model": e.get("model"),
            })
        elif ev == "escalate":
            bucket["escalates"].append({
                "ts": e.get("ts"),
                "from_model": str(e.get("from_model") or ""),
                "to_model": str(e.get("to_model") or ""),
                "directed_by": str(e.get("directed_by") or "") or "verify_lane",
                "jev_confidence": _finite_number(e.get("jev_confidence")),
                "target_rung": e.get("target_rung")
                if isinstance(e.get("target_rung"), int) else None,
                "condensed_context_chars": e.get("condensed_context_chars")
                if isinstance(e.get("condensed_context_chars"), int) else None,
            })
        elif ev == "complete":
            # Keep the LAST complete (a retried task's final state wins).
            if bucket["complete"] is None or str(e.get("ts") or "") >= \
                    str(bucket["complete"].get("ts") or ""):
                bucket["complete"] = e
        elif ev == "abort":
            bucket["abort"] = True
        elif ev == "defer_midtask":
            bucket["deferred"] = True

    runs = []
    for task_id in order:
        b = by_task[task_id]
        complete = b["complete"]
        status = str((complete or {}).get("status") or "")
        note = str((complete or {}).get("note") or "")
        if status == "ok":
            outcome = "pass"
        elif b["deferred"]:
            outcome = "deferred"
        elif b["abort"]:
            outcome = "aborted"
        else:
            outcome = "fail"
        gated = bool(b["verify"]) and "no verification gate" not in note
        rounds = None
        if complete is not None and isinstance(complete.get("rounds"), int):
            rounds = complete["rounds"]
        elif b["verify"]:
            rounds = len(b["verify"])

        primary = None
        if complete is not None and complete.get("model"):
            primary = str(complete["model"])
        elif b["models"]:
            primary = sorted(b["models"])[0]

        run = {
            "run_id": _sha16(str(task_id)),
            "task_ref": _sha16(f"task:{task_id}"),
            "lane": _run_lane(task_id),
            "ts": b["ts"],
            "primary_model": primary,
            "tier": _model_tier(primary) if primary else None,
            "entry_tier": b["entry_tier"],   # where the run ENTERED the ladder;
                                              # tier - entry_tier is the escape
            "rounds": rounds,
            "tokens_in": None,   # generative token counts are not ledgered;
            "tokens_out": None,  # never fabricate -- see module docstring.
            "cost": round(b["generative_cost"], 9),
            "outcome": outcome,
            "gated": gated,
            "deepest_tier_reached": b["deepest"],
            "jev_evals": {
                "count": len(b["jev"]),
                "cost": round(b["jev_cost"], 9),
                "fallback": b["jev_fallback"],
            },
        }

        if b["escalates"]:
            first, last = b["escalates"][0], b["escalates"][-1]
            lower_fail_rounds = sum(1 for v in b["verify"] if not v["passed"])
            conf_at_handoff = None
            handoff_ts = first["ts"]
            for j in b["jev"]:
                if handoff_ts is None or (j["ts"] or "") <= str(handoff_ts or ""):
                    conf_at_handoff = j["confidence"]
            run["escalation"] = {
                "from_model": first["from_model"] or None,
                "to_model": last["to_model"] or None,
                "rungs": list(dict.fromkeys(b["rung_models"])) or None,
                "family_changed": None,  # family map is config-owned; filled
                "directed_by": last["directed_by"],
                "jev_confidence": last["jev_confidence"],
                "target_rung": last["target_rung"],
                "condensed_context_chars": last["condensed_context_chars"],
                "warrant": {
                    "lower_rung_rounds": len(b["verify"]),
                    "lower_rung_verify_failures": lower_fail_rounds,
                    "jev_confidence_at_handoff": conf_at_handoff,
                },
            }
            if primary and last["to_model"]:
                run["primary_model"] = primary
        runs.append(run)
    return runs


def build_bench_rows(runs):
    """Bench rows are the run rows whose lane is bench, flattened for the
    site's known-answer proof table."""
    rows = []
    for run in runs:
        if run["lane"] != "bench":
            continue
        rows.append({
            "task_ref": run["task_ref"],
            "model": run["primary_model"],
            "tier": run["tier"],
            "passed": run["outcome"] == "pass",
            "rounds": run["rounds"],
            "cost": run["cost"],
            "gated": run["gated"],
        })
    return rows


def build_capabilities(entries):
    """Observed per-model evidence (the ledger's half of the capability row).

    Declared capability (context length, JSON support) comes from the live
    /models catalog and is NOT fabricated here; the site joins this observed
    block with the aggregate's declared block when both exist.
    """
    stats = {}
    for e in entries:
        ev = e.get("event")
        model = e.get("model")
        if not model:
            continue
        s = stats.setdefault(str(model), {"pass": 0, "fail": 0, "gate_wasted": 0,
                                          "calls": 0})
        if ev == "verify_round" and "passed" in e:
            s["pass" if e.get("passed") else "fail"] += 1
        elif ev == "model_result":
            s["calls"] += 1
        elif ev == "abort" and e.get("reason") == "verify rounds exhausted":
            s["gate_wasted"] += 1
    rows = []
    for model in sorted(stats):
        s = stats[model]
        denom = s["pass"] + s["fail"]
        rows.append({
            "model": model,
            "observed_success": round(s["pass"] / denom, 4) if denom else None,
            "samples": denom,
            "gate_wasted": s["gate_wasted"],
            "calls": s["calls"],
        })
    return rows


def _totals(runs):
    gated = [r for r in runs if r["gated"]]
    return {
        "runs": len(runs),
        "gated_runs": len(gated),
        "completed": sum(1 for r in runs if r["outcome"] == "pass"),
        "escalations": sum(1 for r in runs if "escalation" in r),
        "total_cost": round(sum(r["cost"] for r in runs)
                            + sum(r["jev_evals"]["cost"] for r in runs), 9),
    }


def _secret_scan(text):
    for pattern in _SECRET_PATTERNS:
        hit = pattern.search(text)
        if hit:
            return pattern.pattern
    return None


def _serialize_under_cap(bundle):
    """Serialize; if over the byte cap, drop oldest runs until it fits.

    Truncation is recorded on the bundle -- the site renders the sample
    honestly instead of pretending it saw everything.
    """
    payload = _canon(bundle)
    if len(payload.encode("utf-8")) <= MAX_BUNDLE_BYTES:
        return payload, False
    while bundle["runs"] and len(payload.encode("utf-8")) > MAX_BUNDLE_BYTES:
        bundle["runs"].pop(0)
        bundle["totals"] = _totals(bundle["runs"])
        bundle["truncated"] = True
        payload = _canon(bundle)
    if len(payload.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise HarnessError(
            "bundle cannot fit the 512KB cap even with zero runs; "
            "refusing to emit a misleadingly empty bundle")
    if len(bundle["runs"]) > MAX_RUNS:
        raise HarnessError("internal: run cap violated")  # pragma: no cover
    return payload, bundle.get("truncated", False)


def export_bundle(ledger_path, consent_path, pricing_path=None,
                  harness_version=None):
    """Build the sanitized bundle dict from a verified, consented ledger."""
    consent = load_consent(consent_path)
    pricing = load_pricing(pricing_path) if pricing_path else None
    chain = verify_chain(ledger_path)
    entries, skipped = _read_ledger_entries(ledger_path)
    allowed = _allow(entries)
    if not allowed:
        raise HarnessError(
            "ledger contains no exportable events; run tasks first "
            "(bench/apply/plan lanes write the evidence a bundle needs)")

    runs = build_runs(allowed)
    if len(runs) > MAX_RUNS:
        runs = runs[-MAX_RUNS:]
        truncated = True
    else:
        truncated = False

    bundle = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": "",  # filled after canonical hashing of the core
        "generated_at": _utc_now_iso(),
        "harness_version": str(harness_version or ""),
        "consent": {
            "schema": consent["schema"],
            "accepted_at": consent["accepted_at"],
            "surface": consent["surface"],
        },
        "chain": chain,
        "ledger_quarantined_lines": skipped,
        "truncated": truncated,
        "totals": _totals(runs),
        "pricing_snapshot": pricing,
        "runs": runs,
        "bench": build_bench_rows(runs),
        "capabilities": build_capabilities(allowed),
    }
    # Identity = content, not wall clock: generated_at is excluded from the
    # canonical core so re-exporting an unchanged ledger yields the same
    # bundle_id (the site's dedupe key must be stable across re-runs).
    core = {k: v for k, v in bundle.items()
            if k not in ("bundle_id", "generated_at")}
    bundle["bundle_id"] = _sha16(_canon(core))
    return bundle


def write_bundle(bundle, out_path):
    """Secret-scan, enforce the byte cap, and atomically write the bundle."""
    payload, _ = _serialize_under_cap(json.loads(_canon(bundle)))
    hit = _secret_scan(payload)
    if hit:
        raise HarnessError(
            f"refusing export: secret-shaped content matched {hit!r}; "
            f"the ledger or consent record contains a credential")
    tmp = out_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp, out_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise HarnessError(f"bundle write failed: {out_path} "
                           f"({getattr(exc, 'strerror', None) or exc})") from exc
    return len(payload.encode("utf-8"))
