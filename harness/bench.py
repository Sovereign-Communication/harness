"""harness bench: a manifest-driven free-tier task runner.

The point of `bench` is to *test the free tier itself*. It runs a set of small,
known-answer code tasks through the free-tier apply pipeline and reports pass
rate, real cost, rotations, and per-model confidence calibration (readiness
verdict vs verify outcome). Each task's verify gate is the ground truth, so
"passed" means provably correct, not self-reported.

A manifest is either a directory of JSON files (one task each) or a single JSON
file containing a list (or a {"tasks": [...]} object). Each task:

    {
      "name": "add",
      "file": "add/adds.py",          # path relative to the manifest root
      "instruction": "Fix adds.add ...",
      "verify": "python check.py",    # gate run in the manifest root (cwd)
      "max_rounds": 3,                # optional
      "task_max_cost": 0.05,          # optional
    }

Bench is idempotent and re-runnable: each target file is snapshotted on first
run and restored before every run. Consent is off by default (bench is
CI/batch mode -- the checkbox is `require_consent` per task or `--with-consent`).
Results are recorded in the autonomy ledger (task ids `bench/<name>`), so
confidence-calibration data accumulates across bench runs.
"""
import json
import os

from .filesafety import _atomic_write, default_run_verify, VERIFY_TIMEOUT
from .errors import HarnessError
from .output import eprint


