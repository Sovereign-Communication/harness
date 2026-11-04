"""Terminal state for a validated restart or consent refusal (issue #430).

``jev_packs.validate_restart_request`` already decides whether a restart is
legal. What it did not own was what happens *after* a refusal: the same hold
message could be re-derived forever, and no state said "this run is blocked"
or "consent must be renewed for this exact assignment".

This module is the one owner of that post-refusal state. It never dispatches,
writes files, or spends; it classifies a guard decision, binds it to the
request/assignment/checkpoint, and records it once so repeated automatic
retries are idempotent. A checkpoint resume is a distinct operation from a
restart: it must match the original request and plan, and it may not reuse
expired consent.
"""
import hashlib
import json
import os

from .errors import HarnessError
from .jev_packs import validate_restart_request
from .mcp_log import scrub
from .osal import atomic_write_text

BLOCKED = "blocked"
REAPPROVAL_REQUIRED = "reapproval_required"
ALLOWED = "allowed"
RESUMED = "resumed"


def assignment_hash(assignment):
    """A stable identity for the exact assignment; the content never lands."""
    canonical = json.dumps(assignment, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_key(fields):
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RestartStateStore:
    """Durable, deduplicated terminal restart/refusal state.

    ``path`` is optional; without it the store is process-local. With it, a
    restart reloads the same decisions and does not emit a second event for a
    refusal it already recorded. A malformed store fails closed rather than
    forgetting that a run was blocked.
    """

    def __init__(self, path=None, event=None):
        self.path = path
        self.event = event
        self._states = {}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError) as exc:
                raise HarnessError(
                    f"restart state is unreadable: {path} ({exc})") from exc
            if not isinstance(loaded, dict):
                raise HarnessError(
                    f"restart state must be an object: {path}")
            self._states = loaded

    def _save(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        atomic_write_text(self.path, json.dumps(
            self._states, sort_keys=True, separators=(",", ":")), newline=None)

    def _remember(self, state):
        key = state["event_key"]
        if key in self._states:
            return {**state, "deduplicated": True, "event_emitted": False}
        self._states[key] = state
        self._save()
        emitted = bool(state["terminal"] and self.event)
        if emitted:
            try:
                self.event("restart_terminal", **{
                    "request_id": state.get("request_id"),
                    "status": state["status"],
                    "assignment_hash": state["assignment_hash"],
                    "reason": state["reasons"],
                })
            except Exception:
                emitted = False
        return {**state, "deduplicated": False, "event_emitted": emitted}

    def last(self, request_id):
        """The latest terminal state for ``request_id``, if one exists."""
        found = None
        for state in self._states.values():
            if state.get("request_id") == request_id:
                found = state
        return found

    def decide_restart(self, *, request_id, assignment, current_stage,
                       requested_target, completed_stages=(),
                       consent_fresh=None, consent_id=None,
                       checkpoint=None, prior_results=None):
        """Classify one restart decision and retain it idempotently."""
        guard = validate_restart_request(
            current_stage, requested_target,
            completed_stages=completed_stages,
            consent_fresh=consent_fresh)
        identity = assignment_hash(assignment)
        if guard["allowed"]:
            status, terminal = ALLOWED, False
            next_action = "continue with the validated restart"
        elif guard["consent_renewal_required"]:
            status, terminal = REAPPROVAL_REQUIRED, True
            next_action = ("renew consent for the exact assignment "
                           f"{identity} before any dispatch")
        else:
            status, terminal = BLOCKED, True
            next_action = ("retrieve a compatible checkpoint or choose an "
                           "earlier declared stage; do not repeat this "
                           "transition")
        state = {
            "operation": "restart",
            "status": status,
            "terminal": terminal,
            "request_id": None if request_id is None else str(request_id),
            "assignment_hash": identity,
            "current_stage": guard.get("current_stage"),
            "requested_target": guard.get("target"),
            "completed_stages": list(completed_stages or []),
            "preserved_stages": list(guard.get("preserved_stages") or []),
            "checkpoint": scrub(checkpoint or {}),
            "prior_results": scrub(prior_results or {}),
            "consent_id": scrub(consent_id) if consent_id is not None else None,
            "consent_renewal_required": bool(
                guard.get("consent_renewal_required")),
            "reasons": list(guard.get("reasons") or []),
            "next_action": next_action,
            "event_key": None,
        }
        state["event_key"] = _event_key({
            "operation": "restart", "assignment_hash": identity,
            "current_stage": state["current_stage"],
            "requested_target": state["requested_target"],
            "consent_id": state["consent_id"],
            "consent_fresh": consent_fresh,
            "completed_stages": state["completed_stages"],
        })
        return self._remember(state)

    def decide_resume(self, *, request_id, plan_hash, checkpoint, consent):
        """Resume from a verified checkpoint; distinct from a restart.

        The checkpoint must be bound to the same request and plan, and the
        consent must cover that exact plan. A mismatch is a refusal, never a
        silent reuse of an older approval.
        """
        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        consent = consent if isinstance(consent, dict) else {}
        bound_request = checkpoint.get("request_id")
        bound_plan = checkpoint.get("plan_hash")
        stale = (not consent.get("fresh")
                 or consent.get("assignment_hash") != plan_hash)
        if bound_request != request_id or bound_plan != plan_hash:
            status, terminal = BLOCKED, True
            reasons = ["checkpoint is not bound to this request and plan"]
            next_action = "retrieve the checkpoint written for this request"
        elif stale:
            status, terminal = REAPPROVAL_REQUIRED, True
            reasons = ["consent does not cover the checkpoint's plan"]
            next_action = ("renew consent for the exact plan "
                           f"{plan_hash} before resuming")
        else:
            status, terminal = RESUMED, False
            reasons = []
            next_action = "continue from the verified checkpoint"
        state = {
            "operation": "resume",
            "status": status,
            "terminal": terminal,
            "request_id": None if request_id is None else str(request_id),
            "assignment_hash": plan_hash,
            "checkpoint": scrub(checkpoint),
            "consent_id": scrub(consent.get("id")),
            "reasons": reasons,
            "next_action": next_action,
            "event_key": _event_key({
                "operation": "resume", "request_id": request_id,
                "plan_hash": plan_hash, "checkpoint": checkpoint,
                "consent": consent,
            }),
        }
        return self._remember(state)
