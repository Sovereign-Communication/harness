"""Structured-claims convergence: a deterministic tally over panel votes plus
a rotating specialist lane that renders the final report.

The tally is authoritative. ``real: true`` always means the stated defect is
present; unanimity is counted from votes, never self-reported; and the
merge-gate signal additionally requires every required panel slot to have
voted, so a transport shortfall is reported as a shortfall rather than
disagreement. The specialist defaults to the judge model and rotates down a
fallback ladder on any imperfect outcome, but its prose can never override
the vote count.
"""

from .chat import (_chat_reservation_slots, _extract_json, _reported_cost,
                   assess_output, chat, extract_content_and_cost)
from .errors import HarnessError
from .output import eprint

MAX_429_RETRIES = 2
# Backoff base for bounded 429 retries; Retry-After headers take precedence.
RETRY_429_BACKOFF_SECONDS = 0.5
DEFAULT_CONVERGENCE_PANEL_TOKENS = 4096


def _parse_consensus(judge_text):
    """Parse the judge's JSON verdict; fail softly to unknown on bad output."""
    parsed = _extract_json(judge_text)
    if not parsed:
        # A judge response that cannot be parsed is not an approval. Keep the
        # raw text for diagnosis, but fail closed so callers never interpret
        # malformed prose as a successful synthesis.
        return {"agreement": "unknown", "confidence": None, "disagreements": [],
                "defer": True, "verdict": judge_text or "",
                "defer_reason": "unparseable_judge_output",
                "escalation": None, "plan": None}
    verdict = parsed.get("verdict")
    agreement = str(parsed.get("agreement", "unknown")).lower()
    if agreement not in ("high", "medium", "low", "none"):
        agreement = "unknown"
    conf = parsed.get("confidence")
    try:
        conf = None if conf is None else float(conf)
        conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None
    disagreements = parsed.get("disagreements") or []
    if not isinstance(disagreements, list):
        disagreements = []
    defer = bool(parsed.get("defer", False)) or agreement in ("low", "none")

    # Escalation directive (judge-driven auto-escalation).
    # The judge may direct the system to escalate to a more capable model rung.
    esc = parsed.get("escalation")
    escalation = None
    if isinstance(esc, dict):
        # Only a JSON boolean true enables escalation. Strings like "false"
        # are truthy under bool() and must not open a paid ladder.
        needed = esc.get("needed") is True
        if needed:
            raw_rung = esc.get("target_rung", 0)
            try:
                target_rung = int(raw_rung) if raw_rung is not None else 0
            except (TypeError, ValueError):
                target_rung = 0
            escalation = {
                "needed": True,
                "reason": str(esc.get("reason", ""))[:500],
                "condensed_context": str(esc.get("condensed_context", ""))[:8000],
                "target_rung": max(0, target_rung),
            }
        else:
            escalation = {"needed": False}

    # Plan artifact for de-escalation handoff.
    plan = parsed.get("plan")
    if isinstance(plan, dict):
        # Validate plan structure minimally
        pass
    elif plan is not None:
        plan = {"raw": str(plan)[:4000]}

    return {
        "verdict": verdict or judge_text or "",
        "agreement": agreement,
        "confidence": conf,
        "disagreements": disagreements,
        "defer": defer,
        "escalation": escalation,
        "plan": plan,
    }


def extract_claim_verdicts(content):
    """Return only well-formed per-claim votes from a panelist response.

    A structured convergence vote must contain a nested claim object whose
    ``real`` field is an actual JSON boolean. Strings such as ``"true"`` or
    malformed top-level prose are not votes and cannot satisfy participation.
    """
    if not isinstance(content, str):
        return {}
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items()
            if isinstance(v, dict) and isinstance(v.get("real"), bool)}


