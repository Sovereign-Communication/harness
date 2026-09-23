"""The single Jev policy owner for all Harness decision lanes (JEV-P1/P3).

The P0 evaluator owns TypeSafe parsing and code-owned mechanics. This module
owns lane policy: when a typed call may dispatch, its bounded spend, one ledger
event, and the structural envelope shared by apply, plan, waist, and agent
lanes. P3 utilization packs live in :mod:`harness.jev_packs` and are imported
here — still ONE policy owner, never a second Jev client.
"""
import difflib
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .errors import HarnessError
from .jev import (JevEvaluationResult, JevEvaluator, jev_cost,
                  triage_question_pack)
from .route_pack import (ROUTE_QUERY_SITE, fallback_route, route_combo,
                         route_question_pack as route_query_pack,
                         validate_route_pack)
from .jev_packs import (
    AUDIT_DIMENSIONS_SITE,
    HUL_SCOPE_SITE,
    LOG_FACTOR_SITE,
    PHASE_COMPLETION_SITE,
    REPO_SUMMARY_SITE,
    SCOPE_COVERAGE_HOLD,
    SCOPE_NOUL_HOLD,
    claim_support_question_pack,
    claims_from_payload,
    completion_bar_question_pack,
    escalation_decision_pack,
    completion_question_pack,
    file_relevance_question_pack,
    heuristic_file_relevance,
    heuristic_repo_axes,
    heuristic_requires_iteration,
    heuristic_route,
    hul_scope_question_pack,
    issue_sort_question_pack,
    log_factor_question_pack,
    match_completion_keywords,
    match_keywords,
    named_artifact_status,
    normalize_complexity_class,
    normalize_route,
    repo_summary_question_pack,
    route_question_pack,
    scope_in_scope_holds,
    validate_candidates,
    validate_completion_pack,
    validate_log_pack,
    validate_operator_pack,
    validate_repo_summary_pack,
)

JEV_MAX_INPUT_TOKENS = 1024


def jev_cost_ceiling(max_input_tokens: int = JEV_MAX_INPUT_TOKENS) -> float:
    """Return the worst-case input-token charge for one Jev decision."""
    return jev_cost(max_input_tokens)


def aggregate_structural(
    results: Iterable[Dict[str, Any]], site: str
) -> Optional[Dict[str, Any]]:
    """Aggregate evaluated child envelopes without inventing an empty verdict."""
    values = [
        result.get("structural")
        for result in results
        if isinstance(result, dict)
        and isinstance(result.get("structural"), dict)
    ]
    if not values:
        return None
    verdicts = {value.get("verdict") for value in values}
    if "fail" in verdicts:
        verdict = "fail"
    elif "defer" in verdicts:
        verdict = "defer"
    elif verdicts == {"pass"}:
        verdict = "pass"
    else:
        verdict = "fail"
    models = {value.get("model") for value in values}
    return {
        "verdict": verdict,
        "confidence": min(float(value.get("confidence") or 0.0)
                           for value in values),
        "supported": min(float(value.get("supported") or 0.0)
                          for value in values),
        "cost": round(sum(float(value.get("cost") or 0.0)
                          for value in values), 6),
        "input_tokens": sum(int(value.get("input_tokens") or 0)
                            for value in values),
        "is_fallback": all(bool(value.get("is_fallback"))
                           for value in values),
        "model": next(iter(models)) if len(models) == 1 else "mixed",
        "site": site,
    }


