"""Apply result vocabulary: the one owner of every shape a run terminal emits.

``_round_entry`` builds one record in a result's ``rounds`` list;
``_terminal_result`` is the one builder for every run outcome (preview, ok,
deferred, verify_failed); ``_defer_result`` builds the terminal deferral with
its continuation state; ``_http_error`` renders a failed HTTP attempt as the
user-facing string. The engine (harness/apply.py) calls these; CLI and MCP
only consume the shapes -- so a new outcome field has exactly one place to
land, and every consumer sees one consistent result contract. Names carry the
package's internal-vocabulary underscore (like prompts._parse_ready): the
consumers are all in-package.
"""
import difflib

from .continuation import gate_id
from .prompts import MAX_FILE_LINES
from .filesafety import file_content_hash

_OMIT = object()

# The meaning of a terminal status, ONE place: which outcomes are successes,
# and what exit code each maps to for interfaces (0 ok/preview, 3 deferred,
# 2 failed -- unknown statuses fail closed at 2).
SUCCESS_STATUSES = frozenset({"ok", "preview"})


def model_envelope(*, model_requested=None, model_observed=None):
    """MS envelope: the model a run ASKED for vs the models that actually
    served. Rotation, trust-step escalation, and the escalation ladder can all
    make the two differ; consumers (CLI, MCP, GUI, agent, site export) read one
    shape instead of re-deriving it from ``rounds``."""
    observed = []
    for model in model_observed or []:
        if model and model not in observed:
            observed.append(model)
    return {"model_requested": model_requested, "model_observed": observed}


def terminal_exit_code(status):
    """The interface exit-code policy for a run's terminal status. cli and the
    dogfood report both consume this; neither re-derives the meaning."""
    if status in SUCCESS_STATUSES:
        return 0
    return 3 if status == "deferred" else 2


def _round_entry(round_no, model, status, *, cost, verify_output,
                changed=_OMIT, verify_passed=_OMIT, reason=None, error=None,
                **extra):
    """One round's record in the result's `rounds` list -- one shape, one
    place. Terminal feedback readers (retry context, broken-gate detector,
    CLI/MCP output) all consume these same keys. Fields left at the sentinel
    are omitted, so each status keeps exactly its historical key set."""
    entry = {"round": round_no, "model": model, "status": status,
             "cost": cost, "verify_output": verify_output}
    if changed is not _OMIT:
        entry["changed"] = changed
    if verify_passed is not _OMIT:
        entry["verify_passed"] = verify_passed
    if reason is not None:
        entry["reason"] = reason
    if error is not None:
        entry["error"] = error
    entry.update(extra)
    return entry


def _terminal_result(status, *, task_id, rounds, cost, rotations, backend,
                    **extra):
    """One builder for every run outcome (preview/ok/deferred/verify_failed).
    Callers pass their distinguishing fields as keyword extras; the shared
    core guarantees CLI/MCP consumers see one consistent shape."""
    result = {"status": status, "task_id": task_id, "rounds": rounds,
              "cost": cost, "rotations": rotations, "backend": backend}
    result.update(extra)
    return result


def _defer_result(*, task_id, file_path, category, reason, remaining_scope,
                 rounds, history, cost, backend="harness", verify_only=False,
                 max_lines=MAX_FILE_LINES, edit_snippet=None, verify_cmd=None,
                 partial_content=None):
    """Terminal deferral: partial work preserved in a continuation state, not
    in the working tree."""
    continuation = {
        "schema_version": 1,
        "file_path": file_path,
        "target_hash": file_content_hash(file_path),
        "task_id": task_id,
        "backend": backend,
        "verify_only": verify_only,
        "max_lines": max_lines,
        "edit_snippet": edit_snippet,
        "verify_cmd": verify_cmd,
        "verify_gate_id": gate_id(verify_cmd) if verify_cmd else None,
        "verification_required": bool(verify_cmd) and not verify_only,
        "remaining_scope": remaining_scope,
        "reason": reason,
        "history": history,
    }
    if partial_content is not None:
        # Ungated model output must live in the state file, never in the
        # working tree; a resumed run may inspect it but the gate decides
        # what lands on disk.
        continuation["partial_content"] = partial_content
    return _terminal_result(
        "deferred", task_id=task_id, rounds=rounds, cost=cost,
        rotations=None, backend=backend, verify_only=verify_only,
        category=category, reason=reason, file=file_path,
        remaining_scope=remaining_scope, verify_cmd=verify_cmd,
        verify_gate_id=continuation["verify_gate_id"],
        verification_required=continuation["verification_required"],
        continuation=continuation)


def _content_diff(original, proposed):
    """Unified diff (no header timestamps) of a proposed edit, or None when
    the content is unchanged/unavailable. The UI's change-preview field:
    computed from content the run already held in memory, so previewing
    adds no filesystem reads and no new capability."""
    if not isinstance(original, str) or not isinstance(proposed, str):
        return None
    if original == proposed:
        return None
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True), fromfile="a", tofile="b"))


def _http_error(status, resp):
    """The user-facing error for a failed HTTP attempt: the provider message
    when there is one, else the raw body -- ALWAYS prefixed with the HTTP
    status, which the body text alone may not carry ("Rate limit exceeded"
    says nothing about 429; terminal saturation detection reads this prefix)."""
    body = (resp.get("error", {}).get("message", str(resp))
            if isinstance(resp, dict) else str(resp))
    return f"HTTP {status}: {body}"
