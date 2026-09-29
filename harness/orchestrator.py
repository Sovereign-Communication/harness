"""Autonomous orchestration policy.

This module owns the decisions around a planned edit: repository triage,
completion judgment, bounded round progression, and artifact-truth checks.
Execution mechanics remain injected through ``execute_plan`` so the shared
PlanExecutor stays the single owner of reservations, workers, isolation, and
gates.
"""
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .errors import HarnessError, ToolCancelled

MAX_TRIAGE_FILES = 15
MAX_ORCH_ROUNDS = 3

_COMPLETION_PROMPT = """You are the completion judge for an autonomous coding orchestrator.
A goal was broken into subtasks and executed by cheaper models. Decide whether
the goal is now COMPLETE, using only the execution state below.

GOAL:
{goal}

EXECUTION STATE (per-subtask results, verification gates, errors):
{state}

Answer with ONE JSON object, no prose:
{{"complete": true|false, "remaining": "<what is still missing, empty when complete>", "reason": "<one line justification>"}}
Be strict: partial work, failed gates, or unfinished scope mean complete=false.
"""

_TRIAGE_PROMPT = """You are the file-triage pass for an autonomous coding orchestrator.
GOAL:
{goal}

REPOSITORY FILES:
{files}

Pick the files that plausibly need to be read or modified to serve this goal
(new files the goal should create are NOT in this list; name only existing ones).
Answer with ONE JSON object, no prose:
{{"files": ["<path>", ...]}}
At most {max_n} paths, all copied exactly from the repository list above.
"""


