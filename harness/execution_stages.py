"""Planning and execution-stage composition for repository work."""

from typing import Any, Dict

from .chat import governed_text
from .config import resolve_api_key
from .errors import HarnessError
from .events import emit
from .history import save_chat_turn
from .jev_packs import (
    HOURGLASS_STAGE_DIMENSIONS,
    HOURGLASS_STAGE_SITE,
    declared_restart_targets,
    validate_restart_request,
)
from .jev_policy import policy_for
from .session import ledger_for
from .waist import (
    STAGE_CONTEXT,
    STAGE_EXECUTION,
    STATE_COMPLETED,
    STATE_PENDING,
    compose_arguments,
    compose_plan,
    resolve_scout_ladder,
)


class ExecutionStageMixin:
    """Own planning and execution-stage orchestration behavior."""

    def _orchestrator_chat_fn(self, gov):
        """Injected chat_fn for orchestration calls (decompose/triage/judge):
        governed, tier-0 scout head -- the cheapest rung of the same sliding
        scale that classifies nodes. Raises HarnessError on failure; callers
        degrade loudly."""
        allow_escalation = bool(getattr(self.settings, "allow_escalation", False))
        ladder = resolve_scout_ladder(
            use_free=self.settings.use_free,
            custom_frontier=self.settings.frontier_model,
            allow_escalation=allow_escalation,
        )
        api_key = resolve_api_key()

        def chat_fn(prompt_text):
            # governed_text returns (content, cost); every orchestration
            # consumer (decompose/triage/judge) wants the text alone.
            last = None
            for model in self._rate_circuit.available(ladder) or ladder:
                try:
                    content, _ = governed_text(
                        self.transport, api_key, gov, model, prompt_text, 2048, label="orchestrate"
                    )
                    return content
                except HarnessError as e:
                    last = e
                    if "429" in str(e) or "rate limit" in str(e).lower():
                        self._rate_circuit.note_rate_limited(model)
            raise last or HarnessError("orchestration ladder empty")

        return chat_fn

    def _run_execution_stage(self, goal, plan, *, jev_policy, completed_stages=()):
        """HV-5: judge the work package, then let CODE decide any restart.

        ``HV-1`` published five typed stage dimensions and, until now, no
        production path asked for any of them: the single call site sat inside
        the planning stage, which only asks its question when the brief is
        NOT already sufficient -- so on a clean run the dimension never
        executed. This is the execution stage's own call, and it is the one
        place the ``execution`` and ``restart_target`` dimensions mean
        anything: the package about to be handed to a worker, and the
        decision to walk back to an earlier stage instead.

        The restart split is ``HV-1``'s, and this caller keeps it: Jev may
        only RECOMMEND a declared target, and
        :func:`~harness.jev_packs.validate_restart_request` -- code -- decides
        whether that transition is allowed, preserving completed work and
        forcing consent renewal for a changed assignment. When no native
        answer arrives the recommendation is derived from the execution
        signals, and the same code-owned guard decides it, so the decision
        path exists whether or not a model was reachable.

        Returns the judgment as evidence. It deliberately does NOT mark the
        stage ``completed``: judging a package is not dispatching it, and
        dispatch is the next ``HV-5`` slice. Reporting ``completed`` here
        would claim work this code does not do.
        """
        judgment: Dict[str, Any] = {
            "dimension": "execution",
            "state": STATE_PENDING,
            "dispatched": False,
            "signals": {},
            "native": False,
            "restart": None,
        }
        nodes = [
            {
                "node_id": n.get("node_id"),
                "instruction": n.get("instruction"),
                "target": (n.get("target_files") or [""])[0],
            }
            for n in (plan.get("nodes") or [])
        ]
        package = {
            "goal": goal,
            "status": plan.get("status"),
            "decomposition": plan.get("decomposition"),
            "total_nodes": plan.get("total_nodes") or len(nodes),
            "nodes": nodes,
        }
        if jev_policy is not None:
            judgment.update(
                self._ask_stage_dimension("execution", package, jev_policy, task_id=None)
            )
        judgment["restart"] = self._restart_decision(
            goal, jev_policy, judgment, completed_stages=completed_stages
        )
        return judgment

    def _ask_stage_dimension(self, dimension, state, jev_policy, *, task_id=None):
        """Ask one declared ``HV-1`` dimension and read its signals.

        The dimension name, the question pack and the signal vocabulary are
        all ``HV-1``'s (``harness/jev_packs.py``); this only calls the owner
        and reports what came back. A fallback, a transport failure or a
        malformed answer is never promoted to a native signal -- that is the
        owner's contract, and this caller does not second-guess it.
        """
        try:
            eval_res = jev_policy.evaluate_hourglass_stage(
                dimension, state, site=HOURGLASS_STAGE_SITE, task_id=task_id
            )
            if isinstance(eval_res, tuple) and len(eval_res) >= 2:
                _result, structural = eval_res[0], eval_res[1]
            else:
                _result, structural = None, getattr(eval_res, "structural", {}) or {}
        except (HarnessError, TypeError, ValueError) as exc:
            return {
                "signals": {},
                "native": False,
                "error": "{0}: {1}".format(type(exc).__name__, exc),
            }
        structural = structural or {}
        declared = HOURGLASS_STAGE_DIMENSIONS[dimension]["signals"]
        signals = {
            name: structural.get(name) for name in declared if structural.get(name) is not None
        }
        return {"signals": signals, "native": bool(structural.get("native"))}

    def _restart_decision(self, goal, jev_policy, judgment, *, completed_stages=()):
        """Ask for a restart target, then let code rule on the transition."""
        recommendation = None
        source = "none"
        if jev_policy is not None:
            asked = self._ask_stage_dimension(
                "restart_target",
                {"goal": goal, "completed_stages": list(completed_stages)},
                jev_policy,
            )
            choice = asked["signals"].get("restart_target")
            if isinstance(choice, dict) and choice.get("target"):
                recommendation = choice["target"]
                source = "jev"
        if recommendation is None:
            # No native answer. The recommendation is then DERIVED from the
            # execution signals -- an underspecified package or a required
            # checkpoint both mean the work is not ready where it is -- and
            # it is still only a DECLARED stage: the walk-back target is read
            # out of `HV-1`'s own vocabulary rather than hardcoded here, so
            # this caller and the pack cannot disagree about what a restart
            # may even name. The decision below is code either way.
            walk_back = self._declared_walk_back()
            signals = judgment.get("signals") or {}
            unsuitable = signals.get("execution_suitable")
            checkpoint = signals.get("checkpoint_required")
            if walk_back is not None and (
                (unsuitable is not None and unsuitable < 0.5)
                or (checkpoint is not None and checkpoint >= 0.5)
            ):
                recommendation, source = walk_back, "derived"
        if recommendation is not None and recommendation not in declared_restart_targets():
            # Belt and braces: a recommendation that is not in the declared
            # vocabulary is discarded here rather than handed to the guard as
            # something it would have to reject.
            recommendation, source = None, "undeclared_discarded"

        consent_fresh = None
        if jev_policy is not None:
            consent_eval = self._ask_stage_dimension(
                "consent", {"assignment": goal, "stages": list(completed_stages)}, jev_policy
            )
            judgment["consent"] = consent_eval
            fresh_sig = (consent_eval.get("signals") or {}).get("consent_fresh")
            if isinstance(fresh_sig, (int, float)) and not isinstance(fresh_sig, bool):
                consent_fresh = fresh_sig >= 0.70
            elif consent_eval.get("native") is False:
                consent_fresh = None

        decision = validate_restart_request(
            STAGE_EXECUTION,
            recommendation,
            completed_stages=completed_stages,
            consent_fresh=consent_fresh,
        )
        decision["recommendation_source"] = source
        return decision

    @staticmethod
    def _declared_walk_back():
        """The declared stage a restart from ``execution`` should name.

        Read from `HV-1`'s declared vocabulary, not hardcoded: a restart may
        only ever name a declared stage, and the stage immediately preceding
        execution in that vocabulary is the furthest-back legal target. If
        execution is not in the vocabulary, or nothing precedes it, there is
        no legal walk-back and the caller reports none.
        """
        declared = declared_restart_targets()
        if STAGE_EXECUTION not in declared:
            return None
        index = declared.index(STAGE_EXECUTION)
        return declared[index - 1] if index > 0 else None

    def _compose_run_stages(self, goal, candidate_files, jev_policy=None, plan=None):
        """Attach execution-stage judgments to the composition from waist.

        ``compose_plan`` already performs planning when selected. This agent
        hook must consume that result rather than composing or planning again.
        """
        envelope = dict(plan.get("composition") or {}) if isinstance(plan, dict) else {}
        stages = envelope.get("stages") or []
        selected_execution = any(
            isinstance(item, dict)
            and item.get("stage") == STAGE_EXECUTION
            and item.get("state") != "skipped"
            for item in stages
        )
        selected_context = any(
            isinstance(item, dict)
            and item.get("stage") == STAGE_CONTEXT
            and item.get("state") != "skipped"
            for item in stages
        )
        judgments: Dict[str, Any] = {}
        if selected_context and jev_policy is not None:
            judgments["context"] = self._ask_stage_dimension(
                "context_intake",
                {"request": goal, "files": list(candidate_files or [])},
                jev_policy,
            )
        if selected_execution and isinstance(plan, dict):
            completed = tuple(
                item.get("stage")
                for item in stages
                if isinstance(item, dict) and item.get("state") == STATE_COMPLETED
            )
            judgments["execution"] = self._run_execution_stage(
                goal, plan, jev_policy=jev_policy, completed_stages=completed
            )
        return (envelope, None, judgments)

    def _evidence_reader(self):
        """A reader for brief intake, bound to this run's own root.

        Brief intake is allowed to report a read failure as a read failure --
        it must not curate a different evidence set to work around one -- so
        this raises :class:`HarnessError` rather than letting an ``OSError``
        escape as an unhandled crash. The caller's refusal path is already
        fail-soft: it emits a visible note and leaves the gated plan intact.
        """

        def read(rel):
            try:
                return (self.root_dir / rel).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise HarnessError("could not read evidence {0!r}: {1}".format(rel, exc)) from exc

        return read

    def _plan_round(
        self, goal, candidate_files, gov, confirm=None, token_budget=None, decompose_llm=None
    ):
        """One planning pass through the ONE plan composer
        (harness/waist.py:compose_plan): cheap-LLM decomposition when the
        orchestration ladder answers, tier classification, then (hourglass)
        waist confirmation against the resolved frontier rung.

        The GUI lane plans with the same composer the CLI and MCP lanes use,
        so there is no second plan lane to keep in sync. A confirmation
        failure raises (fail-closed): an unconfirmed plan never executes.
        """
        jev_policy = policy_for(
            self.settings,
            transport=self.transport,
            governor=gov,
            ledger=ledger_for(self.settings, caller="agent"),
        )
        compose_args = compose_arguments(
            self.settings, goal=goal, files=candidate_files, root=self.root_dir
        )
        if token_budget is not None:
            compose_args["token_budget"] = token_budget
        decompose_setting = getattr(self.settings, "hourglass_decompose", None)
        decompose_flag = (
            (bool(decompose_setting) if decompose_setting is not None else bool(confirm))
            if decompose_llm is None
            else bool(decompose_llm)
        )
        plan = compose_plan(
            transport=self.transport,
            api_key=resolve_api_key(),
            governor=gov,
            ledger=ledger_for(self.settings),
            opts_goal=goal,
            candidate_files=candidate_files,
            frontier_model=self.settings.frontier_model,
            use_free=self.settings.use_free,
            decompose_llm=decompose_flag,
            confirm=bool(confirm),
            plan_consensus=bool(getattr(self.settings, "hourglass_plan_consensus", False)),
            # The lane's tree: a node's target size is measured against the
            # files this run actually edits, not the server's CWD.
            root=str(self.root_dir),
            chat_fn=lambda prompt_text: (self._orchestrator_chat_fn(gov)(prompt_text), 0.0),
            execute=True,
            allow_escalation=bool(getattr(self.settings, "allow_escalation", False)),
            jev_policy=jev_policy,
            # HV-4/HV-2-use: the GUI lane composes through the same owner the
            # CLI and MCP lanes use, so "which stages ran, what each was
            # allowed, and what evidence it started from" is answerable from
            # the envelope here too instead of only from the caller's head.
            **compose_args,
        )
        if str(plan.get("decomposition", "")).startswith("heuristic"):
            # compose_plan degrades to the heuristic only after the LLM
            # decomposition failed (execute=True); the GUI needs that on the
            # event stream, not just on stderr.
            emit("orchestration_note", note="LLM decomposition unavailable; heuristic plan in use")

        try:
            _, _, judgments = self._compose_run_stages(
                goal, candidate_files, jev_policy=jev_policy, plan=plan
            )
            if judgments:
                plan["stage_judgments"] = {**plan.get("stage_judgments", {}), **judgments}
                restart = (judgments.get("execution") or {}).get("restart") or {}
                emit(
                    "execution_stage",
                    signals=dict((judgments.get("execution") or {}).get("signals") or {}),
                    native=bool((judgments.get("execution") or {}).get("native")),
                    restart=restart.get("allowed"),
                )
                if restart.get("target") is not None:
                    plan["status"] = "refused"
                    plan["confirmation"] = {
                        "verdict": "refused",
                        "model": "restart-guard",
                        "reason": (
                            "validated restart to {0} requires a resumable "
                            "stage runner: {1}".format(
                                restart.get("target"), "; ".join(restart.get("reasons") or [])
                            )
                        ),
                    }
        except HarnessError:
            pass
        return plan

    def _refused_edit(self, plan, prompt, target_files, session_id):
        """Terminal envelope for a waist refusal: nothing dispatched."""
        confirmation = plan.get("confirmation") or {}
        reason = confirmation.get("reason") or "unspecified"

        # Always provide a prompt response to the user's inquiry whenever possible,
        # using conversational / Jev-driven handling rather than returning only a bare refusal.
        answer_text = ""
        try:
            conv = self._handle_conversation(prompt, session_id)
            if isinstance(conv, dict) and conv.get("response"):
                cand = str(conv["response"]).strip()
                if not cand.startswith("The waist confirmation gate refused"):
                    answer_text = cand
        except (Exception, AssertionError):
            pass

        if not answer_text:
            # #206: refused/empty renders as refused, never as verified.
            from .lifecycle_guards import render_refused_plan

            dag = plan.get("dag") or {}
            answer_text = render_refused_plan(
                prompt,
                reason,
                stages=plan.get("stages"),
                nodes=dag.get("nodes"),
            ).rstrip("\n")

        # #206: guard speaks only to the real write surface; deduped to one.
        from .lifecycle_guards import (
            dedupe_notices,
            has_write_surface,
            render_gate_guard,
        )

        guard_block = render_gate_guard(
            reason,
            has_write_surface(target_files, plan.get("dag")),
        )
        notice_blocks = dedupe_notices([guard_block] if guard_block else [])
        if notice_blocks:
            response_text = f"{answer_text}\n\n---\n" + "\n".join(notice_blocks)
        else:
            response_text = answer_text

        result = {
            "status": "refused",
            "intent": "edit",
            "prompt": prompt,
            "response": response_text,
            "target_files": target_files,
            "dag": plan.get("dag"),
            "confirmation": confirmation,
            # A refused run still composed, still ran planning and still
            # judged execution; the refusal is about the plan, not about
            # erasing what the run did. Same three keys, same reason.
            **(
                {"composition": dict(plan["composition"])}
                if isinstance(plan.get("composition"), dict)
                else {}
            ),
            **(
                {"planning": dict(plan["planning"])}
                if isinstance(plan.get("planning"), dict)
                else {}
            ),
            **(
                {"stage_judgments": dict(plan["stage_judgments"])}
                if isinstance(plan.get("stage_judgments"), dict)
                else {}
            ),
            "cost": float(confirmation.get("cost") or 0.0),
            **(
                {"structural": plan["structural"]}
                if isinstance(plan.get("structural"), dict)
                else {}
            ),
        }
        save_chat_turn(session_id, result, self.history_dir)
        emit("chat_response", intent="edit", status="refused")
        return result
