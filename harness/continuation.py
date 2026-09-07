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
import os

from .errors import HarnessError

from .filesafety import file_content_hash


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