def _load_json_file(path, what):
    """Load a JSON file, presenting missing files and parse errors cleanly."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise HarnessError(f"{what} not readable: {path} ({e.strerror or e})")
    except ValueError as e:
        raise HarnessError(f"{what} is not valid JSON: {path} ({e})")


def load_manifest(path):
    """Load a manifest dir (one JSON per task) or a single JSON file."""
    tasks = []
    if os.path.isdir(path):
        root = os.path.abspath(path)
        for n in sorted(os.listdir(path)):
            full = os.path.join(root, n)
            # Two layouts: a top-level *.json task, or a subdir holding task.json
            # (like the bundled bench/tasks/<name>/task.json example). In both
            # cases the task operates in its own directory: file paths and the
            # verify gate's cwd are relative to `dir`.
            if os.path.isfile(full) and n.endswith(".json"):
                t = _load_json_file(full, f"bench task '{n}'")
                t.setdefault("name", n[:-5])
                t["dir"] = root
                tasks.append(t)
            elif os.path.isdir(full) and os.path.exists(os.path.join(full, "task.json")):
                with open(os.path.join(full, "task.json"), "r", encoding="utf-8") as f:
                    t = json.load(f)
                t.setdefault("name", n)
                t["dir"] = os.path.join(root, n)
                tasks.append(t)
    else:
        root = os.path.dirname(os.path.abspath(path))
        data = _load_json_file(path, "bench manifest")
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("tasks"), list):
            items = data["tasks"]
        elif isinstance(data, dict):
            # A single bare task object: the documented one-JSON-file shape
            # (bench/tasks/<name>/task.json). Silently running zero tasks and
            # exiting 0 was the failure mode; treat it as a one-task manifest.
            items = [data]
        else:
            raise HarnessError(
                f"bench manifest must be a task list, a {{'tasks': [...]}} object, "
                f"or a single task object: {path}")
        for t in items:
            t.setdefault("name", "task")
            t["dir"] = root
            tasks.append(t)
    if not tasks:
        raise HarnessError(f"bench manifest contains no tasks: {path}")
    return tasks


class TaskSandbox:
    """Snapshot + restore so the whole manifest is idempotent and re-runnable.

    Security (audit #6): a task file must be a real file *inside* the task
    directory -- a symlink target or a ``../`` escape would let a manifest
    snapshot/overwrite arbitrary files.
    """

    def __init__(self, task):
        # realpath (not abspath): a parent-dir symlink (taskdir/link -> /etc
        # with file=link/passwd) passes a purely lexical containment check
        # while resolving outside the task directory.
        self.dir = os.path.realpath(os.path.abspath(task["dir"]))
        if not task.get("file"):
            # Same clean contract as the other manifest errors -- a schema
            # error must not surface as a raw KeyError traceback.
            raise HarnessError(
                f"bench task '{task.get('name', 'task')}' is missing required key 'file'")
        self.file = os.path.realpath(
            os.path.abspath(os.path.join(self.dir, task["file"])))
        if not (self.file == self.dir
                or self.file.startswith(self.dir.rstrip(os.sep) + os.sep)):
            raise HarnessError(
                f"bench task file {task['file']!r} escapes its task directory")
        if os.path.islink(self.file):
            raise HarnessError(
                f"bench task file {self.file} is a symlink; refusing to snapshot it")
        self.snapshot = self.file + ".orig"

    def restore(self):
        if os.path.islink(self.file):
            raise HarnessError(f"bench task file {self.file} became a symlink")
        if os.path.islink(self.snapshot):
            # A planted .orig link would redirect the snapshot read/write
            # to an arbitrary file on restore.
            raise HarnessError(
                f"bench snapshot {self.snapshot} is a symlink; refusing")
        if not os.path.exists(self.file):
            raise HarnessError(f"bench task file not found: {self.file}")
        if os.path.exists(self.snapshot):
            with open(self.snapshot, "r", encoding="utf-8", newline="") as src:
                _atomic_write(self.file, src.read())
        else:
            with open(self.file, "r", encoding="utf-8", newline="") as src:
                content = src.read()
            with open(self.snapshot, "w", encoding="utf-8", newline="") as out:
                out.write(content)


def _cwd_runner(cwd, timeout=None):
    bound_timeout = timeout or VERIFY_TIMEOUT
    def runner(command, timeout=VERIFY_TIMEOUT):
        return default_run_verify(command, timeout=max(timeout, bound_timeout) if timeout != VERIFY_TIMEOUT else bound_timeout, cwd=cwd)
    return runner


def run_bench(engine, manifest_tasks, runner=None):
    """Run every task in a manifest through the engine's apply pipeline.

    `runner` overrides the verify runner (used by tests to avoid real
    subprocesses); defaults to running the verify command in each task's root.
    The runner is passed per-task via ``task_runner`` -- the engine's own
    ``run_verify`` is never mutated (audit #17), so concurrent or interleaved
    use of the engine stays safe. Each task gets its own verify-timeout
    (``task['verify_timeout']``, default 300s).
    """
    results = []
    sandboxes = []
    try:
        for task in manifest_tasks:
            # Same clean contract as the loader and the sandbox: schema
            # errors abort the run as HarnessError, never as KeyError.
            name = task.get("name") or "task"
            sandbox = TaskSandbox(task)
            sandbox.restore()
            sandboxes.append(sandbox)
            task_runner = runner or _cwd_runner(sandbox.dir, task.get("verify_timeout"))
            if not task.get("instruction"):
                raise HarnessError(
                    f"bench task '{name}' is missing required key 'instruction'")
            eprint(f"[bench] running '{name}' ...")
            try:
                r = engine.apply_edit(
                    task_id=f"bench/{name}",
                    file_path=sandbox.file,
                    instruction=task["instruction"],
                    verify_cmd=task.get("verify"),
                    max_rounds=task.get("max_rounds", 3),
                    require_consent=task.get("require_consent", False),
                    max_tokens=task.get("max_tokens", 4096),
                    task_max_cost=task.get("task_max_cost", 0.05),
                    renew_consent=task.get("renew_consent", False),
                    max_rotations=task.get("max_rotations", 3),
                    task_runner=task_runner,
                )
            except HarnessError as e:
                r = {"status": "error", "error": str(e)}
            results.append({"name": name, **r})
            eprint(f"[bench] '{name}' -> {r.get('status')}")
    finally:
        # Idempotency includes the tree we leave behind: restore every
        # fixture even on crash, or a successful run leaves solved tasks
        # in the tracked sources (committing that would defeat the bench).
        for sandbox in sandboxes:
            try:
                sandbox.restore()
            except HarnessError as e:
                eprint(f"[bench] fixture restore failed: {e}")
    return summarize(results, engine.ledger)


def summarize(results, ledger):
    """Aggregate bench results + pull confidence calibration from the ledger."""
    statuses = {}
    total_cost = 0.0
    rounds_used = 0
    rotations_used = 0
    for r in results:
        s = r.get("status")
        statuses[s] = statuses.get(s, 0) + 1
        total_cost += float(r.get("cost") or 0.0)
        rounds_used += len(r.get("rounds") or [])
        rotations_used += int(r.get("rotations") or 0)

    report = ledger.participation_report()
    n = len(results)
    passed = statuses.get("ok", 0)
    report["bench"] = {
        "tasks": n,
        "statuses": statuses,
        "pass_rate": round(passed / n, 4) if n else None,
        "total_cost": round(total_cost, 6),
        "mean_cost": round(total_cost / n, 6) if n else None,
        "total_rounds": rounds_used,
        "total_rotations": rotations_used,
        "results": results,
    }
    return report