def tally_convergence(panel_results, claim_polarity=None, of_panel=None):
    """Compute responder agreement and gate eligibility separately.

    ``real: true`` always means that the stated defect is present. A claim is
    *unanimous* when every valid responder agrees. ``converged`` is stricter:
    every required panel slot must have supplied a valid vote as well. This
    distinction makes a 2/3 shortfall report as high responder agreement with
    an explicit fail-closed coverage shortfall, rather than mislabeling it as
    model disagreement.
    """
    claim_polarity = claim_polarity or {}
    required = len(panel_results) if of_panel is None else max(0, int(of_panel))
    buckets = {}
    order = {}
    for r in panel_results:
        verdicts = extract_claim_verdicts(r.get("content") or "")
        for cid, v in verdicts.items():
            kind = "reassurance" if claim_polarity.get(cid, "defect") == "reassurance" else "defect"
            if cid not in buckets:
                buckets[cid] = {"kind": kind, "votes": {"real": 0, "not_real": 0},
                                "confidences": [], "models": []}
                order[cid] = kind
            c = buckets[cid]
            c["votes"]["real" if v["real"] else "not_real"] += 1
            if v.get("confidence") is not None:
                try:
                    c["confidences"].append(float(v["confidence"]))
                except (TypeError, ValueError):
                    pass
            c["models"].append(r.get("model"))

    per_claim = {}
    reassurance = {}
    responder_claims = 0
    gate_claims = 0
    defect_total = 0
    claim_shortfall = False
    claim_disagreement = False
    disagreement_claims = []
    for cid, kind in order.items():
        c = buckets[cid]
        votes = c["votes"]
        total = votes["real"] + votes["not_real"]
        responder_unanimous = total > 0 and (votes["real"] == 0 or votes["not_real"] == 0)
        gate_unanimous = (required > 0 and total == required and responder_unanimous)
        # Reassurance claims are informational and intentionally excluded from
        # the defect gate, including its coverage-shortfall calculation.
        if kind == "defect":
            claim_shortfall = claim_shortfall or total < required
        if kind == "defect" and votes["real"] > 0 and votes["not_real"] > 0:
            # Reassurance/informational claims are intentionally outside the
            # defect tally; their polarity must not pollute disagreement output
            # or the defer reason for an otherwise converged audit.
            claim_disagreement = True
            disagreement_claims.append(cid)
        majority = "real" if votes["real"] >= votes["not_real"] else "not_real"
        mean_conf = round(sum(c["confidences"]) / len(c["confidences"]), 3) if c["confidences"] else None
        entry = {
            "verdict": majority,
            # This is responder unanimity. ``converged``/``gate_unanimous`` is
            # the fail-closed merge-gate value when the panel was short.
            "unanimous": responder_unanimous,
            "responder_unanimous": responder_unanimous,
            "converged": gate_unanimous,
            "gate_unanimous": gate_unanimous,
            "voted_by": total,
            "of_panel": required,
            "confidence": mean_conf,
            "votes": votes,
            "missing_votes": max(0, required - total),
            "panel_shortfall": total < required,
        }
        if kind == "defect":
            defect_total += 1
            if responder_unanimous:
                responder_claims += 1
            if gate_unanimous:
                gate_claims += 1
            per_claim[cid] = entry
        else:
            entry["note"] = "reassurance claim: reported separately and excluded from the defect convergence gate"
            reassurance[cid] = entry

    valid_responders = []
    for index, r in enumerate(panel_results):
        verdicts = extract_claim_verdicts(r.get("content") or "")
        if any(claim_polarity.get(cid, "defect") != "reassurance"
               for cid in verdicts):
            valid_responders.append(r.get("model") or f"panel_{index + 1}")
    # Count responding panel slots, not unique model ids. A caller may
    # deliberately include the same model more than once; each slot still
    # needs its own vote for an accurate voted_by/of_panel report.
    voted_by = len(valid_responders)
    responding_models = list(dict.fromkeys(valid_responders))
    panel_shortfall = bool(claim_shortfall or (required > 0 and voted_by < required))
    missing_votes = max(
        [entry["missing_votes"] for entry in per_claim.values()] or
        [max(0, required - voted_by)])
    responder_converged = defect_total > 0 and responder_claims == defect_total
    gate_converged = defect_total > 0 and gate_claims == defect_total
    responder_rate = round(responder_claims / defect_total, 3) if defect_total else None
    gate_rate = round(gate_claims / defect_total, 3) if defect_total else None
    return {
        # ``converged`` is deliberately gate-safe: it cannot be true while a
        # required panel slot or claim vote is missing.
        "converged": gate_converged,
        "responder_converged": responder_converged,
        "converged_claims": gate_claims,
        "responder_converged_claims": responder_claims,
        "total_claims": defect_total,
        # Keep the historical field useful to callers: it measures agreement
        # among responders. ``gate_convergence_rate`` measures coverage-aware
        # eligibility.
        "convergence_rate": responder_rate,
        "gate_convergence_rate": gate_rate,
        "disagreement": claim_disagreement,
        "disagreement_claims": disagreement_claims,
        "panel_shortfall": panel_shortfall,
        "missing_votes": missing_votes,
        "shortfall": {"voted_by": voted_by, "of_panel": required,
                      "missing_votes": missing_votes} if panel_shortfall else None,
        "voted_by": voted_by,
        "of_panel": required,
        "responding_models": responding_models,
        "claims": per_claim,
        "reassurance": reassurance,
    }


