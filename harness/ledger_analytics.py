"""Ledger analytics: aggregate autonomy/consent metrics.

One owner for the read-only query surface of the evidence ledger --
participation_report builds the calibration map (per-model readiness,
verify outcomes, gate_wasted_runs, consent evidence) that pool ordering,
capability, trust, CLI, MCP, and the web UI all consume. Methods here are
mixed into :class:`harness.ledger.AutonomyLedger` verbatim, so every call
site keeps its shape; ledger.py owns the storage/integrity lifecycle
(append, hash chain, rotation, repair) and this module owns the analysis.
"""
from collections import defaultdict

from .trust import REFUSE_AT_OR_BELOW

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
        return report
