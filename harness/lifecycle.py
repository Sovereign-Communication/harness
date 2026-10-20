"""Coordinator and lane adapters for the shared request lifecycle."""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from functools import wraps
from typing import Any, Dict, Literal, Optional

from .errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from .events import emit
from .osal import atomic_write_text
from .lifecycle_types import (
    Lane, LifecycleState, ReviewRound, ReviewTransition, _LANES, _MAX_CHECKPOINT_BYTES,
    _MAX_FOLLOWUPS, _STATUSES, _STAGES, validate_lifecycle_state,
)


def _serialized_transition(method):
    """Keep parallel lane workers from racing checkpoint mutations/writes."""
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return serialized


def review_probability(value):
    """Return a finite Jev probability, treating malformed claims as absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


class LifecycleCoordinator:
    """The single state owner shared by lane adapters and chat persistence."""

    def __init__(self, history_dir=None, *, event=emit):
        self.history_dir = history_dir
        self.event = event
        self._lock = threading.RLock()

    def _store(self):
        from .history import _chat_checkpoint_path
        return _chat_checkpoint_path

    def _path(self, session_id: str, *, create=False):
        return self._store()(session_id, self.history_dir, create=create)

    @_serialized_transition
    def save(self, state: LifecycleState) -> LifecycleState:
        validate_lifecycle_state(state)
        path = self._path(state["session_id"], create=True)
        try:
            encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False)
            raw = encoded.encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise HarnessError("request lifecycle contains unsupported state") from exc
        if len(raw) > _MAX_CHECKPOINT_BYTES:
            raise HarnessError("request lifecycle exceeds its storage limit")
        atomic_write_text(str(path), encoded, newline=None)
        return state

    @_serialized_transition
    def load(self, session_id: str) -> Optional[LifecycleState]:
        path = self._path(session_id)
        if path.is_symlink():
            raise HarnessError("request lifecycle state is invalid")
        if not path.exists():
            return None
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_CHECKPOINT_BYTES + 1)
            if len(raw) > _MAX_CHECKPOINT_BYTES:
                raise HarnessError("request lifecycle exceeds its storage limit")
            state = validate_lifecycle_state(json.loads(raw.decode("utf-8")))
        except HarnessError:
            raise
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise HarnessError("request lifecycle state is unreadable") from exc
        if state["session_id"] != session_id:
            raise HarnessError("request lifecycle state is invalid")
        return state

    @_serialized_transition
    def clear(self, session_id: str) -> bool:
        path = self._path(session_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    @_serialized_transition
    def begin(self, lane: Lane, session_id: str, request: str, *,
              stage: Optional[str] = None, request_id: Optional[str] = None,
              max_review_rounds: int = 3, context=None, data=None) -> LifecycleState:
        if lane not in _LANES or lane not in _STAGES:
            raise HarnessError("request lifecycle lane is invalid")
        if not 1 <= int(max_review_rounds) <= 8:
            raise HarnessError("request lifecycle review limit must be 1..8")
        initial_stage = stage or {
            "chat": "route", "audit": "inspect", "auto_edit": "inspect",
            "driver": "observe", "provisioning": "probe",
        }[lane]
        state: Dict[str, Any] = {
            "schema": "harness-lifecycle-v1", "lane": lane,
            "stage": initial_stage, "status": "running",
            "request_id": request_id or uuid.uuid4().hex,
            "session_id": session_id, "original_request": request,
            "followups": [], "context": dict(context or {}), "candidate": None,
            "review": {"rounds": 0, "max_rounds": int(max_review_rounds),
                       "decision": "pending", "confidence": None,
                       "history": []},
            "usage": {"known_cost": 0.0, "unknown_liability": 0.0,
                      "provider_calls": {}},
            "execution": {"effects": {}, "completed_steps": {},
                          "approval_state": {}, "requires_reconciliation": []},
            "data": dict(data or {}),
        }
        self.save(state)  # type: ignore[arg-type]
        self._progress(state, resumed=False)
        return state  # type: ignore[return-value]

    @_serialized_transition
    def adopt_chat_payload(self, session_id: str, payload: Dict[str, Any]):
        """Merge the legacy chat view into the shared lifecycle checkpoint."""
        if not isinstance(payload, dict):
            raise HarnessError("chat checkpoint must be an object")
        state = self.load(session_id)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = state["original_request"] if state and state["lane"] == "chat" else None
        if not isinstance(prompt, str) or not prompt.strip():
            raise HarnessError("chat continuation state has no original request")
        if state is None or state["lane"] != "chat":
            state = self.begin("chat", session_id, prompt)
        merged = dict(state["data"])
        merged.update(payload)
        status = payload.get("status", state["status"])
        if status not in _STATUSES:
            raise HarnessError("chat continuation status is invalid")
        stage = ("terminal" if status in ("complete", "deferred", "failed")
                 else payload.get("stage", state["stage"]))
        if stage not in _STAGES["chat"]:
            raise HarnessError("chat continuation stage is invalid")
        state["status"] = status
        state["stage"] = stage
        state["original_request"] = prompt
        state["followups"] = list(
            payload.get("followups", state["followups"]) or [])
        state["candidate"] = merged.get("answer", state["candidate"])
        state["context"] = {
            "text": merged.get("context", state["context"].get("text", "")),
            "files": merged.get("context_files", state["context"].get("files", [])),
            "scope": merged.get("context_scope", state["context"].get("scope", "")),
        }
        usage = state["usage"]
        if "known_cost" in payload:
            usage["known_cost"] = max(
                usage["known_cost"],
                max(0.0, float(payload.get("known_cost") or 0.0)))
        if "unknown_liability" in payload:
            usage["unknown_liability"] = max(
                usage["unknown_liability"],
                max(0.0, float(payload.get("unknown_liability") or 0.0)))
        prior_review = payload.get("rounds")
        if isinstance(prior_review, list) and state["review"]["rounds"] == 0:
            max_rounds = payload.get("max_review_rounds")
            if (isinstance(max_rounds, int) and not isinstance(max_rounds, bool)
                    and 1 <= max_rounds <= 8):
                state["review"]["max_rounds"] = max_rounds
            request_id = state["request_id"]
            threshold = payload.get("confidence_threshold", 0.8)
            if (isinstance(threshold, bool)
                    or not isinstance(threshold, (int, float))
                    or not math.isfinite(float(threshold))
                    or not 0 <= threshold <= 1):
                threshold = 0.8
            for index, item in enumerate(prior_review[:8], 1):
                if not isinstance(item, dict):
                    continue
                envelope = item.get("jev") if isinstance(item.get("jev"), dict) else item
                confidence_data = envelope.get("confidence")
                answers = envelope.get("answers")
                supported = (confidence_data.get("observed")
                             if isinstance(confidence_data, dict) else None)
                confidence = envelope.get("supported")
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                    confidence = None
                if isinstance(supported, bool) or not isinstance(supported, (int, float)):
                    supported = None
                if isinstance(answers, dict):
                    iteration = bool(answers.get("iteration_required"))
                    plan_required = bool(answers.get("plan_required"))
                else:
                    iteration = bool(item.get("iteration_required"))
                    plan_required = bool(item.get("plan_required"))
                native = bool(envelope.get("native"))
                verdict = envelope.get("verdict", "fail")
                if (native and isinstance(supported, (int, float))
                        and float(supported) >= float(threshold)
                        and not iteration and not plan_required):
                    verdict = "pass"
                self.review(
                    state,
                    f"{request_id}:chat-review:{item.get('round', index)}",
                    {
                        "verdict": verdict,
                        "native": native,
                        "confidence": confidence,
                        "supported": supported,
                        "iteration_required": iteration,
                        "plan_required": plan_required,
                        "cost": envelope.get("cost", 0.0),
                    },
                    confidence_threshold=float(threshold))
        merged["prompt"] = prompt
        merged.setdefault("schema", "harness-chat-checkpoint-v1")
        merged["session_id"] = session_id
        merged["updated_at"] = time.time()
        merged.setdefault("lane", "chat")
        merged.setdefault("request_id", state["request_id"])
        state["data"] = merged
        return self.save(state)

    def chat_payload(self, session_id: str) -> Optional[Dict[str, Any]]:
        state = self.load(session_id)
        if state is None or state["lane"] != "chat" or state["status"] == "complete":
            return None
        metadata = {"schema", "session_id", "updated_at", "lane", "request_id"}
        return {key: value for key, value in state["data"].items()
                if key not in metadata}

    @_serialized_transition
    def resume(self, session_id: str, *, followup: Optional[str] = None):
        state = self.load(session_id)
        if (state is None or state["status"] in ("complete", "failed")
                or (state["status"] == "deferred"
                    and not state["data"].get("resumable"))):
            return None
        if state["status"] == "deferred":
            resume_stage = state["data"].pop("resume_stage", None)
            state["data"]["resumable"] = False
            if resume_stage in _STAGES[state["lane"]] - {"terminal"}:
                state["stage"] = resume_stage
        if followup:
            if len(state["followups"]) >= _MAX_FOLLOWUPS:
                raise HarnessError("request lifecycle follow-up limit reached")
            state["followups"].append(followup.strip()[:4_000])
            if state["lane"] == "chat":
                state["data"]["followups"] = list(state["followups"])
        unknown_calls = state["usage"]["provider_calls"]
        for call in unknown_calls.values():
            if call.get("status") == "dispatched":
                call["status"] = "unknown"
                state["usage"]["unknown_liability"] += max(
                    0.0, float(call.get("reserved_cost") or 0.0))
        pending_effects = []
        for effect_id, effect in state["execution"]["effects"].items():
            if effect.get("status") == "started":
                effect["status"] = "unknown"
            if effect.get("status") == "unknown":
                pending_effects.append(effect_id)
        if pending_effects:
            state["execution"]["requires_reconciliation"] = sorted(
                set(state["execution"]["requires_reconciliation"] + pending_effects))
            state["status"] = "deferred"
            state["stage"] = "terminal"
            state["data"]["defer_reason"] = "an interrupted side effect needs reconciliation"
            self.save(state)
            self._progress(state, resumed=True)
            return state
        state["status"] = "running"
        self.save(state)
        self._progress(state, resumed=True)
        return state

    def active_request(self, state: LifecycleState) -> str:
        text = state["original_request"]
        for followup in state["followups"]:
            text += "\n\nFollow-up instruction: " + followup
        return text

    @_serialized_transition
    def advance(self, state: LifecycleState, stage: str, **changes):
        if stage not in _STAGES[state["lane"]] - {"terminal"}:
            raise HarnessError("request lifecycle stage is invalid for its lane")
        if state["status"] not in ("running", "interrupted"):
            raise HarnessError("terminal request lifecycle cannot advance")
        state["stage"] = stage
        state["status"] = "running"
        for key, value in changes.items():
            if key not in {"context", "candidate", "data"}:
                raise HarnessError("request lifecycle update field is invalid")
            state[key] = value
        self.save(state)
        self._progress(state, resumed=False)
        return state

    @_serialized_transition
    def review(self, state: LifecycleState, review_id: str, judgment: Dict[str, Any],
               *, confidence_threshold: float = 0.8, final: bool = True):
        if not isinstance(review_id, str) or not review_id:
            raise HarnessError("review id is required")
        if (isinstance(confidence_threshold, bool)
                or not isinstance(confidence_threshold, (int, float))
                or not math.isfinite(float(confidence_threshold))
                or not 0 <= confidence_threshold <= 1):
            raise HarnessError("review confidence threshold is invalid")
        review = state["review"]
        old = next((r for r in review["history"] if r["review_id"] == review_id), None)
        if old is not None:
            return ReviewTransition(
                old["decision"], old["decision"] == "revise",
                old["decision"] == "pass", review["rounds"])
        if review["rounds"] >= review["max_rounds"]:
            return ReviewTransition("limit", False, False, review["rounds"])
        native = bool(judgment.get("native"))
        supported = review_probability(judgment.get("supported"))
        confidence = review_probability(judgment.get("confidence"))
        iteration = bool(judgment.get("iteration_required"))
        plan_required = bool(judgment.get("plan_required"))
        decisive = (native and final and supported is not None
                    and float(supported) >= confidence_threshold
                    and not iteration and not plan_required
                    and judgment.get("verdict", "pass") == "pass")
        decision = ("pass" if decisive else "revise" if iteration
                    and review["rounds"] + 1 < review["max_rounds"]
                    else "needs_iteration" if iteration else "abstain")
        entry: ReviewRound = {
            "review_id": review_id, "decision": decision, "native": native,
            "confidence": (float(confidence) if confidence is not None else None),
            "supported": (float(supported) if supported is not None else None),
            "iteration_required": iteration, "plan_required": plan_required,
            "cost": max(0.0, float(judgment.get("cost") or 0.0)),
        }
        review["history"].append(entry)
        review["rounds"] += 1
        review["decision"] = decision
        review["confidence"] = entry["confidence"]
        self.save(state)
        return ReviewTransition(decision, decision == "revise", decisive,
                                review["rounds"])

    @_serialized_transition
    def provider_started(self, state: LifecycleState, call_id: str,
                         reserved_cost: float = 0.0) -> bool:
        calls = state["usage"]["provider_calls"]
        previous = calls.get(call_id)
        if previous is not None:
            return False
        calls[call_id] = {"status": "dispatched",
                          "reserved_cost": max(0.0, float(reserved_cost)),
                          "actual_cost": 0.0, "result": None}
        self.save(state)
        return True

    @_serialized_transition
    def provider_completed(self, state: LifecycleState, call_id: str,
                           result: Any, actual_cost: float = 0.0):
        call = state["usage"]["provider_calls"].get(call_id)
        if call is None or call.get("status") != "dispatched":
            raise HarnessError("provider completion has no matching dispatch")
        cost = max(0.0, float(actual_cost or 0.0))
        call.update({"status": "completed", "actual_cost": cost,
                     "result": result})
        state["usage"]["known_cost"] += cost
        self.save(state)

    def provider_call(self, state: LifecycleState, call_id: str, invoke,
                      *, cost=lambda _result: 0.0,
                      retry_unknown: bool = False):
        """Reuse a completion receipt or explicitly retry an unknown call."""
        calls = state["usage"]["provider_calls"]
        retry_prefix = call_id + ":retry:"
        retry_ids = [
            key for key in calls if key.startswith(retry_prefix)
            and key[len(retry_prefix):].isdigit()]
        latest_retry = max(
            retry_ids, key=lambda key: int(key[len(retry_prefix):]),
            default=None)
        completed_ids = sorted(
            retry_ids, key=lambda key: int(key[len(retry_prefix):]),
            reverse=True) + [call_id]
        completed_id = next((key for key in completed_ids
                             if calls.get(key, {}).get("status") == "completed"),
                            None)
        if completed_id is not None:
            return calls[completed_id].get("result")
        previous = calls.get(latest_retry or call_id)
        if previous is not None and previous.get("status") != "unknown":
            raise HarnessError(
                "a prior provider call has unknown completion; it was not replayed")
        if previous is not None:
            if not retry_unknown:
                raise HarnessError(
                    "a prior provider call has unknown completion; it was not replayed")
            call_id = f"{retry_prefix}{1 if latest_retry is None else int(latest_retry[len(retry_prefix):]) + 1}"
        if not self.provider_started(state, call_id):
            previous = calls.get(call_id)
            if previous is not None and previous.get("status") == "completed":
                return previous.get("result")
            raise HarnessError(
                "a prior provider call has unknown completion; it was not replayed")
        try:
            result = invoke()
        except (ToolCancelled, ProviderUsageUnknown) as exc:
            self.interrupt(state, exc, call_id=call_id)
            raise
        self.provider_completed(state, call_id, result, float(cost(result) or 0.0))
        return result

    @_serialized_transition
    def interrupt(self, state: LifecycleState, exc: Exception, *,
                  call_id: Optional[str] = None):
        if state["status"] in ("complete", "deferred", "failed"):
            return state
        usage = state["usage"]
        known = max(0.0, float(getattr(exc, "known_cost", 0.0) or 0.0))
        reserved = max(0.0, float(getattr(exc, "reserved_cost", 0.0) or 0.0))
        usage["known_cost"] += known
        if getattr(exc, "usage_unknown", False):
            if call_id:
                call = usage["provider_calls"].get(call_id)
                if call is None:
                    call = {"status": "unknown", "reserved_cost": reserved,
                            "actual_cost": known, "result": None}
                    usage["provider_calls"][call_id] = call
                if call.get("status") != "unknown":
                    call["status"] = "unknown"
                    usage["unknown_liability"] += max(
                        0.0, float(call.get("reserved_cost") or reserved))
            else:
                usage["unknown_liability"] += reserved
        state["status"] = "interrupted"
        self.save(state)
        self._progress(state, resumed=False)
        return state

    @_serialized_transition
    def begin_effect(self, state: LifecycleState, effect_id: str,
                     operation: str, *, approval_state=None):
        """Persist intent before a side effect; never turns Jev into approval."""
        effects = state["execution"]["effects"]
        prior = effects.get(effect_id)
        if prior is not None:
            if prior["status"] == "completed":
                return "completed", prior.get("result")
            return "blocked", prior
        effects[effect_id] = {
            "effect_id": effect_id, "operation": operation,
            "status": "started", "approval_state": dict(approval_state or {}),
            "result": None,
        }
        self.save(state)
        return "run", None

    @_serialized_transition
    def complete_effect(self, state: LifecycleState, effect_id: str, result: Any):
        effect = state["execution"]["effects"].get(effect_id)
        if effect is None or effect["status"] not in ("started", "completed"):
            raise HarnessError("effect completion has no matching start")
        effect["status"] = "completed"
        effect["result"] = result
        self.save(state)

    @_serialized_transition
    def fail_effect(self, state: LifecycleState, effect_id: str, result: Any = None):
        effect = state["execution"]["effects"].get(effect_id)
        if effect is None or effect.get("status") != "started":
            raise HarnessError("effect failure has no matching start")
        effect["status"] = "failed"
        effect["result"] = result
        self.save(state)

    @_serialized_transition
    def unknown_effect(self, state: LifecycleState, effect_id: str):
        effect = state["execution"]["effects"].get(effect_id)
        if effect is None:
            return
        effect["status"] = "unknown"
        if effect_id not in state["execution"]["requires_reconciliation"]:
            state["execution"]["requires_reconciliation"].append(effect_id)
        self.save(state)

    @_serialized_transition
    def finish(self, state: LifecycleState, status: Literal["complete", "deferred", "failed"],
               *, candidate=None, result=None, resumable: bool = False):
        if status not in ("complete", "deferred", "failed"):
            raise HarnessError("request lifecycle terminal status is invalid")
        prior_stage = state["stage"]
        for call in state["usage"]["provider_calls"].values():
            if call["status"] == "dispatched":
                call["status"] = "unknown"
                state["usage"]["unknown_liability"] += max(
                    0.0, float(call.get("reserved_cost") or 0.0))
        unresolved = []
        for effect_id, effect in state["execution"]["effects"].items():
            if effect["status"] == "started":
                effect["status"] = "unknown"
            if effect["status"] == "unknown":
                unresolved.append(effect_id)
        if unresolved:
            state["execution"]["requires_reconciliation"] = sorted(
                set(state["execution"]["requires_reconciliation"] + unresolved))
            if status in ("complete", "failed"):
                status = "deferred"
            resumable = True
            state["data"].setdefault(
                "defer_reason", "an unresolved side effect needs reconciliation")
        state["stage"] = "terminal"
        state["status"] = status
        state["data"]["resumable"] = bool(resumable)
        if status == "deferred" and resumable:
            state["data"]["resume_stage"] = prior_stage
        if candidate is not None:
            state["candidate"] = candidate
        if result is not None:
            state["data"]["result"] = result
        self.save(state)
        self._progress(state, resumed=False)
        return state

    @_serialized_transition
    def _progress(self, state, *, resumed: bool):
        self.event("lifecycle_progress", lane=state["lane"],
                   stage=state["stage"], status=state["status"],
                   request_id=state["request_id"], resumed=bool(resumed))


class LaneLifecycleAdapter:
    """Small lane boundary that shares transitions without owning execution."""
    lane: Lane

    def __init__(self, coordinator: LifecycleCoordinator):
        self.coordinator = coordinator

    def begin(self, session_id, request, **kwargs):
        return self.coordinator.begin(self.lane, session_id, request, **kwargs)

    def resume(self, session_id, *, followup=None):
        state = self.coordinator.resume(session_id, followup=followup)
        return state if state is not None and state["lane"] == self.lane else None

    def advance(self, state, stage, **changes):
        return self.coordinator.advance(state, stage, **changes)

    def review(self, state, review_id, judgment, **kwargs):
        return self.coordinator.review(state, review_id, judgment, **kwargs)

    def provider_call(self, state, call_id, invoke, **kwargs):
        return self.coordinator.provider_call(
            state, call_id, invoke, **kwargs)

    def finish(self, state, status, **kwargs):
        return self.coordinator.finish(state, status, **kwargs)


class ChatLifecycleAdapter(LaneLifecycleAdapter):
    lane: Lane = "chat"


class AuditLifecycleAdapter(LaneLifecycleAdapter):
    lane: Lane = "audit"


class AutoEditLifecycleAdapter(LaneLifecycleAdapter):
    lane: Lane = "auto_edit"


class DriverLifecycleAdapter(LaneLifecycleAdapter):
    lane: Lane = "driver"


class ProvisionLifecycleAdapter(LaneLifecycleAdapter):
    lane: Lane = "provisioning"


def adapter_for(coordinator: LifecycleCoordinator, lane: Lane) -> LaneLifecycleAdapter:
    adapters = {
        "chat": ChatLifecycleAdapter,
        "audit": AuditLifecycleAdapter,
        "auto_edit": AutoEditLifecycleAdapter,
        "driver": DriverLifecycleAdapter,
        "provisioning": ProvisionLifecycleAdapter,
    }
    try:
        return adapters[lane](coordinator)
    except KeyError as exc:
        raise HarnessError("request lifecycle lane is invalid") from exc
