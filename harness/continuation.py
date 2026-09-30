"""Continuation-state contract: what a resumable task must carry, and the
identity of the verification gate it must be resumed under.

A failed gated apply persists ``verification_required`` so a caller cannot
turn it into a gate-free preview by changing an option while resuming. The
gate's identity is the SHA-256 of the exact command, stored beside it: a
state file whose ``verify_cmd`` and ``verify_gate_id`` disagree is corrupt or
tampered and is refused before any key, model, or file is touched.

The ApplyEngine owns the per-request *pin* (which gate this run is authorized
to execute); this module owns the persisted *contract*, its validation, and
the bind-or-refuse decision that couples a runner to the pinned gate.
"""
import hashlib
import json
import math
import os
import re

from .errors import HarnessError

from .filesafety import file_content_hash


WORK_PACKAGE_HANDOFF_SCHEMA = "jev-work-package-handoff-v1"
WORK_PACKAGE_HANDOFF_VERSION = 1


def _json_safe(value, path="payload"):
    """Reject values that cannot make a stable, portable JSON handoff."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HarnessError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HarnessError(f"{path} has a non-string object key")
            _json_safe(item, f"{path}.{key}")
        return
    raise HarnessError(f"{path} contains non-JSON value {type(value).__name__}")


def _canonical_digest(value):
    _json_safe(value, "assignment")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _node_ids(values, label):
    if not isinstance(values, (list, tuple)) or not values:
        raise HarnessError(f"{label} must be a non-empty node ID list")
    result = list(values)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise HarnessError(f"{label} contains an invalid node ID")
    if len(set(result)) != len(result):
        raise HarnessError(f"{label} contains duplicate node IDs")
    return result


def build_work_package_handoff(*, original_request, plan,
                               remaining_node_ids, completed_nodes,
                               source_brief, source_pins, assignment,
                               limits, gates, run_snapshot, budget_snapshot):
    """Build a typed multi-node handoff; this does not persist or resume it.

    ``plan`` is retained verbatim as JSON data. Completed node evidence is
    keyed by original plan node ID, and remaining nodes must be exactly the
    complement, so a resume consumer cannot accidentally replay completed
    work or silently lose an unfinished node.
    """
    if not isinstance(original_request, str) or not original_request.strip():
        raise HarnessError("handoff original_request must be non-empty text")
    if not isinstance(plan, dict) or not isinstance(plan.get("nodes"), list):
        raise HarnessError("handoff plan must contain its exact nodes list")
    _json_safe(plan, "plan")
    plan_ids = _node_ids([node.get("node_id") for node in plan["nodes"]
                          if isinstance(node, dict)], "plan node IDs")
    if len(plan_ids) != len(plan["nodes"]):
        raise HarnessError("handoff plan contains a malformed node")
    remaining = _node_ids(remaining_node_ids, "remaining_node_ids")
    if not isinstance(completed_nodes, dict):
        raise HarnessError("completed_nodes must map node IDs to evidence")
    completed_ids = list(completed_nodes)
    if any(not isinstance(node_id, str) or not node_id.strip()
           for node_id in completed_ids):
        raise HarnessError("completed_nodes contains an invalid node ID")
    if set(completed_ids) & set(remaining):
        raise HarnessError("completed nodes may not be marked pending")
    if set(completed_ids) | set(remaining) != set(plan_ids):
        raise HarnessError("completed and remaining nodes must cover the exact plan")
    for label, value in (("source_brief", source_brief),
                         ("source_pins", source_pins), ("assignment", assignment),
                         ("limits", limits), ("gates", gates),
                         ("run_snapshot", run_snapshot),
                         ("budget_snapshot", budget_snapshot),
                         ("completed_nodes", completed_nodes)):
        _json_safe(value, label)
    if not isinstance(assignment, dict):
        raise HarnessError("assignment must be a JSON object")
    for label, value in (("source_brief", source_brief), ("limits", limits),
                         ("run_snapshot", run_snapshot),
                         ("budget_snapshot", budget_snapshot)):
        if not isinstance(value, dict):
            raise HarnessError(f"{label} must be a JSON object")
    if assignment.get("limits") != limits:
        raise HarnessError("handoff limits differ from the consented assignment")
    pins = _validate_pins(source_pins)
    gate_map = _gate_map(gates, plan_ids)
    handoff = {
        "schema": WORK_PACKAGE_HANDOFF_SCHEMA,
        "version": WORK_PACKAGE_HANDOFF_VERSION,
        "original_request": original_request,
        "plan": plan,
        "plan_node_ids": plan_ids,
        "remaining_node_ids": remaining,
        "completed_nodes": completed_nodes,
        "source_brief": source_brief,
        "source_pins": pins,
        "assignment": assignment,
        "assignment_digest": _canonical_digest(assignment),
        "limits": limits,
        "gates": gate_map,
        "run_snapshot": run_snapshot,
        "budget_snapshot": budget_snapshot,
    }
    return handoff


def _validate_pins(pins):
    if not isinstance(pins, list):
        raise HarnessError("source_pins must be a list")
    seen = set()
    normalized = []
    for item in pins:
        if not isinstance(item, dict):
            raise HarnessError("each source pin must be an object")
        path, digest = item.get("path"), item.get("sha256")
        if not isinstance(path, str) or not path.strip() or path in seen:
            raise HarnessError("source pin paths must be non-empty and unique")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HarnessError(f"source pin {path!r} has an invalid sha256")
        seen.add(path)
        normalized.append({"path": path, "sha256": digest})
    return normalized


def _gate_map(gates, node_ids):
    if not isinstance(gates, dict) or set(gates) != set(node_ids):
        raise HarnessError("gates must name every plan node exactly once")
    result = {}
    for node_id, command in gates.items():
        if command is not None and (not isinstance(command, str) or not command.strip()):
            raise HarnessError(f"gate for {node_id!r} must be non-empty text or null")
        result[node_id] = {"command": command,
                           "gate_id": gate_id(command) if command else None}
    return result


def validate_work_package_handoff(handoff, *, root, current_gates,
                                  current_assignment, allowed_node_ids=None):
    """Validate an in-memory handoff before a future resume implementation.

    This verifies provenance and identities only. It deliberately does not
    dispatch work or translate the payload into file continuations.
    """
    if not isinstance(handoff, dict):
        raise HarnessError("work-package handoff must be a JSON object")
    _json_safe(handoff, "handoff")
    if handoff.get("schema") != WORK_PACKAGE_HANDOFF_SCHEMA or \
            handoff.get("version") != WORK_PACKAGE_HANDOFF_VERSION:
        raise HarnessError("unsupported work-package handoff schema/version")
    plan = handoff.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("nodes"), list):
        raise HarnessError("handoff plan is missing its exact nodes list")
    plan_ids = _node_ids([node.get("node_id") for node in plan["nodes"]
                          if isinstance(node, dict)], "plan node IDs")
    if len(plan_ids) != len(plan["nodes"]):
        raise HarnessError("handoff plan contains a malformed node")
    if plan_ids != handoff.get("plan_node_ids"):
        raise HarnessError("handoff plan node IDs changed")
    if allowed_node_ids is not None and set(plan_ids) != set(
            _node_ids(allowed_node_ids, "allowed_node_ids")):
        raise HarnessError("handoff plan contains a node outside the allowed plan")
    remaining = _node_ids(handoff.get("remaining_node_ids"), "remaining_node_ids")
    completed = handoff.get("completed_nodes")
    if not isinstance(completed, dict):
        raise HarnessError("completed_nodes must map node IDs to evidence")
    completed_ids = list(completed)
    if len(set(completed_ids)) != len(completed_ids) or \
            set(completed_ids) & set(remaining) or \
            set(completed_ids) | set(remaining) != set(plan_ids):
        raise HarnessError("completed/remaining node partition is invalid")
    assignment = handoff.get("assignment")
    if not isinstance(assignment, dict) or \
            handoff.get("assignment_digest") != _canonical_digest(assignment):
        raise HarnessError("handoff assignment digest mismatch")
    if current_assignment is not None and \
            _canonical_digest(current_assignment) != handoff.get("assignment_digest"):
        raise HarnessError("assignment changed since handoff; consent is stale")
    for name in ("source_brief", "limits", "run_snapshot", "budget_snapshot"):
        if not isinstance(handoff.get(name), dict):
            raise HarnessError(f"handoff {name} must be a JSON object")
    if assignment.get("limits") != handoff.get("limits"):
        raise HarnessError("handoff limits differ from the consented assignment")
    pins = _validate_pins(handoff.get("source_pins"))
    base = os.path.realpath(os.path.abspath(root))
    for pin in pins:
        path = os.path.realpath(os.path.abspath(os.path.join(base, pin["path"])))
        try:
            contained = os.path.commonpath([base, path]) == base
        except ValueError:
            contained = False
        if not contained or \
                not os.path.isfile(path):
            raise HarnessError(f"source pin is missing or outside root: {pin['path']!r}")
        if file_content_hash(path) != pin["sha256"]:
            raise HarnessError(f"source pin changed since handoff: {pin['path']!r}")
    current = _gate_map(current_gates, plan_ids)
    if current != handoff.get("gates"):
        raise HarnessError("verification gate changed since handoff")
    return handoff


def gate_id(verify_cmd):
    """Stable identity of a verification gate (sha256 of the exact command)."""
    return hashlib.sha256(verify_cmd.encode("utf-8")).hexdigest()[:16]


def normalize_continuation(state):
    """Return the nested continuation payload, rejecting malformed state."""
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise HarnessError("continuation must be a JSON object")
    if isinstance(state.get("continuation"), dict) and "file_path" not in state:
        return state["continuation"]
    return state


def validate_continuation(state):
    """Validate the authority boundary before any key or model setup.

    A failed gated apply persists ``verification_required`` so a caller cannot
    turn it into a gate-free preview by changing an option while resuming. A
    deferral that happened before a gate was needed (for example consent or a
    capability handoff with no ``verify_cmd``) remains resumable. Older state
    without this field is treated conservatively and requires its saved gate.
    This helper is shared by the library and CLI public paths.
    """
    state = normalize_continuation(state)
    if not state:
        return state
    schema_version = state.get("schema_version", 1)
    if schema_version != 1:
        raise HarnessError(f"unsupported continuation schema_version: {schema_version!r}")
    verify_only = state.get("verify_only", False)
    if not isinstance(verify_only, bool):
        raise HarnessError("continuation verify_only must be a boolean")
    required = state.get("verification_required")
    if required is None:
        required = not verify_only
    elif not isinstance(required, bool):
        raise HarnessError("continuation verification_required must be a boolean")
    verify_cmd = state.get("verify_cmd")
    if verify_cmd is not None and not isinstance(verify_cmd, str):
        raise HarnessError("continuation verify_cmd must be a string")
    if required:
        if not verify_cmd or not verify_cmd.strip():
            raise HarnessError(
                "continuation is missing its authoritative verify_cmd; "
                "a failed apply cannot be resumed without the original verification gate")
        if verify_only:
            raise HarnessError(
                "a gated continuation cannot be resumed as verify-only; "
                "the authoritative verification gate must run")
    # Gate identity check (gated resumes only, matching the historical
    # apply-time check): a state claiming a gate must carry its hash, and a
    # mismatched or missing hash means the gate was tampered with or the state
    # is from a different gate entirely (#5).
    saved_gate_id = state.get("verify_gate_id")
    if required and verify_cmd and saved_gate_id != gate_id(verify_cmd):
        raise HarnessError(
            "continuation verify_gate_id does not match its verify_cmd; "
            "state may be corrupted or tampered")

    target = state.get("file_path")
    if not isinstance(target, str) or not target.strip():
        raise HarnessError("continuation file_path must be a non-empty string")
    if not os.path.isfile(target) or os.path.islink(target):
        raise HarnessError("continuation target file is missing or is a symlink")
    saved_hash = state.get("target_hash")
    if saved_hash is not None:
        if not isinstance(saved_hash, str) or len(saved_hash) != 64:
            raise HarnessError("continuation target_hash is invalid")
        if file_content_hash(target) != saved_hash:
            raise HarnessError(
                "continuation target file changed since the state was saved; refusing to resume")
    return state


def bound_gate(continuation_gate, verify_cmd, base):
    """Bind-or-refuse: return the runner ``base`` only if it is authorized
    for this run's gate. A continuation supplies the gate the runner was
    built for; a different gate refuses rather than silently running under
    the wrong verification (#5). ``base`` may be a bench task_runner scoped
    per task without mutating engine state (#17)."""
    if not continuation_gate:
        return base
    if gate_id(verify_cmd) == gate_id(continuation_gate):
        return base
    raise HarnessError(
        "verify gate changed between the saved continuation and this run; "
        "refusing to run an unverified gate")
