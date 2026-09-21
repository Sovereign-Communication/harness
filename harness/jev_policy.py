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
    claim_support_question_pack,
    claims_from_payload,
    completion_question_pack,
    file_relevance_question_pack,
    heuristic_file_relevance,
    heuristic_requires_iteration,
    heuristic_route,
    named_artifact_status,
    normalize_route,
    route_question_pack,
    validate_candidates,
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
