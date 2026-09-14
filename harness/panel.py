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
                   looks_truncated, REASONING_FALLBACK_PREFIX)
from .capability import ordered_pool
from .config import DEFAULT_MAX_TOKENS
from . import events as _events
from .convergence import (DEFAULT_CONVERGENCE_PANEL_TOKENS, MAX_429_RETRIES,
                          RETRY_429_BACKOFF_SECONDS, _parse_consensus,
                          extract_claim_verdicts, run_convergence_specialist,
                          tally_convergence)
from .errors import HarnessError
from .output import eprint
from .tokens import estimate_prompt_tokens


def _judge_fallback_candidates(pool, judge, governor, *, exclude=()):
    """Judge-rotation candidates from the panel pool, in pool order.

    ONE predicate for two consumers: the preflight reserve (one judge-sized
    call per candidate keeps the worst-case ceiling a guarantee) and the
    runtime rotation. exclude drops models that may not be called again
    (panelists who already voted or failed a seat). Free-only keeps the
    reserve $0 on free pools and keeps paid members single-attempt.
    """
    blocked = set(exclude)
    return [m_ for m_ in pool
            if m_ != judge and m_ not in blocked
            and not governor.learned_blocked(m_) and governor.is_free(m_)]


def _judge_fallback_reserve(pool, judge, governor, judge_max_tokens, extra_tokens):
    """Preflight reserve rows for the judge rotation (same predicate)."""
    return [(f"{c} (judge fallback reserve)", c, judge_max_tokens, extra_tokens)
            for c in _judge_fallback_candidates(pool, judge, governor)]


