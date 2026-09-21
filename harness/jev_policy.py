"""The single Jev policy owner for all Harness decision lanes (JEV-P1).

The P0 evaluator owns TypeSafe parsing and code-owned mechanics. This module
owns lane policy: when a typed call may dispatch, its bounded spend, one ledger
event, and the structural envelope shared by apply, plan, waist, and agent
lanes.
"""
import difflib
import os
from typing import Any, Dict, Iterable, Optional

from .errors import HarnessError
from .jev import (JevEvaluationResult, JevEvaluator, jev_cost,
                  triage_question_pack)
from .jev_packs import (issue_sort_question_pack, match_keywords,
                        validate_operator_pack)

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
