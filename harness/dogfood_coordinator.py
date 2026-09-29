"""Local SCMessenger <-> Harness dogfood coordinator.

Default execution is read-only. ``apply=True`` is the sole mutation gate: it
refuses dirty source worktrees, fetches candidate revisions into detached local
worktrees, validates and audits both repositories, atomically promotes a known-good
state, and restores the previous state if any candidate step fails.

This module never deploys to a live service. Its deployment target is a local
Git worktree plus a known-good pointer under ``state_dir``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import HarnessError
from .filesafety import _atomic_write


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 300
LOCK_TIMEOUT = 10.0
HARNESS_AUDIT_FILES = ("pyproject.toml", "harness/cli.py")
SCM_AUDIT_FILES = ("Cargo.toml", "scripts/verify_versions.sh")


@dataclass(frozen=True)
class CoordinatorConfig:
    harness_repo: str
    scmessenger_repo: str
    state_dir: str
    apply: bool = False
    remote: str = "origin"
    branch: str = "main"
    timeout: int = DEFAULT_TIMEOUT
    harness_verify: tuple = ()
    scmessenger_verify: tuple = ()
    harness_audit_files: tuple = HARNESS_AUDIT_FILES
    scmessenger_audit_files: tuple = SCM_AUDIT_FILES


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_text(path, value):
    try:
        _atomic_write(str(path), value)
    except OSError as exc:
        raise HarnessError(f"cannot atomically write {path}: {exc}") from None


def _write_json(path, value):
    _write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _run(argv, *, cwd=None, env=None, timeout=DEFAULT_TIMEOUT):
    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout, shell=False, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": list(argv), "exit_code": 124,
            "stdout": exc.stdout or "", "stderr": f"timed out after {timeout}s",
        }
    except OSError as exc:
        return {"command": list(argv), "exit_code": 127, "stdout": "", "stderr": str(exc)}
    return {
        "command": list(argv), "exit_code": proc.returncode,
        "stdout": proc.stdout or "", "stderr": proc.stderr or "",
    }


def _git(repo, *args, check=True, timeout=60):
    result = _run(["git", "-C", str(repo), *args], timeout=timeout)
    if check and result["exit_code"] != 0:
        detail = (result["stderr"] or result["stdout"]).strip()
        raise HarnessError(
            f"git {' '.join(args[:2])} failed in {repo}: {detail or 'unknown error'}")
    return result


def _git_text(repo, *args, check=True, timeout=60):
    return _git(repo, *args, check=check, timeout=timeout)["stdout"].strip()


def _config_error(config):
    for name in ("harness_repo", "scmessenger_repo", "state_dir"):
        value = getattr(config, name)
        if not isinstance(value, str) or not value.strip():
            return f"{name} must be a nonblank path"
    for name in ("remote", "branch"):
        value = getattr(config, name)
        if not isinstance(value, str) or not value.strip():
            return f"{name} must be nonblank"
    return None


def _repo_root(path):
    root = _git_text(path, "rev-parse", "--show-toplevel")
    if not root:
        raise HarnessError(f"not a Git worktree: {path}")
    return os.path.realpath(root)


def _manifest_version(text, manifest):
    if manifest.endswith("pyproject.toml"):
        match = re.search(
            r'(?ms)^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"\s*$', text)
        return match.group(1) if match else None
    if manifest.endswith("Cargo.toml"):
        match = re.search(
            r'(?ms)^\[workspace\.package\]\s*$.*?^version\s*=\s*"([^"]+)"\s*$',
            text,
        )
        return match.group(1) if match else None
    return None


def _version_at(repo, revision, manifest):
    result = _git(repo, "show", f"{revision}:{manifest}", check=False)
    if result["exit_code"] != 0:
        return None
    return _manifest_version(result["stdout"], manifest)


def _relation(repo, current, available):
    if current == available:
        return "current"
    present = _git(repo, "cat-file", "-e", f"{available}^{{commit}}", check=False)
    if present["exit_code"] != 0:
        return "unknown"
    if _git(repo, "merge-base", "--is-ancestor", current, available,
             check=False)["exit_code"] == 0:
        return "stale"
    if _git(repo, "merge-base", "--is-ancestor", available, current,
             check=False)["exit_code"] == 0:
        return "ahead"
    return "diverged"


def _remote_revision(repo, remote, branch):
    ref = f"refs/heads/{branch}"
    result = _git(repo, "ls-remote", "--exit-code", remote, ref, check=False)
    if result["exit_code"] != 0:
        detail = (result["stderr"] or result["stdout"]).strip()
        return None, f"remote revision unavailable for {remote}/{branch}: {detail}"
    line = result["stdout"].splitlines()[0].split()
    if len(line) < 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", line[0]):
        return None, f"remote returned an invalid revision for {remote}/{branch}"
    return line[0].lower(), None


def _inspect_repository(name, path, config):
    root = _repo_root(path)
    revision = _git_text(root, "rev-parse", "HEAD").lower()
    branch = _git_text(root, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None
    status = _git_text(
        root, "status", "--porcelain=v1", "--untracked-files=all")
    dirty_entries = [line for line in status.splitlines() if line]
    manifest = "pyproject.toml" if name == "harness" else "Cargo.toml"
    try:
        version_text = (Path(root) / manifest).read_text(encoding="utf-8")
        working_tree_version = _manifest_version(version_text, manifest)
    except (OSError, UnicodeError):
        working_tree_version = None
    available, remote_error = _remote_revision(root, config.remote, config.branch)
    relation = (_relation(root, revision, available)
                if available and not remote_error else "unknown")
    return {
        "repository": name,
        "path": root,
        "branch": branch,
        "revision": revision,
        "describe": _git_text(
            root, "describe", "--tags", "--always", "--dirty", check=False) or revision[:12],
        "version": _version_at(root, revision, manifest),
        "working_tree_version": working_tree_version,
        "dirty": bool(dirty_entries),
        "dirty_entries": dirty_entries,
        "remote": config.remote,
        "remote_branch": config.branch,
        "available_revision": available,
        "available_version": (
            _version_at(root, available, manifest) if available else None),
        "relation": relation,
        "candidate_changed": bool(available and available != revision),
        "newer_available": relation == "stale",
        "remote_error": remote_error,
    }


def _state_path(config):
    return Path(os.path.abspath(config.state_dir)) / "known-good.json"


def _empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "known_good": {"harness": None, "scmessenger": None},
    }


def _read_state(config):
    path = _state_path(config)
    if not path.exists():
        return _empty_state()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read known-good state {path}: {exc}") from None
    if (not isinstance(body, dict)
            or body.get("schema_version") != SCHEMA_VERSION
            or not isinstance(body.get("known_good"), dict)):
        raise HarnessError(f"unsupported known-good state: {path}")
    return body


def _validate_known_good(state):
    records = state["known_good"]
    if set(records) != {"harness", "scmessenger"}:
        raise HarnessError(
            "known-good state must contain harness and scmessenger records")
    for name in ("harness", "scmessenger"):
        record = records[name]
        if record is None:
            continue
        if (not isinstance(record, dict)
                or not {"revision", "version", "worktree", "recorded_at"} <= set(record)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", str(record["revision"]))
                or not isinstance(record["worktree"], str)
                or not record["worktree"].strip()
                or not isinstance(record["recorded_at"], str)
                or not record["recorded_at"].strip()):
            raise HarnessError(f"known-good {name} record has an invalid shape")
        worktree = record["worktree"]
        try:
            root = _repo_root(worktree)
        except HarnessError:
            raise HarnessError(
                f"known-good {name} worktree is missing or is not a Git worktree: "
                f"{worktree}") from None
        if root != os.path.realpath(worktree):
            raise HarnessError(
                f"known-good {name} path is not a Git worktree root: {worktree}")
        status = _git(root, "status", "--porcelain=v1", "--untracked-files=all",
                      check=False)
        if status["exit_code"] != 0:
            raise HarnessError(f"known-good {name} worktree cannot be inspected")
        if status["stdout"].strip():
            raise HarnessError(f"known-good {name} worktree is dirty: {worktree}")
        head = _git_text(root, "rev-parse", "HEAD").lower()
        if head != record["revision"].lower():
            raise HarnessError(
                f"known-good {name} HEAD {head} does not match recorded revision "
                f"{record['revision']}")


def _restore_state(config, previous):
    path = _state_path(config)
    if previous.get("updated_at") is None and all(
            value is None for value in previous.get("known_good", {}).values()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _write_json(path, previous)


@contextmanager
def _coordinator_lock(state_dir):
    """Hold a process-scoped cross-process lock until the caller finishes."""
    root = Path(os.path.abspath(state_dir))
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "coordinator.lock"
    try:
        lock_file = open(lock_path, "a+b")
    except OSError as exc:
        raise HarnessError(f"cannot open coordinator lock {lock_path}: {exc}") from None
    locked = False
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                lock_file.seek(0)
                try:
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except ImportError:
                    import fcntl
                    fcntl.flock(
                        lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise HarnessError(
                        f"coordinator lock is busy: {lock_path}") from None
                time.sleep(0.05)
        yield str(lock_path)
    finally:
        if locked:
            try:
                lock_file.seek(0)
                try:
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except ImportError:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_file.close()


def _validation_commands(name, config):
    override = (config.harness_verify if name == "harness"
                else config.scmessenger_verify)
    if override:
        return (tuple(override),)
    if name == "harness":
        return (
            (sys.executable, "-m", "compileall", "-q", "harness", "tests"),
            (sys.executable, "-m", "unittest", "discover", "-s", "tests"),
        )
    return (("bash", "scripts/verify_versions.sh"),)


def _run_logged(name, command, cwd, artifact_dir, timeout, index):
    # Validation must not make the candidate dirty merely by importing or
    # compiling it. Keep Python's generated bytecode in this run's artifacts.
    with tempfile.TemporaryDirectory(prefix="harness-dogfood-") as pycache_dir:
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = pycache_dir
        result = _run(command, cwd=cwd, env=env, timeout=timeout)
    stem = f"{name}-validation-{index + 1}"
    stdout_path = artifact_dir / f"{stem}.stdout.log"
    stderr_path = artifact_dir / f"{stem}.stderr.log"
    _write_text(stdout_path, result["stdout"])
    _write_text(stderr_path, result["stderr"])
    return {
        "repository": name,
        "status": "passed" if result["exit_code"] == 0 else "failed",
        "exit_code": result["exit_code"],
        "command": result["command"],
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def _run_harness_audit(name, root, files, artifact_dir, timeout):
    report_path = artifact_dir / f"{name}-audit.json"
    stdout_path = artifact_dir / f"{name}-audit.stdout.log"
    stderr_path = artifact_dir / f"{name}-audit.stderr.log"
    argv = [sys.executable, "-m", "harness.cli", "brief",
            f"Audit {name} candidate for SCMessenger-Harness dogfooding"]
    for relative in files:
        argv.extend(("--file", str(Path(root) / relative)))
    argv.extend(("--validate", "--quiet", "--out", str(report_path)))
    source_root = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_root, env.get("PYTHONPATH")) if value)
    result = _run(argv, cwd=str(artifact_dir), env=env, timeout=timeout)
    _write_text(stdout_path, result["stdout"])
    _write_text(stderr_path, result["stderr"])
    summary = {
        "repository": name,
        "status": "failed",
        "exit_code": result["exit_code"],
        "command": argv,
        "report_path": str(report_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "grounding_issues": [],
        "source_count": 0,
    }
    if result["exit_code"] == 0 and report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            summary["grounding_issues"] = report.get("grounding_issues", [])
            summary["source_count"] = len(
                report.get("brief", {}).get("grounding", {}).get("sources", []))
            if report.get("status") == "ok" and report.get("ok") is True:
                summary["status"] = "passed"
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            summary["status"] = "failed"
            summary["report_error"] = str(exc)
    return summary


def _remove_candidate_worktree(repo, path):
    if not path:
        return
    _git(repo, "worktree", "remove", "--force", str(path), check=False)


def _candidate_ref(run_id, name):
    return f"refs/harness-dogfood/{run_id}/{name}"


def _candidate_record(revision, version, worktree):
    return {
        "revision": revision,
        "version": version,
        "worktree": str(worktree),
        "recorded_at": _now(),
    }


def _receipt(name, source, targets, validations, audits, deployment, receipt_path):
    repo = "scmessenger" if name == "harness" else "harness"
    target = targets[name]
    own_audit = audits.get(name, {"repository": name, "status": "not_run"})
    other_audit = audits.get(repo, {"repository": repo, "status": "not_run"})
    other_receipt = str(receipt_path.parent / f"{repo}.json")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "scmessenger_harness_handoff",
        "created_at": _now(),
        "repository": name,
        "counterpart_repository": repo,
        "source_revision": source[name]["revision"],
        "source_version": source[name]["version"],
        "target_revision": target["revision"],
        "target_version": target["version"],
        "validation": {
            "status": ("passed" if validations and all(
                item["status"] == "passed" for item in validations) else "failed"),
            "results": validations,
        },
        "audit": own_audit,
        "counterpart_audit": {**other_audit, "receipt_path": other_receipt},
        "deployment": deployment,
    }


def _apply(config, initial, run_id):
    state_dir = Path(os.path.abspath(config.state_dir))
    artifact_dir = state_dir / "runs" / run_id
    receipt_dir = state_dir / "receipts" / run_id
    receipt_paths = {
        name: receipt_dir / f"{name}.json" for name in ("harness", "scmessenger")
    }
    worktree_root = state_dir / "worktrees" / run_id
    targets = {}
    validations = []
    audits = {}
    worktrees = {"harness": None, "scmessenger": None}
    private_refs = {}
    previous = _empty_state()
    previous_verified = False
    current = initial
    promoted = False
    failure = None
    deployment = {
        "kind": "local-git-worktree",
        "live_deployment": False,
        "changed": False,
        "known_good_before": {},
        "known_good_after": {},
        "rollback": {"status": "not_needed", "restored_state": False},
    }
    try:
        with _coordinator_lock(config.state_dir):
            previous = _read_state(config)
            _validate_known_good(previous)
            previous_verified = True
            deployment["known_good_before"] = previous.get("known_good", {})
            artifact_dir.mkdir(parents=True, exist_ok=True)
            receipt_dir.mkdir(parents=True, exist_ok=True)
            worktree_root.mkdir(parents=True, exist_ok=True)
            current = {
                name: _inspect_repository(name, path, config)
                for name, path in (
                    ("harness", config.harness_repo),
                    ("scmessenger", config.scmessenger_repo),
                )
            }
            dirty = [name for name, item in current.items() if item["dirty"]]
            if dirty:
                raise HarnessError(
                    "apply refused; source worktrees are dirty: " + ", ".join(dirty))
            for name, item in current.items():
                relation = item["relation"]
                if relation not in ("current", "stale", "unknown"):
                    raise HarnessError(
                        f"apply refused; {name} remote relation is {relation}")
                private_ref = _candidate_ref(run_id, name)
                private_refs[name] = private_ref
                try:
                    _git(
                        item["path"], "fetch", "--no-tags", "--no-write-fetch-head",
                        config.remote,
                        f"refs/heads/{config.branch}:{private_ref}",
                    )
                    fetched = _git_text(
                        item["path"], "rev-parse", "--verify",
                        f"{private_ref}^{{commit}}").lower()
                finally:
                    _git(item["path"], "update-ref", "-d", private_ref, check=False)
                if not re.fullmatch(r"[0-9a-fA-F]{40}", fetched):
                    raise HarnessError(f"{name} fetch returned no candidate commit")
                relation = _relation(item["path"], item["revision"], fetched)
                if relation not in ("current", "stale"):
                    raise HarnessError(
                        f"apply refused; fetched {name} candidate is {relation}")
                manifest = "pyproject.toml" if name == "harness" else "Cargo.toml"
                targets[name] = {
                    "revision": fetched,
                    "version": _version_at(item["path"], fetched, manifest),
                    "available_revision": item["available_revision"],
                }
                candidate_path = worktree_root / name
                _git(item["path"], "worktree", "add", "--detach",
                     str(candidate_path), fetched)
                worktrees[name] = str(candidate_path)
                if _git_text(candidate_path, "rev-parse", "HEAD").lower() != fetched:
                    raise HarnessError(
                        f"{name} worktree revision does not match candidate")
                required = (config.harness_audit_files if name == "harness"
                            else config.scmessenger_audit_files)
                missing = [relative for relative in required
                           if not (Path(candidate_path) / relative).is_file()]
                if missing:
                    raise HarnessError(
                        f"{name} candidate missing audit files: {', '.join(missing)}")
                for index, command in enumerate(_validation_commands(name, config)):
                    validations.append(_run_logged(
                        name, command, candidate_path, artifact_dir, config.timeout,
                        index=index,
                    ))
            if any(item["status"] != "passed" for item in validations):
                raise HarnessError("candidate validation failed")
            for name in ("harness", "scmessenger"):
                root = Path(worktrees[name])
                files = (config.harness_audit_files if name == "harness"
                         else config.scmessenger_audit_files)
                audits[name] = _run_harness_audit(
                    name, root, files, artifact_dir, config.timeout)
            if any(item["status"] != "passed" for item in audits.values()):
                raise HarnessError("Harness candidate audit failed")
            dirty_candidates = [
                name for name, path in worktrees.items()
                if _git_text(path, "status", "--porcelain=v1",
                             "--untracked-files=all")
            ]
            if dirty_candidates:
                raise HarnessError(
                    "candidate worktrees became dirty: " + ", ".join(dirty_candidates))
            known_good = {
                name: _candidate_record(
                    targets[name]["revision"], targets[name]["version"],
                    worktrees[name])
                for name in ("harness", "scmessenger")
            }
            new_state = {
                "schema_version": SCHEMA_VERSION,
                "updated_at": _now(),
                "known_good": known_good,
            }
            _write_json(_state_path(config), new_state)
            promoted = True
            deployment["changed"] = True
            deployment["known_good_after"] = known_good
            receipts = {
                name: _receipt(
                    name, current, targets, validations, audits, deployment,
                    receipt_paths[name])
                for name in known_good
            }
            for name, receipt in receipts.items():
                _write_json(receipt_paths[name], receipt)
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "ok",
                "mode": "apply",
                "run_id": run_id,
                "repositories": current,
                "targets": targets,
                "validations": validations,
                "audits": audits,
                "deployment": deployment,
                "receipts": {name: str(path) for name, path in receipt_paths.items()},
            }
    except Exception as exc:
        failure = str(exc)
        if promoted:
            try:
                _restore_state(config, previous)
                deployment["changed"] = False
                deployment["known_good_after"] = previous.get("known_good", {})
                deployment["rollback"] = {
                    "status": "restored",
                    "restored_state": True,
                    "known_good": previous.get("known_good", {}),
                }
            except Exception as rollback_exc:
                deployment["rollback"] = {
                    "status": "failed",
                    "restored_state": False,
                    "error": str(rollback_exc),
                }
        elif previous_verified and previous.get("updated_at") is not None:
            deployment["rollback"] = {
                "status": "preserved",
                "restored_state": True,
                "known_good": previous.get("known_good", {}),
            }
        for name, path in worktrees.items():
            if path and name in initial:
                _remove_candidate_worktree(initial[name]["path"], path)
        for name, private_ref in private_refs.items():
            if name in initial:
                _git(initial[name]["path"], "update-ref", "-d", private_ref,
                     check=False)
        if receipt_dir.exists():
            fallback_targets = {
                name: targets.get(name, {
                    "revision": initial[name]["available_revision"]
                    or initial[name]["revision"],
                    "version": initial[name].get("available_version"),
                    "available_revision": initial[name].get("available_revision"),
                })
                for name in initial
            }
            fallback_validations = list(validations) + [{
                "repository": "coordinator", "status": "failed",
                "exit_code": 1, "command": [], "error": failure,
            }]
            try:
                for name in ("harness", "scmessenger"):
                    path = receipt_dir / f"{name}.json"
                    _write_json(path, _receipt(
                        name, current, fallback_targets, fallback_validations,
                        audits, deployment, path))
            except Exception as receipt_exc:
                failure = f"{failure}; failure receipt write failed: {receipt_exc}"
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "mode": "apply",
            "run_id": run_id,
            "error": failure,
            "repositories": current,
            "targets": targets,
            "validations": validations,
            "audits": audits,
            "deployment": deployment,
            "receipts": {
                name: str(path) for name, path in receipt_paths.items()
                if path.is_file()
            },
        }


def coordinate(config):
    """Inspect both repositories and, only in apply mode, rotate candidates."""
    run_id = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "-" + uuid.uuid4().hex[:8])
    config_error = _config_error(config)
    if config_error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "mode": "apply" if config.apply else "dry-run",
            "run_id": run_id,
            "error": f"invalid coordinator configuration: {config_error}",
            "repositories": {},
            "blockers": [config_error],
            "receipts": {},
        }
    paths = {"harness": config.harness_repo, "scmessenger": config.scmessenger_repo}
    repositories = {
        name: _inspect_repository(name, path, config)
        for name, path in paths.items()
    }
    if not config.apply:
        state_dir = Path(os.path.abspath(config.state_dir))
        planned = {
            name: (item["available_revision"]
                   if item["candidate_changed"] else item["revision"])
            for name, item in repositories.items()
        }
        blockers = []
        for name, item in repositories.items():
            if item["remote_error"]:
                blockers.append(f"{name}: {item['remote_error']}")
            if item["dirty"]:
                blockers.append(
                    f"{name}: source worktree is dirty; apply would refuse")
            if item["relation"] in ("diverged", "ahead"):
                blockers.append(
                    f"{name}: source is {item['relation']} from {config.remote}/{config.branch}")
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "failed" if any(item["remote_error"] for item in repositories.values()) else "ok",
            "mode": "dry-run",
            "run_id": run_id,
            "repositories": repositories,
            "planned_targets": planned,
            "apply_eligible": not blockers,
            "blockers": blockers,
            "receipts_written": False,
            "planned_receipts": {
                name: str(state_dir / "receipts" / run_id / f"{name}.json")
                for name in repositories
            },
        }
    blockers = []
    for name, item in repositories.items():
        if item["dirty"]:
            blockers.append(f"{name}: source worktree is dirty")
        if item["relation"] in ("diverged", "ahead"):
            blockers.append(f"{name}: source is {item['relation']} from remote")
    if blockers:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "mode": "apply",
            "run_id": run_id,
            "error": "apply refused before fetch/worktree/audit mutation",
            "repositories": repositories,
            "blockers": blockers,
            "receipts": {},
        }
    return _apply(config, repositories, run_id)
