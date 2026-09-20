"""Pure-stdlib TypeSafe System One adapter (JEV-P0).

Code owns exact mechanics (paths, syntax, hunk shape and thresholds); Jev owns
only bounded semantic judgments. The API contract is deliberately strict:
questions are only ``noul``, ``choice`` or ``score`` and answers must use the
official typed shapes from docs.typesafe.ai/api.md.
"""
import ast
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._http import HttpTransport

JEV_INPUT_PRICE_PER_MILLION = 42.0
_PRIMITIVES = frozenset(("noul", "choice", "score"))


def jev_cost(input_tokens: int) -> float:
    """Return TypeSafe's input-only price; output tokens are free."""
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ValueError("input_tokens must be a non-negative integer")
    return input_tokens * JEV_INPUT_PRICE_PER_MILLION / 1_000_000


@dataclass(frozen=True)
class JevEvaluationResult:
    """An honest typed evaluation envelope."""

    verdict: str
    # Confidence is only populated from Choice/Score confidence. Noul values
    # remain probabilities in ``supported``/``answers`` and are never renamed.
    confidence: float
    supported: float
    answers: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    is_fallback: bool = False
    model: Optional[str] = None

    def is_passing(self, min_confidence: float = 0.70) -> bool:
        """Apply the configured action threshold without conflating signals."""
        return (self.verdict == "pass" and self.supported >= min_confidence
                and (self.confidence <= 0.0 or self.confidence >= min_confidence))


def _noul(question: str, yes: str, no: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": question,
            "criteria": {"true": yes, "false": no}}


def diff_question_pack() -> Dict[str, Dict[str, Any]]:
    """The one semantic question answerable from the diff and instruction.

    Paths, hunk shape, change presence, and AST validity are code-owned facts,
    so they are enforced locally rather than asking Jev to re-judge them.
    """
    return {
        "instruction_matches": _noul(
            "Does the changed code implement the supplied instruction?",
            "The changed behavior directly addresses the instruction.",
            "The changed behavior does not address, or contradicts, the instruction."),
    }


def plan_question_pack() -> Dict[str, Dict[str, Any]]:
    """A narrow plan-site pack; no generic confidence question."""
    return {
        "requires_iteration": _noul(
            "Does the stated coding task require iterative control flow or multiple dependent steps?",
            "The task requires iteration or dependent steps.",
            "The task is a bounded declarative or single-step edit."),
        "requirement_complexity": {
            "type": "score",
            "instructions": "Rate the task's execution complexity from the supplied prompt and target list.",
            "criteria": ["single bounded edit", "several dependent edits", "iterative or algorithmic work"],
        },
    }


