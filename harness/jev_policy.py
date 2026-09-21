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
from .jev_packs import (
    HUL_SCOPE_SITE,
    LOG_FACTOR_SITE,
    SCOPE_COVERAGE_HOLD,
    SCOPE_NOUL_HOLD,
    claim_support_question_pack,
    claims_from_payload,
    escalation_decision_pack,
    completion_question_pack,
    file_relevance_question_pack,
    heuristic_file_relevance,
    heuristic_requires_iteration,
    heuristic_route,
    hul_scope_question_pack,
    issue_sort_question_pack,
    log_factor_question_pack,
    match_keywords,
    named_artifact_status,
    normalize_complexity_class,
    normalize_route,
    route_question_pack,
    scope_in_scope_holds,
    validate_candidates,
    validate_log_pack,
    validate_operator_pack,
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