def _result_probability(result, key):
    """Read a typed noul probability without treating it as confidence."""
    answers = getattr(result, "answers", None)
    if not isinstance(answers, dict):
        return None
    value = answers.get(key)
    if isinstance(value, dict):
        value = value.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _extract_json_blob(text):
    """The first balanced {...} block in the response, or None."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def assess_context_intake(*, request, retained_brief, jev_policy,
                          token_budget, token_stage_budget, site,
                          threshold=0.7):
    """Fail closed unless a complete native judgment approves retained context.

    The agent, CLI, and MCP lanes share this policy boundary. A heuristic or
    unavailable answer is useful evidence for the envelope, but never permits
    planning or execution.
    """
    names = ("context_relevant", "context_coverage_sufficient",
             "context_conflict_present")
    result = None
    structural = {"native": False, "result_state": "unavailable"}
    if (jev_policy is not None
            and callable(getattr(jev_policy, "evaluate_hourglass_stage", None))):
        try:
            result, structural = jev_policy.evaluate_hourglass_stage(
                "context_intake",
                {"request": request, "retained_brief": retained_brief},
                site=site, token_budget=token_budget,
                token_stage_budget=token_stage_budget)
        except Exception as exc:
            structural = {"native": False, "result_state": "unavailable",
                          "reason": "context intake evaluation failed: "
                                    + type(exc).__name__}
    values = {name: (structural or {}).get(name) for name in names}
    native = (bool((structural or {}).get("native"))
              and not bool(getattr(result, "is_fallback", True)))
    threshold = float(threshold)
    approved = bool(
        native
        and all(isinstance(value, (int, float))
                and not isinstance(value, bool)
                for value in values.values())
        and values["context_relevant"] >= threshold
        and values["context_coverage_sufficient"] >= threshold
        and values["context_conflict_present"] <= 1.0 - threshold)
    reason = None if approved else (
        (structural or {}).get("reason")
        or "context intake unavailable or insufficient; planning and execution stopped")
    evidence = {
        **values,
        "native": native,
        "approved": approved,
        "result_state": (structural or {}).get("result_state"),
        "reason": reason or (structural or {}).get("reason"),
        "cost": float(getattr(result, "cost", 0.0) or 0.0),
    }
    return result, evidence


def assess_completion(goal, state_summary, chat_fn):
    """Return a strict completion verdict, or ``None`` for unusable output."""
    prompt = _COMPLETION_PROMPT.format(goal=goal.strip(),
                                       state=state_summary.strip())
    data = _extract_json_blob(chat_fn(prompt))
    if not isinstance(data, dict) or not isinstance(data.get("complete"), bool):
        return None
    return {
        "complete": data["complete"],
        "remaining": str(data.get("remaining") or "")[:2000],
        "reason": str(data.get("reason") or "")[:500],
    }


def triage_files(goal, files, chat_fn, max_files=MAX_TRIAGE_FILES,
                 jev_policy=None):
    """Select relevant files, validating every model-selected path.

    When ``jev_policy`` is supplied, JEV-P3-triage-files runs through the one
    policy owner (typed noul relevance + listing validation). Unkeyed policy
    and model failure fall through to the keyword heuristic honestly.
    """
    if not files:
        return []
    if jev_policy is not None:
        try:
            result, envelope = jev_policy.evaluate_file_triage(
                goal, files, known_files=files, site="triage-files",
                max_files=max_files)
            picked = list((result.answers or {}).get("files") or
                          envelope.get("files") or [])
            if picked:
                return picked
            if not result.is_fallback:
                # Keyed live answer said nothing relevant — keep empty.
                return []
        except (HarnessError, OSError, ValueError):
            pass
        return keyword_fallback(goal, files, max_files=max_files)
    listing = "\n".join(files[:400])
    prompt = _TRIAGE_PROMPT.format(goal=goal.strip(), files=listing,
                                   max_n=max_files)
    try:
        data = _extract_json_blob(chat_fn(prompt))
    except (HarnessError, OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return []
    known = set(files)
    picked: List[str] = []
    for item in data["files"]:
        path = str(item).strip().replace("\\", "/")
        if path in known and path not in picked:
            picked.append(path)
        if len(picked) >= max_files:
            break
    return picked


def assess_completion_nouls(goal, state_summary, *, jev_policy=None,
                            named_artifacts=None, root_dir=None,
                            token_budget=None):
    """JEV-P3-completion: typed/artifact nouls before the generative judge.

    Missing named artifact is code-owned: ``cannot_complete`` is True and the
    generative judge must not stand as a complete verdict.
    """
    if jev_policy is not None:
        kwargs = {"named_artifacts": named_artifacts,
                  "root_dir": root_dir, "site": "completion"}
        if token_budget is not None:
            kwargs["token_budget"] = token_budget
        result, structural = jev_policy.evaluate_completion_nouls(
            goal, state_summary, **kwargs)
        missing = list(structural.get("missing_artifacts") or [])
        cannot = bool(structural.get("cannot_complete"))
        if cannot:
            remaining = "; ".join(missing) or (
                result.reasons[0] if result.reasons else
                "completion nouls refused goal achievement")
            reason = ("named artifact missing" if missing else
                      "completion nouls did not affirm goal")
            return {
                "cannot_complete": True,
                "missing_artifacts": missing,
                "remaining": remaining,
                "reason": reason,
                "structural": structural,
                "result": result,
            }
        return {
            "cannot_complete": False,
            "missing_artifacts": [],
            "remaining": "",
            "reason": "completion nouls passed",
            "structural": structural,
            "result": result,
            "jev_native": not bool(getattr(result, "is_fallback", True)),
            "jev_supported": _result_probability(result, "goal_achieved"),
        }
    # Pure code-owned path when no policy is attached.
    from .jev_packs import missing_named_artifacts, named_artifact_status
    facts = named_artifacts
    if facts is None:
        facts = named_artifact_status(goal, root_dir=root_dir)
    normalized = []
    for item in facts:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            from pathlib import Path as _Path
            base = _Path(root_dir) if root_dir is not None else _Path.cwd()
            rel = str(item).replace("\\", "/")
            normalized.append({"path": rel,
                               "present": (base / rel).is_file(),
                               "lines": None})
    missing = missing_named_artifacts(normalized)
    return {
        "cannot_complete": bool(missing),
        "missing_artifacts": missing,
        "remaining": "; ".join(f"artifact truth: {p} MISSING"
                                for p in missing),
        "reason": "named artifact missing" if missing else "no named artifacts",
        "structural": None,
        "result": None,
    }


def assess_final_alignment(*, goal, candidate, retained_brief, jev_policy,
                           token_budget=None, threshold=0.99):
    """Compare the exact request, execution facts, and full retained brief.

    The serialized inputs are deterministic and deliberately untruncated;
    JevPolicy receives character caps equal to their actual lengths.
    """
    from .tokens import estimate_prompt_tokens

    request = str(goal)
    answer = json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
    context = json.dumps(retained_brief, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    token_cap = estimate_prompt_tokens(request + answer + context) + 512
    result, structural = jev_policy.evaluate_answer(
        request, answer, context, site="final-alignment",
        max_input_tokens=token_cap, max_request_chars=len(request),
        max_candidate_chars=len(answer), max_context_chars=len(context),
        token_budget=token_budget)
    supported = _result_probability(result, "answer_sufficient")
    native = bool(structural.get("native")) and not bool(
        getattr(result, "is_fallback", True))
    aligned = bool(native and supported is not None
                   and supported >= float(threshold)
                   and not bool((result.answers or {}).get("iteration_required"))
                   and not bool((result.answers or {}).get("plan_required")))
    return result, {
        "native": native, "supported": supported, "aligned": aligned,
        "threshold": float(threshold),
        "iteration_required": (result.answers or {}).get("iteration_required"),
        "plan_required": (result.answers or {}).get("plan_required"),
        "structural": structural,
    }


def _work_fingerprint(node) -> Tuple[str, Tuple[str, ...], Optional[str]]:
    """Identity for already successful work, independent of a model's node id."""
    instruction = " ".join(str(node.instruction or "").split())
    targets = tuple(sorted(str(path).replace("\\", "/").strip()
                           for path in (node.target_files or ())))
    gate = (" ".join(str(node.local_gate or "").split()) or None)
    return instruction, targets, gate


