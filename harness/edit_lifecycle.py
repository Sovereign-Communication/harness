"""Auto-edit lane orchestration for :class:`AutonomousAgent`.

The mixin owns planning and execution policy for repository edits. The runtime
agent supplies shared orchestration helpers such as `_plan_round` and
`_run_execution_stage`; lifecycle checkpoints and receipts are owned by
`LifecycleCoordinator`.
"""
import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .condenser import condense_error_log, distill_context
from .config import resolve_hourglass
from .dag import DAGNode, TaskDAG, node_apply_kwargs
from .errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from .escalation import escalation_evidence, escalation_evidence_fields
from .events import emit
from .executor import PlanExecutor
from .history import load_chat_history, save_chat_turn
from .jev_policy import JevPolicy, aggregate_structural, policy_for
from .lifecycle import adapter_for
from .orchestrator import drive
from .prompts import MAX_FILE_LINES
from .repo_scope import discover_verification_gate, enumerate_repo_files
from .results import SUCCESS_STATUSES, model_envelope
from .session import apply_session, attest_model_for, governor_for, jev_for, ledger_for
from .token_budget import budget_from_settings
from .waist import STAGE_EXECUTION
from .workspace import require_workspace


EXECUTION_PHRASES = frozenset({
    "execute the plan", "execute plan", "run the plan", "run plan",
    "apply the plan", "apply plan", "test the plan", "test plan",
    "see if it works", "test if it works", "check if it works",
    "run the test", "run the tests", "run tests", "run test",
    "execute tests", "execute test", "execute it", "run it", "apply it",
    "proceed with plan", "proceed with the plan", "execute this", "run this",
    "test this", "verify the plan", "test the implementation",
    "execute implementation", "run the implementation",
    "execute the code", "run the code", "test the code",
})


