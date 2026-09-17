"""Plan-confirmation waist (M2) and LLM decomposition lane (M1).

The wide base plans (heuristic, or a cheap model via
``dag.decompose_via_llm``); the waist confirms or repairs the plan with ONE
frontier model round-trip budget before any execution spend; execution then
honors the confirmed routing (``dag.node_apply_kwargs``).

Cost discipline: the frontier's brief (file signatures + bounded source
windows) is its ONLY repo access -- bounded ``request_windows`` round-trips
replace open-ended reading, capped at ``MAX_WAIST_ROUNDS``. Every call is
preflighted and billed through the run's ONE governor (the caller's, so
decomposition + confirmation + execution share a single ceiling). Every
terminal verdict is ledgered with its round count and cost.

Sovereignty: the confirming model can refuse -- and a refusal is evidence,
ledgered, and stops execution (fail-closed: a refused plan never dispatches).
"""
import hashlib
import os
from typing import Any, Dict

from .chat import governed_text
from .condenser import distill_context
from .dag import (build_waist_prompt, parse_waist_verdict, plan_task,
                  decompose_via_llm)
from .errors import HarnessError
from .output import eprint

MAX_WAIST_ROUNDS = 2
MAX_WINDOWS_PER_ROUND = 8
MAX_WINDOW_LINES = 200
WAIST_MAX_TOKENS = 1500
DECOMPOSE_MAX_TOKENS = 1024
MAX_BRIEF_FILES = 12


def plan_task_id(plan_result: Dict[str, Any]) -> str:
    """Deterministic ledger task id for a plan confirmation."""
    digest = hashlib.sha256(
        (plan_result.get("goal") or "").encode("utf-8")).hexdigest()[:12]
    return f"plan_{digest}"


def read_window(path: str, start_line=None, end_line=None) -> str:
    """Bounded source-window read for the brief (cwd-contained).

    Model-chosen paths are contained to the working tree (absolute paths
    and traversal outside the cwd are refused in-band, as an honest
    "refused" note the model can react to); missing files answer in-band
    too -- the window round-trip stays cheap and non-fatal.
    """
    raw = str(path)
    refused = "WINDOW REFUSED: only paths inside the working tree are readable"
    if os.path.isabs(raw) or ".." in raw.split(os.sep) or ".." in raw.split("/"):
        return refused
    root = os.path.realpath(os.getcwd())
    full = os.path.realpath(os.path.join(root, raw))
    if full != root and not full.startswith(root + os.sep):
        return refused
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return f"FILE NOT FOUND: {raw}"
    total = len(lines)
    start = max(int(start_line or 1), 1)
    if start > total:
        return f"WINDOW EMPTY: {raw} has {total} lines (requested start {start})"
    end = min(int(end_line or total), total, start + MAX_WINDOW_LINES - 1)
    body = "".join(lines[start - 1:end])
    return f"--- WINDOW: {raw} lines {start}-{end} of {total} ---\n{body}"


def _brief_for(plan_result: Dict[str, Any]) -> str:
    """Condensed signatures for the planned target files (the ONE brief)."""
    files: Dict[str, str] = {}
    for node in plan_result.get("nodes", []):
        for path in node.get("target_files", ()):
            if path in files or len(files) >= MAX_BRIEF_FILES:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    files[path] = f.read()
            except OSError:
                continue
    if not files:
        return "(no readable target files; the plan names files that do not exist yet)"
    brief = distill_context(files, summary=plan_result.get("goal", ""))
    return brief.to_prompt_context()


def _verdict_cost(governor, spent_before: float) -> float:
    try:
        return round(governor.spent - spent_before, 6)
    except AttributeError:
        return 0.0