def _amendment_goal(request):
    """Keep an alignment amendment target-aware and bounded for the planner."""
    facts = {
        "restart_target": request.get("target"),
        "original_request": request.get("original_request"),
        "alignment": request.get("alignment"),
        "completed_stages": request.get("completed_stages"),
        "prior_results": request.get("prior_results"),
    }
    return (
        "Create a bounded delta plan for this final-alignment gap. Start from "
        "the declared restart target. Return only new or changed work needed "
        "to close the gap; preserve all successful prior work and evidence, "
        "and do not repeat an unchanged completed task. Keep the original "
        "request intact.\nAMENDMENT FACTS:\n" +
        json.dumps(facts, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))


def _prior_result_summary(results, limit=16):
    """Expose compact provenance to an amendment planner, not full diffs."""
    rows = []
    for key, result in results.items():
        if not isinstance(result, dict):
            continue
        row = {"run_node": str(key), "node_id": result.get("node_id"),
               "status": result.get("status")}
        summary = (result.get("summary") or result.get("reason")
                   or result.get("error"))
        if summary:
            row["summary"] = " ".join(str(summary).split())[:240]
        artifacts = result.get("artifacts")
        if isinstance(artifacts, (list, tuple)):
            row["artifacts"] = list(artifacts[:4])
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _remove_completed_work(plan, completed_work):
    """Remove exact successful tasks from an amendment delta before dispatch.

    A planner may repeat prior nodes while constructing an otherwise useful
    amendment. Their results remain in the run history; delta nodes no longer
    depend on dispatching those same writes again.
    """
    if not completed_work:
        return plan
    from dataclasses import replace
    from .dag import TaskDAG

    try:
        candidate = TaskDAG.from_dict(plan.get("dag") or {})
    except HarnessError as exc:
        return {"status": "refused", "reason":
                "alignment amendment DAG is invalid: {0}".format(exc)}
    removed = {node_id for node_id, node in candidate.nodes.items()
               if _work_fingerprint(node) in completed_work}
    if not removed:
        return plan
    remaining = {
        node_id: replace(node, dependencies=tuple(
            dep for dep in node.dependencies if dep not in removed))
        for node_id, node in candidate.nodes.items() if node_id not in removed
    }
    if not remaining:
        return {"status": "refused", "reason":
                "final-alignment amendment contains no new work after already "
                "completed tasks are preserved", "completed_nodes":
                sorted(removed)}
    try:
        delta = TaskDAG(nodes=remaining)
    except HarnessError as exc:
        return {"status": "refused", "reason":
                "alignment amendment delta is invalid: {0}".format(exc),
                "completed_nodes": sorted(removed)}

    result = dict(plan)
    result["dag"] = delta.to_dict()
    result["total_nodes"] = len(delta.nodes)
    result["batches"] = [[node.node_id for node in batch]
                         for batch in delta.topological_batches()]
    routes = plan.get("nodes")
    if isinstance(routes, list):
        route_rows = []
        for row in routes:
            if not isinstance(row, dict) or row.get("node_id") in removed:
                continue
            updated = dict(row)
            updated["dependencies"] = [
                dep for dep in (row.get("dependencies") or [])
                if dep not in removed]
            route_rows.append(updated)
        result["nodes"] = route_rows
        result["total_cost_ceiling"] = round(sum(
            float(row.get("cost_ceiling") or
                  (row.get("route") or {}).get("cost_ceiling") or 0.0)
            for row in route_rows), 4)
    result["amendment"] = {
        **(dict(result.get("amendment") or {})),
        "completed_nodes_preserved": sorted(removed),
        "delta_nodes": sorted(delta.nodes),
    }
    return result


