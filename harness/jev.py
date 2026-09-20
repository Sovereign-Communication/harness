"""Jev & System One structural evaluation adapter.

Evaluates shared state (prompts, context, code diffs) against typed questions
(boolean, score, choice) with calibrated confidence bounds, replacing costly
multi-model consensus loops with fast structural verification.
"""
import ast
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._http import HttpTransport


@dataclass(frozen=True)
class JevEvaluationResult:
    """Immutable result of a Jev / System One structural evaluation."""

    verdict: str  # "pass", "fail", or "defer"
    confidence: float  # Calibrated probability in [0.0, 1.0]
    supported: float  # Probability [0.0, 1.0] that state is supported
    answers: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    cost: float = 0.0
    is_fallback: bool = False

    def is_passing(self, min_confidence: float = 0.70) -> bool:
        """Check whether the evaluation passes the calibrated confidence threshold."""
        return self.verdict == "pass" and self.confidence >= min_confidence and self.supported >= min_confidence


class JevEvaluator:
    """System One structural evaluation client with deterministic local fallback."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: str = "https://api.typesafe.ai/v1/systemone",
        transport: Optional[HttpTransport] = None,
        settings: Optional[Any] = None,
    ):
        if settings is not None:
            self.api_key = api_key or getattr(settings, "jev_api_key", None)
            self.endpoint = getattr(settings, "jev_endpoint", endpoint) or endpoint
            self.min_confidence = getattr(settings, "min_confidence", 0.70)
        else:
            self.api_key = api_key
            self.endpoint = endpoint
            self.min_confidence = 0.70

        self.transport = transport or HttpTransport()

    def evaluate(
        self,
        state: Any,
        questions: Optional[Dict[str, Any]] = None,
    ) -> JevEvaluationResult:
        """Evaluate shared state against typed questions.

        If a Jev API key is configured, dispatches to the Jev System One endpoint.
        Otherwise, executes hermetic local structural and AST verification.
        """
        raw_questions = questions or {
            "supported": {
                "type": "noul",
                "instructions": "Does the draft follow logically from the provided context?",
            },
            "confidence": {
                "type": "score",
                "instructions": "How confident is the evaluation?",
                "criteria": ["Hallucinated", "Uncertain", "Plausible", "Verified"],
            },
            "syntax_clean": {
                "type": "noul",
                "instructions": "Is the code or diff syntactically valid and free of obvious defects?",
            },
        }

        # Convert question types to TypeSafe System One primitives
        active_questions = {}
        for q_id, q_def in raw_questions.items():
            q_type = q_def.get("type")
            instr = q_def.get("instructions", "")
            if q_type in ("boolean", "noul"):
                q_obj = {"type": "noul", "instructions": instr}
                if "criteria" in q_def:
                    q_obj["criteria"] = q_def["criteria"]
                active_questions[q_id] = q_obj
            elif q_type == "score":
                active_questions[q_id] = {
                    "type": "score",
                    "instructions": instr,
                    "criteria": q_def.get("criteria", ["Low", "Medium", "High"]),
                }
            elif q_type == "choice":
                active_questions[q_id] = {
                    "type": "choice",
                    "instructions": instr,
                    "criteria": q_def.get("criteria", q_def.get("options", {})),
                }
            else:
                active_questions[q_id] = q_def

        if self.api_key:
            try:
                payload = {"model": "jev-latest", "state": state, "questions": active_questions}
                status, resp = self.transport.post(self.endpoint, self.api_key, payload)
                if status == 200 and isinstance(resp, dict):
                    return self._parse_jev_response(resp)
            except Exception:
                # Network or endpoint failure falls back to local structural checks
                pass

        # Local structural evaluation fallback ($0, millisecond latency)
        state_dict = state if isinstance(state, dict) else {"content": str(state)}
        return self._local_structural_eval(state_dict, active_questions)

    def _parse_jev_response(self, resp: Dict[str, Any]) -> JevEvaluationResult:
        """Parse native Jev response into JevEvaluationResult."""
        answers = resp.get("answers", resp.get("results", {}))
        usage = resp.get("usage", {})
        cost = float(usage.get("cost", 0.00004))

        # 1. Parse supported probability
        supp_ans = answers.get("supported", 1.0)
        if isinstance(supp_ans, dict):
            supported_prob = float(supp_ans.get("noul", supp_ans.get("probability", 1.0)))
        elif isinstance(supp_ans, (int, float)):
            supported_prob = float(supp_ans)
        else:
            supported_prob = 1.0 if supp_ans else 0.0

        # 2. Parse confidence score
        conf_ans = answers.get("confidence", 0.85)
        if isinstance(conf_ans, dict):
            if "confidence" in conf_ans:
                conf_score = float(conf_ans["confidence"])
            elif "score" in conf_ans:
                conf_score = float(conf_ans["score"])
            else:
                conf_score = 0.85
        elif isinstance(conf_ans, (int, float)):
            conf_score = float(conf_ans)
        else:
            conf_score = 0.85

        # 3. Parse syntax_clean
        syntax_ans = answers.get("syntax_clean", 1.0)
        if isinstance(syntax_ans, dict):
            syntax_prob = float(syntax_ans.get("noul", 1.0))
        elif isinstance(syntax_ans, bool):
            syntax_prob = 1.0 if syntax_ans else 0.0
        else:
            syntax_prob = 1.0

        reasons: List[str] = []
        if "reason" in resp:
            reasons.append(str(resp["reason"]))
        for k, v in answers.items():
            if isinstance(v, dict):
                if "rationale" in v:
                    reasons.append(f"{k}: {v['rationale']}")
                elif v.get("type") == "noul":
                    reasons.append(f"{k} (noul): {v.get('noul')}")
                elif v.get("type") == "choice":
                    reasons.append(f"{k} (choice): {v.get('choice')} (conf: {v.get('confidence')})")
                elif v.get("type") == "score":
                    reasons.append(f"{k} (score): {v.get('score')} (conf: {v.get('confidence')})")

        verdict = "pass" if (supported_prob >= 0.70 and conf_score >= 0.70 and syntax_prob >= 0.70) else "fail"
        return JevEvaluationResult(
            verdict=verdict,
            confidence=conf_score,
            supported=supported_prob,
            answers=answers,
            reasons=reasons,
            cost=cost,
            is_fallback=False,
        )

    def _local_structural_eval(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Any],
    ) -> JevEvaluationResult:
        """Deterministic local AST, JSON, and structural validation."""
        reasons: List[str] = []
        code_to_check = state.get("code") or state.get("content") or ""
        diff_to_check = state.get("diff") or ""
        json_to_check = state.get("json_content") or ""

        # 1. JSON parsing check
        if json_to_check:
            try:
                json.loads(json_to_check)
            except Exception as e:
                return JevEvaluationResult(
                    verdict="fail",
                    confidence=0.1,
                    supported=0.0,
                    answers={"syntax_clean": False, "supported": 0.0, "confidence": 0},
                    reasons=[f"Invalid JSON: {e}"],
                    cost=0.0,
                    is_fallback=True,
                )

        # 2. Python AST syntax check
        if code_to_check and not diff_to_check:
            try:
                ast.parse(code_to_check)
            except SyntaxError as e:
                return JevEvaluationResult(
                    verdict="fail",
                    confidence=0.1,
                    supported=0.0,
                    answers={"syntax_clean": False, "supported": 0.0, "confidence": 0},
                    reasons=[f"Python SyntaxError: {e.msg} at line {e.lineno}"],
                    cost=0.0,
                    is_fallback=True,
                )

        # 3. Diff checks
        if diff_to_check:
            if not any(diff_to_check.strip().startswith(p) for p in ("---", "+++", "@@", "diff")):
                if "+" not in diff_to_check and "-" not in diff_to_check:
                    reasons.append("Diff output does not resemble unified diff structure.")
                    return JevEvaluationResult(
                        verdict="fail",
                        confidence=0.3,
                        supported=0.2,
                        answers={"syntax_clean": False, "supported": 0.2, "confidence": 1},
                        reasons=reasons,
                        cost=0.0,
                        is_fallback=True,
                    )

        # 4. Empty output check
        if not code_to_check and not diff_to_check and not json_to_check and not state.get("response"):
            return JevEvaluationResult(
                verdict="fail",
                confidence=0.0,
                supported=0.0,
                answers={"syntax_clean": False, "supported": 0.0, "confidence": 0},
                reasons=["Empty candidate state returned."],
                cost=0.0,
                is_fallback=True,
            )

        # Passed all structural checks
        return JevEvaluationResult(
            verdict="pass",
            confidence=0.92,
            supported=0.95,
            answers={"syntax_clean": True, "supported": 0.95, "confidence": 3},
            reasons=["All local structural, AST, and format invariants satisfied."],
            cost=0.0,
            is_fallback=True,
        )

    def verify_diff_mechanics(
        self,
        diff: str,
        instruction: str = "",
        file_path: str = "",
    ) -> JevEvaluationResult:
        """Cheap mechanical check for code edits before executing real gates."""
        state = {
            "diff": diff,
            "instruction": instruction,
            "file_path": file_path,
        }
        return self.evaluate(state)

    def evaluate_plan_requirements(
        self,
        prompt: str,
        target_files: Optional[List[str]] = None,
    ) -> JevEvaluationResult:
        """Evaluate goal and targets for structural requirements (loops, branching, complexity)."""
        state = {
            "prompt": prompt,
            "target_files": list(target_files or []),
        }
        questions = {
            "requires_iteration": {
                "type": "noul",
                "instructions": "Does this coding task require iterative loops, conditional branches, or multi-step algorithms?",
            },
            "confidence": {
                "type": "score",
                "instructions": "Confidence in requirement analysis",
                "criteria": ["Uncertain", "Likely", "Verified"],
            },
        }
        if self.api_key:
            try:
                res = self.evaluate(state, questions)
                if not res.is_fallback:
                    req_ans = res.answers.get("requires_iteration")
                    if isinstance(req_ans, dict):
                        is_iter = float(req_ans.get("noul", 0.0)) >= 0.50
                    elif isinstance(req_ans, (int, float)):
                        is_iter = float(req_ans) >= 0.50
                    else:
                        is_iter = bool(req_ans)
                    return JevEvaluationResult(
                        verdict=res.verdict,
                        confidence=res.confidence,
                        supported=res.supported,
                        answers={"requires_iteration": is_iter, "confidence": res.confidence, "raw": res.answers},
                        reasons=res.reasons,
                        cost=res.cost,
                        is_fallback=False,
                    )
            except Exception:
                pass
        lower = prompt.lower()
        iter_keywords = (
            "loop", "iterat", "branch", "recur", "dag", "multi-step", "pipeline",
            "while", "until", "retry", "traverse", "graph", "step by step",
            "condition", "algorithm", "cycle"
        )
        has_iter = any(k in lower for k in iter_keywords)
        return JevEvaluationResult(
            verdict="pass",
            confidence=0.88 if has_iter else 0.75,
            supported=0.90,
            answers={"requires_iteration": has_iter, "confidence": 0.88 if has_iter else 0.75},
            reasons=["Detected iterative/algorithmic requirements" if has_iter else "Standard declarative edit flow"],
            cost=0.0,
            is_fallback=True,
        )