def confirm_plan(*, transport, api_key, governor, ledger, plan_result,
                 model, use_free=True, custom_frontier=None,
                 chat_fn=None, reader=read_window,
                 max_rounds=MAX_WAIST_ROUNDS) -> Dict[str, Any]:
    """Run the waist: frontier confirms/repairs the plan; return the plan.

    Returns the (possibly amended) plan_result with a ``confirmation``
    field, or a ``status: "refused"`` envelope when the model refuses
    (callers must not execute it -- ``terminal_exit_code`` maps the
    unknown status to 2 fail-closed). Raises HarnessError when the model
    keeps requesting windows past ``max_rounds`` (the plan stays
    unconfirmed and unexecuted), on any transport/parse failure, or when
    ``model`` is unset (confirmation cannot silently skip itself).

    ``chat_fn(prompt) -> (text, cost)`` and ``reader(path, start, end) ->
    str`` are injectable seams for hermetic tests.
    """
    if not model:
        raise HarnessError(
            "waist confirmation requires a frontier model "
            "(--frontier-model or HARNESS_FRONTIER_MODEL)")
    if chat_fn is None:
        def chat_fn(prompt):
            return governed_text(transport, api_key, governor, model, prompt,
                                 WAIST_MAX_TOKENS, label="waist")
    brief = _brief_for(plan_result)
    task_id = plan_task_id(plan_result)
    window_context = ""
    spent_before = governor.spent if governor is not None else 0.0

    for round_no in range(1, max_rounds + 1):
        prompt = build_waist_prompt(plan_result, brief, window_context)
        text, _cost = chat_fn(prompt)
        try:
            verdict = parse_waist_verdict(text)
        except HarnessError:
            # Fail closed on a malformed verdict: the spend already happened,
            # the plan stays unconfirmed, and the raw body is the evidence.
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="unparseable",
                              model=model, round=round_no)
            raise

        kind = verdict["verdict"]
        cost = _verdict_cost(governor, spent_before)
        if kind == "approve":
            plan_result["confirmation"] = {
                "verdict": "approved", "model": model, "rounds": round_no,
                "cost": cost}
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="approved",
                              model=model, rounds=round_no, cost=cost)
            return plan_result
        if kind == "refuse":
            refused = dict(plan_result)
            refused["status"] = "refused"
            refused["confirmation"] = {
                "verdict": "refused", "model": model, "rounds": round_no,
                "reason": verdict["reason"], "evidence": verdict["evidence"],
                "cost": cost}
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="refused",
                              model=model, rounds=round_no, cost=cost,
                              reason=verdict["reason"])
            return refused
        if kind == "amend":
            amended = plan_task(
                plan_result.get("goal", ""),
                custom_frontier=custom_frontier, use_free=use_free,
                decomposed_dag=verdict["dag"])
            amended["confirmation"] = {
                "verdict": "amended", "model": model, "rounds": round_no,
                "cost": cost}
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="amended",
                              model=model, rounds=round_no, cost=cost)
            return amended

        # request_windows: attach the bounded windows and spend another round
        blocks = [reader(req["path"], req.get("start_line"), req.get("end_line"))
                  for req in verdict["file_window_requests"][:MAX_WINDOWS_PER_ROUND]]
        window_context = "\n\n".join(blocks)
        if ledger is not None:
            ledger.append("plan_verdict", task_id=task_id,
                          verdict="request_windows", model=model, round=round_no,
                          windows=len(verdict["file_window_requests"]))
    raise HarnessError(
        f"waist requested file windows beyond the {max_rounds}-round budget; "
        "plan stays unconfirmed and will not execute")


def compose_plan(*, transport, api_key, governor, ledger, opts_goal,
                 candidate_files=None, frontier_model=None, use_free=True,
                 decompose_llm=False, confirm=False, decompose_model=None,
                 chat_fn=None, max_cost=None, keep_going=False, out=None,
                 execute=False) -> Dict[str, Any]:
    """ONE owner of the plan-lane flow (CLI and MCP call this).

    Order: optional cheap-LLM decomposition (M1) -> tier classification ->
    optional waist confirmation (M2). Decomposition failures fall back to
    the heuristic only when ``execute`` is set (the run spends anyway, so a
    loud note + fallback keeps it going); a plan-only preview fails
    loudly -- the operator asked for LLM planning, and silently handing
    back the heuristic plan would be dishonest.
    """
    if (decompose_llm or confirm) and governor is None:
        raise HarnessError("LLM plan features require a governor")

    decomposed = None
    decomposition = "heuristic"
    if decompose_llm:
        if chat_fn is None:
            if not decompose_model:
                scout = resolve_scout_ladder(use_free=use_free,
                                             custom_frontier=frontier_model)
                decompose_model = scout[0]
            def chat_fn(prompt):
                return governed_text(transport, api_key, governor, decompose_model,
                                     prompt, DECOMPOSE_MAX_TOKENS, label="decompose")
        try:
            # chat_fn's contract is (text, cost) -- decomposition consumes
            # the text only; the cost stays on the governor/caller side.
            decomposed = decompose_via_llm(lambda p: chat_fn(p)[0], opts_goal,
                                           candidate_files=candidate_files)
            decomposition = (f"llm:{decompose_model}"
                             if decompose_model else "llm:injected")
        except HarnessError as exc:
            if not execute:
                raise
            eprint(f"[plan] LLM decomposition failed ({exc}); heuristic fallback")

    plan_result = plan_task(
        goal=opts_goal, candidate_files=candidate_files,
        custom_frontier=frontier_model, use_free=use_free,
        decomposed_dag=decomposed)
    plan_result["decomposition"] = decomposition

    if confirm:
        plan_result = confirm_plan(
            transport=transport, api_key=api_key, governor=governor,
            ledger=ledger, plan_result=plan_result, model=frontier_model,
            use_free=use_free, custom_frontier=frontier_model, chat_fn=chat_fn)
    return plan_result


def resolve_scout_ladder(use_free=True, custom_frontier=None):
    """The tier-0 ladder from the same sliding-scale policy that classifies
    nodes -- the decomposition model is simply the cheapest rung, with no
    new routing policy of its own."""
    from .sliding_scale import resolve_sliding_scale_route
    route = resolve_sliding_scale_route(
        instruction="decompose the goal into subtasks",
        target_files=(), dependency_depth=0, is_leaf=True,
        use_free=use_free, custom_frontier=custom_frontier)
    return list(route.ladder)
