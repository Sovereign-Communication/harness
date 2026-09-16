"""Multi-file batch orchestration (#12): one governed session per file
through the engine/router/gate, sharing the task budget, fail-fast on the
first non-success, and the batch envelope. The engine (harness/apply.py)
owns the per-file apply; this module owns the LOOP and the result shapes.
"""
from collections import Counter
from dataclasses import asdict, dataclass

from .prompts import MAX_APPLY_ROUNDS, MAX_FILE_LINES
from .results import SUCCESS_STATUSES
from .continuation import validate_continuation
from .validation import validate_batch_files


@dataclass(frozen=True)
class BatchOptions:
    """Per-file options for a batch, resolved once by the caller and consumed
    uniformly by every file in the loop. One definition replaces the kwargs
    threading in the CLI face (the same hand-threading class the arc killed
    with ResolvedVerifyInputs / PanelLanePolicy). Defaults match run_batch's
    own, so legacy direct calls without a bundle keep their exact behavior.

    Run-level parameters (task_id, apply_pool, cancel_check, keep_going,
    continuation routing) stay named run_batch parameters: they describe the
    batch as a whole, not a file's session."""
    instruction: object = None
    edit_snippet: object = None
    verify_cmd: object = None
    max_rounds: object = MAX_APPLY_ROUNDS
    require_consent: object = None
    model: object = None
    max_tokens: object = None
    task_max_cost: object = None
    allow_escalation: object = None
    reasoning_effort: object = None
    renew_consent: object = None
    max_rotations: object = None
    backend: object = "harness"
    verify_only: object = False
    max_lines: object = MAX_FILE_LINES
    continuation: object = None


def run_batch(engine, files, *, task_id=None, apply_pool=None,
              continuation=None, cancel_check=None, keep_going=False,
              options=None):
    """Multi-file batch (#12): one governed session per file through this
    engine/router/gate, sharing the task budget. Fail-fast: the batch stops
    at the first file that does not succeed. A single-file batch returns
    the bare result dict (never wrapped in the batch envelope).

    ``continuation`` resumes a saved state instead: the file list is
    replaced by the continuation's own file path and the session runs as
    a resume (the CLI apply --continue-from and continue subcommands both
    land here).

    ``keep_going`` (fail-soft) continues past a non-success file instead of
    aborting; every per-file result -- failures included -- stays in the
    envelope and the overall status still names the FIRST failure, so a
    mixed batch can never read as success. Default remains fail-fast.

    ``options`` carries the per-file session options as one :class:`BatchOptions`
    bundle (the CLI face constructs it once); its ``continuation`` field is
    honored when the run-level parameter is unset."""
    if continuation is None:
        # A caller may carry the resume state in the bundle instead of the
        # run-level parameter; the parameter wins when both are given.
        continuation = options.continuation
    continuation = validate_continuation(continuation)
    if continuation:
        # Resuming: the saved state owns the target file AND the task
        # identity -- a resume is the same task, not a new one, so its
        # ledger events stay attributable to the original run (the CLI
        # continue path leaves task_id unset for exactly this reason).
        # An explicit --task-id override still wins.
        files = [continuation.get("file_path")]
        task_id = task_id or continuation.get("task_id")
    if not continuation:
        files = validate_batch_files(files)
    kw = asdict(options)
    kw["apply_pool"] = apply_pool
    kw["cancel_check"] = cancel_check
    kw["continuation"] = continuation
    if len(files) == 1:
        # One file is not a batch: bare result, keyed off the INPUT --
        # a multi-file batch that dies on file 1 still gets the envelope.
        return engine.apply_edit(task_id=task_id or "apply",
                                 file_path=files[0], **kw)
    results = []
    shared_gate = None
    first_failure = None
    for i, fp in enumerate(files):
        r = engine.apply_edit(task_id=(task_id or "apply") + f"-{i + 1}",
                              file_path=fp, **kw)
        results.append(r)
        if r.get("status") not in SUCCESS_STATUSES:
            # Name the FIRST failure: under keep_going a later success must
            # never mask it (a mixed batch is not a success).
            first_failure = r if first_failure is None else first_failure
            if not keep_going:
                break  # fail fast: stop the batch at the first non-success
        shared_gate = r.get("verify", {}).get("command") \
            if isinstance(r.get("verify"), dict) else shared_gate
    statuses = dict(Counter(r.get("status") for r in results))
    total = sum(float(r.get("cost") or 0.0) for r in results)
    last = results[-1]
    last_verify = last.get("verify") if isinstance(last.get("verify"), dict) else {}
    gate_cmd = last_verify.get("command") or shared_gate
    # Overall status keys off the first failure, not the last result: with
    # keep_going, results[-1] can be a success while an earlier file died.
    overall = last.get("status") if first_failure is None \
        else first_failure.get("status")
    return {"status": overall,
            "batch": True, "files": list(files), "results": results,
            "statuses": statuses, "cost": total,
            "verify": {"command": gate_cmd,
                       "passed": bool(last_verify.get("passed", True))
                       if first_failure is None else False}
                      if gate_cmd else None}