def _polarity_note(claim_polarity):
    """Name the reassurance claims (if any) with their inverted polarity.

    The panel prompt marks reassurance claims as real:true == correctness
    holds -- the opposite of defect polarity. Without the same note here
    the specialist inverts them in prose while the deterministic tally
    (which excludes them from the defect gate) stays right: two verdicts
    that disagree about the same votes.
    """
    reassurance = sorted(cid for cid, kind in (claim_polarity or {}).items()
                         if kind == "reassurance")
    if not reassurance:
        return ("All claims below are defect propositions; no reassurance "
                "claims are present.")
    ids = ", ".join(reassurance)
    return ("REASSURANCE claims (opposite polarity -- real:true means the "
            f"stated correctness HOLDS, not that a defect exists): {ids}. "
            "Render their verdicts in the panel's own real/not_real "
            "vocabulary and keep them out of defect convergence.")


def _trim_votes_to_window(vote_lines, profiles, candidates, head_text,
                          max_tokens):
    """Cap specialist votes at the SMALLEST known candidate window.

    The ladder rotates across models with different context windows; the
    prompt must fit the tightest one or a smaller-window fallback
    truncates mid-JSON and burns the ladder for nothing. Newest votes are
    kept first; dropped models are named, never silent. Unknown windows
    (or none known) mean no trim -- cost is still preflighted and a
    truncation rotates fail-closed. The deterministic tally (computed
    from full votes upstream) stays authoritative regardless.
    Returns (kept_lines, dropped_model_names).
    """
    windows = []
    for m_ in candidates:
        try:
            profile = (profiles or {}).get(m_)
            if profile is not None:
                windows.append(profile.max_source_tokens)
        except AttributeError:
            continue
    if not windows:
        return vote_lines, []
    from .tokens import estimate_prompt_tokens
    budget = int((min(windows) - estimate_prompt_tokens(head_text)
                  - max_tokens) * 0.9)
    if budget <= 0:
        # Head alone fills the window: trimming votes cannot fix it.
        # Let the provider truncate and the lane rotate, loudly.
        return vote_lines, []
    kept, dropped, used = [], [], 0
    for line in reversed(vote_lines):
        header = line.split("\n", 1)[0]
        name = header
        if name.startswith("--- Model: "):
            name = name[len("--- Model: "):]
        if name.endswith(" ---"):
            name = name[:-len(" ---")]
        tokens = estimate_prompt_tokens(line)
        if used + tokens > budget and kept:
            dropped.append(name)
            continue
        used += tokens
        kept.append(line)
    if dropped:
        eprint(f"[convergence] context budget {budget} tokens: dropped oldest "
               f"votes from {dropped} to fit the specialist window.")
    return list(reversed(kept)), dropped