def _run_judge_attempt(*, transport, api_key, governor, judge_prompt, model,
                       judge_max_tokens, reasoning_effort,
                       reasoning_token_budget, task_id, ledger, task_type,
                       cancel_check=None, retry=False):
    """One judge-seat attempt: call, classify, bill, ledger, emit.

    The ONE owner of attempt mechanics, shared by the primary seat, the
    bounded transient retry, and fallback rotation. Returns
    ``(status_note, content, cost)`` where status_note is one of
    ``parseable`` (content is the verdict body), ``byok_blocked``,
    ``reasoning_only``, ``truncated``, ``unparseable``, or ``http_<code>``.
    Content is the raw body for any HTTP 200 (evidence survives in the
    envelope even when unparseable); reasoning-only and HTTP failures return
    None. BYOK-routed paid responses are recorded via record_byok only
    (their spend is invisible to the tracked key).
    """
    if cancel_check and cancel_check():
        from .errors import ToolCancelled
        raise ToolCancelled()
    status, resp = chat(transport, api_key, model,
                        [{"role": "user", "content": judge_prompt}],
                        judge_max_tokens, reasoning_effort,
                        reasoning_token_budget, governor)
    cost = _reported_cost(resp)
    err = (resp.get("error", {}).get("message", str(resp))
           if isinstance(resp, dict) else str(resp))
    note = f"http_{status}" if status != 200 else "unparseable"
    content = None
    paid_byok = False
    if status == 200:
        content, _, cost, is_byok = extract_content_and_cost(resp)
        paid_byok = bool(is_byok and not governor.is_free(model))
        if paid_byok:
            governor.record_byok(model)
            note = "byok_blocked"
        elif content and content.startswith(REASONING_FALLBACK_PREFIX):
            note = "reasoning_only"
            content = None
        else:
            note = ("parseable" if _extract_json(content) is not None
                    else ("truncated" if looks_truncated(content)
                          else "unparseable"))
    if cost and not paid_byok:
        governor.record_actual(cost, model)
    if ledger and task_id:
        fields = dict(event_note="judge", model=model, task_type=task_type,
                      json_expected=True, json_ok=note == "parseable",
                      status="ok" if note == "parseable" else "error",
                      cost=0.0 if paid_byok else cost,
                      synthesis_status=note)
        if retry:
            fields["retry"] = True
        ledger.append("model_result", task_id=task_id, **fields)
    eprint(f"[judge] {model}: {note}" + (f" ({err})" if status != 200 else ""))
    _events.emit("judge_result", task_id=task_id, model=model,
                 status=note, cost=cost,
                 **({"error": err} if status != 200 else {}))
    return note, content, cost


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
    # Judge fallback reserve (P0 handoff fix): a judge seat with no second
    # attempt turned one bad judge body (truncation, http_502, reasoning-only)
    # into a lost verdict despite a converged panel -- three proven modes in
    # the SCMessenger handoff. Same predicate the rotation uses, so the
    # worst-case ceiling always covers every call rotation can make.
    calls.extend(_judge_fallback_reserve(
        panel_pool, judge, governor, judge_max_tokens,
        target * panel_tokens + 100))
    if run_convergence:
        spec_slots = _chat_reservation_slots(spec_model, reasoning_effort)
        for i in range(spec_slots):
            calls.append((f"{spec_model} (convergence attempt {i + 1}/{spec_slots})",
                          spec_model, judge_max_tokens, target * panel_tokens + 100))
    total_estimate, breakdown = governor.preflight(prompt, calls)
    _events.emit("preflight", task_id=task_id, lane="panel",
                 worst_case=total_estimate, ceiling=governor.max_cost,
                 calls=[{"label": label, "model": model, "cost": cost}
                        for label, model, cost in breakdown])
    eprint("[preflight] worst-case cost breakdown:")
    for label, _model, cost in breakdown:
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
        _events.emit("panel_call", task_id=task_id, model=model,
                     structured=run_convergence)
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
            _events.emit("rotation", task_id=task_id, model=model,
                         reason="rate_limited", retry=retry_count)
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
            _events.emit("rotation", task_id=task_id, model=model,
                         reason="http_error", http_status=status, error=err,
                         retries=retry_count)
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
            _events.emit("rotation", task_id=task_id, model=model,
                         reason="paid_byok", reported_cost=cost)
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
            _events.emit("rotation", task_id=task_id, model=model,
                         reason="unusable_output", detail=unusable)
            return None
        governor.record_actual(cost, model)
        eprint(f"[panel] {model}: cost=${cost:.6f}, finish_reason={finish_reason}, {elapsed:.1f}s")
        _events.emit("panel_vote", task_id=task_id, model=model, cost=cost,
                     finish_reason=finish_reason, elapsed_s=round(elapsed, 3),
                     truncated=finish_reason == "length")
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
            _events.emit("rotation", task_id=task_id, model=model,
                         reason="malformed_claims")
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
    # Report votes in pool order regardless of completion order: with a fast
    # (or sequential) transport several futures can be done before the first
    # wait(), and _done's set order would otherwise decide which vote is
    # "oldest" for the trim below. Pool order is the defined order; on every
    # interpreter, both fan-out modes.
    _pool_order = {m_: i for i, m_ in enumerate(panel_pool)}
    panel_results.sort(key=lambda r: _pool_order.get(r["model"], len(_pool_order)))

    # Context-budget guard (#14b): cap the assembled prompt at the judge
    # model's usable window when known, trimming the OLDEST panel
    # contributions first (newest votes are the most reliable), and never
    # silently. Trim a COPY: the tally below always counts FULL votes, so
    # a resource-cap trim reports as trimmed_for_judge -- never as a
    # transport panel_shortfall, which means peers actually failed.
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
            _events.emit("judge_trim", task_id=task_id, dropped=dropped,
                         budget_tokens=budget)
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
    # P0 handoff fix: a judge seat with no rotation turned one bad body into a
    # lost verdict. Candidates come from the shared predicate (the preflight
    # reserve already covers every call the loop below can make); the primary
    # seat also gets a bounded transient retry. BYOK judges stay
    # single-attempt.
    judge_task_type = "structured" if run_convergence else "judge"
    _judge_candidates = [judge]
    if not judge_blocked:
        _judge_candidates.extend(_judge_fallback_candidates(
            panel_pool, judge, governor,
            exclude={r.get("model") for r in panel_results}
            | {f.get("model") for f in panel_failures}))
    judge_content = None
    judge_cost = 0.0
    judge_synthesis_status = "not_run"
    _attempt_kw = dict(transport=transport, api_key=api_key, governor=governor,
                       judge_prompt=judge_prompt,
                       judge_max_tokens=judge_max_tokens,
                       reasoning_effort=reasoning_effort,
                       reasoning_token_budget=reasoning_token_budget,
                       task_id=task_id, ledger=ledger,
                       task_type=judge_task_type, cancel_check=cancel_check)
    if judge_blocked:
        judge_synthesis_status = "byok_blocked"
        eprint(f"[judge] {judge} routes via paid BYOK on this account; raw panel outputs only.")
    else:
        eprint(f"[judge] calling {judge} ...")
        _events.emit("judge_call", task_id=task_id, model=judge,
                     structured=run_convergence)
        note, content, judge_cost = _run_judge_attempt(model=judge, **_attempt_kw)
        code = int(note[5:]) if note.startswith("http_") else 0
        if code >= 500 or code in (408, 429):
            # One bounded same-seat retry on transient provider errors
            # (round7's http_502 lost a verdict one retry would have saved).
            eprint("[judge] transient provider error; one bounded retry.")
            _events.emit("rotation", task_id=task_id, model=judge,
                         reason="judge_transient_retry", note=note)
            note, content, judge_cost = _run_judge_attempt(
                model=judge, retry=True, **_attempt_kw)
        # Any HTTP-200 body is kept as envelope evidence, even when
        # unparseable/truncated (the Sep-11 artifact's honesty contract).
        judge_content = content
        judge_synthesis_status = note

    # Judge fallback rotation (P0 handoff fix): a reasoning-only, truncated,
    # unparseable, or failed primary seat must not discard a converged panel's
    # evidence. Same candidate predicate the preflight reserve used, so the
    # ceiling guarantee covers every call this loop can make.
    for _cand in _judge_candidates[1:]:
        if judge_synthesis_status in ("parseable", "byok_blocked", "not_run"):
            break
        eprint(f"[judge] rotating to fallback candidate {_cand} ...")
        _events.emit("rotation", task_id=task_id, model=_cand,
                     reason="judge_fallback", prior_status=judge_synthesis_status)
        note, content, judge_cost = _run_judge_attempt(model=_cand, **_attempt_kw)
        judge = _cand
        judge_synthesis_status = note
        judge_content = content  # evidence preserved on any HTTP-200 attempt
        if note == "parseable":
            break

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
        # Specialist prose can invert polarity or disagree with the tally.
        # Record conflicts so operators never trust specialist claim maps over
        # the deterministic majority.
        if isinstance(spec.get("specialist"), dict):
            spec_claims = spec["specialist"].get("claims") or {}
            conflicts = []
            for cid, entry in (convergence_tally.get("claims") or {}).items():
                sc = spec_claims.get(cid)
                if not isinstance(sc, dict):
                    continue
                tally_v = entry.get("verdict")
                spec_v = sc.get("verdict")
                if tally_v and spec_v and tally_v != spec_v:
                    conflicts.append({
                        "claim": cid,
                        "tally": tally_v,
                        "specialist": spec_v,
                        "tally_votes": f"{entry.get('real_votes')}R/"
                                       f"{entry.get('not_real_votes')}NR",
                    })
            if conflicts:
                spec["specialist"]["tally_conflicts"] = conflicts
                spec["specialist"]["note"] = (
                    (spec["specialist"].get("note") + " " if spec["specialist"].get("note") else "")
                    + "specialist claim map disagrees with the deterministic tally; "
                      "tally is authoritative")
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
            # Participation is (voted_by/of_panel). Unanimity is a separate
            # fact -- never print "3/3" as if it meant all agreed.
            real_n = entry.get("real_votes")
            nr_n = entry.get("not_real_votes")
            if real_n is not None and nr_n is not None:
                vote_note = f"{real_n}R/{nr_n}NR of {entry['voted_by']}"
            else:
                vote_note = f"{entry['voted_by']}/{entry['of_panel']}"
            summary.append(f"{cid}={entry['verdict']} ({vote_note})")
        if convergence_tally["panel_shortfall"]:
            s = convergence_tally["shortfall"]
            summary.append(
                f"SHORTFALL {s['voted_by']}/{s['of_panel']} slots voted; "
                "merge gate deferred")
        consensus["verdict"] = "Deterministic panel tally: " + ("; ".join(summary) or "no defect claims")
        # Structured-lane demotion evidence: a model that repeatedly votes in
        # the minority on defect claims (lone dissenter) is a routing signal.
        # One event is a strike; order_pool demotes at two (same policy as
        # unusable_outputs).
        if ledger and task_id:
            for cid, entry in (convergence_tally.get("claims") or {}).items():
                for model in (entry.get("minority_models") or []):
                    ledger.append(
                        "model_result", task_id=task_id, model=model,
                        task_type="structured", json_expected=True,
                        json_ok=True, status="ok",
                        event_note="panel_minority_dissent",
                        reason=f"minority dissent on {cid} "
                               f"({entry.get('real_votes')}R/"
                               f"{entry.get('not_real_votes')}NR)",
                        minority_dissent=True)
        convergence_spec = spec

    # Print only after every planned call, including the optional specialist,
    # has settled so the human-facing total agrees with the returned result and
    # ledger evidence.
    eprint(f"\n[TOTAL] actual cost this run: ${governor.spent:.6f} "
           f"(ceiling: ${governor.max_cost:.6f})")
    _events.emit("spend_check", task_id=task_id, lane="panel",
                 spent=governor.spent, ceiling=governor.max_cost)

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
    # Surface the specialist's escalation/plan directives on the consensus
    # payload so callers can consume them without digging into the nested
    # specialist blob (verify-lane telemetry for the apply ladder).
    if isinstance(convergence_spec, dict):
        spec_body = convergence_spec.get("specialist")
        if isinstance(spec_body, dict):
            if "escalation" in spec_body:
                consensus_payload["escalation"] = spec_body.get("escalation")
            if "plan" in spec_body:
                consensus_payload["plan"] = spec_body.get("plan")
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
