"""Panel + judge verification: rotating independent takes, one synthesis.

The panel is an ordered pool: failing members are replaced by the next model
until ``max_panelists`` succeed or the pool is exhausted. A structured-claims
run (:func:`panel_judge` with ``run_convergence=True``) adds the deterministic
tally and the specialist lane from :mod:`harness.convergence`; the tally, not
the judge's prose, owns the verdict.

Output policy: every response body passes through
:func:`harness.chat.assess_output`. Empty and reasoning-only bodies are never
panel votes in any mode; a truncated body is tolerated (with a warning) in
prose mode and rejected in structured mode.
"""
import concurrent.futures
import time

from .chat import (_chat_reservation_slots, _extract_json, _reported_cost,
                   assess_output, chat, extract_content_and_cost,
                   REASONING_FALLBACK_PREFIX)
from .capability import ordered_pool
from .config import DEFAULT_MAX_TOKENS
from .convergence import (DEFAULT_CONVERGENCE_PANEL_TOKENS, MAX_429_RETRIES,
                          RETRY_429_BACKOFF_SECONDS, _parse_consensus,
                          extract_claim_verdicts, run_convergence_specialist,
                          tally_convergence)
from .errors import HarnessError
from .output import eprint
from .tokens import estimate_prompt_tokens


def panel_judge(*, transport, api_key, governor, prompt, panel, judge, max_tokens=None,
                reasoning_effort="auto", reasoning_token_budget=0.4, task_id=None,
                ledger=None, max_panelists=3, run_convergence=False,
                convergence_model=None, specialist_pool=None, claim_polarity=None,
                free_tier=False, cancel_check=None):
    """Rotating panel of independent cheap takes + 1 structured judge verdict.

    panel is an ordered pool; members that fail are replaced by the next model
    in the pool until max_panelists succeed or the pool is exhausted.
    """
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    panel_tokens = max_tokens
    if run_convergence:
        # Structured claim JSON is frequently longer than ordinary prose. Keep
        # the caller's lower bound but avoid wasting panel slots on truncation.
        panel_tokens = max(panel_tokens, DEFAULT_CONVERGENCE_PANEL_TOKENS)
    panel_pool = list(panel)

    spec_model = convergence_model or judge
    for _m in panel_pool + [judge, spec_model]:
        governor.check_byok(_m)  # P0: raise on mistralai//anthropic/

    # Capability-aware ordering: order the panel so the MORE capable model is
    # tried first (cost is equal on the free tier), using reliability as the
    # tiebreaker. Uses the ONE ordering owner; degrades to the caller's order
    # when capability data is unavailable or ordering empties the pool (e.g.
    # all hard-gated out). call_lane="panel" distinguishes this internal
    # request from an engine's apply-lane request.
    ordered, _profiles = ordered_pool(
        panel_pool, governor=governor, ledger=ledger,
        task="structured" if run_convergence else "default",
        free_tier=bool(free_tier), call_lane="panel")
    if _profiles is not None:
        panel_pool = ordered

    # Rotate out any org-prefix previously observed routing via BYOK (paid).
    panel_pool = [m_ for m_ in panel_pool if not governor.learned_blocked(m_)]
    judge_blocked = governor.learned_blocked(judge)
    if not panel_pool:
        raise HarnessError("no available panel models after BYOK filtering")
    target = max(1, min(int(max_panelists), len(panel_pool)))

    judge_max_tokens = max(768, max_tokens + 200)
    # Reserve for every candidate, bounded 429 retries, and provider reasoning
    # fallbacks. A malformed/rate-limited member may consume a call before a
    # replacement fills its slot; a reasoning rejection may consume a fallback
    # request before the same logical call succeeds.
    calls = []
    for m_ in panel_pool:
        slots = _chat_reservation_slots(m_, reasoning_effort, MAX_429_RETRIES)
        for i in range(slots):
            calls.append((f"{m_} (panel attempt {i + 1}/{slots})", m_, panel_tokens, 0))
    judge_slots = _chat_reservation_slots(judge, reasoning_effort)
    for i in range(judge_slots):
        calls.append((f"{judge} (judge attempt {i + 1}/{judge_slots})", judge,
                      judge_max_tokens, target * panel_tokens + 100))
    if run_convergence:
        spec_slots = _chat_reservation_slots(spec_model, reasoning_effort)
        for i in range(spec_slots):
            calls.append((f"{spec_model} (convergence attempt {i + 1}/{spec_slots})",
                          spec_model, judge_max_tokens, target * panel_tokens + 100))
    total_estimate, breakdown = governor.preflight(prompt, calls)
    eprint("[preflight] worst-case cost breakdown:")
    for label, model, cost in breakdown:
        eprint(f"  {label}: ${cost:.6f}")
    eprint(f"[preflight] TOTAL worst-case: ${total_estimate:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    def _run_panel_slot(model):
        # One panel seat: call, gate, bill, ledger. Thread-safe: the only
        # shared mutable state (governor spend, ledger appends) locks.
        if cancel_check and cancel_check():
            from .errors import ToolCancelled
            raise ToolCancelled()
        eprint(f"[panel] calling {model} ...")
        t0 = time.time()
        retry_count = 0
        response_cost = 0.0
        response_cost_recorded = False
        while True:
            status, resp = chat(transport, api_key, model,
                                [{"role": "user", "content": prompt}], panel_tokens,
                                reasoning_effort, reasoning_token_budget, governor)
            if cancel_check and cancel_check():
                from .errors import ToolCancelled
                raise ToolCancelled()
            response_cost = _reported_cost(resp)
            response_cost_recorded = False
            if status != 429 or retry_count >= MAX_429_RETRIES:
                break
            if response_cost:
                governor.record_actual(response_cost, f"{model} (429 retry)")
            response_cost_recorded = True
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="panel",
                              model=model, task_type="structured" if run_convergence else "panel",
                              json_expected=run_convergence,
                              json_ok=False if run_convergence else None,
                              status="error", cost=response_cost, retry=True)
            retry_count += 1
            eprint(f"[panel] {model} rate-limited; bounded retry {retry_count}/{MAX_429_RETRIES}.")
            delay = RETRY_429_BACKOFF_SECONDS * retry_count
            if cancel_check:
                if cancel_check():
                    from .errors import ToolCancelled
                    raise ToolCancelled()
                time.sleep(min(delay, 0.1))
                if delay > 0.1:
                    time.sleep(delay - 0.1)
            else:
                time.sleep(delay)
        elapsed = time.time() - t0
        if status != 200:
            err = resp.get("error", {}).get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            cost = response_cost
            if cost and not response_cost_recorded:
                governor.record_actual(cost, model)
            panel_failures.append({"model": model, "reason": err,
                                   "status": status, "cost": cost,
                                    "retries": retry_count})
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="panel",
                              model=model,
                              task_type="structured" if run_convergence else "panel",
                              json_expected=run_convergence,
                              json_ok=False if run_convergence else None,
                              status="error", cost=cost, retries=retry_count)
            eprint(f"[panel] {model} FAILED ({status}): {err} -- rotating to next model.")
            return None
        content, finish_reason, cost, is_byok = extract_content_and_cost(resp)
        paid_byok = bool(is_byok and not governor.is_free(model))
        if paid_byok:
            governor.record_byok(model)
            panel_failures.append({"model": model, "reason": "paid BYOK route", "status": "byok",
                                   "cost": 0.0, "reported_cost": cost})
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="panel",
                              model=model, task_type="structured" if run_convergence else "panel",
                              json_expected=run_convergence, json_ok=False if run_convergence else None,
                              status="error", cost=0.0, reported_cost=cost)
            eprint(f"[panel] {model} is BYOK-routed (paid); recorded and rotating.")
            return None
        usable, unusable = assess_output(content, allow_truncated=True)
        if not usable:
            # A successful HTTP status is not a panel vote. Empty and
            # reasoning-only bodies are protocol conditions, not content: the
            # judge never sees them and convergence cannot count them as
            # participation. (Truncation is tolerated in prose mode with a
            # warning below; structured mode rejects it via valid_claims.)
            governor.record_actual(cost, model)
            panel_failures.append({"model": model, "reason": unusable,
                                    "status": "invalid_output", "cost": cost,
                                    "retries": retry_count})
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="panel",
                              model=model,
                              task_type="structured" if run_convergence else "panel",
                              json_expected=run_convergence,
                              json_ok=False if run_convergence else None,
                              status="error", cost=cost, retries=retry_count)
            eprint(f"[panel] {model} returned {unusable} -- rotating to next model.")
            return None
        governor.record_actual(cost, model)
        eprint(f"[panel] {model}: cost=${cost:.6f}, finish_reason={finish_reason}, {elapsed:.1f}s")
        valid_claims = (bool(extract_claim_verdicts(content)) and
                        finish_reason != "length") if run_convergence else True
        if run_convergence and not valid_claims:
            panel_failures.append({"model": model, "reason": "malformed, missing, or truncated per-claim JSON",
                                    "status": "invalid_output", "cost": cost,
                                    "retries": retry_count})
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="panel",
                              model=model, task_type="structured", json_expected=True,
                              json_ok=False, status="error", cost=cost)
            eprint(f"[panel] {model} returned malformed/missing claim JSON -- rotating.")
            return None
        if finish_reason == "length":
            eprint(f"[panel] WARNING: {model} truncated by --max-tokens.")
        return {"ok": True, "result": {
            "model": model, "content": content, "finish_reason": finish_reason,
            "cost": cost, "truncated": finish_reason == "length",
        }, "valid_claims": valid_claims, "cost": cost, "retries": retry_count}

    # Fan out up to `target` seats at once when the transport is safe for
    # concurrent POSTs (audit #10 latency fix); hermetic fakes opt out via
    # the same attribute so their canned ordering stays deterministic.
    _parallel = bool(getattr(transport, 'parallel_safe', False))
    panel_results = []
    panel_failures = []
    candidates = iter(panel_pool)
    tried = 0
    _futures = set()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=target if _parallel else 1) as _pool:
        for _ in range(target):
            _first = next(candidates, None)
            if _first is None:
                break
            tried += 1
            _futures.add(_pool.submit(_run_panel_slot, _first))
        while _futures and len(panel_results) < target:
            _done, _futures = concurrent.futures.wait(
                _futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for _fut in _done:
                _slot = _fut.result()
                if _slot is None:
                    if len(panel_results) < target:
                        _next_model = next(candidates, None)
                        if _next_model is not None:
                            tried += 1
                            _futures.add(_pool.submit(_run_panel_slot, _next_model))
                    continue
                if _slot["ok"]:
                    panel_results.append(_slot["result"])
                    if ledger and task_id:
                        ledger.append(
                            "model_result", task_id=task_id, event_note="panel",
                            model=_slot["result"]["model"],
                            task_type="structured" if run_convergence else "panel",
                            json_expected=run_convergence,
                            json_ok=_slot["valid_claims"] if run_convergence else None,
                            status="ok", cost=_slot["cost"], retries=_slot["retries"])
                else:
                    panel_failures.append(_slot["failure"])
                    if len(panel_results) < target:
                        _next_model = next(candidates, None)
                        if _next_model is not None:
                            tried += 1
                            _futures.add(_pool.submit(_run_panel_slot, _next_model))
    if not panel_results:
        raise HarnessError("all panel calls failed. Aborting.")

    # Context-budget guard (#14b): untruncated votes are a fidelity win but a
    # context hazard. Cap the assembled prompt at the judge model's usable
    # window when known, trimming the OLDEST panel contributions first (the
    # newest votes carry the most reliable verdicts) and never silently -- the
    # guard always reports what it dropped.
    # Context-budget guard: trim a COPY for the judge prompt, never the
    # evidence itself. The deterministic tally (below) always counts FULL
    # votes, so a resource-cap trim reports as trimmed_for_judge -- never
    # as a transport panel_shortfall, which means peers actually failed.
    judge_results = list(panel_results)
    trimmed_for_judge = []
    judge_ctx = None
    try:
        if _profiles and judge in _profiles:
            judge_ctx = _profiles[judge].max_source_tokens
    except AttributeError:
        judge_ctx = None
    if judge_ctx:
        available = max(0, judge_ctx - estimate_prompt_tokens(prompt) - judge_max_tokens)
        budget = int(available * 0.9)
        keep = list(reversed(judge_results))
        dropped = []
        used = 0
        trimmed = []
        for r in keep:
            t = estimate_prompt_tokens(r["content"])
            if used + t > budget and trimmed:
                dropped.append(r["model"])
                continue
            used += t
            trimmed.append(r)
        if dropped:
            eprint(f"[judge] context budget {budget} tokens: dropped oldest votes "
                   f"from {dropped} to stay within {judge}'s window.")
        judge_results = list(reversed(trimmed))
        trimmed_for_judge = dropped

    judge_prompt = (
        f"{len(judge_results)} independent models were asked the same question. Synthesize "
        f"their answers. Respond with a SINGLE JSON object and nothing else:\n"
        f"{{\"verdict\": \"<clear final recommendation>\", "
        f"\"agreement\": \"high\"|\"medium\"|\"low\"|\"none\", "
        f"\"confidence\": <0.0 to 1.0>, "
        f"\"disagreements\": [\"<each point where models disagree>\"], "
        f"\"defer\": true|false}}\n"
        f"Set defer=true when the panel cannot reach enough agreement to make a reliable call "
        f"(the work should be deferred rather than guessed). Do not paper over disagreement.\n\n")
    if trimmed_for_judge:
        judge_prompt += (
            f"[NOTE: votes from {', '.join(trimmed_for_judge)} were omitted "
            f"for context budget; synthesize from the votes shown.]\n\n")
    for r in judge_results:
        # Never truncate: panel verdicts are structured claims the judge must
        # weigh in full, and the preflight reserve covers their worst case.
        note = " [NOTE: cut off by token limit, may be incomplete]" if r["truncated"] else ""
        judge_prompt += f"--- Model: {r['model']}{note} ---\n{r['content']}\n\n"

    judge_content = None
    judge_cost = 0.0
    judge_synthesis_status = "not_run"
    if judge_blocked:
        judge_synthesis_status = "byok_blocked"
        eprint(f"[judge] {judge} routes via paid BYOK on this account; raw panel outputs only.")
    else:
        eprint(f"[judge] calling {judge} ...")
        status, resp = chat(transport, api_key, judge,
                            [{"role": "user", "content": judge_prompt}], judge_max_tokens,
                            reasoning_effort, reasoning_token_budget, governor)
        if status != 200:
            judge_synthesis_status = f"http_{status}"
            err = resp.get("error", {}).get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            judge_cost = _reported_cost(resp)
            if judge_cost:
                governor.record_actual(judge_cost, judge)
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="judge",
                              model=judge,
                              task_type="structured" if run_convergence else "judge",
                              json_expected=True, json_ok=False, status="error",
                              cost=judge_cost, synthesis_status=judge_synthesis_status)
            eprint(f"[judge] FAILED ({status}): {err} -- raw panel outputs only.")
        else:
            raw_judge, _, judge_cost, is_byok = extract_content_and_cost(resp)
            paid_byok = bool(is_byok and not governor.is_free(judge))
            if paid_byok:
                governor.record_byok(judge)
                judge_synthesis_status = "byok_blocked"
                eprint("[judge] BYOK-routed (paid); raw panel outputs only.")
            else:
                governor.record_actual(judge_cost, judge)
                if raw_judge and raw_judge.startswith(REASONING_FALLBACK_PREFIX):
                    judge_synthesis_status = "reasoning_only"
                    judge_content = None
                else:
                    judge_content = raw_judge
                    judge_synthesis_status = "parseable" if _extract_json(raw_judge) is not None else "unparseable"
            eprint(f"[judge] synthesis status: {judge_synthesis_status}")
            if ledger and task_id:
                ledger.append("model_result", task_id=task_id, event_note="judge",
                              model=judge, task_type="structured" if run_convergence else "judge",
                              json_expected=True,
                              json_ok=judge_synthesis_status == "parseable",
                              status="ok" if judge_synthesis_status == "parseable" else "error",
                              cost=0.0 if paid_byok else judge_cost,
                              synthesis_status=judge_synthesis_status)

    consensus = _parse_consensus(judge_content) if judge_content else {
        "agreement": "unknown", "confidence": None, "disagreements": [],
        "defer": True, "verdict": "[raw panel outputs only -- no synthesis available]",
    }

    # Optional structured-claims convergence step: a dedicated specialist
    # renders the final verdict from the panel's per-claim JSON (defaults to the
    # judge model), and the deterministic tally gives the ground-truth 5/5 rate.
    convergence_spec = None
    convergence_tally = None
    if run_convergence:
        convergence_tally = tally_convergence(panel_results, claim_polarity=claim_polarity,
                                              of_panel=target)
        spec = run_convergence_specialist(
            transport, api_key, governor, panel_results, spec_model,
            max_tokens=judge_max_tokens, reasoning_effort=reasoning_effort,
            reasoning_token_budget=reasoning_token_budget, ledger=ledger,
            task_id=task_id, fallback_pool=specialist_pool,
            claim_polarity=claim_polarity, profiles=_profiles)
        spec["tally"] = convergence_tally
        # The deterministic tally owns structured convergence. Responder
        # agreement and merge-gate eligibility are separate signals: a short
        # panel may be unanimously aligned while still being ineligible to
        # approve the task. This avoids reporting a transport shortfall as
        # model disagreement.
        if convergence_tally.get("panel_shortfall"):
            # Live finding: the specialist block on a 1-of-3 panel read as
            # 'converged: true, agreement: high' right next to a tally that
            # said converged: false -- two conflicting verdicts in one
            # report. Disclose the coverage gap inside the specialist's own
            # consensus object so it cannot be read as coverage-complete.
            shortfall = convergence_tally.get("shortfall") or {}
            note = (
                "panel shortfall: {voted_by} of {of_panel} slots voted; "
                "this consensus is NOT coverage-complete and must not "
                "authorize a task on its own").format(**shortfall)
            if isinstance(spec.get("specialist"), dict):
                spec["specialist"]["note"] = note
            else:
                spec["note"] = note
        if convergence_tally["responder_converged"]:
            consensus["agreement"] = "high"
            consensus["confidence"] = convergence_tally["convergence_rate"] or 0.0
        else:
            consensus["agreement"] = "low" if convergence_tally["disagreement"] else "unknown"
            consensus["confidence"] = convergence_tally["convergence_rate"] or 0.0
        consensus["defer"] = not convergence_tally["converged"]
        consensus["panel_shortfall"] = convergence_tally["panel_shortfall"]
        consensus["missing_votes"] = convergence_tally["missing_votes"]
        consensus["voted_by"] = convergence_tally["voted_by"]
        consensus["of_panel"] = convergence_tally["of_panel"]
        consensus["responder_converged"] = convergence_tally["responder_converged"]
        consensus["gate_converged"] = convergence_tally["converged"]
        consensus["defer_reason"] = ("panel_shortfall" if convergence_tally["panel_shortfall"]
                                      else "responder_disagreement" if convergence_tally["disagreement"]
                                      else None)
        # In structured mode, disagreements are claim-level facts, not the
        # judge's free-form severity/prose list. A shortfall is reported
        # separately and must not be mislabeled as disagreement.
        consensus["disagreements"] = list(convergence_tally["disagreement_claims"])
        # Do not let a judge's prose become the authoritative verdict for a
        # structured audit; it can be absent or semantically inverted. Keep it
        # in judge_synthesis, but expose a deterministic claim summary instead.
        consensus["judge_verdict"] = consensus.get("verdict", "")
        summary = []
        for cid, entry in convergence_tally["claims"].items():
            summary.append(f"{cid}={entry['verdict']} ({entry['voted_by']}/{entry['of_panel']})")
        if convergence_tally["panel_shortfall"]:
            s = convergence_tally["shortfall"]
            summary.append(f"panel shortfall {s['voted_by']}/{s['of_panel']}; merge gate deferred")
        consensus["verdict"] = "Deterministic panel tally: " + ("; ".join(summary) or "no defect claims")
        convergence_spec = spec

    # Print only after every planned call, including the optional specialist,
    # has settled so the human-facing total agrees with the returned result and
    # ledger evidence.
    eprint(f"\n[TOTAL] actual cost this run: ${governor.spent:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")

    consensus_payload = {k: consensus[k] for k in
                         ("agreement", "confidence", "disagreements", "defer")}
    if convergence_tally is not None:
        consensus_payload.update({
            "panel_shortfall": convergence_tally["panel_shortfall"],
            "missing_votes": convergence_tally["missing_votes"],
            "voted_by": convergence_tally["voted_by"],
            "of_panel": convergence_tally["of_panel"],
            "responder_converged": convergence_tally["responder_converged"],
            "gate_converged": convergence_tally["converged"],
            "defer_reason": consensus.get("defer_reason"),
            "judge_verdict": consensus.get("judge_verdict"),
        })
    result = {
        "panel_results": panel_results,
        "trimmed_for_judge": trimmed_for_judge,
        "panel_failures": panel_failures,
        "required_panelists": target,
        "panel_tried": tried,
        "judge_model": judge,
        "judge_synthesis": judge_content,
        "judge_synthesis_status": judge_synthesis_status,
        "verdict": consensus["verdict"],
        "consensus": consensus_payload,
        "estimated_worst_case_cost": total_estimate,
        "actual_cost": governor.spent,
        "max_cost_ceiling": governor.max_cost,
    }
    if convergence_spec is not None:
        result["convergence"] = convergence_spec
    if ledger and task_id:
        ledger.append("complete", task_id=task_id, event_note="panel_judge",
                      model=judge, session_spent=governor.spent, status="ok",
                      agreement=consensus["agreement"],
                      voted_by=convergence_tally.get("voted_by") if convergence_tally else None,
                      of_panel=convergence_tally.get("of_panel") if convergence_tally else None)
    return result