def triage_files_keyword(goal, files, max_files=MAX_TRIAGE_FILES):
    """Expose the keyword fallback for callers that skip generative/policy triage."""
    return keyword_fallback(goal, files, max_files=max_files)


def keyword_fallback(goal, files, max_files=MAX_TRIAGE_FILES):
    """The no-model triage fallback: keyword overlap with file names/stems."""
    words = set(re.findall(r"[a-z_0-9]+", goal.lower()))
    scored: List[tuple] = []
    for f in files:
        stem = re.sub(r"\.[^.]+$", "", f.rsplit("/", 1)[-1]).lower()
        stem_words = set(stem.split("_")) | {stem}
        score = len(words & stem_words)
        if score:
            scored.append((-score, f))
    scored.sort()
    return [f for _, f in scored[:max_files]]


def build_state_summary(node_results, extra_notes=(), max_chars=8000):
    """Render bounded execution state for the completion judge."""
    lines = [f"- note: {n}" for n in extra_notes]
    for res in node_results:
        if not isinstance(res, dict):
            continue
        lines.append(
            f"- subtask {res.get('node_id', '?')} target={res.get('file_path') or '?'} "
            f"status={res.get('status', '?')} error={str(res.get('error') or '')[:200]}")
    return "\n".join(lines)[:max_chars]


