#!/usr/bin/env python3
"""Single-flight, fail-closed local dogfood worker.

Task JSON: {task_id, repo, repo_sha, file, instruction, verify_argv}.
Only an operator may enqueue a task. The model supplies one unified diff;
the controller owns every path, command, budget and completion decision.
"""
import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse

MODEL = "sparkx25:4b"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MAX_FILE = 12000
MAX_DIFF = 12000
CHILD_TIMEOUT = 900
VERIFY_TIMEOUT = 180
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
 phase TEXT NOT NULL DEFAULT 'queued', lease_until INTEGER,
 receipt TEXT, error_code TEXT, created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
 task_id TEXT PRIMARY KEY, receipt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 created_at INTEGER NOT NULL
);
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args, timeout=30):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=timeout, check=False)
    if p.returncode:
        raise RuntimeError("git_" + args[0].replace("-", "_"))
    return p.stdout.strip()


def validate_manifest(raw):
    if not isinstance(raw, dict) or set(raw) != {
            "task_id", "repo", "repo_sha", "file", "instruction", "verify_argv"}:
        raise ValueError("manifest_fields")
    task_id = raw["task_id"]
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", task_id):
        raise ValueError("task_id")
    repo = Path(raw["repo"])
    if not repo.is_absolute() or not repo.is_dir() or repo.is_symlink():
        raise ValueError("repo")
    repo = repo.resolve(strict=True)
    relative = raw["file"]
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("file")
    rel = Path(relative)
    if rel.is_absolute() or any(x in ("", ".", "..") for x in relative.split("/")):
        raise ValueError("file_scope")
    target = repo / rel
    if not target.is_file() or target.is_symlink() or not target.resolve().is_relative_to(repo):
        raise ValueError("file_scope")
    sha = raw["repo_sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("repo_sha")
    instruction = raw["instruction"]
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 1000:
        raise ValueError("instruction")
    argv = raw["verify_argv"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 16
            or any(not isinstance(a, str) or not a or len(a) > 256 or "\x00" in a for a in argv)):
        raise ValueError("verify_argv")
    if any(re.search(r"[;&|`$<>\n\r]", a) for a in argv):
        raise ValueError("verify_shell_syntax")
    normalized = dict(raw, repo=str(repo))
    return normalized


def clean_baseline(task):
    repo = Path(task["repo"])
    if git(repo, "rev-parse", "HEAD") != task["repo_sha"]:
        raise RuntimeError("sha_mismatch")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("dirty_repo")
    tracked = git(repo, "ls-files", "--error-unmatch", "--", task["file"])
    if tracked != task["file"]:
        raise RuntimeError("untracked_target")


def validate_diff(patch, target, original):
    if not isinstance(patch, str) or not patch or len(patch) > MAX_DIFF or "\x00" in patch:
        raise ValueError("diff_size")
    lines = patch.splitlines()
    headers = [x for x in lines if x.startswith(("--- ", "+++ "))]
    if headers != ["--- a/" + target, "+++ b/" + target]:
        raise ValueError("diff_scope")
    if lines[:2] != headers or any(not x.startswith(("@@ ", " ", "-", "+", "\\"))
                                      for x in lines[2:]):
        raise ValueError("diff_extras")
    if sum(x.startswith("@@ ") for x in lines) < 1:
        raise ValueError("diff_hunks")
    if any(x.startswith(("diff --git ", "rename ", "copy ", "new file ",
                          "deleted file ", "GIT binary patch", "Binary files")) for x in lines):
        raise ValueError("diff_extras")
    from harness.prompts import _apply_unified_diff
    candidate = _apply_unified_diff(original, patch)
    if candidate is None or candidate == original:
        raise ValueError("diff_noop_or_mismatch")
    return candidate


class GeneratedDiff(str):
    """A candidate with provider-reported usage, never estimated token counts."""
    def __new__(cls, content, usage):
        value = super().__new__(cls, content)
        value.usage = usage
        return value


def generate_diff(task, original):
    prompt = ("Return only a unified diff for this approved task. Edit exactly one file: "
              + task["file"] + ". Use --- a/" + task["file"] + " and +++ b/"
              + task["file"] + ". No other files or prose.\nInstruction: "
              + task["instruction"] + "\nCurrent file:\n" + original)
    body = json.dumps({"model": MODEL, "stream": False,
                       "options": {"num_predict": 2000, "temperature": 0},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as response:
        data = json.loads(response.read(MAX_DIFF * 3))
    usage = {"cost": 0}
    for source, target in (("prompt_eval_count", "prompt_tokens"), ("eval_count", "completion_tokens")):
        count = data.get(source)
        if type(count) is int and count >= 0:
            usage[target] = count
    return GeneratedDiff(data["message"]["content"].strip(), usage)


class OneCandidateTransport:
    """Harness model seam: one local SparkX result; Jev uses its native wire."""
    def __init__(self, patch):
        self.patch = patch
        self.usage = getattr(patch, "usage", {"cost": 0})
        self.used = False

    def get(self, url, api_key, timeout=15):
        if not url.endswith("/models"):
            raise RuntimeError("external_model_lookup_refused")
        return {"data": [{"id": MODEL, "pricing": {"prompt": "0", "completion": "0"}}]}

    def post(self, url, api_key, payload, timeout=120):
        endpoint = urllib.parse.urlsplit(url)
        if endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.port not in (None, 443):
            raise RuntimeError("unexpected_provider_refused")
        if endpoint.hostname == "openrouter.ai":
            if self.used or payload.get("model") not in (MODEL, MODEL + ":floor"):
                raise RuntimeError("extra_model_call_refused")
            self.used = True
            return 200, {"choices": [{"message": {"content": self.patch}}],
                         "usage": self.usage}
        from harness._http import HttpTransport
        if endpoint.hostname != "api.typesafe.ai" or endpoint.path != "/v1/systemone":
            raise RuntimeError("unexpected_provider_refused")
        return HttpTransport().post(url, api_key, payload, timeout=timeout)


class NativeOnlyPolicy:
    def __init__(self, delegate):
        self.delegate = delegate
        self.settings = delegate.settings
        self.keyed = delegate.keyed
        self.last = None

    def evaluate_candidate(self, *args, **kwargs):
        result, structural = self.delegate.evaluate_candidate(*args, **kwargs)
        self.last = structural
        if structural.get("is_fallback") or not self.keyed:
            return dataclasses.replace(result, verdict="fail"), structural
        return result, structural


def governed_apply(task, patch):
    from harness.apply import ApplyEngine
    from harness.config import load_settings
    from harness.jev_policy import policy_for
    from harness.session import ledger_for, router_for
    from harness.spend import SpendGovernor
    settings = load_settings()
    if not getattr(settings, "jev_api_key", None):
        raise RuntimeError("native_jev_unavailable")
    wire = OneCandidateTransport(patch)
    governor = SpendGovernor(wire, "local-only", max_cost=0.10)
    ledger = ledger_for(settings, caller="oc-dogfood-worker")
    policy = NativeOnlyPolicy(policy_for(settings, transport=wire,
                                        governor=governor, ledger=ledger))
    router = router_for(settings, jev_policy=policy)
    router.apply_model = MODEL
    router.apply_pool = [MODEL]
    router.escalation_pool = []
    def verify(_command, timeout=VERIFY_TIMEOUT, cwd=None):
        p = subprocess.run(task["verify_argv"], cwd=task["repo"],
                           capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
        return p.returncode, (p.stdout + p.stderr)[-4000:]
    engine = ApplyEngine(wire, "local-only", governor, ledger, router,
                         default_require_consent=False, default_renew_consent=False,
                         default_max_rotations=0, default_task_max_cost=0.10,
                         allowed_roots=[task["repo"]], jev_policy=policy,
                         run_verify=verify)
    result = engine.apply_edit(task_id=task["task_id"],
                               file_path=str(Path(task["repo"]) / task["file"]),
                               instruction=task["instruction"], backend="diff",
                               verify_cmd=shlex.join(task["verify_argv"]),
                               model=MODEL, max_rounds=1, max_rotations=0,
                               allow_escalation=False, require_consent=False,
                               renew_consent=False, max_tokens=2200,
                               task_max_cost=0.10)
    if not wire.used or result.get("status") != "ok" or not result.get("changed"):
        raise RuntimeError("harness_apply_refused")
    if result.get("verify", {}).get("passed") is not True:
        raise RuntimeError("harness_verify_missing")
    if not policy.last or policy.last.get("is_fallback") or policy.last.get("verdict") != "pass":
        raise RuntimeError("native_jev_refused")
    return policy.last


@contextlib.contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()


def execute(db_path, task_id):
    with connect(db_path) as db:
        row = db.execute("SELECT manifest, state, attempt FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row or row[1] != "leased":
            raise RuntimeError("lease_missing")
        task = json.loads(row[0])
        clean_baseline(task)
        target = Path(task["repo"]) / task["file"]
        original = target.read_text(encoding="utf-8")
        if len(original) > MAX_FILE:
            raise RuntimeError("file_too_large")
        patch = generate_diff(task, original)
        candidate = validate_diff(patch, task["file"], original)
        db.execute("UPDATE tasks SET phase='editing', updated_at=? WHERE task_id=?", (int(time.time()), task_id))
        db.commit()
        native = governed_apply(task, patch)
        if target.read_text(encoding="utf-8") != candidate:
            raise RuntimeError("candidate_mismatch")
        changed = git(task["repo"], "diff", "--name-only")
        if changed != task["file"] or git(task["repo"], "ls-files", "--others", "--exclude-standard"):
            raise RuntimeError("post_apply_scope")
        git(task["repo"], "add", "--", task["file"])
        git(task["repo"], "commit", "-m", "OC dogfood " + task_id, timeout=60)
        commit = git(task["repo"], "rev-parse", "HEAD")
        if git(task["repo"], "diff-tree", "--no-commit-id", "--name-only", "-r", commit) != task["file"]:
            raise RuntimeError("commit_scope")
        receipt = {"task_id": task_id, "state": "verified", "repo_sha": task["repo_sha"],
                   "commit": commit, "file": task["file"], "patch_sha256": digest(patch.encode()),
                   "candidate_sha256": digest(candidate.encode()), "verify_passed": True,
                   "native_jev": {k: native.get(k) for k in ("verdict", "is_fallback", "model", "supported", "input_tokens")}}
        return receipt


@contextlib.contextmanager
def single_flight(db_path):
    lock_path = str(db_path) + ".lock"
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("worker_already_running")
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("worker_already_running")
            yield


def enqueue(db_path, manifest_path):
    task = validate_manifest(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    clean_baseline(task)
    body = json.dumps(task, sort_keys=True, separators=(",", ":"))
    now = int(time.time())
    with connect(db_path) as db:
        try:
            db.execute("INSERT INTO tasks(task_id, manifest, manifest_hash, state, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                       (task["task_id"], body, digest(body.encode()), "queued", now, now))
        except sqlite3.IntegrityError:
            existing = db.execute("SELECT manifest_hash FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
            if existing[0] != digest(body.encode()):
                raise RuntimeError("duplicate_id_conflict")
    return {"task_id": task["task_id"], "state": "queued_or_existing"}


def run_once(db_path):
    with single_flight(db_path):
        with connect(db_path) as db:
            now = int(time.time())
            db.execute("UPDATE tasks SET state='uncertain', error_code='expired_lease' WHERE state='leased' AND lease_until<?", (now,))
            row = db.execute("SELECT task_id, attempt FROM tasks WHERE state='queued' ORDER BY created_at, task_id LIMIT 1").fetchone()
            if not row:
                return {"state": "idle"}
            task_id, attempt = row
            if attempt >= 2:
                db.execute("UPDATE tasks SET state='failed', error_code='attempt_limit' WHERE task_id=?", (task_id,))
                return {"task_id": task_id, "state": "failed", "error_code": "attempt_limit"}
            db.execute("UPDATE tasks SET state='leased', phase='generating', attempt=attempt+1, lease_until=?, updated_at=? WHERE task_id=?",
                       (now + CHILD_TIMEOUT + 30, now, task_id))
        try:
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_execute",
                                "--db", str(db_path), "--task-id", task_id],
                               capture_output=True, text=True, timeout=CHILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            p = None
        with connect(db_path) as db:
            phase = db.execute("SELECT phase FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            if p is None or p.returncode:
                # Once editing begins, even a crashed child may have written bytes.
                state = ("uncertain" if phase == "editing" else
                         "queued" if attempt + 1 < 2 else "failed")
                code = "child_timeout" if p is None else (p.stdout.strip()[-80:] or "child_failed")
                db.execute("UPDATE tasks SET state=?, error_code=?, lease_until=NULL, updated_at=? WHERE task_id=?",
                           (state, code, int(time.time()), task_id))
                return {"task_id": task_id, "state": state, "error_code": code}
            receipt = json.loads(p.stdout)
            body = json.dumps(receipt, sort_keys=True)
            db.execute("UPDATE tasks SET state='verified', phase='complete', receipt=?, lease_until=NULL, updated_at=? WHERE task_id=?",
                       (body, int(time.time()), task_id))
            db.execute("INSERT OR IGNORE INTO outbox(task_id, receipt, created_at) VALUES(?,?,?)",
                       (task_id, body, int(time.time())))
            return {"task_id": task_id, "state": "verified", "delivery": "pending", "commit": receipt["commit"]}


def status(db_path):
    with connect(db_path) as db:
        rows = db.execute("SELECT task_id, state, attempt, phase, error_code FROM tasks ORDER BY created_at, task_id").fetchall()
        return [{"task_id": a, "state": b, "attempt": c, "phase": d, "error_code": e}
                for a, b, c, d, e in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["enqueue", "run-once", "status", "_execute"])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--task-id")
    args = parser.parse_args()
    try:
        if args.command == "enqueue":
            if not args.manifest:
                raise ValueError("manifest_required")
            output = enqueue(args.db, args.manifest)
        elif args.command == "run-once":
            output = run_once(args.db)
        elif args.command == "status":
            output = status(args.db)
        else:
            if not args.task_id:
                raise ValueError("task_id_required")
            output = execute(args.db, args.task_id)
        print(json.dumps(output, sort_keys=True))
    except Exception as exc:
        # Intentionally never print prompts, responses, credentials or message bodies.
        code = str(exc) if type(exc) in (ValueError, RuntimeError) else type(exc).__name__
        if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
            code = type(exc).__name__
        print(code)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