class AutoEditLifecycleMixin:
    """The auto-edit route, including its approval and side-effect lifecycle."""

    def _handle_edit(
        self,
        prompt: str,
        session_id: str,
        auto_apply: bool,
        cancel_check: Optional[Callable[[], bool]] = None,
        escalation_note: Optional[str] = None,
        use_jev_completion: bool = False,
        lifecycle_state=None,
    ) -> Dict[str, Any]:
        # Handle code edit/refactor requests: the orchestrator drives the
        # hourglass -- relevance triage over the whole repo, DAG decomposition
        # and waist confirmation (the same composer the CLI/MCP lanes use),
        # governed execution through the shared PlanExecutor (parallel
        # workers, per-node cost reservations, worktree isolation, diff
        # attestation), then a completion judge round that re-plans remaining
        # scope until the goal is met or the round budget is spent.
        root_dir = require_workspace(self.root_dir)
        hourglass = resolve_hourglass(self.settings)
        adapter = adapter_for(self.lifecycle, "auto_edit")
        state = lifecycle_state or adapter.begin(
            session_id, prompt, data={"auto_apply": bool(auto_apply)})
        execution_directive = (
            any(p in prompt.lower() for p in EXECUTION_PHRASES)
            or any(re.search(
                rf"\b{action}\b.*?\b(?:plan|code|test|tests|implementation|script)\b",
                prompt.lower()) for action in ("execute", "run", "apply"))
        )
        saved_result = state["data"].get("result")
        if (lifecycle_state is not None and not auto_apply
                and not execution_directive
                and isinstance(saved_result, dict)
                and saved_result.get("status") == "preview_ready"):
            self.lifecycle.finish(state, "deferred", result=saved_result,
                                  resumable=True)
            return saved_result
        if state["status"] in ("complete", "failed"):
            return state["data"].get("result") or {
                "status": state["status"], "intent": "edit",
                "prompt": prompt,
                "response": "This edit request is already terminal.",
                "cost": state["usage"]["known_cost"],
            }
        saved_plan = (state.get("candidate")
                      if lifecycle_state is not None
                      and isinstance(state.get("candidate"), dict)
                      and state.get("candidate", {}).get("dag")
                      else None)
        target_files = (list(state["context"].get("target_files") or [])
                        if saved_plan else [])

        # Context-aware plan continuation & execution:
        # If prompt is an execution directive or target_files is empty, inspect
        # past session turns to resolve a prior formulated plan or discussed targets.
        past_turns = load_chat_history(session_id, self.history_dir)
        is_exec_directive = (
            any(p in prompt.lower() for p in EXECUTION_PHRASES)
            or any(re.search(rf"\b{action}\b.*?\b(?:plan|code|test|tests|implementation|script)\b", prompt.lower())
                   for action in ("execute", "run", "apply"))
        )
        if is_exec_directive:
            auto_apply = True

        prior_plan = None
        if past_turns and is_exec_directive:
            for turn in reversed(past_turns):
                if isinstance(turn, dict):
                    if turn.get("dag") and turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break
                    elif turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break

        if not target_files:
            target_files = self._triage_scope(prompt)

        if past_turns and not target_files:
            for turn in reversed(past_turns):
                if isinstance(turn, dict):
                    if turn.get("dag") and turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break
                    elif turn.get("target_files"):
                        prior_plan = turn
                        target_files = list(turn["target_files"])
                        break
                    elif turn.get("response"):
                        candidate_text = turn.get("response", "") + "\n" + turn.get("prompt", "")
                        discovered = self._triage_scope(candidate_text)
                        if discovered:
                            target_files = discovered
                            prior_plan = turn
                            break

        if prior_plan is not None:
            if not target_files and prior_plan.get("target_files"):
                target_files = list(prior_plan.get("target_files"))
            prior_goal = prior_plan.get("prompt", "")
            if prior_goal and prior_goal.lower() not in prompt.lower():
                prompt = f"{prior_goal}\n\n[EXECUTION DIRECTIVE]: {prompt}"

        if not target_files:
            # Fallback for execution requests without explicit files:
            # discover candidate test/verification targets in the workspace.
            repo_files = enumerate_repo_files(self.root_dir)
            test_files = [f for f in repo_files if "test" in f.lower() or f.endswith(".py")]
            if test_files:
                target_files = test_files[:3]

        emit("files_discovered", target_files=target_files)

        # Condense candidate file contexts
        file_contents: Dict[str, str] = {}
        original_contents: Dict[str, str] = {}
        for tf in target_files:
            fp = self.root_dir / tf
            if fp.exists():
                try:
                    text = fp.read_text(encoding="utf-8")
                    file_contents[tf] = text
                    original_contents[tf] = text
                except Exception:
                    pass

        if saved_plan and isinstance(state["context"].get("original_contents"), dict):
            original_contents = dict(state["context"]["original_contents"])
        brief = distill_context(files=file_contents, summary=prompt)
        ledger_for(self.settings, caller="agent").append(
            "brief_built", task_id=session_id, site="hourglass", schema=2,
            **brief.ledger_fields())
        emit("context_condensed", estimated_tokens=brief.estimated_tokens)

        # Formulate the FIRST-round DAG plan (LLM decomposition when the
        # orchestration ladder answers; heuristic fallback) and gate.
        # The generative plan lane still requires the normal Harness
        # governor. Jev's own key controls only the typed structural call;
        # an unkeyed Jev evaluator remains the explicit local fallback.
        _, gov = governor_for(self.settings)
        run_token_budget = budget_from_settings(self.settings, label="edit")
        plan_policy = policy_for(
            self.settings, transport=self.transport, governor=gov,
            ledger=ledger_for(self.settings, caller="agent"))
        plan_prompt = prompt
        if saved_plan:
            plan = dict(saved_plan)
            plan_structural = plan.get("structural")
        else:
            def provider_once(call_id, invoke, *, result_cost=None):
                previous = state["usage"]["provider_calls"].get(call_id)
                if previous is not None:
                    if previous.get("status") == "completed":
                        return previous.get("result")
                    raise HarnessError(
                        "a prior provider call has unknown completion; it was not replayed")
                self.lifecycle.provider_started(state, call_id)
                try:
                    value = invoke()
                except (ToolCancelled, ProviderUsageUnknown) as exc:
                    self.lifecycle.interrupt(state, exc, call_id=call_id)
                    raise
                cost = float(result_cost(value) if result_cost else 0.0)
                self.lifecycle.provider_completed(state, call_id, value, cost)
                return value

            def evaluate_plan_once():
                evaluation, structural = plan_policy.evaluate_plan(
                    prompt, target_files, site="agent-plan",
                    task_id=state["request_id"])
                return {
                    "requires_iteration": bool(
                        evaluation.answers.get("requires_iteration")),
                    "structural": dict(structural or {}),
                }

            state["stage"] = "plan"
            self.lifecycle.save(state)
            try:
                plan_review = provider_once(
                    state["request_id"] + ":edit-plan-review",
                    evaluate_plan_once,
                    result_cost=lambda value: float(
                        value.get("structural", {}).get("cost") or 0.0))
                if not isinstance(plan_review, dict):
                    raise HarnessError("saved Jev plan review is invalid")
                plan_structural = plan_review.get("structural")
                if plan_review.get("requires_iteration"):
                    emit("orchestration_note",
                         note="Jev structural analysis detected algorithmic iteration; injecting DAG loop directive")
                    plan_prompt = (
                        f"{prompt}\n\n[STRUCTURAL GUIDELINE]: This goal requires iterative "
                        "control flow, conditional branching, or multi-step execution. "
                        "Ensure the decomposed DAG explicitly breaks down the iterative "
                        "loop and discrete steps into executable nodes.")

                def make_plan():
                    try:
                        return self._plan_round(
                            plan_prompt, target_files, gov,
                            confirm=hourglass["confirm"],
                            token_budget=run_token_budget)
                    except TypeError:
                        return self._plan_round(
                            plan_prompt, target_files, gov,
                            confirm=hourglass["confirm"])

                plan = provider_once(
                    state["request_id"] + ":edit-initial-plan", make_plan,
                    result_cost=lambda value: float(value.get("cost") or 0.0)
                    if isinstance(value, dict) else 0.0)
                if not isinstance(plan, dict):
                    raise HarnessError("saved edit plan is invalid")
                if isinstance(plan_structural, dict):
                    plan["structural"] = plan_structural
                # Persist a complete plan before any apply or consent call.
                state["context"].update({
                    "target_files": list(target_files),
                    "original_contents": dict(original_contents),
                })
                state["data"]["auto_apply"] = bool(auto_apply)
                state["candidate"] = plan
                self.lifecycle.save(state)
                if plan.get("status") == "refused" and str(
                        plan.get("confirmation", {}).get("reason", "")).startswith(
                            "validated restart to planning"):
                    emit("orchestration_note",
                         note="Iterating on plan with Jev stage-runner feedback")
                    refined_prompt = (
                        f"{plan_prompt}\n\n[JEV REVISION DIRECTIVE]: The initial work package required planning refinement: "
                        f"{plan.get('confirmation', {}).get('reason')}. Provide a more specific, verifiable, and safe execution plan.")
                    try:
                        revised = provider_once(
                            state["request_id"] + ":edit-plan-revision",
                            lambda: self._plan_round(
                                refined_prompt, target_files, gov,
                                confirm=hourglass["confirm"],
                                token_budget=run_token_budget),
                            result_cost=lambda value: float(value.get("cost") or 0.0)
                            if isinstance(value, dict) else 0.0)
                        if isinstance(revised, dict) and revised.get("status") != "refused":
                            plan = revised
                            state["candidate"] = plan
                            self.lifecycle.save(state)
                    except ToolCancelled:
                        raise
                    except Exception:
                        # Keep the original refusal when a refinement attempt
                        # fails; it remains non-executable and user-visible.
                        pass
            except (HarnessError, ProviderUsageUnknown, ToolCancelled) as exc:
                if state["status"] not in ("interrupted", "complete", "failed"):
                    self.lifecycle.interrupt(state, exc)
                if isinstance(exc, ToolCancelled):
                    raise
                result = {
                    "status": "deferred", "intent": "edit",
                    "prompt": prompt,
                    "response": "Edit planning stopped because an earlier provider outcome cannot be safely replayed.",
                    "cost": state["usage"]["known_cost"],
                    "defer_reason": str(exc),
                }
                if state["status"] == "interrupted":
                    self.lifecycle.finish(state, "deferred", result=result,
                                          resumable=True)
                return result
        if plan.get("status") == "refused":
            result = self._refused_edit(plan, prompt, target_files, session_id)
            self.lifecycle.finish(state, "failed", candidate=plan, result=result)
            return result
        if not any(isinstance(entry, dict)
                   and entry.get("stage") == STAGE_EXECUTION
                   for entry in (plan.get("composition") or {}).get("stages", [])):
            plan["status"] = "refused"
            plan["confirmation"] = {
                "verdict": "refused", "model": "execution-budget",
                "reason": "execution stage is not selected; refusing dispatch",
            }
            result = self._refused_edit(plan, prompt, target_files, session_id)
            self.lifecycle.finish(state, "failed", candidate=plan, result=result)
            return result
        emit("dag_planned", total_nodes=plan["total_nodes"],
             total_ceiling=plan["total_cost_ceiling"],
             nodes=[{"node_id": n["node_id"], "instruction": n["instruction"],
                     "target": (n.get("target_files") or [""])[0]}
                    for n in plan.get("nodes", [])])

        # Auto-discover local verification gate
        verification_gate = discover_verification_gate(target_files, self.root_dir)

        state["context"].update({
            "target_files": list(target_files),
            "original_contents": dict(original_contents),
        })
        state["data"]["auto_apply"] = bool(auto_apply)
        state["data"]["execution_prompt"] = prompt
        self.lifecycle.advance(state, "plan", context=state["context"],
                               candidate=plan)

        if not auto_apply:
            # Review-first mode: return proposed plan and candidate target files
            result = {
                "status": "preview_ready",
                "intent": "edit",
                "prompt": prompt,
                "response": f"Plan formulated for {len(target_files)} target file(s). Ready for execution.",
                "target_files": target_files,
                "dag": plan["dag"],
                "verification_gate": verification_gate,
                "cost_ceiling": plan["total_cost_ceiling"],
                "cost": 0.0,
                # HV-5: the composed stages and the planning outcome are part
                # of what the caller asked for, so they are part of the
                # answer. A run that reports a plan without saying which
                # stages it was allowed to spend, and how planning ended,
                # is asking the reader to take the composition on trust.
                **({"composition": dict(plan["composition"])}
                   if isinstance(plan.get("composition"), dict) else {}),
                **({"planning": dict(plan["planning"])}
                   if isinstance(plan.get("planning"), dict) else {}),
                **({"stage_judgments": dict(plan["stage_judgments"])}
                   if isinstance(plan.get("stage_judgments"), dict) else {}),
                **({"structural": dict(plan["structural"])}
                   if isinstance(plan.get("structural"), dict) else {}),
            }
            self.lifecycle.advance(state, "propose", candidate=plan)
            self.lifecycle.finish(state, "deferred", candidate=plan,
                                  result=result, resumable=True)
            save_chat_turn(session_id, result, self.history_dir)
            return result

        self.lifecycle.advance(state, "apply", candidate=plan)
        if cancel_check and cancel_check():
            exc = ToolCancelled("Prompt execution was cancelled by user")
            self.lifecycle.interrupt(state, exc)
            raise exc

        # Autonomous execution mode: the orchestrator drives rounds --
        # plan -> execute EVERY node (keep_going: failures are state for the
        # judge, not abort) -> completion judge -> re-plan remaining scope --
        # until the judge calls it complete or the round budget is spent.
        # Keep the injected transport on the actual apply engine as well as
        # planning/judging. This is the sole transport boundary for the agent
        # lane; otherwise a dogfood/test transport silently fell back to live
        # HTTP during node execution.
        engine = apply_session(self.settings, transport=self.transport)

        # Recreate the exact execution-stage ceiling reported by compose_plan,
        # as a child of the SAME run budget. The live child is shared by all
        # node calls, so concurrent reservations are accounted atomically.
        execution_record = next((entry for entry in
                                 (plan.get("composition") or {}).get("stages", [])
                                 if isinstance(entry, dict)
                                 and entry.get("stage") == STAGE_EXECUTION), None)
        execution_budget = None
        if execution_record:
            execution_budget = run_token_budget.stage(
                STAGE_EXECUTION,
                max_input_tokens=execution_record.get("max_input_tokens"),
                max_output_tokens=execution_record.get("max_output_tokens"))

        def apply_node(target, node: DAGNode, route_kwargs, task_runner):
            """One node's engine call for the agent lane: the absolute target
            (the engine resolves paths against process CWD, the server's
            directory, not the agent's chosen root), the gate fallback, the
            MAX_FILE_LINES -> diff-backend switch, and the self-healing
            retry. How the round runs -- worker count, reservations, worktree
            isolation, attestation -- is the shared PlanExecutor's."""
            if cancel_check and cancel_check():
                raise ToolCancelled("Subtask cancelled by user")
            if target is None and target_files:
                target = target_files[0]
            # Isolated nodes arrive absolute, inside their own worktree.
            engine_target = target
            if target and not Path(target).is_absolute():
                engine_target = str(self.root_dir / target)
            # The planner owns gate derivation. This lane deliberately does
            # not repair or invent a gate: a missing plan-time gate remains a
            # fail-closed trust refusal rather than becoming an ungated ok.
            gate = node.local_gate
            emit("subtask_start", node_id=node.node_id, instruction=node.instruction, target=target)

            # Whole-file rewrites cap at MAX_FILE_LINES by engine policy;
            # large-file nodes go straight to the diff backend (touched
            # hunks only) instead of dying as "out of scope" fatals.
            if engine_target and Path(engine_target).is_file():
                try:
                    line_count = len(Path(engine_target).read_text(
                        encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    line_count = 0
                if line_count > MAX_FILE_LINES:
                    route_kwargs.setdefault("backend", "diff")

            apply_kwargs = dict(route_kwargs)
            if execution_budget is not None:
                apply_kwargs["token_budget"] = execution_budget
            # Consent is bound to this exact node assignment and source pins.
            # Pin contents are represented by digests; no prior chat history
            # or web response bodies enter the consent prompt.
            source_pins = {}
            for rel in target_files:
                source = self.root_dir / rel
                if source.is_file():
                    try:
                        source_pins[str(rel)] = hashlib.sha256(
                            source.read_bytes()).hexdigest()
                    except OSError:
                        source_pins[str(rel)] = "unreadable"
            task_limit = route_kwargs.get(
                "task_max_cost", engine.default_task_max_cost)
            assignment = {
                "schema": "jev-work-package-v1",
                "request": prompt,
                "node_id": node.node_id,
                "instruction": node.instruction,
                "target": str(target or ""),
                "verification_gate": gate,
                "context_pins": source_pins,
                "model_policy": {
                    "pinned_model": route_kwargs.get("model"),
                    "apply_pool": list(route_kwargs.get("apply_pool") or
                                        engine.router.apply_pool),
                    "allow_escalation": bool(route_kwargs.get(
                        "allow_escalation", self.settings.allow_escalation)),
                },
                "limits": {
                    "task_max_cost": task_limit,
                    "run_max_cost": gov.max_cost,
                    "run_remaining_cost": gov.remaining(),
                    "max_input_tokens": (execution_budget.max_input_tokens
                                          if execution_budget else None),
                    "max_output_tokens": (execution_budget.max_output_tokens
                                           if execution_budget else None),
                },
            }
            encoded = json.dumps(assignment, sort_keys=True,
                                 separators=(",", ":"), default=str)
            assignment["assignment_id"] = "wp-" + hashlib.sha256(
                encoded.encode("utf-8")).hexdigest()[:24]
            apply_kwargs["assignment_context"] = assignment
            if task_runner is not None:
                apply_kwargs["task_runner"] = task_runner

            attempt = {"number": 0}

            def run_apply(instruction):
                attempt["number"] += 1
                effect_id = (f"edit:{state['request_id']}:{node.node_id}:"
                             f"{attempt['number']}")
                approval = {
                    "required": True,
                    "assignment_id": assignment["assignment_id"],
                    "require_diff_authorization": bool(
                        hourglass["require_diff_authorization"]),
                }
                action, saved = self.lifecycle.begin_effect(
                    state, effect_id, "auto_edit.apply",
                    approval_state=approval)
                if action == "completed":
                    return saved
                if action == "blocked":
                    return {
                        "status": "deferred",
                        "error": "a previous edit attempt has unknown completion; not replayed",
                        "node_id": node.node_id,
                    }
                try:
                    value = engine.apply_edit(
                        file_path=engine_target,
                        instruction=instruction,
                        verify_cmd=gate,
                        allow_verify=True,
                        require_consent=True,
                        require_diff_authorization=hourglass[
                            "require_diff_authorization"],
                        **apply_kwargs,
                    )
                except BaseException as exc:
                    self.lifecycle.unknown_effect(state, effect_id)
                    if isinstance(exc, Exception):
                        self.lifecycle.interrupt(state, exc)
                    raise
                self.lifecycle.complete_effect(state, effect_id, value)
                return value

            res = run_apply(node.instruction)
            if isinstance(res, dict):
                res.setdefault("assignment_id", assignment["assignment_id"])
                res.setdefault("assignment_digest", hashlib.sha256(
                    json.dumps(assignment, sort_keys=True, separators=(",", ":"),
                               default=str).encode("utf-8")).hexdigest())
                if execution_budget is not None:
                    res["token_usage"] = execution_budget.snapshot()

            # The shared engine gate owns Jev when it was composed normally.
            # A lightweight injected engine (used by library callers/tests)
            # has no policy, so this lane supplies the same policy owner as a
            # compatibility boundary rather than silently skipping the check.
            # JEV-P4: composition goes through session.jev_for → policy_for
            # (no raw JevEvaluator outside the policy owner).
            structural = res.get("structural") if isinstance(res, dict) else None
            policy = getattr(engine, "jev_policy", None)
            if (res.get("status") in SUCCESS_STATUSES and res.get("diff")
                    and not isinstance(policy, JevPolicy)):
                policy = jev_for(
                    self.settings, transport=self.transport, governor=gov,
                    ledger=ledger_for(self.settings, caller="agent"))
                jev_res, structural = policy.evaluate_diff(
                    diff=res["diff"], instruction=node.instruction,
                    file_path=engine_target or "", candidate=res.get("content"),
                    site="agent-apply", task_id=session_id,
                    node_id=node.node_id)
                emit("structural_eval", node_id=node.node_id,
                     verdict=structural["verdict"],
                     confidence=structural["confidence"],
                     supported=structural["supported"])
                if not jev_res.is_passing(min_confidence=self.settings.min_confidence):
                    emit("subtask_retry", node_id=node.node_id,
                         error="Structural check failed: " + "; ".join(jev_res.reasons))
                    healing_inst = (f"{node.instruction}\nSTRUCTURAL EVALUATION FAILED:\n"
                                    + "\n".join(jev_res.reasons))
                    prior_cost = float(res.get("cost", 0.0) or 0.0)
                    res = run_apply(healing_inst)
                    if isinstance(res, dict):
                        res["cost"] = round(prior_cost + float(res.get("cost", 0.0) or 0.0), 6)
                        res["structural"] = structural

            # Self-healing retry on verification failure
            if res.get("status") not in SUCCESS_STATUSES and res.get("error"):
                prior_cost = float(res.get("cost", 0.0) or 0.0)
                condensed_err = condense_error_log(str(res.get("error")))
                emit("subtask_retry", node_id=node.node_id, error=condensed_err[:120])
                # Attempt retry with healing instruction
                healing_inst = f"{node.instruction}\nPREVIOUS TEST FAILURE:\n{condensed_err}"
                res = run_apply(healing_inst)
                if isinstance(res, dict):
                    res["cost"] = round(prior_cost + float(res.get("cost", 0.0) or 0.0), 6)

            emit("subtask_finish", node_id=node.node_id, status=res.get("status"))
            return res

        def execute_plan(plan_to_run):
            dag = TaskDAG.from_dict(plan_to_run["dag"])
            node_routes = {n.get("node_id"): n for n in plan_to_run["nodes"]}
            run_gate = None
            for n in plan_to_run.get("nodes") or ():
                if isinstance(n, dict) and n.get("local_gate"):
                    run_gate = n["local_gate"]
                    break
            plan_exec = PlanExecutor(
                engine, node_routes,
                parallel=hourglass["parallel"], isolate=hourglass["isolate"],
                keep_going=True,
                require_diff_authorization=hourglass["require_diff_authorization"],
                route_kwargs_fn=lambda route: node_apply_kwargs(
                    route,
                    allow_escalation=self.settings.allow_escalation,
                    attest_model=attest_model_for(self.settings)),
                repo=str(root_dir), run_ceiling=gov.max_cost,
                apply=apply_node,
                # HG-final-gate: default ON when a verify command was
                # discovered/declared; shared with CLI/MCP via PlanExecutor.
                final_gate=hourglass.get("final_gate"),
                run_gate=run_gate)
            return plan_exec.execute(dag)

        drive_kwargs = dict(
            goal=prompt, target_files=target_files, initial_plan=plan,
            root_dir=root_dir, plan_round=lambda next_goal: self._plan_round(
                next_goal, target_files, gov, confirm=hourglass["confirm"],
                token_budget=run_token_budget),
            execute_plan=execute_plan,
            completion_chat=lambda prompt_text: self._orchestrator_chat_fn(gov)(prompt_text),
            emit=emit, cancel_check=cancel_check,
            refused=lambda refused_plan: self._refused_edit(
                refused_plan, prompt, target_files, session_id))
        if use_jev_completion:
            drive_kwargs.update(jev_policy=plan_policy,
                                jev_completion_threshold=0.99)
        try:
            driven = drive(**drive_kwargs)
        except BaseException as exc:
            if isinstance(exc, Exception):
                self.lifecycle.interrupt(state, exc)
            raise
        if driven.get("status") == "refused":
            return driven
        all_results = driven["all_results"]
        total_cost = driven["total_cost"]
        rounds_history = driven["rounds_history"]
        final_all_ok = driven["final_all_ok"]
        remaining_scope = driven["remaining_scope"]
        handoff = driven.get("handoff")
        plan = driven["plan"]

        # Evidence-bound escalation. The deferral note is a HANDOFF, not proof
        # that a rung ran: a run that planned, handoff-routed, and executed
        # every node on the free lane used to report itself as "escalated".
        # Only a gate-passed escalation whose family differs from the primary
        # it replaced may carry the label (escalation.escalation_evidence is
        # the ONE definition of that evidence).
        escalation_ev = None
        for _node_result in all_results.values():
            escalation_ev = escalation_evidence(_node_result)
            if escalation_ev is not None:
                break

        # Generate unified diffs
        diffs: List[str] = []
        for tf in target_files:
            fp = self.root_dir / tf
            if fp.exists():
                try:
                    new_text = fp.read_text(encoding="utf-8")
                    old_text = original_contents.get(tf, "")
                    if old_text != new_text:
                        diff = difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{tf}",
                            tofile=f"b/{tf}",
                        )
                        diffs.append("".join(diff))
                except Exception:
                    pass

        unified_diff_str = "\n".join(diffs).strip()

        # Synthesize clear natural language conclusion
        if handoff:
            response_msg = ("Execution stopped at the consent boundary. "
                            "Completed node evidence is retained in the handoff.")
        elif final_all_ok:
            response_msg = (
                f"Orchestrator complete after {len(rounds_history)} round(s). "
                f"Modified {len(target_files)} file(s); the completion judge "
                f"confirmed the goal is met."
            )
        elif remaining_scope:
            response_msg = (f"Orchestrator stopped after {len(rounds_history)} "
                            f"round(s) with honest remaining scope: "
                            f"{remaining_scope}")
        else:
            response_msg = "Task encountered a verification failure during execution. Check diff and error details."
        if escalation_note:
            if escalation_ev is not None:
                response_msg += (
                    f"\n\n**Auto-escalated to the plan lane (hourglass)** after "
                    f"a capability defer: {escalation_note} -- escalation rung "
                    f"ran: {escalation_ev['from_family']} -> "
                    f"{escalation_ev['family']} on {escalation_ev['model']}.")
            else:
                response_msg += (
                    f"\n\n**Auto-escalation handoff ran, but no escalation rung "
                    f"actually ran** (capability defer: {escalation_note}): every "
                    f"node stayed on its configured lane, so this run is not "
                    f"escalated.")

        agent_structural = aggregate_structural(
            list(all_results.values()), site="agent")
        if agent_structural is None and isinstance(plan.get("structural"), dict):
            agent_structural = dict(plan["structural"])
            agent_structural["site"] = "agent"
        last_jev = next((r.get("jev") for r in reversed(rounds_history)
                         if isinstance(r, dict) and r.get("jev")), None)
        hourglass_evidence = {
            "stages": ["context_intake", "planning_waist", "execution",
                       "completion_jev"] if use_jev_completion else
                      ["context_intake", "planning_waist", "execution"],
            "rounds": rounds_history,
            "settings": hourglass,
            "token_usage": run_token_budget.snapshot(),
            "source": "agent_edit_lane",
        }
        result = {
            "status": "deferred" if handoff else
                      ("ok" if final_all_ok else "failed"),
            **({"escalated_from_defer":
                escalation_note or "free model capability defer"}
               if escalation_ev is not None else {}),
            **escalation_evidence_fields(escalation_ev),
            "intent": "edit",
            "prompt": prompt,
            "response": response_msg,
            "target_files": target_files,
            "diff": unified_diff_str,
            "verification_gate": verification_gate,
            # The waist gate's own verdict rides the envelope, so a reader
            # can see what confirmed this plan (and on which model).
            **({"confirmation": plan["confirmation"]}
               if plan.get("confirmation") else {}),
            **({"structural": agent_structural}
               if agent_structural is not None else {}),
            # MS envelope: requested = first child's requested primary;
            # observed = every model that served across the run.
            **model_envelope(
                model_requested=next((r.get("model_requested") for r in
                                      all_results.values()
                                      if isinstance(r, dict)
                                      and r.get("model_requested")), None),
                model_observed=[m for r in all_results.values()
                                if isinstance(r, dict)
                                for m in (r.get("model_observed") or [])]),
            "cost": round(total_cost, 6),
            # HV-5: the composed stages, the planning outcome and the
            # execution-stage judgment are the same evidence the review-first
            # envelope already carried, and `auto_apply` defaults to false --
            # so leaving them on the preview branch only would have made the
            # whole slice invisible on an ordinary autonomous run. They ride
            # the final plan, next to the `hourglass` block that already
            # carries stage evidence, because a reader of a completed run has
            # at least as much claim to them as a reader of a preview.
            **({"composition": dict(plan["composition"])}
               if isinstance(plan.get("composition"), dict) else {}),
            **({"planning": dict(plan["planning"])}
               if isinstance(plan.get("planning"), dict) else {}),
            **({"stage_judgments": dict(plan["stage_judgments"])}
               if isinstance(plan.get("stage_judgments"), dict) else {}),
            **engine.governor.snapshot(),
            "results": list(all_results.values()),
            "orchestrator_rounds": len(rounds_history),
            "orchestrator_history": rounds_history,
            "hourglass": hourglass_evidence,
            **({"handoff": handoff} if handoff else {}),
            **({"confidence": {
                "threshold": 0.99,
                "observed": (last_jev or {}).get("supported"),
                "passed": bool(
                    final_all_ok
                    and (last_jev or {}).get("native")
                    and isinstance((last_jev or {}).get("supported"), (int, float))
                    and float((last_jev or {}).get("supported")) >= 0.99),
                "source": "jev.noul.goal_achieved",
            }} if last_jev else {}),
            **({"remaining_scope": remaining_scope} if not final_all_ok else {}),
        }

        save_chat_turn(session_id, result, self.history_dir)
        terminal = ("deferred" if handoff else
                    "complete" if final_all_ok else "failed")
        self.lifecycle.finish(state, terminal, candidate=plan, result=result,
                              resumable=bool(handoff))
        emit("chat_response", intent="edit", status=result["status"], cost=total_cost)
        return result