def drive(*, goal: str, target_files: List[str], initial_plan: Dict,
          root_dir, plan_round: Callable[[str], Dict],
          execute_plan: Callable[[Dict], Dict],
          completion_chat: Callable[[str], str], emit: Callable[..., None],
          cancel_check=None, refused=None, max_rounds=MAX_ORCH_ROUNDS,
          jev_policy=None, jev_completion_threshold=None,
          jev_token_budget=None, jev_stage_token_budget=None,
          retained_brief=None,
          completed_stages=None, jev_alignment_threshold=0.99,
          plan_amendment: Optional[Callable[[str, Dict], Dict]] = None,
          alignment_only: bool = False):
    """Run plan -> execute -> judge until complete or the round budget ends.

    ``execute_plan`` is the only execution seam: the caller supplies the
    already-composed PlanExecutor invocation, while this function owns round
    state, re-planning, artifact truth, and completion semantics.
    """
    all_results: Dict[str, Dict] = {}
    last_round_results: Dict[str, Dict] = {}
    total_cost = 0.0
    rounds_history: List[Dict] = []
    final_all_ok = False
    remaining_scope = ""
    current_goal = goal
    plan = initial_plan
    pending_amendment = None
    completed_work: Set[Tuple[str, Tuple[str, ...], Optional[str]]] = set()
    latest_alignment = None

    def run_token_budget():
        return (jev_token_budget() if callable(jev_token_budget)
                else jev_token_budget)

    def stage_token_budget(stage):
        if callable(jev_stage_token_budget):
            return jev_stage_token_budget(stage)
        return None

    def judgment_token_budget(stage):
        return stage_token_budget(stage) or run_token_budget()

    for round_no in range(1, max_rounds + 1):
        if cancel_check and cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        if round_no > 1:
            emit("orchestration_round", round=round_no, goal=current_goal)
            if pending_amendment is not None:
                if not callable(plan_amendment):
                    remaining_scope = (
                        "final-alignment restart was validated, but no bounded "
                        "amendment handler is available")
                    rounds_history[-1].setdefault("restart", {})[
                        "reason"] = remaining_scope
                    emit("orchestration_note", note=remaining_scope)
                    break
                plan = plan_amendment(current_goal, pending_amendment)
                pending_amendment = None
                if plan.get("status") == "refused":
                    remaining_scope = (plan.get("reason") or
                                       "bounded amendment was refused")
                    rounds_history[-1].setdefault("restart", {})[
                        "amendment_refused"] = remaining_scope
                    emit("orchestration_note", note=remaining_scope)
                    break
                plan = _remove_completed_work(plan, completed_work)
                if plan.get("status") == "refused":
                    remaining_scope = plan.get("reason") or (
                        "bounded amendment could not be safely dispatched")
                    rounds_history[-1].setdefault("restart", {})[
                        "amendment_refused"] = remaining_scope
                    emit("orchestration_note", note=remaining_scope)
                    break
            else:
                plan = plan_round(current_goal)
            if plan.get("status") == "refused":
                if refused is not None:
                    return refused(plan)
                return {"status": "refused", "plan": plan}
            emit("dag_planned", total_nodes=plan["total_nodes"],
                 total_ceiling=plan["total_cost_ceiling"],
                 nodes=[{"node_id": n["node_id"],
                         "instruction": n["instruction"],
                         "target": (n.get("target_files") or [""])[0]}
                        for n in plan.get("nodes", [])])

        nodes = (plan.get("dag") or {}).get("nodes") or []
        if not nodes:
            remaining_scope = ("planner produced no executable nodes "
                               f"for: {current_goal[:150]}")
            break

        round_results = execute_plan(plan)
        last_round_results = round_results
        for nid, result in round_results.items():
            if isinstance(result, dict):
                result.setdefault("node_id", nid)
        all_results.update({f"r{round_no}/{nid}": result
                            for nid, result in round_results.items()})
        round_cost = sum(float(result.get("cost", 0.0) or 0.0)
                         for result in round_results.values())
        total_cost = round(total_cost + round_cost, 6)
        round_ok = bool(round_results) and all(
            result.get("status") in {"ok", "changed", "completed"}
            for result in round_results.values())
        from .dag import TaskDAG
        try:
            round_dag = TaskDAG.from_dict(plan.get("dag") or {})
        except HarnessError:
            round_dag = None
        if round_dag is not None:
            for node_id, node in round_dag.nodes.items():
                result = round_results.get(node_id)
                if (isinstance(result, dict)
                        and result.get("status") in {"ok", "changed", "completed"}):
                    completed_work.add(_work_fingerprint(node))
        rounds_history.append({
            "round": round_no, "goal": current_goal[:200],
            "nodes": len(nodes), "all_nodes_ok": round_ok,
            "cost": round(round_cost, 6),
        })

        interrupted = next((
            (nid, result) for nid, result in round_results.items()
            if isinstance(result, dict)
            and result.get("status") in {"consent_blocked", "deferred"}), None)
        if interrupted is not None:
            node_id, result = interrupted
            status = result.get("status")
            detail = (result.get("remaining_scope") or result.get("reason")
                      or result.get("error") or "operator action is required")
            remaining_scope = (
                f"Execution stopped at {node_id} ({status}): {detail}")
            emit("orchestration_note", note=remaining_scope)
            # Consent refusal and explicit deferral are handoff boundaries:
            # don't spend on a completion judge or dispatch a re-planned task.
            final_all_ok = False
            break

        artifact_notes = []
        artifact_facts = []
        seen_paths = set()

        def note_artifact(rel):
            rel = str(rel).replace("\\", "/").strip("`'\" .")
            if (not rel or rel in seen_paths or len(seen_paths) >= 8
                    or ".." in rel):
                return
            seen_paths.add(rel)
            path = Path(root_dir) / rel
            if path.is_file():
                try:
                    lines = len(path.read_text(encoding="utf-8",
                                               errors="replace").splitlines())
                except OSError:
                    lines = 0
                artifact_notes.append(f"artifact truth: {rel} present ({lines} lines)")
                artifact_facts.append({"path": rel, "present": True,
                                       "lines": lines})
            else:
                artifact_notes.append(f"artifact truth: {rel} MISSING from the repository")
                artifact_facts.append({"path": rel, "present": False,
                                       "lines": None})

        for target in target_files or ():
            note_artifact(target)
        for match in re.finditer(
                r"(?<![/\\])\b[\w-]+\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml)\b",
                goal):
            note_artifact(match.group(0))

        summary = build_state_summary(
            list(round_results.values()),
            extra_notes=[f"orchestrator round {round_no} of {max_rounds}"]
            + artifact_notes)
        missing = [note for note in artifact_notes if "MISSING" in note]
        completed = list(dict.fromkeys(list(completed_stages or [])
                                       + (["execution"] if round_ok else [])))
        current_brief = (retained_brief() if callable(retained_brief)
                         else retained_brief)
        alignment_checked = False
        if (current_brief is not None and jev_policy is not None
                and round_ok and not missing):
            facts = {
                "original_request": goal,
                "round_goal": current_goal,
                "results": all_results,
                "round_results": round_results,
                "artifacts": artifact_facts,
                "completed_stages": completed,
                "round": round_no,
                "round_ok": round_ok,
            }
            alignment_result, alignment = assess_final_alignment(
                goal=goal, candidate=facts, retained_brief=current_brief,
                jev_policy=jev_policy,
                token_budget=judgment_token_budget("verification"),
                threshold=jev_alignment_threshold)
            latest_alignment = alignment
            alignment_checked = True
            history = rounds_history[-1]
            history["alignment"] = {
                key: alignment.get(key) for key in
                ("native", "supported", "aligned", "threshold",
                 "iteration_required", "plan_required")}
            if not alignment["aligned"]:
                final_all_ok = False
                remaining_scope = (
                    "final alignment unavailable or insufficient"
                    if not alignment["native"] or alignment["supported"] is None
                    else "final alignment requires another iteration or plan"
                    if alignment["iteration_required"] or alignment["plan_required"]
                    else "final alignment support {:.3f} is below {:.3f}".format(
                        alignment["supported"], alignment["threshold"]))
                restart = {"attempted": False, "allowed": False}
                if alignment["native"] and hasattr(
                        jev_policy, "evaluate_hourglass_stage"):
                    restart["attempted"] = True
                    restart_facts = dict(facts)
                    restart_facts["final_alignment"] = {
                        key: alignment.get(key) for key in
                        ("native", "supported", "threshold",
                         "iteration_required", "plan_required")}
                    from .tokens import estimate_prompt_tokens
                    restart_state = json.dumps(
                        restart_facts, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
                    restart_token_cap = (
                        estimate_prompt_tokens(restart_state) + 512)
                    restart_result, restart_structural = (
                        jev_policy.evaluate_hourglass_stage(
                            "restart_target", restart_facts,
                            site="final-alignment-restart",
                            max_input_tokens=restart_token_cap,
                            token_budget=run_token_budget(),
                            token_stage_budget=(
                                stage_token_budget("verification"))))
                    target_answer = (restart_result.answers or {}).get(
                        "restart_target")
                    target = (target_answer or {}).get("target") if isinstance(
                        target_answer, dict) else None
                    from .jev_packs import validate_restart_request
                    decision = validate_restart_request(
                        "final_alignment", target,
                        completed_stages=completed)
                    restart.update({"allowed": bool(decision.get("allowed")),
                                    "target": decision.get("target"),
                                    "mode": decision.get("mode"),
                                    "preserved_stages": decision.get(
                                        "preserved_stages", []),
                                    "reason": (decision.get("reasons") or [None])[0],
                                    "native": bool(restart_structural.get(
                                        "native")) and not bool(getattr(
                                            restart_result, "is_fallback", True)),
                                    "consent_renewal_required": bool(
                                        decision.get("consent_renewal_required"))})
                    if (decision.get("allowed") and restart["native"]
                            and callable(plan_amendment)
                            and round_no < max_rounds):
                        pending_amendment = {
                            "target": decision.get("target"),
                            "original_request": goal,
                            "alignment": {
                                key: alignment.get(key) for key in
                                ("native", "supported", "threshold",
                                 "iteration_required", "plan_required")},
                            "completed_stages": completed,
                            "prior_results": _prior_result_summary(all_results),
                        }
                        current_goal = _amendment_goal(pending_amendment)
                        restart["retry_scheduled"] = True
                        history["restart"] = restart
                        emit("orchestration_note", note=remaining_scope)
                        continue
                    if decision.get("allowed") and restart["native"]:
                        if not callable(plan_amendment):
                            restart["reason"] = (
                                "bounded amendment handler is unavailable")
                        elif round_no >= max_rounds:
                            restart["reason"] = (
                                "round limit reached before amendment dispatch")
                history["restart"] = restart
                emit("orchestration_note", note=remaining_scope)
                break
        if alignment_only:
            if not alignment_checked:
                final_all_ok = False
                if not remaining_scope:
                    remaining_scope = (
                        "final alignment could not run because execution, "
                        "the retained brief, or the Jev policy was unavailable")
                latest_alignment = {
                    "native": False, "supported": None,
                    "aligned": False, "threshold": jev_alignment_threshold,
                    "reason": remaining_scope,
                }
                break
            final_all_ok = bool(round_ok and latest_alignment.get("aligned"))
            break
        # JEV-P3-completion: when a policy owner is attached, run artifact/goal
        # nouls BEFORE the generative judge. Without policy, keep the existing
        # judge-then-artifact-override contract (unchanged hermetic tests).
        if jev_policy is not None:
            pre_judge = assess_completion_nouls(
                goal, summary, jev_policy=jev_policy,
                named_artifacts=list(seen_paths) if seen_paths else None,
                root_dir=root_dir,
                token_budget=judgment_token_budget("verification"))
            rounds_history[-1]["jev"] = {
                "native": bool(pre_judge.get("jev_native")),
                "supported": pre_judge.get("jev_supported"),
                "cannot_complete": bool(pre_judge.get("cannot_complete")),
                "reason": pre_judge.get("reason"),
            }
            if pre_judge["cannot_complete"]:
                emit("orchestration_note",
                     note=f"completion nouls refuse complete: {pre_judge['reason']}"
                          + (f" ({len(pre_judge['missing_artifacts'])} missing)"
                             if pre_judge["missing_artifacts"] else ""))
                if pre_judge["missing_artifacts"]:
                    final_all_ok = False
                    remaining_scope = (pre_judge["remaining"]
                                       or pre_judge["reason"])
                    current_goal = remaining_scope or goal
                    # Code-owned fail: do not spend the generative judge.
                    continue
            # A live Jev completion signal can require another round before
            # the generative judge is allowed to spend.  The threshold is
            # opt-in so existing library callers retain their historical
            # contract; the agent lane supplies the operator's confidence gate.
            jev_supported = pre_judge.get("jev_supported")
            if (pre_judge.get("jev_native")
                    and jev_completion_threshold is not None
                    and isinstance(jev_supported, (int, float))
                    and float(jev_supported) < float(jev_completion_threshold)):
                final_all_ok = False
                remaining_scope = (
                    f"Jev completion support {float(jev_supported):.3f} is below "
                    f"the required {float(jev_completion_threshold):.3f}")
                current_goal = remaining_scope
                emit("orchestration_note", note=remaining_scope)
                continue
        try:
            verdict = assess_completion(goal, summary, completion_chat)
        except HarnessError as exc:
            emit("orchestration_note", note=f"completion judge failed: {exc}")
            verdict = None
        if verdict is None:
            final_all_ok = round_ok
            if not final_all_ok:
                remaining_scope = "completion judge unavailable; see per-node failures"
            break
        if verdict["complete"] and not round_ok:
            emit("orchestration_note",
                 note="judge said complete but one or more nodes did not succeed; "
                      "overriding to incomplete")
            verdict = {"complete": False,
                       "remaining": "one or more execution nodes did not complete",
                       "reason": "node failure despite completion verdict"}
        if verdict["complete"] and missing:
            emit("orchestration_note",
                 note=f"judge said complete but {len(missing)} named artifact(s) missing; "
                      "overriding to incomplete")
            verdict = {"complete": False,
                       "remaining": "; ".join(missing),
                       "reason": "named artifacts missing despite verdict"}
        if verdict["complete"]:
            final_all_ok = True
            break
        remaining_scope = verdict["remaining"] or verdict["reason"]
        current_goal = remaining_scope or goal

    return {
        "all_results": all_results,
        "last_round_results": last_round_results,
        "total_cost": total_cost,
        "rounds_history": rounds_history,
        "final_all_ok": final_all_ok,
        "remaining_scope": remaining_scope,
        "plan": plan,
        "final_alignment": latest_alignment,
    }
