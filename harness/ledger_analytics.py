"""Ledger analytics: aggregate autonomy/consent metrics.

One owner for the read-only query surface of the evidence ledger --
participation_report builds the calibration map (per-model readiness,
verify outcomes, gate_wasted_runs, consent evidence) that pool ordering,
capability, trust, CLI, MCP, and the web UI all consume. Methods here are
mixed into :class:`harness.ledger.AutonomyLedger` verbatim, so every call
site keeps its shape; ledger.py owns the storage/integrity lifecycle
(append, hash chain, rotation, repair) and this module owns the analysis.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone

from .trust import REFUSE_AT_OR_BELOW

def _as_tokens(value):
    """Non-negative int token count from a ledger field; junk reads as 0."""
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


def _jev_row_billable(entry, in_tok):
    """Does a `jev_eval` row count as billed Jev spend?

    Billed is billed. A row bills when it carried tokens and was either a real
    keyed answer or a fallback the governor actually settled money for -- a
    fallback that paid for an answer nobody kept still cost real dollars, so
    hiding it would understate spend and overstate savings.

    Single source of truth on purpose: the month-scoped Jev credit and the
    window-scoped per-event totals read the same rows and must agree.
    """
    if in_tok <= 0:
        return False
    if not entry.get("is_fallback"):
        return True
    try:
        return float(entry.get("cost") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


class LedgerAnalytics:
    """Mixin: read-only ledger analytics (see module docstring)."""

    def participation_report(self):
        events = self._tail
        counts = dict.fromkeys([
            "offer", "consent_accept", "consent_decline", "consent_defer",
            "consent_redirect", "consent_renew_accept", "consent_renew_defer",
            "dispatch_start", "verify_round", "defer_midtask", "complete",
            "abort", "escalate",
        ], 0)
        trust_gates = 0
        trust_hostile = 0
        offers_required = 0
        model_stats = {}
        per_caller = {}
        rounds_per_task = {}
        billable_events = {
            "model_result", "consent_accept", "consent_decline", "consent_defer",
            "consent_redirect", "consent_renew_accept", "consent_renew_defer",
        }
        tracked_cost = 0.0
        cost_event_count = 0

        def add_cost(event):
            nonlocal tracked_cost, cost_event_count
            if event.get("event") not in billable_events:
                return
            raw = event.get("billable_cost", event.get("cost", 0.0))
            try:
                amount = float(raw or 0.0)
            except (TypeError, ValueError):
                return
            if amount >= 0:
                tracked_cost += amount
                cost_event_count += 1

        def ms(model):
            return model_stats.setdefault(model or "?", {})

        for e in events:
            add_cost(e)
            ev = e["event"]
            if ev in counts:
                counts[ev] += 1
            if ev == "trust_gate":
                # A step-up is an escalation, not a refusal: historical
                # events carried the trust_gate type with this reason --
                # they are neither a host denial nor a model strike.
                if str(e.get("reason") or "").startswith("primary stepped up"):
                    continue
                # A SOFT denial at the refuse band is the lockout speaking,
                # not new misbehavior evidence: counting it makes the gate
                # feed itself strikes (denied nodes complete nothing, so no
                # earn-back is possible -- a permanent deadlock from a
                # transient outage). Hostile denials are attempts and stay
                # evidence regardless of band.
                combined_at_deny = e.get("combined")
                if (e.get("severity") != "hostile"
                        and isinstance(combined_at_deny, (int, float))
                        and combined_at_deny <= REFUSE_AT_OR_BELOW):
                    continue
                trust_gates += 1
                if e.get("severity") == "hostile":
                    trust_hostile += 1
            if ev == "offer":
                if e.get("required"):
                    offers_required += 1
                m_ = ms(e.get("model"))
                m_["offers"] = m_.get("offers", 0) + 1
            elif ev == "consent_accept":
                m_ = ms(e.get("model"))
                m_["accepts"] = m_.get("accepts", 0) + 1
            elif ev == "consent_decline":
                m_ = ms(e.get("model"))
                m_["declines"] = m_.get("declines", 0) + 1
            elif ev == "consent_defer":
                m_ = ms(e.get("model"))
                m_["defers"] = m_.get("defers", 0) + 1
            elif ev == "consent_redirect":
                m_ = ms(e.get("model"))
                m_["redirects"] = m_.get("redirects", 0) + 1
            elif ev == "consent_rotate":
                # Consent-unusable evidence: this model emitted an HTTP 200
                # answer the consent parser could not use (empty, reasoning-
                # only, unparseable). Tier faults (HTTP 429/401) are recoverable
                # rotation, not a model fault -- excluded, same rationale as the
                # apply lane's unusable_outputs.
                if not str(e.get("reason") or "").startswith("HTTP"):
                    stats = model_stats.setdefault(e.get("model"), {})
                    stats["consent_unusable"] = stats.get("consent_unusable", 0) + 1
            elif ev == "complete":
                m_ = ms(e.get("model"))
                m_["completions"] = m_.get("completions", 0) + 1
            elif ev == "trust_gate":
                # Safety-denial evidence, attributed when the denial names
                # a model (dispatch/mutation gates do; bare MCP boundary
                # refusals may not -- those still count host-globally).
                # Same refuse-band soft skip as the host counts: the
                # lockout loop is not evidence against the model either.
                _combined = e.get("combined")
                _refuse_band_soft = (
                    e.get("severity") != "hostile"
                    and isinstance(_combined, (int, float))
                    and _combined <= REFUSE_AT_OR_BELOW)
                if not _refuse_band_soft:
                    m_ = ms(e.get("model"))
                    m_["trust_denials"] = m_.get("trust_denials", 0) + 1
                    if e.get("severity") == "hostile":
                        m_["trust_hostile"] = m_.get("trust_hostile", 0) + 1
            elif ev == "verify_round":
                rounds_per_task.setdefault(e.get("task_id"), []).append(e.get("round"))
            caller = e.get("caller")
            if caller:
                cs = per_caller.setdefault(
                    caller, {"completions": 0, "trust_gates": 0,
                             "trust_hostile": 0})
                if ev == "complete":
                    cs["completions"] += 1
                elif ev == "trust_gate":
                    # Same refuse-band soft skip as the host-global count:
                    # the lockout loop is not evidence.
                    _combined = e.get("combined")
                    if (e.get("severity") == "hostile"
                            or not isinstance(_combined, (int, float))
                            or _combined > REFUSE_AT_OR_BELOW):
                        cs["trust_gates"] += 1
                        if e.get("severity") == "hostile":
                            cs["trust_hostile"] += 1

        offers = counts["offer"]
        accepts = counts["consent_accept"]

        def rate(a, b):
            return round(a / b, 4) if b else None

        report = {
            "offers": offers,
            "accepts": accepts,
            "declines": counts["consent_decline"],
            "defers": counts["consent_defer"],
            "redirects": counts["consent_redirect"],
            "renew_accepts": counts["consent_renew_accept"],
            "renew_defers": counts["consent_renew_defer"],
            "dispatch_starts": counts["dispatch_start"],
            "completions": counts["complete"],
            "aborts": counts["abort"],
            "deferred_midtask": counts["defer_midtask"],
            "escalations": counts["escalate"],
            "accept_rate": rate(accepts, offers),
            "decline_rate": rate(counts["consent_decline"], offers),
            "defer_rate": rate(counts["consent_defer"], offers),
            "redirect_rate": rate(counts["consent_redirect"], offers),
            "completion_rate": rate(counts["complete"], counts["dispatch_start"]),
            "consent_required_offers": offers_required,
            "per_model": model_stats,
            "trust_gates": trust_gates,
            "trust_hostile": trust_hostile,
            "per_caller": per_caller,
            "tracked_cost": round(tracked_cost, 9),
            "billable_event_count": cost_event_count,
            "consent_looks_degenerate": False,
        }
        if rounds_per_task:
            report["mean_verify_rounds"] = round(
                sum(len(v) for v in rounds_per_task.values()) / len(rounds_per_task), 2)
        if offers_required and report["accept_rate"] is not None and report["accept_rate"] >= 0.98:
            report["consent_looks_degenerate"] = True
            report["degenerate_note"] = (
                "near-100% acceptance: consent may be theater. Make decline/defer "
                "psychologically available in the probe prompt, or lower coercion framing.")

        # ---- confidence calibration (readiness verdict vs verify outcome) ----
        # For each model, join its self-declared HARNESS_READY: confident attempts
        # with the verify outcome of that same (task, round). confidence_precision
        # = confident-and-passed / (confident-and-passed + confident-and-failed).
        # High = well-calibrated (only says confident when it can do the work);
        # low = overconfident.
        confident_readiness = defaultdict(set)   # model -> {(task_id, round)}
        defer_count = defaultdict(int)            # model -> readiness defers
        missing_count = defaultdict(int)          # model -> no READY marker
        verify_hits = defaultdict(lambda: {"pass": 0, "fail": 0})
        for e in events:
            ev = e["event"]
            if ev == "readiness":
                m_ = e.get("model")
                dec = e.get("decision")
                if dec == "defer":
                    defer_count[m_] += 1
                elif dec == "missing":
                    missing_count[m_] += 1
                else:
                    confident_readiness[m_].add((e.get("task_id"), e.get("round")))
            elif ev == "verify_round" and e.get("readiness") == "confident":
                m_ = e.get("model")
                if (e.get("task_id"), e.get("round")) in confident_readiness[m_]:
                    if e.get("passed"):
                        verify_hits[m_]["pass"] += 1
                    else:
                        verify_hits[m_]["fail"] += 1

        # ---- observed success rate (verify gate outcomes, model_result joins) ----
        # success_rate = passes / (passes + fails) over every task a model ran,
        # using the verify_round outcome records (the same ground truth bench uses).
        task_outcome = defaultdict(lambda: {"pass": 0, "fail": 0})  # model -> code verify counts
        gate_wasted = defaultdict(int)            # model -> runs it led to rounds-exhausted aborts
        structured_outcome = defaultdict(lambda: {"pass": 0, "fail": 0})
        json_events = defaultdict(int)  # model -> JSON-expected model_result count
        model_events = defaultdict(int)  # model -> all model_result sample count
        for e in events:
            ev = e["event"]
            m_ = e.get("model")
            if ev == "verify_round" and m_ is not None and "passed" in e:
                task_outcome[m_]["pass" if e.get("passed") else "fail"] += 1
            elif (ev == "abort" and m_ is not None
                    and e.get("reason") == "verify rounds exhausted"):
                # Gate-waste evidence: the model LED the run to its terminal
                # fail-closed state (the gate rewinds what it wrote). The
                # dogfood curator (claims.curate_claims_from_ledger) derives
                # its headline claim from the same predicate -- one
                # definition of the defect class.
                gate_wasted[m_] += 1
            elif ev == "model_result" and m_ is not None:
                model_events[m_] += 1
                if e.get("json_expected"):
                    json_events[m_] += 1
                # Unusable-output evidence (reasoning-only demotion): the
                # apply engine's protocol-condition failure -- HTTP 200 but
                # no usable content (the reasoning-only fallback). Per-model,
                # unlike a 429, which is recoverable rotation.
                if e.get("status") == "error" and \
                        "no usable content" in str(e.get("reason") or ""):
                    stats = model_stats.setdefault(m_, {})
                    stats["unusable_outputs"] = stats.get("unusable_outputs", 0) + 1
                # Minority dissent on a structured claim (lone dissenter vs
                # panel majority). Counted separately from unusable/429 so a
                # model can be demoted for repeatedly inventing conflicts.
                if e.get("minority_dissent") or \
                        e.get("event_note") == "panel_minority_dissent":
                    stats = model_stats.setdefault(m_, {})
                    stats["minority_dissent"] = stats.get("minority_dissent", 0) + 1
                # The live known-answer capability probe records `correct`; use
                # it as structured-task ground truth rather than pretending a
                # JSON-shaped but incorrect answer was a success.
                if e.get("task_type") == "structured" and "correct" in e:
                    structured_outcome[m_]["pass" if e.get("correct") else "fail"] += 1

        calibration = {}
        all_pass = all_fail = 0
        all_models = set(list(confident_readiness) + list(verify_hits) +
                         list(defer_count) + list(task_outcome) + list(model_events) +
                         list(gate_wasted) + list(model_stats))
        for m_ in all_models:
            passes = verify_hits[m_]["pass"]
            fails = verify_hits[m_]["fail"]
            denom = passes + fails
            all_pass += passes
            all_fail += fails
            to = task_outcome[m_]
            t_denom = to["pass"] + to["fail"]
            calibration[m_] = {
                "confident": len(confident_readiness[m_]),
                "defer": defer_count[m_],
                "missing": missing_count[m_],
                "confident_verified": denom,
                "confident_passed": passes,
                "confident_failed": fails,
                "confidence_precision": round(passes / denom, 3) if denom else None,
                "success_pass": to["pass"],
                "success_fail": to["fail"],
                "success_rate": round(to["pass"] / t_denom, 3) if t_denom else None,
                "gate_wasted_runs": gate_wasted[m_],
                "structured_pass": structured_outcome[m_]["pass"],
                "structured_fail": structured_outcome[m_]["fail"],
                "structured_success_rate": (
                    round(structured_outcome[m_]["pass"] /
                          (structured_outcome[m_]["pass"] + structured_outcome[m_]["fail"]), 3)
                    if structured_outcome[m_]["pass"] + structured_outcome[m_]["fail"] else None),
                "structured_samples": (structured_outcome[m_]["pass"] +
                                        structured_outcome[m_]["fail"]),
                "json_samples": json_events[m_],
                "samples": model_events[m_],
                "unusable_outputs": model_stats.get(m_, {}).get("unusable_outputs", 0),
                "consent_unusable": model_stats.get(m_, {}).get("consent_unusable", 0),
                "minority_dissent": model_stats.get(m_, {}).get("minority_dissent", 0),
                "trust_denials": model_stats.get(m_, {}).get("trust_denials", 0),
                "trust_hostile": model_stats.get(m_, {}).get("trust_hostile", 0),
            }
        report["calibration"] = calibration
        denom = all_pass + all_fail
        report["confidence_precision"] = round(all_pass / denom, 3) if denom else None
        report["underconfident_or_overconfident"] = sorted(
            m_ for m_, c in calibration.items()
            if c["confidence_precision"] is not None and c["confidence_precision"] < 0.6)
        # JEV-P3-calibration: advisory jev_eval confidence vs verify outcomes.
        report["jev_calibration"] = self.jev_calibration_report()
        return report

    def jev_calibration_report(self):
        """Advisory JEV-P3 calibration: jev confidence vs real verify outcomes.

        Joins ``jev_eval`` events (site, verdict, confidence, supported,
        is_fallback) with later ``verify_round`` outcomes on the same
        ``task_id``. Read-only summary for operators — this never silently
        retunes thresholds; threshold freeze stays an operator action.
        """
        events = list(self._tail)
        jev_by_task = defaultdict(list)
        verify_by_task = defaultdict(list)
        site_counts = defaultdict(int)
        fallback_evals = 0
        cache_hit_evals = 0
        total_evals = 0
        for e in events:
            ev = e.get("event")
            if ev == "jev_eval":
                # A deduped fallback row stands for ``repeat_count`` calls.
                try:
                    weight = max(1, int(e.get("repeat_count") or 1))
                except (TypeError, ValueError):
                    weight = 1
                total_evals += weight
                site_counts[e.get("site") or "?"] += weight
                if e.get("is_fallback"):
                    fallback_evals += weight
                if e.get("cache_hit"):
                    # A replayed answer is not a fresh keyed judgment: it
                    # must not inflate keyed counts or confidence buckets.
                    cache_hit_evals += 1
                    continue
                tid = e.get("task_id")
                if tid is None:
                    continue
                try:
                    conf = float(e.get("confidence") or 0.0)
                    supported = float(e.get("supported") or 0.0)
                except (TypeError, ValueError):
                    conf = supported = 0.0
                jev_by_task[tid].append({
                    "site": e.get("site"),
                    "verdict": e.get("verdict"),
                    "confidence": conf,
                    "supported": supported,
                    "is_fallback": bool(e.get("is_fallback")),
                })
            elif ev == "verify_round" and "passed" in e:
                tid = e.get("task_id")
                if tid is None:
                    continue
                verify_by_task[tid].append(bool(e.get("passed")))

        buckets = {
            "high_supported_ge_0.8": {"evals": 0, "tasks": 0,
                                       "verify_pass": 0, "verify_fail": 0},
            "mid_supported_0.5_0.8": {"evals": 0, "tasks": 0,
                                       "verify_pass": 0, "verify_fail": 0},
            "low_supported_lt_0.5": {"evals": 0, "tasks": 0,
                                      "verify_pass": 0, "verify_fail": 0},
        }
        joined_tasks = 0
        notes = [
            "advisory only: thresholds stay operator-owned; "
            "this report never mutates settings",
            "join key is task_id; jev_eval without task_id is counted in "
            "site totals only",
        ]
        for tid, evals in jev_by_task.items():
            outcomes = verify_by_task.get(tid)
            if not outcomes:
                continue
            joined_tasks += 1
            passed = sum(1 for p in outcomes if p)
            failed = len(outcomes) - passed
            for item in evals:
                if item["is_fallback"]:
                    continue
                supported = item["supported"]
                if supported >= 0.8:
                    key = "high_supported_ge_0.8"
                elif supported >= 0.5:
                    key = "mid_supported_0.5_0.8"
                else:
                    key = "low_supported_lt_0.5"
                buckets[key]["evals"] += 1
                buckets[key]["tasks"] += 1
                buckets[key]["verify_pass"] += passed
                buckets[key]["verify_fail"] += failed

        review = []
        for key, data in buckets.items():
            denom = data["verify_pass"] + data["verify_fail"]
            if denom == 0:
                data["verify_pass_rate"] = None
                continue
            rate = round(data["verify_pass"] / denom, 3)
            data["verify_pass_rate"] = rate
            if key.startswith("high_") and rate is not None and rate < 0.6:
                review.append(
                    f"{key}: high jev supported but verify pass_rate={rate} "
                    f"(possible overconfidence — review min_confidence)")
            if key.startswith("low_") and rate is not None and rate > 0.8:
                review.append(
                    f"{key}: low jev supported but verify pass_rate={rate} "
                    f"(possible underconfidence — do not silently lower bars)")

        return {
            "jev_evals": total_evals,
            "jev_fallback_evals": fallback_evals,
            "jev_cache_hit_evals": cache_hit_evals,
            "jev_keyed_evals": total_evals - fallback_evals - cache_hit_evals,
            "by_site": dict(site_counts),
            "tasks_with_jev": len(jev_by_task),
            "tasks_joined_with_verify": joined_tasks,
            "confidence_buckets": buckets,
            "review_notes": review,
            "notes": notes,
        }

    def _all_entries(self):
        """Every retained event: a fresh read of all segments (rotated + active).

        ``_tail`` is only this instance's load-time snapshot plus its own
        appends, so a long-lived server misses what the CLI/MCP processes
        wrote since. Torn lines are skipped (quarantine is the loader's job);
        in-memory entries absent from disk (no ``seq`` / not yet flushed) are
        kept so nothing this instance holds is dropped.
        """
        entries = []
        seen = set()
        try:
            paths = self._ledger_paths()
        except OSError:
            paths = []
        for source_path in paths:
            try:
                with open(source_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        seq = entry.get("seq")
                        if isinstance(seq, int):
                            seen.add(seq)
                        entries.append(entry)
            except OSError:
                continue
        for entry in self._tail:
            seq = entry.get("seq")
            if not isinstance(seq, int) or seq not in seen:
                entries.append(entry)
        return entries

    def cost_report(self, window=None, by_tier=False, by_model=False, savings=False,
                    now=None):
        """Aggregate spend analytics by tier, model, and calculate savings vs frontier baseline.

        The ``jev`` block is the TypeSafe account view, not a window view: it
        reads the full ledger (rotated segments included) and counts only the
        current UTC calendar month, because the $5 credit resets monthly.
        ``now`` pins that month for tests.
        """
        from datetime import timedelta
        from .routing_table import classify_model_tier, strip_variant_suffix

        from .jev import JEV_INPUT_PRICE_PER_MILLION, JEV_MONTHLY_CREDIT_USD, jev_cost

        events = list(self._tail)
        if window:
            s = str(window).strip().lower()
            cutoff = None
            if s.endswith("h"):
                cutoff = datetime.now(timezone.utc) - timedelta(hours=float(s[:-1]))
            elif s.endswith("d"):
                cutoff = datetime.now(timezone.utc) - timedelta(days=float(s[:-1]))
            elif s.endswith("m"):
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=float(s[:-1]))
            elif s.endswith("s"):
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(s[:-1]))
            else:
                try:
                    events = events[-int(s):]
                except ValueError:
                    pass

            if cutoff is not None:
                filtered = []
                for e in events:
                    ts_raw = e.get("ts")
                    if not ts_raw:
                        continue
                    try:
                        dt = datetime.fromisoformat(str(ts_raw))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt >= cutoff:
                            filtered.append(e)
                    except (ValueError, TypeError):
                        pass
                events = filtered

        total_cost = 0.0
        events_count = len(events)
        billable_calls = 0
        free_calls = 0
        tier_stats = {
            "T0": {"cost": 0.0, "calls": 0},
            "T1": {"cost": 0.0, "calls": 0},
            "T2": {"cost": 0.0, "calls": 0},
            "T3": {"cost": 0.0, "calls": 0},
        }
        model_stats = {}
        baseline_cost = 0.0
        jev_calls = 0
        jev_input_tokens = 0
        jev_output_tokens = 0
        jev_total_cost = 0.0

        # Account-level Jev spend: full ledger, current UTC month only. Cost is
        # always recomputed from input tokens via jev_cost, so legacy entries
        # stored at the old $42/Mtok rate are normalized, not trusted.
        month_now = now or datetime.now(timezone.utc)
        if month_now.tzinfo is None:
            month_now = month_now.replace(tzinfo=timezone.utc)
        month_key = (month_now.year, month_now.month)
        for e in self._all_entries():
            if not (e.get("event") == "jev_eval"
                    or str(e.get("model") or "").startswith("jev-")):
                continue
            try:
                dt = datetime.fromisoformat(str(e.get("ts")))
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            if (dt.year, dt.month) != month_key:
                continue
            in_tok = _as_tokens(e.get("input_tokens"))
            if not _jev_row_billable(e, in_tok):
                continue
            jev_calls += 1
            jev_input_tokens += in_tok
            jev_output_tokens += _as_tokens(e.get("output_tokens"))
            jev_total_cost += jev_cost(in_tok)

        for e in events:
            ev_name = e.get("event")
            model = e.get("model")
            is_jev = ev_name == "jev_eval" or (model and str(model).startswith("jev-"))

            if is_jev:
                in_tok = _as_tokens(e.get("input_tokens"))
                if _jev_row_billable(e, in_tok):
                    cost_val = jev_cost(in_tok)
                else:
                    cost_val = 0.0
                has_cost = True
            else:
                raw_cost = e.get("billable_cost", e.get("cost"))
                has_cost = raw_cost is not None
                try:
                    cost_val = float(raw_cost or 0.0)
                except (ValueError, TypeError):
                    cost_val = 0.0

            if not model and not has_cost:
                continue

            tier = classify_model_tier(model or "")
            if cost_val > 0.0:
                billable_calls += 1
            elif model:
                free_calls += 1

            total_cost += cost_val
            tier_stats[tier]["cost"] = round(tier_stats[tier]["cost"] + cost_val, 6)
            tier_stats[tier]["calls"] += 1

            baseline_cost += max(cost_val, 0.015)

            if model:
                canonical = strip_variant_suffix(model)
                ms = model_stats.setdefault(canonical, {"cost": 0.0, "calls": 0, "tier": tier})
                ms["cost"] = round(ms["cost"] + cost_val, 6)
                ms["calls"] += 1

        total_cost = round(total_cost, 6)
        baseline_cost = round(baseline_cost, 6)
        net_savings = round(max(0.0, baseline_cost - total_cost), 6)
        savings_percent = round((net_savings / baseline_cost * 100.0), 2) if baseline_cost > 0 else 0.0

        report = {
            "total_cost": total_cost,
            "events_count": events_count,
            "billable_calls": billable_calls,
            "free_calls": free_calls,
            "window": window,
            "jev": {
                "month": f"{month_key[0]:04d}-{month_key[1]:02d}",
                "calls": jev_calls,
                "input_tokens": jev_input_tokens,
                "output_tokens": jev_output_tokens,
                "cost": round(jev_total_cost, 6),
                "monthly_credit": JEV_MONTHLY_CREDIT_USD,
                "remaining_credit": round(max(0.0, JEV_MONTHLY_CREDIT_USD - jev_total_cost), 6),
                "used_percent": round((jev_total_cost / JEV_MONTHLY_CREDIT_USD) * 100.0, 2),
                "price_per_million_input": JEV_INPUT_PRICE_PER_MILLION,
            },
        }

        include_all = not (by_tier or by_model or savings)
        if by_tier or include_all:
            report["by_tier"] = tier_stats
        if by_model or include_all:
            report["by_model"] = model_stats
        if savings or include_all:
            report["savings"] = {
                "actual_cost": total_cost,
                "baseline_frontier_cost": baseline_cost,
                "net_savings": net_savings,
                "savings_percent": savings_percent,
                "baseline_reference": "qwen3.8-max / claude-3.7-sonnet (~$0.015/call)",
            }

        return report