def _validate_questions(questions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty map")
    out = {}
    for key, question in questions.items():
        if not isinstance(key, str) or not isinstance(question, dict):
            raise ValueError("questions must map string ids to objects")
        kind = question.get("type")
        if kind not in _PRIMITIVES:
            raise ValueError(f"unsupported TypeSafe primitive: {kind!r}")
        if "instructions" not in question:
            raise ValueError(f"question {key} is missing instructions")
        if kind in ("choice", "score") and "criteria" not in question:
            raise ValueError(f"question {key} is missing criteria")
        if kind == "choice" and (not isinstance(question["criteria"], dict) or not question["criteria"]):
            raise ValueError("choice criteria must be a non-empty map")
        if kind == "score" and (not isinstance(question["criteria"], list) or len(question["criteria"]) < 2):
            raise ValueError("score criteria must contain at least two levels")
        out[key] = {k: question[k] for k in ("type", "instructions", "criteria") if k in question}
    return out


def _number(value: Any, name: str, lo: float = 0.0, hi: Optional[float] = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < lo or (hi is not None and result > hi):
        raise ValueError(f"{name} outside range")
    return result


def _probabilities(value: Any, name: str) -> Dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a non-empty map")
    parsed = {str(k): _number(v, name) for k, v in value.items()}
    if abs(sum(parsed.values()) - 1.0) > 1e-6:
        raise ValueError(f"{name} must sum to 1")
    return parsed


def _parse_answer(answer: Any, expected: str, key: str,
                 question: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != expected:
        raise ValueError(f"answer {key} is not an official {expected} answer")
    if expected == "noul":
        return {"type": "noul", "noul": _number(answer["noul"], key + ".noul")}
    if expected == "choice":
        choice = answer.get("choice")
        parsed_probs = _probabilities(answer.get("probabilities"), key + ".probabilities")
        criteria = question.get("criteria")
        if not isinstance(choice, str) or choice not in parsed_probs:
            raise ValueError(f"choice answer {key} has an invalid choice")
        if isinstance(criteria, dict) and set(parsed_probs) != set(criteria):
            raise ValueError(f"choice answer {key} probabilities do not match criteria")
        return {"type": "choice", "choice": choice, "probabilities": parsed_probs,
                "confidence": _number(answer["confidence"], key + ".confidence")}
    score = answer.get("score")
    legend = answer.get("legend")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isinstance(legend, dict) or not legend:
        raise ValueError(f"score answer {key} is missing required fields")
    parsed_probs = _probabilities(answer.get("probabilities"), key + ".probabilities")
    if set(parsed_probs) != {str(k) for k in legend}:
        raise ValueError(f"score answer {key} legend does not match probabilities")
    return {"type": "score", "score": float(score), "legend": dict(legend),
            "probabilities": parsed_probs,
            "confidence": _number(answer["confidence"], key + ".confidence")}


class JevEvaluator:
    def __init__(self, api_key: Optional[str] = None, endpoint: str = "https://api.typesafe.ai/v1/systemone",
                 transport: Optional[HttpTransport] = None, settings: Optional[Any] = None):
        self.api_key = api_key or (getattr(settings, "jev_api_key", None) if settings else None)
        self.endpoint = (getattr(settings, "jev_endpoint", endpoint) if settings else endpoint) or endpoint
        self.model = getattr(settings, "jev_model", "jev-latest") if settings else "jev-latest"
        self.min_confidence = getattr(settings, "min_confidence", 0.70) if settings else 0.70
        self.transport = transport or HttpTransport()

    def evaluate(self, state: Any, questions: Optional[Dict[str, Any]] = None) -> JevEvaluationResult:
        raw = diff_question_pack() if questions is None else questions
        try:
            active = _validate_questions(raw)
        except ValueError as exc:
            return self._failure(str(exc), fallback=False)
        if self.api_key:
            try:
                status, resp = self.transport.post(self.endpoint, self.api_key,
                                                   {"model": self.model, "state": state, "questions": active})
                if status == 200 and isinstance(resp, dict):
                    try:
                        return self._parse_jev_response(resp, active)
                    except (KeyError, TypeError, ValueError) as exc:
                        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        if (isinstance(input_tokens, bool) or not isinstance(input_tokens, int)
                                or input_tokens < 0):
                            input_tokens = 0
                        if (isinstance(output_tokens, bool) or not isinstance(output_tokens, int)
                                or output_tokens < 0):
                            output_tokens = 0
                        return self._failure(
                            "invalid TypeSafe response: " + str(exc), fallback=False,
                            input_tokens=input_tokens, output_tokens=output_tokens)
                if status in (401, 422):
                    return self._failure(f"TypeSafe request rejected (HTTP {status})", fallback=False)
            except Exception:
                pass
        return self._local_structural_eval(state if isinstance(state, dict) else {"content": str(state)})

    def _failure(self, reason: str, fallback: bool, input_tokens: int = 0,
                 output_tokens: int = 0) -> JevEvaluationResult:
        return JevEvaluationResult(
            "fail", 0.0, 0.0, {}, [reason],
            cost=jev_cost(input_tokens), input_tokens=input_tokens,
            output_tokens=output_tokens, is_fallback=fallback, model=self.model)

    def _parse_jev_response(self, resp: Dict[str, Any], questions: Dict[str, Any]) -> JevEvaluationResult:
        if not isinstance(resp.get("answers"), dict) or not isinstance(resp.get("usage"), dict):
            raise ValueError("response requires answers and usage")
        usage = resp["usage"]
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError("usage.input_tokens must be a non-negative integer")
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
            raise ValueError("usage.output_tokens must be a non-negative integer")
        expected = _validate_questions(questions)
        answers = {}
        for key, question in expected.items():
            if key not in resp["answers"]:
                raise ValueError(f"response is missing answer {key}")
            answers[key] = _parse_answer(resp["answers"][key], question["type"], key, question)
        nouls = [a["noul"] for a in answers.values() if a["type"] == "noul"]
        supported = min(nouls) if nouls else 1.0
        action_confidences = [a["confidence"] for a in answers.values() if a["type"] in ("choice", "score")]
        confidence = min(action_confidences) if action_confidences else 0.0
        # With no Choice/Score action confidence, noul probabilities are the
        # separate supported signal. A purely-noul pack is still thresholded
        # by supported in is_passing; confidence remains explicitly absent/0.
        verdict = "pass" if supported >= self.min_confidence and (not action_confidences or confidence >= self.min_confidence) else "fail"
        reasons = []
        for key, answer in answers.items():
            if answer["type"] == "noul":
                reasons.append(f"{key} (noul): {answer['noul']}")
            elif answer["type"] == "choice":
                reasons.append(f"{key} (choice): {answer['choice']} (conf: {answer['confidence']})")
            else:
                reasons.append(f"{key} (score): {answer['score']} (conf: {answer['confidence']})")
        return JevEvaluationResult(verdict, confidence, supported, answers, reasons,
                                   cost=jev_cost(input_tokens), input_tokens=input_tokens,
                                   output_tokens=output_tokens, model=resp.get("model", self.model))

    def _local_structural_eval(self, state: Dict[str, Any], fallback: bool = True) -> JevEvaluationResult:
        code = state.get("code") or state.get("content") or ""
        json_content = state.get("json_content") or ""
        if json_content:
            try:
                json.loads(json_content)
            except Exception as exc:
                return self._failure(f"Invalid JSON: {exc}", fallback)
        if code and not state.get("diff"):
            try:
                ast.parse(code)
            except SyntaxError as exc:
                return self._failure(f"Python SyntaxError: {exc.msg} at line {exc.lineno}", fallback)
        if state.get("diff"):
            facts = state
            checks = [("hunk_shape_ok", bool(facts.get("hunk_shape_ok", False)), "diff hunk shape"),
                      ("has_changes", bool(facts.get("has_changes", False)), "diff change"),
                      ("path_matches", bool(facts.get("path_matches", False)), "target path")]
            if facts.get("ast_parse_ok") is not None:
                checks.append(("ast_parse_ok", bool(facts["ast_parse_ok"]), "AST parse"))
            for _, ok, label in checks:
                if not ok:
                    return JevEvaluationResult("fail", 0.0, 0.0, {}, [f"Code-owned {label} check failed."], is_fallback=fallback, model=self.model)
        elif not code and not json_content and not state.get("response") and not state.get("prompt"):
            return self._failure("Empty candidate state returned.", fallback)
        return JevEvaluationResult("pass", 0.0, 1.0,
                                   {"mechanical_checks": "passed"},
                                   ["All local code-owned structural checks passed."], is_fallback=fallback, model=self.model)

    def verify_diff_mechanics(self, diff: str, instruction: str = "", file_path: str = "",
                              candidate: Optional[str] = None) -> JevEvaluationResult:
        state = _diff_state(diff or "", instruction or "", file_path or "", candidate=candidate)
        mechanical = self._local_structural_eval(state, fallback=not bool(self.api_key))
        if mechanical.verdict != "pass":
            return mechanical
        return self.evaluate(state)

    def evaluate_plan_requirements(self, prompt: str, target_files: Optional[List[str]] = None) -> JevEvaluationResult:
        prompt = prompt or ""
        questions = plan_question_pack()
        result = self.evaluate({"prompt": prompt, "target_files": list(target_files or [])}, questions)
        if not result.is_fallback:
            answer = result.answers.get("requires_iteration", {"noul": 0.0})
            return JevEvaluationResult(result.verdict, result.confidence, result.supported,
                                       {"requires_iteration": answer["noul"] >= 0.5, "raw": result.answers},
                                       result.reasons, result.cost, result.input_tokens,
                                       result.output_tokens, False, result.model)
        lower = prompt.lower()
        has_iter = any(word in lower for word in ("loop", "iterat", "branch", "recur", "dag", "retry", "traverse", "graph", "algorithm", "cycle"))
        return JevEvaluationResult("pass", 0.0, 1.0, {"requires_iteration": has_iter},
                                   ["Detected iterative/algorithmic requirements" if has_iter else "Standard declarative edit flow"], is_fallback=True)


def _looks_like_diff(diff: str) -> bool:
    return bool(diff.strip()) and diff.lstrip().startswith(("---", "diff "))


def _valid_hunks(lines: List[str]) -> bool:
    """Require a hunk header, valid body prefixes, and at least one change."""
    saw_hunk = saw_change = False
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            saw_hunk = in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("--- ", "+++ ", "diff ")):
            in_hunk = False
            continue
        if line.startswith(("+", "-")):
            saw_change = True
        elif not line.startswith((" ", "\\")):
            return False
    return saw_hunk and saw_change


def _path_matches(file_path: str, paths: List[str]) -> bool:
    if not file_path or not paths:
        return bool(not file_path)
    target = file_path.replace("\\", "/").rstrip("/")
    for path in paths:
        path = path.replace("\\", "/").rstrip("/")
        if target == path or target.endswith("/" + path) or path.endswith("/" + target):
            return True
    return False


def _diff_state(diff: str, instruction: str, file_path: str,
                candidate: Optional[str] = None) -> Dict[str, Any]:
    lines = diff.splitlines()
    headers = [line[4:].strip() for line in lines if line.startswith(("--- ", "+++ "))]
    paths = [p[2:] if p.startswith(("a/", "b/")) else p for p in headers]
    changed = [line for line in lines
               if line.startswith(("+", "-"))
               and not line.startswith(("--- ", "+++ "))]
    ast_ok = None
    if isinstance(candidate, str) and file_path.lower().endswith(".py"):
        try:
            ast.parse(candidate)
            ast_ok = True
        except SyntaxError:
            ast_ok = False
    return {"diff": diff, "instruction": instruction, "file_path": file_path,
            "path_matches": _path_matches(file_path, paths),
            "hunk_shape_ok": _looks_like_diff(diff) and _valid_hunks(lines),
            "ast_parse_ok": ast_ok, "has_changes": bool(changed)}