def run_convergence_specialist(transport, api_key, governor, panel_results, model,
                                max_tokens=1200, reasoning_effort="auto",
                                reasoning_token_budget=0.4, ledger=None, task_id=None,
                                fallback_pool=None, claim_polarity=None,
                                profiles=None):
    """A dedicated 'convergence specialist' renders the final verdict from the
    panel's per-claim JSON (defaults to the judge model when not overridden).

    The specialist is itself a rotating lane: ``model`` is the primary, then
    ``fallback_pool`` (strongest first -- the curated free ladder leads with
    proven free emitters on observed track record; stale ids are skipped
    observed JSON record) is tried in order. A candidate is rotated out on any
    imperfect outcome: HTTP error, paid-BYOK route, empty or reasoning-only
    output, truncation against the token cap, or unparseable JSON. Every
    attempt is preflight-reserved before the first call and billed per
    attempt, so the ceiling stays exact. The deterministic tally remains
    authoritative even if the whole lane fails.

    Auto-escalation: the specialist may emit an "escalation" directive to
    request a more capable model rung, with a condensed context for the next
    rung. It may also emit a "plan" artifact for de-escalation handoff to
    cheaper models after the escalated rung completes.
    """
    lines = [
        "You are a convergence specialist. N independent models each reviewed the same claims "
        "and emitted per-claim verdicts {\"claim\":{\"real\":bool,\"confidence\":..}}. "
        "Produce the FINAL convergence consensus as ONE JSON object, no prose:",
        "{\"converged\":true|false,\"agreement\":\"high|medium|low|none\","
        "\"confidence\":<0-1>,\"claims\":{\"<claim>\":{\"verdict\":\"real|not_real\","
        "\"converged\":true|false,\"confidence\":<0-1>}}"
        ",\"escalation\":{\"needed\":true|false,\"reason\":\"...\","
        "\"condensed_context\":\"...\",\"target_rung\":0}"
        ",\"plan\":{\"steps\":[\"...\"],\"target_tier\":\"cheap|paid|free\"}}",
        "Responder unanimity is present when every model that answered agrees on its verdict. "
        "The merge-gate converged field additionally requires every required panel slot to answer. "
        "Claims are DEFECT propositions: real:true means the stated defect genuinely exists. "
        "Do not invent claims or models.",
        _polarity_note(claim_polarity),
        # Resource-cap disclosure: the model must know its budget up front so it
        # can plan to finish inside it instead of truncating mid-JSON.
        f"RESOURCE CAP: your entire response is limited to {max_tokens} output tokens, and "
        f"hidden reasoning counts against it (reasoning itself capped at "
        f"{int(max_tokens * reasoning_token_budget)} tokens). A truncated or reasoning-only "
        "response will be discarded and the task rotated to another model. Do your best "
        "within the cap, assume nothing beyond it, and emit ONLY the JSON object as your "
        "visible content, starting with {.",
        # Escalation guidance — needed must be the JSON boolean true, never a string.
        "ESCALATION GUIDANCE: If the panel's verdicts are inconclusive (low agreement, "
        "high deferral, or conflicting evidence), you MAY set \"escalation.needed\": true "
        "(JSON boolean true only) and provide a \"condensed_context\" (max 8000 chars) "
        "summarizing the stuck state for a more capable model. Set \"target_rung\" to a "
        "non-negative integer ladder index (0 = first rung). Include a \"plan\" with "
        "ordered steps for cheaper models to execute.",
    ]
    # Full per-claim JSON: these are short, structured verdicts, and the
    # preflight reserve (target * panel_tokens) already covers them. A
    # truncated vote can silently drop claims and corrupt the tally.
    vote_lines = [f"--- Model: {r.get('model')} ---\n{r.get('content') or ''}"
                  for r in panel_results]

    # Build the rotation ladder: primary first, then fallbacks (deduped,
    # learned-BYOK-blocked models dropped). Unknown fallback models are skipped
    # rather than fatal; the caller-chosen primary must be real.
    candidates = [model]
    for m_ in (fallback_pool or []):
        if m_ and m_ not in candidates:
            candidates.append(m_)
    governor.check_byok(candidates[0])  # P0: raise on mistralai//anthropic/
    usable = []
    for i, m_ in enumerate(candidates):
        if i and governor.learned_blocked(m_):
            eprint(f"[convergence] skipping {m_}: previously routed via BYOK (paid).")
            continue
        try:
            governor.fetch_pricing([m_])
        except HarnessError as e:
            if i == 0:
                raise
            eprint(f"[convergence] skipping fallback {m_}: {e}")
            continue
        usable.append(m_)
    candidates = usable

    vote_lines, dropped_votes = _trim_votes_to_window(
        vote_lines, profiles, candidates, "\n".join(lines), max_tokens)
    lines.extend(vote_lines)
    if dropped_votes:
        lines.append(
            "[NOTE: oldest panel votes from "
            f"{', '.join(dropped_votes)} were omitted to fit the "
            "specialist's context window; the deterministic tally remains "
            "authoritative for those claims.]")
    prompt = "\n".join(lines)

    # Reserve the whole ladder up front (including each reasoning model's
    # possible no-reasoning retry) so the ceiling is exact before any call.
    calls = []
    for m_ in candidates:
        slots = _chat_reservation_slots(m_, reasoning_effort)
        for s in range(slots):
            calls.append((f"convergence {m_} attempt {s + 1}/{slots}", m_, max_tokens, 0))
    governor.preflight(prompt, calls)

    def _bill_event(m_, ok, ev_status, cost):
        if ledger and task_id:
            ledger.append("model_result", task_id=task_id, event_note="convergence",
                          model=m_, task_type="structured", json_expected=True,
                          json_ok=ok, status=ev_status, cost=cost)

    attempts = []
    total_cost = 0.0
    result = {"status": "error", "model": model,
              "error": "no specialist candidate available", "cost": 0.0}
    for m_ in candidates:
        status, resp = chat(transport, api_key, m_, [{"role": "user", "content": prompt}],
                            max_tokens, reasoning_effort, reasoning_token_budget, governor)
        if status != 200:
            error_cost = _reported_cost(resp)
            if error_cost:
                governor.record_actual(error_cost, m_)
                total_cost += error_cost
            error = (resp.get("error", {}).get("message", str(resp))
                     if isinstance(resp, dict) else str(resp))
            attempts.append({"model": m_, "status": "error", "error": error,
                             "cost": error_cost})
            _bill_event(m_, False, "error", error_cost)
            eprint(f"[convergence] {m_}: HTTP {status}; rotating.")
            result = {"status": "error", "model": m_, "error": error,
                      "cost": total_cost}
            continue
        content, finish, cost, is_byok = extract_content_and_cost(resp)
        if is_byok and not governor.is_free(m_):
            # Paid BYOK route: spend is invisible to the tracked key.
            governor.record_byok(m_)
            attempts.append({"model": m_, "status": "error",
                             "error": "paid BYOK route; no specialist verdict", "cost": 0.0})
            _bill_event(m_, False, "error", 0.0)
            eprint(f"[convergence] {m_}: BYOK-routed (paid); rotating.")
            result = {"status": "error", "model": m_,
                      "error": "paid BYOK route; no specialist verdict",
                      "cost": total_cost}
            continue
        governor.record_actual(cost, m_)
        total_cost += cost
        # Imperfect-output triage: a reasoning-only, empty, or truncated body
        # must never be JSON-mined (a reasoning trace can embed JSON-looking
        # text that is not the verdict). Rotate instead.
        usable_body, unusable = assess_output(content, finish, allow_truncated=False)
        if not usable_body:
            attempts.append({"model": m_, "status": "error", "error": unusable,
                             "cost": cost})
            _bill_event(m_, False, "error", cost)
            eprint(f"[convergence] {m_}: {unusable}; rotating.")
            result = {"status": "error", "model": m_, "error": unusable,
                      "cost": total_cost}
            continue
        parsed = _extract_json(content)
        json_ok = isinstance(parsed, dict) and bool(parsed)
        _bill_event(m_, json_ok, "ok" if json_ok else "error", cost)
        if json_ok:
            attempts.append({"model": m_, "status": "ok", "cost": cost})
            return {"status": "ok", "model": m_, "specialist": parsed,
                    "raw": content, "cost": total_cost, "error": None,
                    "attempts": attempts,
                    "dropped_votes_from_prompt": dropped_votes}
        attempts.append({"model": m_, "status": "error",
                         "error": "no parseable JSON", "cost": cost})
        eprint(f"[convergence] {m_}: no parseable JSON; rotating.")
        result = {"status": "error", "model": m_,
                  "error": "specialist returned no parseable JSON",
                  "cost": total_cost}
    result["attempts"] = attempts
    return result