class JevPolicy:
    """Coordinate typed Jev calls, spend, ledger evidence, and envelopes."""

    def __init__(self, settings, *, transport=None, governor=None, ledger=None,
                 evaluator=None):
        if settings is None:
            raise HarnessError("Jev policy requires settings")
        self.settings = settings
        self.transport = transport
        self.governor = governor
        self.ledger = ledger
        self.evaluator = evaluator or JevEvaluator(
            api_key=getattr(settings, "jev_api_key", None),
            endpoint=getattr(settings, "jev_endpoint", None),
            transport=transport,
            settings=settings,
        )

    @property
    def keyed(self) -> bool:
        return bool(self.evaluator.api_key)

    def _preflight(self, *, site: str, max_input_tokens: int):
        """Reserve one bounded Jev call before its network dispatch."""
        if not self.keyed:
            return None
        if self.governor is None:
            raise HarnessError(
                "keyed Jev evaluation requires the shared spend governor")
        label = "jev:" + site
        worst = jev_cost(max_input_tokens)
        reserve = getattr(self.governor, "reserve", None)
        if callable(reserve):
            return reserve(worst, label)
        preflight = getattr(self.governor, "preflight_jev", None)
        if callable(preflight):
            preflight(max_input_tokens, label=label)
            return None
        ceiling = getattr(self.governor, "max_cost", None)
        spent = getattr(self.governor, "spent", 0.0)
        outstanding = getattr(self.governor, "outstanding", 0.0)
        if (ceiling is not None
                and float(spent) + float(outstanding) + worst > float(ceiling)):
            raise HarnessError(
                f"Jev worst-case cost ${worst:.6f} exceeds the remaining budget")
        return None

    def _structural(self, result: JevEvaluationResult, site: str) -> Dict[str, Any]:
        return {
            "verdict": result.verdict,
            "confidence": result.confidence,
            "supported": result.supported,
            "cost": float(result.cost or 0.0),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "is_fallback": result.is_fallback,
            "model": result.model,
            "site": site,
        }

    def _record_refusal(self, reason: str, *, site: str,
                        task_id: Optional[str] = None,
                        node_id: Optional[str] = None):
        result = JevEvaluationResult(
            "fail", 0.0, 0.0, {}, [reason], cost=0.0,
            input_tokens=0, output_tokens=0, is_fallback=False,
            model=self.evaluator.model,
        )
        structural = self._structural(result, site)
        if self.ledger is not None:
            self.ledger.append(
                "jev_refusal", task_id=task_id, node_id=node_id, site=site,
                model=result.model, reason=reason, cost=0.0,
                input_tokens=0, is_fallback=False,
            )
        return result, structural

    def _account(self, result: JevEvaluationResult, *, site: str,
                 task_id: Optional[str] = None,
                 node_id: Optional[str] = None,
                 reservation=None) -> Dict[str, Any]:
        """Settle live spend and append exactly one hash-chained jev_eval."""
        cost = float(result.cost or 0.0)
        if reservation is not None and self.governor is not None:
            self.governor.reconcile(
                reservation, 0.0 if result.is_fallback else cost)
        elif self.governor is not None and not result.is_fallback:
            # A governor without reservations still gets one actual settlement,
            # including a zero-cost response.
            self.governor.record_actual(cost, result.model or "jev")
        structural = self._structural(result, site)
        if self.ledger is not None:
            self.ledger.append(
                "jev_eval", task_id=task_id, node_id=node_id, site=site,
                model=result.model, verdict=result.verdict,
                supported=result.supported, confidence=result.confidence,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cost=cost, is_fallback=result.is_fallback,
            )
        return structural

    def evaluate_diff(self, diff: str, instruction: str, file_path: str,
                      *, candidate: Optional[str] = None,
                      site: str = "apply", task_id: Optional[str] = None,
                      node_id: Optional[str] = None,
                      max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Run local mechanics first, then the answerable semantic pack."""
        reservation = None
        try:
            def reserve_for_evaluator():
                nonlocal reservation
                reservation = self._preflight(
                    site=site, max_input_tokens=max_input_tokens)

            result = self.evaluator.verify_diff_mechanics(
                diff, instruction, file_path, candidate=candidate,
                preflight=reserve_for_evaluator,
            )
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
            )
            reservation = None
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            return self._record_refusal(
                str(exc), site=site, task_id=task_id, node_id=node_id)

    def evaluate_candidate(self, original: str, candidate: str, instruction: str,
                           file_path: str, *, site: str = "apply",
                           task_id: Optional[str] = None,
                           node_id: Optional[str] = None,
                           max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Evaluate a candidate diff before the verification gate can write it."""
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile="a/" + os.path.basename(file_path),
            tofile="b/" + os.path.basename(file_path),
        ))
        return self.evaluate_diff(
            diff, instruction, file_path, candidate=candidate, site=site,
            task_id=task_id, node_id=node_id, max_input_tokens=max_input_tokens,
        )

    def evaluate_triage(self, prompt: str, target_files=None, *,
                        site: str = "triage", task_id: Optional[str] = None):
        """Return a bounded route choice plus iteration signal for Pillar 1."""
        reservation = None
        try:
            reservation = self._preflight(site=site, max_input_tokens=JEV_MAX_INPUT_TOKENS)
            result = self.evaluator.evaluate(
                {"prompt": prompt or "", "target_files": list(target_files or [])},
                triage_question_pack())
            if result.is_fallback and "route" not in result.answers:
                lower = (prompt or "").lower()
                iterative = any(word in lower for word in
                                ("iterat", "loop", "branch", "recur", "algorithm", "architect"))
                route = "frontier" if iterative else (
                    "diff" if len(target_files or []) > 1 else "free-distill")
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"route": route, "requires_iteration": iterative},
                    result.reasons, is_fallback=True, model=result.model)
            structural = self._account(result, site=site, task_id=task_id,
                                       reservation=reservation)
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            # Honest local triage is heuristic only; it never pretends to be live.
            lower = (prompt or "").lower()
            iterative = any(word in lower for word in
                            ("iterat", "loop", "branch", "recur", "algorithm", "architect"))
            route = "frontier" if iterative else ("diff" if len(target_files or []) > 1 else "free-distill")
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"route": route, "requires_iteration": iterative}, [str(exc)],
                is_fallback=True, model=self.evaluator.model)
            return fallback, self._structural(fallback, site)

    def evaluate_escalation_decision(
            self, failure_context: str, *, site: str = "escalation-decision",
            task_id: Optional[str] = None,
            max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Jev-directed escalation signals for the P2 decision pipeline
        (JEV-P2-dead-code: wire, not delete).

        Two nouls over the code-owned failure context (verify-output tail +
        attempt history -- never model output treated as state):

        - ``escalation_decision``: the ``decide_probe_verify_escalate``
          noul. Its calibrated confidence IS the confidence the dead
          function's ``confidence`` parameter was always meant to receive.
        - ``capability_budget``: the ``should_abstain`` noul (remaining
          attempt budget worth another same-tier retry?).

        Unkeyed runs dispatch no network and resolve to the evaluator's
        honest local fallback (``is_fallback=True``), so callers treat a
        fallback result as "no Jev signal available" and keep the
        status-quo walk. Live transport/contract failures refund the
        reservation and refuse honestly. Returns ``(result, structural)``.
        """
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"context": failure_context or ""},
                escalation_decision_pack())
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            reservation = None
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            return self._record_refusal(
                str(exc), site=site, task_id=task_id)

    def evaluate_plan(self, prompt: str, target_files=None, *, site: str = "waist",
                      task_id: Optional[str] = None, node_id: Optional[str] = None,
                      max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Evaluate the bounded plan pack and account it like every other site."""
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate_plan_requirements(
                prompt, target_files)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
            )
            reservation = None
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            return self._record_refusal(
                str(exc), site=site, task_id=task_id, node_id=node_id)

    def evaluate_route(self, prompt: str, target_files=None, *,
                       site: str = "route", task_id: Optional[str] = None,
                       node_id: Optional[str] = None,
                       max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-route: typed route choice over the shared vocabulary.

        Keyed answers use the route pack; unkeyed/transport failure returns
        the existing heuristic with ``is_fallback=True`` (never brand ids).
        """
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"prompt": prompt or "", "target_files": list(target_files or [])},
                route_question_pack())
            answers = dict(result.answers or {})
            if result.is_fallback or "route" not in answers:
                route = heuristic_route(prompt, target_files)
                iterative = heuristic_requires_iteration(prompt)
                answers = {
                    "route": route,
                    "requires_iteration": iterative,
                    **{k: v for k, v in answers.items() if k not in ("route", "requires_iteration")},
                }
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0, answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=True, model=result.model)
            else:
                # Normalize live choice into the vocabulary (or fall back).
                normalized = normalize_route(
                    answers.get("route", {}).get("choice")
                    if isinstance(answers.get("route"), dict)
                    else answers.get("route"))
                if normalized is None:
                    route = heuristic_route(prompt, target_files)
                    answers = dict(answers)
                    answers["route"] = route
                    result = JevEvaluationResult(
                        result.verdict, result.confidence, result.supported,
                        answers, list(result.reasons) + [
                            "live route outside vocabulary; heuristic applied"],
                        cost=result.cost, input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        is_fallback=True, model=result.model)
                else:
                    answers = dict(answers)
                    answers["route"] = normalized
                    raw_iter = answers.get("requires_iteration")
                    if isinstance(raw_iter, dict) and "noul" in raw_iter:
                        answers["requires_iteration"] = float(raw_iter["noul"]) >= 0.5
                    elif not isinstance(raw_iter, bool):
                        answers["requires_iteration"] = heuristic_requires_iteration(prompt)
                    result = JevEvaluationResult(
                        result.verdict, result.confidence, result.supported,
                        answers, result.reasons,
                        cost=result.cost, input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        is_fallback=False, model=result.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            route = heuristic_route(prompt, target_files)
            answers = {
                "route": route,
                "requires_iteration": heuristic_requires_iteration(prompt),
            }
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0, answers, [str(exc)],
                is_fallback=True, model=self.evaluator.model)
            return fallback, self._structural(fallback, site)

    def evaluate_file_triage(self, goal: str, candidates: Sequence[str],
                             known_files: Optional[Sequence[str]] = None, *,
                             site: str = "triage-files",
                             task_id: Optional[str] = None,
                             max_files: int = 15,
                             max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-triage-files: noul relevance over orchestrator candidates.

        Every kept path is validated against ``known_files`` when supplied.
        Unkeyed path uses the keyword heuristic with honest ``is_fallback``.
        """
        listing = list(known_files) if known_files is not None else None
        scoped = validate_candidates(candidates, listing)[:max_files]
        if not scoped:
            empty = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"files": [], "is_fallback": True},
                ["no candidates to triage"], is_fallback=True,
                model=self.evaluator.model)
            structural = self._structural(empty, site)
            structural["files"] = []
            return empty, structural
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"goal": goal or "", "files": list(scoped)},
                file_relevance_question_pack(scoped))
            picked: List[str] = []
            if result.is_fallback:
                picked = heuristic_file_relevance(goal, scoped, max_files=max_files)
                answers = {"files": picked, "heuristic": True}
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0, answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=True, model=result.model)
            else:
                for index, path in enumerate(scoped):
                    key = f"file_{index}_relevant"
                    answer = (result.answers or {}).get(key)
                    prob = None
                    if isinstance(answer, dict):
                        prob = answer.get("noul")
                    elif isinstance(answer, (int, float)):
                        prob = answer
                    if isinstance(prob, (int, float)) and float(prob) >= 0.5:
                        picked.append(path)
                if not picked:
                    # Live pack said nothing relevant — keep honest empty list.
                    picked = []
                answers = {"files": picked}
                result = JevEvaluationResult(
                    result.verdict, result.confidence, result.supported,
                    answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=False, model=result.model)
            # Defense in depth: never emit a path outside the real listing.
            result.answers["files"] = validate_candidates(picked, listing)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            structural["files"] = list(result.answers["files"])
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            picked = heuristic_file_relevance(goal, scoped, max_files=max_files)
            picked = validate_candidates(picked, listing)
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"files": picked, "heuristic": True}, [str(exc)],
                is_fallback=True, model=self.evaluator.model)
            structural = self._structural(fallback, site)
            structural["files"] = picked
            return fallback, structural

    def evaluate_claim_support(self, claims, source_context: str, *,
                               enabled: bool = False,
                               site: str = "claims",
                               task_id: Optional[str] = None,
                               max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-claims: optional lean support checks before panel judge.

        Default off (``enabled=False``). Does not own claims lint — that stays
        in :mod:`harness.claims`. This only adds advisory typed flags.
        """
        normalized = claims_from_payload(claims)
        if not enabled or not normalized:
            skipped = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"enabled": bool(enabled), "claims": [], "skipped": True},
                ["claim-support checks disabled or empty"],
                is_fallback=True, model=self.evaluator.model)
            structural = self._structural(skipped, site)
            structural["claim_flags"] = []
            structural["skipped"] = True
            return skipped, structural
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {
                    "claims": [{"id": c["id"], "text": c["text"]}
                               for c in normalized],
                    "evidence": (source_context or "")[:4000],
                },
                claim_support_question_pack(len(normalized)))
            flags = []
            if result.is_fallback:
                # Unkeyed: no support claim either way — advisory unknown.
                flags = [{"id": c["id"], "supported": None, "fallback": True}
                         for c in normalized]
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"claim_flags": flags, "skipped": False},
                    result.reasons, cost=result.cost,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=True, model=result.model)
            else:
                for index, claim in enumerate(normalized):
                    key = f"claim_{index}_supported"
                    answer = (result.answers or {}).get(key)
                    prob = None
                    if isinstance(answer, dict):
                        prob = answer.get("noul")
                    elif isinstance(answer, (int, float)):
                        prob = answer
                    supported = None
                    if isinstance(prob, (int, float)):
                        supported = float(prob) >= 0.5
                    flags.append({"id": claim["id"], "supported": supported,
                                  "noul": prob, "fallback": False})
                result = JevEvaluationResult(
                    result.verdict, result.confidence, result.supported,
                    {"claim_flags": flags, "skipped": False}, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=False, model=result.model)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            structural["claim_flags"] = flags
            structural["skipped"] = False
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            flags = [{"id": c["id"], "supported": None, "fallback": True}
                     for c in normalized]
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"claim_flags": flags, "skipped": False}, [str(exc)],
                is_fallback=True, model=self.evaluator.model)
            structural = self._structural(fallback, site)
            structural["claim_flags"] = flags
            structural["skipped"] = False
            return fallback, structural

    def evaluate_completion_nouls(self, goal: str, state_summary: str,
                                  named_artifacts=None, root_dir=None, *,
                                  site: str = "completion",
                                  task_id: Optional[str] = None,
                                  max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-completion: artifact/goal nouls before the generative judge.

        Missing named artifact is code-owned truth: the envelope cannot
        complete regardless of live answers.
        """
        from pathlib import Path as _Path
        if named_artifacts is None:
            artifact_facts = named_artifact_status(goal, root_dir=root_dir)
        else:
            base = _Path(root_dir) if root_dir is not None else _Path.cwd()
            artifact_facts = []
            for item in named_artifacts:
                if isinstance(item, dict):
                    path = str(item.get("path") or "").replace("\\", "/")
                    present = bool(item.get("present"))
                    if path and not item.get("present") and "present" not in item:
                        present = (base / path).is_file()
                    artifact_facts.append({
                        "path": path,
                        "present": present,
                        "lines": item.get("lines"),
                    })
                else:
                    path = str(item).replace("\\", "/").strip("`'\" .")
                    present = bool(path) and (base / path).is_file()
                    artifact_facts.append({
                        "path": path, "present": present, "lines": None,
                    })
        missing = [item["path"] for item in artifact_facts
                   if item.get("path") and not item.get("present")]
        artifacts_payload = artifact_facts
        if missing:
            # Code-owned refuse: no spend required to know completion is false.
            answers = {
                "named_artifacts_present": 0.0,
                "goal_achieved": 0.0,
                "missing_artifacts": missing,
                "artifacts": artifacts_payload,
            }
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, answers,
                [f"named artifact missing: {p}" for p in missing],
                is_fallback=True, model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=None)
            structural["cannot_complete"] = True
            structural["missing_artifacts"] = missing
            return result, structural
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {
                    "goal": goal or "",
                    "execution_state": (state_summary or "")[:4000],
                    "named_artifacts": artifacts_payload,
                },
                completion_question_pack())
            answers = dict(result.answers or {})
            cannot = False
            if result.is_fallback:
                # Unkeyed: artifacts exist; generative judge remains the seat.
                answers.setdefault("named_artifacts_present", 1.0)
                answers.setdefault("goal_achieved", None)
                answers["artifacts"] = artifacts_payload
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0, answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=True, model=result.model)
            else:
                present = answers.get("named_artifacts_present")
                achieved = answers.get("goal_achieved")
                present_p = float(present.get("noul", 0.0)) if isinstance(present, dict) else float(present or 0.0)
                achieved_p = float(achieved.get("noul", 0.0)) if isinstance(achieved, dict) else float(achieved or 0.0)
                answers = dict(answers)
                answers["named_artifacts_present"] = present_p
                answers["goal_achieved"] = achieved_p
                answers["artifacts"] = artifacts_payload
                verdict = "pass" if present_p >= 0.5 and achieved_p >= 0.5 else "fail"
                result = JevEvaluationResult(
                    verdict, result.confidence,
                    min(present_p, achieved_p),
                    answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=False, model=result.model)
                cannot = present_p < 0.5 or achieved_p < 0.5
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            structural["cannot_complete"] = bool(cannot)
            structural["missing_artifacts"] = missing
            return result, structural
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            answers = {
                "named_artifacts_present": 1.0,
                "goal_achieved": 0.5,
                "artifacts": artifacts_payload,
            }
            fallback = JevEvaluationResult(
                "pass", 0.0, 0.5, answers, [str(exc)],
                is_fallback=True, model=self.evaluator.model)
            structural = self._structural(fallback, site)
            structural["cannot_complete"] = True
            structural["missing_artifacts"] = missing
            structural["reason"] = str(exc)
            return fallback, structural

    @staticmethod
    def _scope_noul(answers: Dict[str, Any], key: str) -> Optional[float]:
        val = (answers or {}).get(key)
        if isinstance(val, dict) and "noul" in val:
            try:
                return float(val["noul"])
            except (TypeError, ValueError):
                return None
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        return float(val)

    @staticmethod
    def _scope_score(answers: Dict[str, Any], key: str) -> Optional[float]:
        val = (answers or {}).get(key)
        if isinstance(val, dict) and "score" in val:
            try:
                return float(val["score"])
            except (TypeError, ValueError):
                return None
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        return float(val)

    @staticmethod
    def _scope_choice(answers: Dict[str, Any], key: str) -> Optional[str]:
        val = (answers or {}).get(key)
        if isinstance(val, dict):
            return normalize_complexity_class(val.get("choice"))
        return normalize_complexity_class(val)

    @staticmethod
    def _scope_determination(
        *,
        verifier_holds: bool,
        success_met: Optional[float],
        coverage: Optional[float],
        claims: Optional[float],
        needs_human: Optional[float],
        complexity: Optional[str],
        is_fallback: bool,
        scope_declared: bool,
        reasons: List[str],
    ) -> Dict[str, Any]:
        """HUL-C determination: complete only when every hold is true.

        Unkeyed / fallback evaluations may NEVER alone mark complete.
        """
        success_ok = (
            success_met is not None
            and success_met >= SCOPE_NOUL_HOLD
            and not is_fallback
        )
        claims_ok = (
            claims is not None
            and claims >= SCOPE_NOUL_HOLD
            and not is_fallback
        )
        coverage_ok = (
            coverage is not None
            and coverage >= SCOPE_COVERAGE_HOLD
            and not is_fallback
        )
        human_clear = (
            needs_human is None
            or needs_human < SCOPE_NOUL_HOLD
        )
        scope_holds = bool(
            scope_declared and coverage_ok and claims_ok and human_clear
        )
        complete = bool(
            verifier_holds
            and success_ok
            and scope_holds
            and not is_fallback
        )
        out_reasons = list(reasons)
        if not scope_declared:
            out_reasons.append("missing scope.in_scope — cannot complete")
        if is_fallback:
            out_reasons.append(
                "unkeyed fallback cannot alone mark mission complete")
        if verifier_holds is False:
            out_reasons.append("verifier does not hold")
        if success_met is not None and success_met < SCOPE_NOUL_HOLD:
            out_reasons.append("success_definition_met is low")
        if claims is not None and claims < SCOPE_NOUL_HOLD:
            out_reasons.append("claims_supported is low")
        if coverage is not None and coverage < SCOPE_COVERAGE_HOLD:
            out_reasons.append("scope_coverage is low")
        if needs_human is not None and needs_human >= SCOPE_NOUL_HOLD:
            out_reasons.append("needs_human is true")
        return {
            "complete": complete,
            "verifier_holds": bool(verifier_holds),
            "success_definition_met": success_ok,
            "scope_holds": scope_holds,
            "scope_coverage": coverage,
            "claims_supported": claims,
            "needs_human": (
                None if needs_human is None
                else bool(needs_human >= SCOPE_NOUL_HOLD)),
            "complexity_class": complexity,
            "is_fallback": bool(is_fallback),
            "site": HUL_SCOPE_SITE,
            "reasons": out_reasons,
        }

    @staticmethod
    def _scope_text(state: Any) -> str:
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("evidence_summary", "state_summary", "request",
                        "text", "prompt"):
                value = state.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return ""
        return "" if state is None else str(state)

    @staticmethod
    def _scope_state_facts(state: Any) -> Dict[str, Any]:
        if not isinstance(state, dict):
            return {
                "mission_id": None,
                "request": "",
                "success_definition": "",
                "scope": {"in_scope": [], "out_of_scope": []},
                "verifier_holds": False,
                "evidence_summary": JevPolicy._scope_text(state),
            }
        scope = state.get("scope")
        if not isinstance(scope, dict):
            scope = {"in_scope": [], "out_of_scope": []}
        in_scope = scope.get("in_scope")
        out_scope = scope.get("out_of_scope")
        return {
            "mission_id": state.get("mission_id"),
            "request": str(state.get("request") or ""),
            "success_definition": str(state.get("success_definition") or ""),
            "scope": {
                "in_scope": list(in_scope) if isinstance(in_scope, (list, tuple)) else [],
                "out_of_scope": list(out_scope) if isinstance(out_scope, (list, tuple)) else [],
            },
            "verifier_holds": bool(state.get("verifier_holds", False)),
            "evidence_summary": JevPolicy._scope_text(state),
        }

    def evaluate_scope(self, mission_state, *, site: str = HUL_SCOPE_SITE,
                       task_id: Optional[str] = None,
                       node_id: Optional[str] = None,
                       max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """HUL-C: mission scope gate via the ONE policy owner.

        Determination contract:
        - complete only if verifier_holds AND success_definition_met AND
          scope hold (declared in_scope + coverage + claims + no human block)
        - unkeyed / fallback may NOT alone mark complete
        - missing scope or low success → complete false
        - ledger gets one ``jev_eval``; mission packs store the same via
          ``mission_record.append_jev_eval`` (no second Jev client)

        Returns ``(result, structural, determination)``.
        """
        facts = self._scope_state_facts(mission_state)
        scope_declared = scope_in_scope_holds(facts["scope"])
        payload = {
            "mission_id": facts["mission_id"],
            "request": facts["request"],
            "success_definition": facts["success_definition"],
            "scope": facts["scope"],
            "evidence_summary": facts["evidence_summary"][:4000],
            "verifier_holds": facts["verifier_holds"],
        }
        questions = hul_scope_question_pack()

        if not self.keyed:
            answers = {
                "scope_coverage": {"type": "score", "score": 0.0},
                "success_definition_met": {"type": "noul", "noul": 0.0},
                "claims_supported": {"type": "noul", "noul": 0.0},
                "needs_human": {"type": "noul", "noul": 0.0},
                "complexity_class": {"type": "choice", "choice": None},
            }
            determination = self._scope_determination(
                verifier_holds=facts["verifier_holds"],
                success_met=0.0,
                coverage=0.0,
                claims=0.0,
                needs_human=0.0,
                complexity=None,
                is_fallback=True,
                scope_declared=scope_declared,
                reasons=["unkeyed: scope pack not evaluated live"],
            )
            result = JevEvaluationResult(
                "fail" if not determination["complete"] else "pass",
                0.0, 0.0, answers, determination["reasons"],
                is_fallback=True, model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            structural["determination"] = determination
            return result, structural, determination

        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(payload, questions)
            answers = dict(result.answers or {}) if isinstance(result.answers, dict) else {}
            is_fallback = bool(result.is_fallback)
            coverage = self._scope_score(answers, "scope_coverage")
            success_met = self._scope_noul(answers, "success_definition_met")
            claims = self._scope_noul(answers, "claims_supported")
            needs_human = self._scope_noul(answers, "needs_human")
            complexity = self._scope_choice(answers, "complexity_class")
            if is_fallback:
                # Transport/fallback: never allow complete from unkeyed path.
                success_met = min(success_met, 0.0) if success_met is not None else 0.0
                coverage = min(coverage, 0.0) if coverage is not None else 0.0
                claims = min(claims, 0.0) if claims is not None else 0.0
            determination = self._scope_determination(
                verifier_holds=facts["verifier_holds"],
                success_met=success_met,
                coverage=coverage,
                claims=claims,
                needs_human=needs_human,
                complexity=complexity,
                is_fallback=is_fallback,
                scope_declared=scope_declared,
                reasons=list(result.reasons or []),
            )
            normalized = dict(answers)
            normalized["scope_coverage"] = coverage
            normalized["success_definition_met"] = success_met
            normalized["claims_supported"] = claims
            normalized["needs_human"] = determination["needs_human"]
            normalized["complexity_class"] = complexity
            normalized["determination"] = determination
            verdict = "pass" if determination["complete"] else "fail"
            supported = min(
                [x for x in (success_met, claims, coverage) if x is not None]
                or [0.0])
            result = JevEvaluationResult(
                verdict,
                float(result.confidence or 0.0),
                float(supported),
                normalized,
                determination["reasons"] or list(result.reasons or []),
                cost=result.cost,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                is_fallback=is_fallback,
                model=result.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            structural["determination"] = determination
            return result, structural, determination
        except HarnessError as exc:
            if reservation is not None and self.governor is not None:
                try:
                    self.governor.reconcile(reservation, 0.0)
                except HarnessError:
                    pass
            determination = self._scope_determination(
                verifier_holds=facts["verifier_holds"],
                success_met=0.0,
                coverage=0.0,
                claims=0.0,
                needs_human=0.0,
                complexity=None,
                is_fallback=True,
                scope_declared=scope_declared,
                reasons=[str(exc), "scope evaluation failed closed"],
            )
            fallback = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, determination["reasons"],
                is_fallback=True, model=self.evaluator.model)
            structural = self._structural(fallback, site)
            structural["determination"] = determination
            structural["reason"] = str(exc)
            return fallback, structural, determination

    @staticmethod
    def _issue_sort_text(state: Any) -> str:
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("issue", "text", "prompt", "note", "reason"):
                value = state.get(key)
                if isinstance(value, str):
                    return value
            return ""
        return "" if state is None else str(state)

    @staticmethod
    def _issue_sort_empty_combo(*, is_fallback: bool = True,
                                structural: Optional[Dict[str, Any]] = None,
                                evidence=None,
                                pack_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "bucket": None,
            "path_id": None,
            "confidence": 0.0,
            "evidence_refs": list(evidence or []),
            "suggested_next_action": None,
            "is_fallback": bool(is_fallback),
            "pack_id": pack_id,
            "kind": None,
            "attention": None,
            "structural": structural,
        }

    @staticmethod
    def _issue_sort_combo(bucket_id, pack_doc, *, confidence, evidence,
                          is_fallback, structural) -> Dict[str, Any]:
        """Bind the combo to pack fields only — never invent path/action."""
        pack_id = (pack_doc or {}).get("id")
        buckets = (pack_doc or {}).get("buckets") or {}
        entry = buckets.get(bucket_id) if bucket_id else None
        if not entry:
            combo = JevPolicy._issue_sort_empty_combo(
                is_fallback=True, structural=structural, evidence=evidence,
                pack_id=pack_id)
            return combo
        return {
            "bucket": bucket_id,
            "path_id": entry["path_id"],
            "confidence": float(confidence or 0.0),
            "evidence_refs": list(evidence or []),
            "suggested_next_action": entry.get("suggested_next_action"),
            "is_fallback": bool(is_fallback),
            "pack_id": pack_id,
            "kind": entry["kind"],
            "attention": entry.get("attention"),
            "structural": structural,
        }

    def evaluate_issue_sort(self, state, pack, *, site: str = "issue_sort",
                            task_id: Optional[str] = None,
                            node_id: Optional[str] = None,
                            max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Sort an issue into an operator-declared bucket (JEV-P5 owner).

        0-hallucination contract:
        - choice criteria = operator bucket labels only (via jev_packs);
          ``_parse_answer`` already requires choice ∈ criteria.
        - unkeyed / transport fail / out-of-pack → ``is_fallback=true``;
          keyword match only against pack keywords.
        - no match → ``bucket=None``, ``path_id=None``.
        - ``suggested_next_action`` always equals ``pack[bucket]`` when set.
        ONE owner: this method + ``structural.site=issue_sort`` + one
        ledger ``jev_eval`` per call.
        Returns ``(result, structural, combo)``.
        """
        issue_text = self._issue_sort_text(state)

        try:
            pack_doc = validate_operator_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            combo = self._issue_sort_empty_combo(
                is_fallback=True, structural=structural,
                evidence=result.reasons)
            return result, structural, combo

        def local_result(bucket_id, score, evidence, reasons, *, model=None):
            evidence_refs = list(reasons or []) + list(evidence or [])
            if bucket_id:
                return JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"bucket": bucket_id, "score": score},
                    (reasons or ["keyword match against operator pack"]),
                    is_fallback=True,
                    model=model or self.evaluator.model), evidence_refs
            return JevEvaluationResult(
                "fail", 0.0, 0.0, {"bucket": None},
                (list(reasons or []) + ["no declared pack keyword match"]),
                is_fallback=True,
                model=model or self.evaluator.model), evidence_refs

        def keyword_sort(reasons, *, model=None, cost=0.0, input_tokens=0,
                         output_tokens=0, reservation=None):
            bucket_id, score, evidence = match_keywords(issue_text, pack_doc)
            result, evidence_refs = local_result(
                bucket_id, score, evidence, reasons, model=model)
            if cost or input_tokens or output_tokens:
                result = JevEvaluationResult(
                    result.verdict, result.confidence, result.supported,
                    result.answers, result.reasons, cost=cost,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    is_fallback=True, model=result.model)
            # ONE ledger jev_eval per evaluate_issue_sort call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            combo = self._issue_sort_combo(
                bucket_id, pack_doc, confidence=0.0, evidence=evidence_refs,
                is_fallback=True, structural=structural)
            return result, structural, combo

        if not self.keyed:
            # Unkeyed: skip live; code-owned keyword match only.
            return keyword_sort(["unkeyed: keyword match only"])

        reservation = None
        try:
            questions = issue_sort_question_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"issue": issue_text, "pack_id": pack_doc["id"]},
                questions)
        except HarnessError as exc:
            return keyword_sort([str(exc)], reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        bucket_ans = answers.get("bucket")
        choice = bucket_ans.get("choice") if isinstance(bucket_ans, dict) else None
        pack_ids = set(pack_doc["buckets"])
        declared = (not result.is_fallback
                    and isinstance(choice, str)
                    and choice in pack_ids)

        if declared and result.verdict == "pass":
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            combo = self._issue_sort_combo(
                choice, pack_doc,
                confidence=result.confidence,
                evidence=list(result.reasons) + [f"choice:{choice}"],
                is_fallback=False, structural=structural)
            return result, structural, combo

        # Out-of-pack / transport fail / fallback / unparseable choice:
        # never invent a bucket. Keyword match against pack only.
        reasons = list(result.reasons) if result.reasons else []
        if isinstance(choice, str) and choice not in pack_ids:
            reasons = reasons + [
                f"out-of-pack choice refused: {choice!r}"]
        return keyword_sort(
            reasons, model=result.model,
            cost=float(result.cost or 0.0),
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            reservation=reservation)

    @staticmethod
    def _log_item_text(state: Any) -> str:
        """Text extraction for log items (accepts the ``item`` key)."""
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("item", "text", "issue", "reason", "note"):
                value = state.get(key)
                if isinstance(value, str):
                    return value
            return ""
        return "" if state is None else str(state)

    @staticmethod
    def _log_judgment(bucket_id, pack_doc, *, score_level=None,
                      score_value=None, score_confidence=0.0,
                      evidence=None, is_fallback, structural):
        """Bind the log judgment to pack fields only — never invent."""
        pack_doc = pack_doc or {}
        entry = (pack_doc.get("buckets") or {}).get(bucket_id) \
            if isinstance(bucket_id, str) else None
        if entry is None:
            bucket_id = None
        return {
            "bucket": bucket_id,
            "path_id": entry.get("path_id") if entry else None,
            "kind": entry.get("kind") if entry else None,
            "attention": entry.get("attention") if entry else None,
            "suggested_next_action": entry.get("suggested_next_action") if entry else None,
            "score": {
                "id": (pack_doc.get("score") or {}).get("id"),
                "level": score_level,
                "value": score_value,
                "confidence": float(score_confidence or 0.0),
            },
            "evidence_refs": list(evidence or []),
            "is_fallback": bool(is_fallback),
            "pack_id": pack_doc.get("id"),
            "structural": structural,
        }

    def evaluate_log_item(self, state, pack, *, site=LOG_FACTOR_SITE,
                          task_id: Optional[str] = None,
                          node_id: Optional[str] = None,
                          max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Jev audit of one log item against an operator log pack (JEV-LOG).

        0-hallucination contract (same class as ``evaluate_issue_sort``):
        - ``bucket`` ∈ operator pack keys, else ``None`` — out-of-pack,
          unparseable, transport-failed, or unkeyed runs fall back to the
          code-owned keyword matcher against pack keywords only.
        - ``score.level`` ∈ operator score levels, else ``None`` — the live
          level is the declared level string with the highest probability.
        - ``path_id`` / ``suggested_next_action`` always come from the pack.
        - ONE ledger ``jev_eval`` per call; ``structural.site=log_factor``.
        Returns ``(result, structural, judgment)``.
        """
        text = self._log_item_text(state)
        try:
            pack_doc = validate_log_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            judgment = self._log_judgment(
                None, None, is_fallback=True, structural=structural,
                evidence=result.reasons)
            return result, structural, judgment

        def keyword_judgment(reasons, *, model=None, cost=0.0,
                             input_tokens=0, output_tokens=0,
                             reservation=None):
            bucket_id, hits, evidence = match_keywords(text, pack_doc)
            evidence_refs = list(reasons or []) + list(evidence or [])
            if bucket_id:
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"bucket": bucket_id},
                    (reasons or ["keyword match against operator pack"]),
                    cost=cost, input_tokens=input_tokens,
                    output_tokens=output_tokens, is_fallback=True,
                    model=model or self.evaluator.model)
            else:
                result = JevEvaluationResult(
                    "fail", 0.0, 0.0, {"bucket": None},
                    (list(reasons or []) + ["no declared pack keyword match"]),
                    cost=cost, input_tokens=input_tokens,
                    output_tokens=output_tokens, is_fallback=True,
                    model=model or self.evaluator.model)
            # ONE ledger jev_eval per evaluate_log_item call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._log_judgment(
                bucket_id, pack_doc, is_fallback=True,
                structural=structural, evidence=evidence_refs)
            return result, structural, judgment

        if not self.keyed:
            # Unkeyed: skip live; code-owned keyword match only.
            return keyword_judgment(["unkeyed: keyword match only"])

        reservation = None
        try:
            questions = log_factor_question_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"item": text, "pack_id": pack_doc["id"]}, questions)
        except HarnessError as exc:
            return keyword_judgment([str(exc)], reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        score_id = pack_doc["score"]["id"]
        bucket_ans = answers.get("bucket")
        choice = bucket_ans.get("choice") if isinstance(bucket_ans, dict) else None
        pack_ids = set(pack_doc["buckets"])
        declared = (not result.is_fallback
                    and isinstance(choice, str)
                    and choice in pack_ids)

        # Live score: the DECLARED level string (via the official legend:
        # anchors -> criteria strings) with the highest probability, else None.
        score_level = score_value = None
        score_conf = 0.0
        score_ans = answers.get(score_id)
        if not result.is_fallback and isinstance(score_ans, dict):
            probs = score_ans.get("probabilities")
            legend = score_ans.get("legend")
            pack_levels = set(pack_doc["score"]["levels"])
            if isinstance(probs, dict) and isinstance(legend, dict):
                for anchor, level in legend.items():
                    if not isinstance(level, str) or level not in pack_levels:
                        continue
                    prob = probs.get(str(anchor))
                    if isinstance(prob, (int, float)) and (
                            score_value is None or float(prob) > score_value):
                        score_level, score_value = level, float(prob)
            raw_conf = score_ans.get("confidence")
            if isinstance(raw_conf, (int, float)):
                score_conf = float(raw_conf)

        if declared and result.verdict == "pass":
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._log_judgment(
                choice, pack_doc, score_level=score_level,
                score_value=score_value, score_confidence=score_conf,
                evidence=list(result.reasons) + [f"choice:{choice}"],
                is_fallback=False, structural=structural)
            return result, structural, judgment

        # Out-of-pack / transport fail / fallback / unparseable: never invent
        # a bucket or a score level. Keyword match against the pack only.
        reasons = list(result.reasons) if result.reasons else []
        if isinstance(choice, str) and choice not in pack_ids:
            reasons = reasons + [f"out-of-pack choice refused: {choice!r}"]
        return keyword_judgment(
            reasons, model=result.model,
            cost=float(result.cost or 0.0),
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            reservation=reservation)

    @staticmethod
    def _repo_element_text(state: Any) -> str:
        """Bounded text view of one element state for keyword fallback."""
        if isinstance(state, str):
            return state[:1200]
        if not isinstance(state, dict):
            return str(state)[:1200]
        parts = [str(state.get("path") or ""), str(state.get("kind") or ""),
                 str(state.get("summary") or ""),
                 " ".join(str(s) for s in (state.get("symbols") or [])[:18]),
                 " ".join(str(h) for h in (state.get("headings") or [])[:12])]
        if state.get("element_kind") == "symbol":
            parts.append(str(state.get("symbol") or ""))
            parts.append(str(state.get("module_summary") or ""))
        return " ".join(p for p in parts if p)[:1200]

    @staticmethod
    def _repo_judgment(pack_doc, axes, level, value, confidence, nouls, *,
                       is_fallback: bool, evidence,
                       axis_confidence=None) -> Dict[str, Any]:
        """The stable JEV-P6 judgment shape (declared ids or None only).

        ``axis_confidence`` carries each choice's distribution confidence
        (TypeSafe-derived) so consumers can tell a settled axis from a
        near-tie that the seat may flip run-to-run (measured non-zero on
        identical input; see the 2026-09-22 determinism probe).
        """
        pack_doc = pack_doc if isinstance(pack_doc, dict) else {}
        score = pack_doc.get("score") if isinstance(pack_doc.get("score"), dict) else {}
        return {
            "pack_id": pack_doc.get("id"),
            "axes": dict(axes or {}),
            "axis_confidence": {
                axis: (float(value)
                       if isinstance(value, (int, float))
                       and not isinstance(value, bool) else None)
                for axis, value in (axis_confidence or {}).items()},
            "attention": {"id": score.get("id"), "level": level,
                          "value": value, "confidence": confidence},
            "nouls": dict(nouls or {}),
            "is_fallback": bool(is_fallback),
            "evidence": list(evidence or []),
        }

    def evaluate_repo_summary(self, state, pack, *, site=REPO_SUMMARY_SITE,
                              task_id: Optional[str] = None,
                              node_id: Optional[str] = None,
                              max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Judge ONE repo element against the operator repo pack (JEV-P6).

        0-hallucination contract (same class as ``evaluate_log_item``):
        - every axis value \u2208 operator criteria keys, else ``None`` -- an
          out-of-vocabulary choice is reported, never replaced by a guess;
        - ``attention.level`` \u2208 operator score levels (via the official
          legend), else ``None``; ``attention.value`` is the winning
          probability;
        - nouls ride as raw probabilities -- a low noul is an answer, not a
          parse failure, so verdict thresholds never discard classifications;
        - unkeyed / transport-fail / shape-invalid paths fall back to the
          code-owned keyword matcher (``keywords`` in the pack only);
        - ONE ledger ``jev_eval`` per call; ``structural.site=repo_summary``.
        Returns ``(result, structural, judgment)``.
        """
        text = self._repo_element_text(state)
        try:
            pack_doc = validate_repo_summary_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            judgment = self._repo_judgment(
                None, None, None, None, None, None,
                is_fallback=True, evidence=result.reasons)
            return result, structural, judgment

        axis_ids = {axis: set(spec["criteria"])
                    for axis, spec in pack_doc["axes"].items()}
        level_ids = set(pack_doc["score"]["levels"])

        def fallback_judgment(reasons, *, model=None, cost=0.0,
                              input_tokens=0, output_tokens=0,
                              reservation=None):
            axes = heuristic_repo_axes(text, pack_doc)
            matched = any(v for v in axes.values())
            evidence = list(reasons or []) + [
                f"{axis}:{value}" for axis, value in sorted(axes.items())
                if value is not None]
            result = JevEvaluationResult(
                "pass" if matched else "fail", 0.0, 1.0 if matched else 0.0,
                {"axes": axes}, evidence,
                cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._repo_judgment(
                pack_doc, axes, None, None, None, None,
                is_fallback=True, evidence=evidence)
            return result, structural, judgment

        if not self.keyed:
            return fallback_judgment(
                ["unkeyed: code-owned keyword fallback only"])

        reservation = None
        try:
            questions = repo_summary_question_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            payload = state if isinstance(state, dict) else {"item": str(state)}
            result = self.evaluator.evaluate(payload, questions)
        except HarnessError as exc:
            return fallback_judgment([str(exc)], reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        if result.is_fallback or not answers:
            # Transport fail or shape-invalid response: never present a
            # heuristic classification as a live one.
            return fallback_judgment(
                list(result.reasons or ["invalid TypeSafe response"]),
                model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation)

        axes: Dict[str, Optional[str]] = {}
        axis_confidence: Dict[str, Optional[float]] = {}
        evidence: List[str] = []
        for axis in pack_doc["axes"]:
            answer = answers.get(axis)
            choice = answer.get("choice") if isinstance(answer, dict) else None
            raw_axis_conf = (answer.get("confidence")
                             if isinstance(answer, dict) else None)
            axis_confidence[axis] = (
                float(raw_axis_conf)
                if isinstance(raw_axis_conf, (int, float))
                and not isinstance(raw_axis_conf, bool) else None)
            if isinstance(choice, str) and choice in axis_ids[axis]:
                axes[axis] = choice
                evidence.append(f"{axis}:{choice}")
            else:
                axes[axis] = None
                evidence.append(f"{axis}:unmatched")

        level = None
        value = None
        confidence = 0.0
        score_id = pack_doc["score"]["id"]
        score_answer = answers.get(score_id)
        if isinstance(score_answer, dict):
            probabilities = score_answer.get("probabilities")
            legend = score_answer.get("legend")
            if isinstance(probabilities, dict) and isinstance(legend, dict):
                for anchor, label in legend.items():
                    if not isinstance(label, str) or label not in level_ids:
                        continue
                    probability = probabilities.get(str(anchor))
                    if isinstance(probability, (int, float)) and (
                            value is None or float(probability) > value):
                        level, value = label, float(probability)
            raw_confidence = score_answer.get("confidence")
            if isinstance(raw_confidence, (int, float)):
                confidence = float(raw_confidence)
        if level is None:
            evidence.append("attention:unmatched")

        nouls: Dict[str, Optional[float]] = {}
        for name in pack_doc["nouls"]:
            answer = answers.get(name)
            raw = answer.get("noul") if isinstance(answer, dict) else None
            nouls[name] = (float(raw)
                           if isinstance(raw, (int, float))
                           and not isinstance(raw, bool) else None)
            if nouls[name] is None:
                evidence.append(f"{name}:unmatched")

        if not any(v is not None for v in axes.values()) and level is None:
            return fallback_judgment(
                evidence + list(result.reasons or []), model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation)

        structural = self._account(
            result, site=site, task_id=task_id, node_id=node_id,
            reservation=reservation)
        judgment = self._repo_judgment(
            pack_doc, axes, level, value, confidence, nouls,
            is_fallback=False, evidence=evidence,
            axis_confidence=axis_confidence)
        return result, structural, judgment

    @staticmethod
    def _route_query_text(state: Any) -> str:
        """Text extraction for route queries (accepts the ``goal`` key)."""
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("goal", "query", "prompt", "issue", "text"):
                value = state.get(key)
                if isinstance(value, str):
                    return value
            return ""
        return "" if state is None else str(state)

    def evaluate_model_route(self, state, pack, *, site: str = ROUTE_QUERY_SITE,
                             task_id: Optional[str] = None,
                             node_id: Optional[str] = None,
                             max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Route a user query onto the declared model ladder (SITE-2).

        0-hallucination contract (same class as ``evaluate_issue_sort``):
        - choice criteria = declared rung ids only (via ``route_pack``);
          ``_parse_answer`` already requires choice ∈ criteria.
        - unkeyed / transport fail / out-of-ladder → the code-owned tier
          heuristic answers with ``is_fallback=True``; no rung is invented.
        - ladder cannot satisfy the heuristic floor → ``rung_id=None`` (the
          honest "no declared rung can do this" answer).
        - ONE ledger ``jev_eval`` per call; ``structural.site=model_route``.
        Extends (never modifies) ``evaluate_route``: lane choice stays there;
        this method chooses the cheapest capable DECLARED rung.
        Returns ``(result, structural, combo)``.
        """
        goal_text = self._route_query_text(state)

        try:
            pack_doc = validate_route_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            combo = route_combo(
                None, None, tier=None, reasons=[str(exc)],
                is_fallback=True, structural=structural)
            return result, structural, combo

        def heuristic_route(reasons, *, model=None, cost=0.0,
                            input_tokens=0, output_tokens=0,
                            reservation=None):
            rung_id, tier, fb_reasons = fallback_route(goal_text, pack_doc)
            result = JevEvaluationResult(
                "pass" if rung_id else "fail", 0.0, 1.0 if rung_id else 0.0,
                {"rung": rung_id, "tier": tier},
                list(reasons or []) + list(fb_reasons),
                is_fallback=True,
                model=model or self.evaluator.model)
            if cost or input_tokens or output_tokens:
                result = JevEvaluationResult(
                    result.verdict, result.confidence, result.supported,
                    result.answers, result.reasons, cost=cost,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    is_fallback=True, model=result.model)
            # ONE ledger jev_eval per evaluate_model_route call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            combo = route_combo(
                rung_id, pack_doc, tier=tier,
                reasons=list(result.reasons), is_fallback=True,
                structural=structural)
            return result, structural, combo

        if not self.keyed:
            return heuristic_route(["unkeyed: deterministic tier heuristic only"])

        reservation = None
        try:
            questions = route_query_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"goal": goal_text, "pack_id": pack_doc["id"]},
                questions)
        except HarnessError as exc:
            return heuristic_route([str(exc)], reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        rung_ans = answers.get("rung")
        choice = rung_ans.get("choice") if isinstance(rung_ans, dict) else None
        declared = {r["rung_id"] for r in pack_doc["rungs"]}
        in_ladder = (not result.is_fallback
                     and isinstance(choice, str)
                     and choice in declared)

        if in_ladder and result.verdict == "pass":
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            entry = next(r for r in pack_doc["rungs"]
                         if r["rung_id"] == choice)
            combo = route_combo(
                choice, pack_doc, tier=entry["tier"],
                reasons=list(result.reasons or []) + [f"choice:{choice}"],
                confidence=result.confidence, is_fallback=False,
                structural=structural)
            return result, structural, combo

        # Out-of-ladder / transport fail / fallback / unparseable choice:
        # never invent a rung. Deterministic heuristic answers instead.
        reasons = list(result.reasons) if result.reasons else []
        if isinstance(choice, str) and choice not in declared:
            reasons = reasons + [
                f"out-of-ladder choice refused: {choice!r}"]
        return heuristic_route(
            reasons, model=result.model,
            cost=float(result.cost or 0.0),
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            reservation=reservation)

    @staticmethod
    def _completion_state_text(state: Any) -> str:
        """Bounded text view of one phase's evidence for the JEV-BAR TypeSafe
        payload and the code-owned keyword fallback."""
        if isinstance(state, str):
            return state[:1200]
        if not isinstance(state, dict):
            return str(state)[:1200]
        parts = [
            str(state.get("phase") or ""),
            str(state.get("status_row") or ""),
            " ".join(str(b) for b in (state.get("open_blockers") or [])[:12]),
            " ".join(str(t) for t in (state.get("tests_missing") or [])[:12]),
            f"pr_merged={state.get('pr_merged')}",
            f"local_gates_green={state.get('local_gates_green')}",
            f"ci_green={state.get('ci_green')}",
            " ".join(str(n) for n in (state.get("notes") or [])[:6]),
        ]
        return " ".join(p for p in parts if p)[:1200]

    @staticmethod
    def _completion_judgment(pack_doc, live_levels, live_confidence, primary_gap,
                             *, is_fallback: bool, evidence) -> Dict[str, Any]:
        pack_doc = pack_doc if isinstance(pack_doc, dict) else {}
        return {
            "pack_id": pack_doc.get("id"),
            "live_levels": dict(live_levels or {}),
            "live_confidence": dict(live_confidence or {}),
            "primary_gap": primary_gap,
            "is_fallback": bool(is_fallback),
            "evidence": list(evidence or []),
        }

    def evaluate_phase_completion(self, state, pack, *, site=PHASE_COMPLETION_SITE,
                                  task_id: Optional[str] = None,
                                  node_id: Optional[str] = None,
                                  max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Judge one mission phase's evidence against the operator
        phase-completion sentiment pack (JEV-BAR).

        0-hallucination contract (same class as ``evaluate_log_item`` /
        ``evaluate_repo_summary``):
        - every axis's ``live_levels`` value is an index into the pack's own
          declared ``sentiment.levels`` (via the official legend: anchors ->
          criteria strings, highest probability wins), else ``None`` -- a
          missing/invalid answer never invents a level;
        - ``primary_gap`` ∈ declared bucket ids, else ``None``; the choice
          ``\"none\"`` also maps to ``None`` (no improvement needed); an
          out-of-pack choice is refused (reason recorded) without failing the
          whole call;
        - unkeyed / invalid-pack / transport-fail / all-axes-invalid paths
          fall back to the code-owned keyword matcher (``buckets[].keywords``
          in the pack only) for ``primary_gap``; ``live_levels`` stay ``None``
          across the board -- code's own heuristic (``jev_packs.
          heuristic_completion_sentiment``) lives outside this call and is
          mixed in by the caller (``jev_completion.score_phase_completion``),
          never invented here;
        - ONE ledger ``jev_eval`` per call; ``structural.site=phase_completion``.
        Returns ``(result, structural, judgment)``.
        """
        text = self._completion_state_text(state)
        try:
            pack_doc = validate_completion_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            judgment = self._completion_judgment(
                None, None, None, None, is_fallback=True, evidence=result.reasons)
            return result, structural, judgment

        axis_ids = list(pack_doc["axes"])
        level_ids = list(pack_doc["sentiment"]["levels"])
        bucket_ids = set(pack_doc["buckets"])
        none_live_levels = {axis: None for axis in axis_ids}
        none_live_confidence = {axis: None for axis in axis_ids}

        def fallback_judgment(reasons, *, model=None, cost=0.0,
                              input_tokens=0, output_tokens=0,
                              reservation=None):
            bucket_id, _hits, kw_evidence = match_completion_keywords(text, pack_doc)
            evidence_refs = list(reasons or []) + list(kw_evidence or [])
            result = JevEvaluationResult(
                "pass" if bucket_id else "fail", 0.0, 1.0 if bucket_id else 0.0,
                {"primary_gap": bucket_id}, evidence_refs,
                cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or self.evaluator.model)
            # ONE ledger jev_eval per evaluate_phase_completion call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._completion_judgment(
                pack_doc, none_live_levels, none_live_confidence, bucket_id,
                is_fallback=True, evidence=evidence_refs)
            return result, structural, judgment

        if not self.keyed:
            return fallback_judgment(["unkeyed: keyword match only"])

        reservation = None
        try:
            questions = completion_bar_question_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            payload = {
                "phase": state.get("phase") if isinstance(state, dict) else None,
                "evidence": text, "pack_id": pack_doc["id"],
            }
            result = self.evaluator.evaluate(payload, questions)
        except HarnessError as exc:
            return fallback_judgment([str(exc)], reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        if result.is_fallback or not answers:
            # Transport fail or shape-invalid response: never present a
            # heuristic classification as a live one.
            return fallback_judgment(
                list(result.reasons or ["invalid TypeSafe response"]),
                model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation)

        live_levels: Dict[str, Optional[int]] = {}
        live_confidence: Dict[str, Optional[float]] = {}
        evidence: List[str] = []
        any_axis_valid = False
        for axis in axis_ids:
            answer = answers.get(axis)
            level = None
            value = None
            conf = None
            if isinstance(answer, dict):
                probs = answer.get("probabilities")
                legend = answer.get("legend")
                if isinstance(probs, dict) and isinstance(legend, dict):
                    for anchor, lvl in legend.items():
                        if not isinstance(lvl, str) or lvl not in level_ids:
                            continue
                        prob = probs.get(str(anchor))
                        if isinstance(prob, (int, float)) and (
                                value is None or float(prob) > value):
                            level, value = lvl, float(prob)
                raw_conf = answer.get("confidence")
                if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
                    conf = float(raw_conf)
            if level is not None:
                any_axis_valid = True
                live_levels[axis] = level_ids.index(level)
                evidence.append(f"{axis}:{level}")
            else:
                live_levels[axis] = None
                evidence.append(f"{axis}:unmatched")
            live_confidence[axis] = conf

        primary_gap: Optional[str] = None
        gap_answer = answers.get("primary_gap")
        choice = gap_answer.get("choice") if isinstance(gap_answer, dict) else None
        if isinstance(choice, str) and choice == "none":
            evidence.append("primary_gap:none")
        elif isinstance(choice, str) and choice in bucket_ids:
            primary_gap = choice
            evidence.append(f"primary_gap:{choice}")
        elif isinstance(choice, str):
            evidence.append(f"out-of-pack primary_gap refused: {choice!r}")
        else:
            evidence.append("primary_gap:unmatched")

        if not any_axis_valid:
            # Every axis was missing/invalid: present the whole call as a
            # fallback rather than a live judgment with nothing declared.
            return fallback_judgment(
                evidence + list(result.reasons or []), model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation)

        structural = self._account(
            result, site=site, task_id=task_id, node_id=node_id,
            reservation=reservation)
        judgment = self._completion_judgment(
            pack_doc, live_levels, live_confidence, primary_gap,
            is_fallback=False, evidence=evidence)
        return result, structural, judgment

    @staticmethod
    def _audit_dimension_text(dimension_evidence: Any) -> str:
        lines = ["Harness 4-Dimensional Self-Audit Evidence:"]
        for dim in ("A", "R", "SM", "SD"):
            ev = (dimension_evidence or {}).get(dim, {})
            score = ev.get("score", 0.0) if isinstance(ev, dict) else 0.0
            satisfied = ev.get("checks_satisfied", 0)
            total = ev.get("checks_count", 0)
            lines.append(f"Dimension {dim}: score={score:.2f}/10 ({satisfied}/{total} checks fully satisfied)")
            for ch in (ev.get("checks") or [])[:5]:
                cid = ch.get("id", "")
                mark = "pass" if ch.get("score", 0) >= 1 else "part"
                label = ch.get("label", "")
                lines.append(f"  [{cid}] {mark} {label}")
        return "\n".join(lines)

    def evaluate_audit_dimensions(
        self,
        dimension_evidence: Dict[str, Any],
        pack: Any = None,
        *,
        site: str = AUDIT_DIMENSIONS_SITE,
        task_id: Optional[str] = "audit",
    ) -> Dict[str, Any]:
        """Evaluate the 4 self-audit dimensions with Jev as the authoritative gate.

        0-hallucination / honesty contract (same class as
        ``evaluate_log_item`` / ``evaluate_phase_completion``):

        - a dimension ABSENT from ``dimension_evidence`` (a partial ``--dim``
          run) is reported ``not_evaluated`` -- ``level_index``/``score`` are
          ``None`` -- never a false 0.0/"failing" score for a check that
          never ran;
        - a live ``dim_<id>`` answer resolves to a level via the official
          legend (anchor -> declared level string) + highest probability,
          exactly like every other score-typed site in this module -- never
          a raw score-as-index guess. An unmatched/invalid live answer for
          one EVALUATED dimension falls back to that dimension's own
          evidence-calibrated heuristic; if every evaluated dimension is
          unmatched, the whole call is presented as a fallback rather than a
          live judgment with nothing actually declared by Jev;
        - ``bar_95_pass`` is true only when ALL FOUR declared dimensions
          were evaluated AND every one scores >= 9.5 -- fail closed, never a
          false PASS on a partial run;
        - preflight reservation + EXACTLY ONE ledger ``jev_eval`` per call
          (``structural.site=audit_dimensions``) on every path -- unkeyed,
          governor-missing, transport-error, and invalid-response fallbacks
          included -- via the shared ``_account`` accounting helper (never a
          bare ``jev_refusal`` standing in for the one required
          ``jev_eval``).
        """
        from .jev_packs import (
            audit_dimensions_question_pack,
            heuristic_audit_dimensions,
            validate_audit_pack,
        )

        pack_doc = validate_audit_pack(pack)
        questions = audit_dimensions_question_pack(pack_doc)
        levels = pack_doc["sentiment"]["levels"]
        ordinals = pack_doc["sentiment"]["ordinals"]
        bar_idx = pack_doc["sentiment"].get("bar_met_index", 3)
        dim_ids = list(pack_doc["dimensions"])
        evidence_is_dict = isinstance(dimension_evidence, dict)

        model_name = getattr(self.evaluator, "model", "jev-latest")

        def dim_evaluated(dim: str) -> bool:
            return (not evidence_is_dict) or (dim in dimension_evidence)

        def bar_pass(dim_results: Dict[str, Any]) -> bool:
            if set(dim_results) != set(dim_ids):
                return False
            if any(not d.get("evaluated") for d in dim_results.values()):
                return False
            scores = [d.get("score") for d in dim_results.values()]
            if any(s is None for s in scores):
                return False
            return all(float(s) >= 9.5 for s in scores)

        def fallback_judgment(reasons, *, model=None, cost=0.0, input_tokens=0,
                              output_tokens=0, reservation=None):
            dim_results = heuristic_audit_dimensions(dimension_evidence, pack_doc)
            scores = {dim: d.get("score") for dim, d in dim_results.items()}
            evaluated_scores = [s for s in scores.values() if s is not None]
            passed = bar_pass(dim_results)
            fallback_result = JevEvaluationResult(
                "pass" if passed else "fail", 0.0,
                1.0 if evaluated_scores else 0.0,
                {f"dim_{dim}": dim_results[dim] for dim in dim_ids},
                list(reasons), cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or model_name)
            self._account(fallback_result, site=site, task_id=task_id,
                          reservation=reservation)
            return {
                "dimensions": dim_results,
                "scores": scores,
                "bar_95_pass": passed,
                "min_score": min(evaluated_scores) if evaluated_scores else 0.0,
                "is_fallback": True,
                "reasons": list(reasons),
                "model": model or model_name,
                "cost": cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

        if not self.keyed or self.governor is None:
            return fallback_judgment(["unkeyed" if not self.keyed else "governor_not_provided"])

        text = self._audit_dimension_text(dimension_evidence)
        reservation = None
        try:
            reservation = self._preflight(site=site, max_input_tokens=JEV_MAX_INPUT_TOKENS)
            payload = {"evidence": text, "pack_id": pack_doc["id"]}
            result = self.evaluator.evaluate(payload, questions)
        except HarnessError as exc:
            return fallback_judgment(
                [f"transport_error: {exc}"], reservation=reservation)
        except Exception as exc:  # unexpected transport failure -- fail closed
            return fallback_judgment(
                [f"transport_error: {exc}"], reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        if result.is_fallback or not answers:
            return fallback_judgment(
                list(result.reasons or ["invalid TypeSafe response"]),
                model=result.model or model_name,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation,
            )

        dim_results = {}
        any_dim_valid = False
        evaluated_dim_ids = []
        for dim, spec in pack_doc["dimensions"].items():
            if not dim_evaluated(dim):
                dim_results[dim] = {
                    "name": spec["name"],
                    "level_index": None,
                    "level": "not_evaluated",
                    "score": None,
                    "bar_met": False,
                    "confidence": None,
                    "evaluated": False,
                }
                continue
            evaluated_dim_ids.append(dim)
            ans = answers.get(f"dim_{dim}")
            level = None
            value = None
            conf = None
            if isinstance(ans, dict):
                probs = ans.get("probabilities")
                legend = ans.get("legend")
                if isinstance(probs, dict) and isinstance(legend, dict):
                    for anchor, lvl in legend.items():
                        if not isinstance(lvl, str) or lvl not in levels:
                            continue
                        prob = probs.get(str(anchor))
                        if isinstance(prob, (int, float)) and (
                                value is None or float(prob) > value):
                            level, value = lvl, float(prob)
                raw_conf = ans.get("confidence")
                if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
                    conf = float(raw_conf)
            if level is not None:
                idx = levels.index(level)
                any_dim_valid = True
            else:
                # Declared answer missing/invalid: fall back to this ONE
                # dimension's own evidence-calibrated index -- never an
                # invented level.
                ev = dimension_evidence.get(dim) if evidence_is_dict else None
                ev_score = float(ev.get("score", 0.0)) if isinstance(ev, dict) else 0.0
                idx = (4 if ev_score >= 9.99 else
                      3 if ev_score >= 9.5 else
                      2 if ev_score >= 8.5 else
                      1 if ev_score >= 7.0 else 0)
            dim_results[dim] = {
                "name": spec["name"],
                "level_index": idx,
                "level": levels[idx],
                "score": round(ordinals[idx], 2),
                "bar_met": idx >= bar_idx,
                "confidence": conf,
                "evaluated": True,
            }

        if evaluated_dim_ids and not any_dim_valid:
            # Every evaluated dimension's live answer was unmatched/invalid:
            # present the whole call as a fallback rather than a live
            # judgment with nothing actually declared by Jev.
            return fallback_judgment(
                [f"dim_{dim}: unmatched" for dim in evaluated_dim_ids]
                + list(result.reasons or []),
                model=result.model, cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation)

        self._account(result, site=site, task_id=task_id, reservation=reservation)
        scores = {dim: d.get("score") for dim, d in dim_results.items()}
        evaluated_scores = [s for s in scores.values() if s is not None]
        return {
            "dimensions": dim_results,
            "scores": scores,
            "bar_95_pass": bar_pass(dim_results),
            "min_score": min(evaluated_scores) if evaluated_scores else 0.0,
            "is_fallback": False,
            "reasons": [],
            "model": result.model or model_name,
            "cost": float(result.cost or 0.0),
            "input_tokens": int(result.input_tokens or 0),
            "output_tokens": int(result.output_tokens or 0),
        }

    @staticmethod
    def attach(envelope: Dict[str, Any], structural: Optional[Dict[str, Any]]):
        """Attach the stable structural block without changing status."""
        if structural is not None:
            envelope["structural"] = dict(structural)
        return envelope


def policy_for(settings, *, transport=None, governor=None, ledger=None,
               evaluator=None) -> JevPolicy:
    """Construct the shared policy at a session composition boundary."""
    return JevPolicy(
        settings, transport=transport, governor=governor,
        ledger=ledger, evaluator=evaluator,
    )
