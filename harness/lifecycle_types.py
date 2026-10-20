"""Typed state contract and invariant checks for request lifecycles."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

from .errors import HarnessError

LifecycleStatus = Literal[
    "running", "interrupted", "complete", "deferred", "failed",
]
ActiveStatus = Literal["running", "interrupted"]
TerminalStatus = Literal["complete", "deferred", "failed"]
ReviewDecision = Literal[
    "pending", "pass", "revise", "needs_iteration", "abstain", "limit",
]
ReviewOutcome = Literal["pass", "revise", "needs_iteration", "abstain"]
ChatStage = Literal[
    "route", "context", "answer", "generation", "review", "render",
    "response", "jev_review", "strategy", "answer_generation",
    "answer_review", "workspace_selection", "terminal",
]
AuditStage = Literal["inspect", "review", "render", "terminal"]
AutoEditStage = Literal[
    "inspect", "plan", "review", "propose", "approval", "apply",
    "verify", "terminal",
]
DriverStage = Literal["observe", "review", "execute", "verify", "terminal"]
ProvisionStage = Literal[
    "probe", "select", "plan", "review", "approval", "execute",
    "verify", "terminal",
]
Lane = Literal["chat", "audit", "auto_edit", "driver", "provisioning"]


class ReviewRound(TypedDict):
    review_id: str
    decision: ReviewOutcome
    native: bool
    confidence: Optional[float]
    supported: Optional[float]
    iteration_required: bool
    plan_required: bool
    cost: float


class ReviewState(TypedDict):
    rounds: int
    max_rounds: int
    decision: ReviewDecision
    confidence: Optional[float]
    history: List[ReviewRound]


class ProviderCall(TypedDict):
    status: Literal["dispatched", "completed", "unknown"]
    reserved_cost: float
    actual_cost: float
    result: Any


class UsageState(TypedDict):
    known_cost: float
    unknown_liability: float
    provider_calls: Dict[str, ProviderCall]


class EffectReceipt(TypedDict):
    effect_id: str
    operation: str
    status: Literal["started", "completed", "unknown", "failed"]
    approval_state: Dict[str, Any]
    result: Any


class ExecutionState(TypedDict):
    effects: Dict[str, EffectReceipt]
    completed_steps: Dict[str, Any]
    approval_state: Dict[str, Any]
    requires_reconciliation: List[str]


class _CommonState(TypedDict):
    schema: Literal["harness-lifecycle-v1"]
    request_id: str
    session_id: str
    original_request: str
    followups: List[str]
    context: Dict[str, Any]
    candidate: Any
    review: ReviewState
    usage: UsageState
    execution: ExecutionState
    data: Dict[str, Any]


class _ChatActiveState(_CommonState):
    lane: Literal["chat"]
    status: ActiveStatus
    stage: Literal[
        "route", "context", "answer", "generation", "review", "render",
        "response", "jev_review", "strategy", "answer_generation",
        "answer_review", "workspace_selection",
    ]


class _ChatTerminalState(_CommonState):
    lane: Literal["chat"]
    status: TerminalStatus
    stage: Literal["terminal"]


ChatLifecycleState = Union[_ChatActiveState, _ChatTerminalState]


class _AuditActiveState(_CommonState):
    lane: Literal["audit"]
    status: ActiveStatus
    stage: Literal["inspect", "review", "render"]


class _AuditTerminalState(_CommonState):
    lane: Literal["audit"]
    status: TerminalStatus
    stage: Literal["terminal"]


AuditLifecycleState = Union[_AuditActiveState, _AuditTerminalState]


class _AutoEditActiveState(_CommonState):
    lane: Literal["auto_edit"]
    status: ActiveStatus
    stage: Literal["inspect", "plan", "review", "propose", "approval",
                   "apply", "verify"]


class _AutoEditTerminalState(_CommonState):
    lane: Literal["auto_edit"]
    status: TerminalStatus
    stage: Literal["terminal"]


AutoEditLifecycleState = Union[_AutoEditActiveState, _AutoEditTerminalState]


class _DriverActiveState(_CommonState):
    lane: Literal["driver"]
    status: ActiveStatus
    stage: Literal["observe", "review", "execute", "verify"]


class _DriverTerminalState(_CommonState):
    lane: Literal["driver"]
    status: TerminalStatus
    stage: Literal["terminal"]


DriverLifecycleState = Union[_DriverActiveState, _DriverTerminalState]


class _ProvisionActiveState(_CommonState):
    lane: Literal["provisioning"]
    status: ActiveStatus
    stage: Literal["probe", "select", "plan", "review", "approval",
                   "execute", "verify"]


class _ProvisionTerminalState(_CommonState):
    lane: Literal["provisioning"]
    status: TerminalStatus
    stage: Literal["terminal"]


ProvisionLifecycleState = Union[_ProvisionActiveState, _ProvisionTerminalState]


LifecycleState = Union[
    ChatLifecycleState,
    AuditLifecycleState,
    AutoEditLifecycleState,
    DriverLifecycleState,
    ProvisionLifecycleState,
]

_STAGES = {
    "chat": {"route", "context", "answer", "generation", "review",
             "render", "response", "jev_review", "strategy",
             "answer_generation", "answer_review", "workspace_selection",
             "terminal"},
    "audit": {"inspect", "review", "render", "terminal"},
    "auto_edit": {"inspect", "plan", "review", "propose", "approval",
                  "apply", "verify", "terminal"},
    "driver": {"observe", "review", "execute", "verify", "terminal"},
    "provisioning": {"probe", "select", "plan", "review", "approval",
                     "execute", "verify", "terminal"},
}
_LANES = frozenset(_STAGES)
_STATUSES = frozenset(("running", "interrupted", "complete", "deferred", "failed"))
_MAX_CHECKPOINT_BYTES = 96_000
_MAX_FOLLOWUPS = 16


def validate_lifecycle_state(value: Any) -> LifecycleState:
    """Validate the discriminant and cross-field invariants before use/save."""
    if not isinstance(value, dict):
        raise HarnessError("request lifecycle state must be an object")
    required = {
        "schema", "lane", "stage", "request_id", "session_id", "status",
        "original_request", "followups", "context", "candidate", "review",
        "usage", "execution", "data",
    }
    if required - set(value):
        raise HarnessError("request lifecycle state is incomplete")
    lane, stage, status = value.get("lane"), value.get("stage"), value.get("status")
    if value.get("schema") != "harness-lifecycle-v1":
        raise HarnessError("request lifecycle schema is unsupported")
    if lane not in _LANES or stage not in _STAGES.get(lane, set()):
        raise HarnessError("request lifecycle lane and stage do not match")
    if status not in _STATUSES:
        raise HarnessError("request lifecycle status is invalid")
    if (status in ("complete", "deferred", "failed")) != (stage == "terminal"):
        raise HarnessError("request lifecycle status and stage do not match")
    if status == "interrupted" and stage == "terminal":
        raise HarnessError("an interrupted lifecycle cannot be terminal")
    for key, limit in (("request_id", 128), ("session_id", 512)):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            raise HarnessError(f"request lifecycle {key} is invalid")
    original = value.get("original_request")
    followups = value.get("followups")
    if not isinstance(original, str) or not original.strip() or len(original) > 20_000:
        raise HarnessError("request lifecycle original request is invalid")
    if (not isinstance(followups, list) or len(followups) > _MAX_FOLLOWUPS
            or any(not isinstance(item, str) or not item.strip() or len(item) > 4_000
                   for item in followups)):
        raise HarnessError("request lifecycle follow-ups are invalid")
    if not all(isinstance(value.get(key), dict) for key in ("context", "review", "usage", "execution", "data")):
        raise HarnessError("request lifecycle state contains an invalid section")
    resumable = value["data"].get("resumable")
    if resumable is not None and not isinstance(resumable, bool):
        raise HarnessError("request lifecycle resumable state is invalid")
    if resumable and status != "deferred":
        raise HarnessError("only a deferred lifecycle can be resumable")
    if resumable:
        resume_stage = value["data"].get("resume_stage")
        if resume_stage not in _STAGES[lane] - {"terminal"}:
            raise HarnessError("request lifecycle resume stage is invalid")

    def finite_number(value, *, maximum=None):
        return (not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value)) and value >= 0
                and (maximum is None or value <= maximum))

    review = value["review"]
    rounds, maximum, history = (review.get("rounds"), review.get("max_rounds"),
                                review.get("history"))
    if (not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0
            or not isinstance(maximum, int) or isinstance(maximum, bool)
            or not 1 <= maximum <= 8 or rounds > maximum
            or not isinstance(history, list) or len(history) != rounds):
        raise HarnessError("request lifecycle review state is invalid")
    if review.get("decision") not in {
            "pending", "pass", "revise", "needs_iteration", "abstain", "limit"}:
        raise HarnessError("request lifecycle review decision is invalid")
    review_ids = set()
    for item in history:
        if (not isinstance(item, dict)
                or not isinstance(item.get("review_id"), str)
                or not item["review_id"].strip()
                or item["review_id"] in review_ids
                or item.get("decision") not in {
                    "pass", "revise", "needs_iteration", "abstain"}
                or not isinstance(item.get("native"), bool)
                or not isinstance(item.get("iteration_required"), bool)
                or not isinstance(item.get("plan_required"), bool)
                or not finite_number(item.get("cost"))):
            raise HarnessError("request lifecycle review entry is invalid")
        review_ids.add(item["review_id"])
        for key in ("confidence", "supported"):
            number = item.get(key)
            if number is not None and not finite_number(number, maximum=1):
                raise HarnessError("request lifecycle review probability is invalid")
        decision = item["decision"]
        if decision == "pass" and (
                not item["native"] or item["supported"] is None
                or item["iteration_required"] or item["plan_required"]):
            raise HarnessError("request lifecycle pass decision is inconsistent")
        if decision in {"revise", "needs_iteration"} \
                and not item["iteration_required"]:
            raise HarnessError("request lifecycle iteration decision is inconsistent")
    confidence = review.get("confidence")
    if confidence is not None and not finite_number(confidence, maximum=1):
        raise HarnessError("request lifecycle review confidence is invalid")
    if rounds == 0:
        if review["decision"] != "pending" or confidence is not None:
            raise HarnessError("empty request lifecycle review is not pending")
    elif (review["decision"] != history[-1]["decision"]
          or confidence != history[-1]["confidence"]):
        raise HarnessError("request lifecycle review summary is inconsistent")
    usage = value["usage"]
    for key in ("known_cost", "unknown_liability"):
        amount = usage.get(key)
        if not finite_number(amount):
            raise HarnessError("request lifecycle usage state is invalid")
    if not isinstance(usage.get("provider_calls"), dict):
        raise HarnessError("request lifecycle provider calls are invalid")
    for call_id, call in usage["provider_calls"].items():
        if (not isinstance(call_id, str) or not call_id.strip()
                or not isinstance(call, dict)
                or call.get("status") not in {"dispatched", "completed", "unknown"}
                or not finite_number(call.get("reserved_cost"))
                or not finite_number(call.get("actual_cost"))):
            raise HarnessError("request lifecycle provider call is invalid")
        if status in ("complete", "deferred", "failed") \
                and call["status"] == "dispatched":
            raise HarnessError("terminal lifecycle has an unsettled provider call")
    execution = value["execution"]
    if (not isinstance(execution.get("effects"), dict)
            or not isinstance(execution.get("completed_steps"), dict)
            or not isinstance(execution.get("approval_state"), dict)
            or not isinstance(execution.get("requires_reconciliation"), list)):
        raise HarnessError("request lifecycle execution state is invalid")
    for effect_id, effect in execution["effects"].items():
        if (not isinstance(effect_id, str) or not isinstance(effect, dict)
                or effect.get("effect_id") != effect_id
                or not isinstance(effect.get("operation"), str)
                or not effect["operation"].strip()
                or effect.get("status") not in {"started", "completed", "unknown", "failed"}
                or not isinstance(effect.get("approval_state"), dict)):
            raise HarnessError("request lifecycle effect receipt is invalid")
    pending = execution["requires_reconciliation"]
    if (any(not isinstance(item, str) or not item.strip() for item in pending)
            or len(set(pending)) != len(pending)
            or any(execution["effects"].get(item, {}).get("status") != "unknown"
                   for item in pending)
            or any(effect.get("status") == "unknown" and effect_id not in pending
                   for effect_id, effect in execution["effects"].items())):
        raise HarnessError("request lifecycle reconciliation state is invalid")
    unresolved = [
        effect_id for effect_id, effect in execution["effects"].items()
        if effect.get("status") in {"started", "unknown"}
    ]
    if status in ("complete", "failed") and unresolved:
        raise HarnessError("terminal lifecycle has unresolved side effects")
    if any(not isinstance(step_id, str) or not step_id.strip()
           for step_id in execution["completed_steps"]):
        raise HarnessError("request lifecycle completed step state is invalid")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class ReviewTransition:
    decision: str
    should_continue: bool
    terminal_pass: bool
    rounds: int
