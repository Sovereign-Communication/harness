"""Governed script execution -- the one owner of "an agent writes code and runs it".

Prime Agent's lesson (PrimeIntellect-ai/prime-agent, 2026-08) is that a coding
or research agent does not need a tool per task: one execution surface plus the
model's own code covers scraping, parsing, driving a browser, and computing a
result. Harness had the opposite shape -- a closed action vocabulary plus a
handler per capability -- so any request outside it (audit this site, parse
this file, run this probe) either failed or needed a new module.

This module is that one surface, and it is deliberately small. The safety
boundary is the runner, not the judge:

* **Off by default.** ``HARNESS_SCRIPT_ENABLED=1`` (or an explicit
  ``ScriptPolicy``) turns it on. Nothing here runs because a package was
  imported.
* **Consent is bound to the source.** ``run_script`` requires ``allow_exec``
  plus the sha256 of the exact script the operator approved, so an edited
  script cannot ride an old approval.
* **An explicit workspace.** The script runs with ``cwd`` set to the selected
  root and only writes under ``<root>/.harness/scripts/<run>/``.
* **A scrubbed environment.** Provider keys and every other inherited variable
  are dropped; only PATH/HOME/TMP/locale survive.
* **A hard bound.** ``osal.run_tree`` gives the child its own process group and
  kills the whole tree on timeout; output is capped.
* **The product is importable.** The runner runs the same interpreter, pins
  ``PYTHONPATH`` to the package's own parent, and passes the scrubbed
  environment, so a script can call Harness's fetch ladder or Playwright
  instead of re-implementing them. The operator's shell cannot inject modules:
  the environment is rebuilt from an allowlist and ``PYTHONPATH`` is a value
  the runner sets. (No ``-s``/``-I``: a user-site Playwright is the point of
  "drive the browser from a script".)

What this is *not*: an OS sandbox. A script that opens a socket is not blocked
by the runner (there is no namespace or firewall here), and the interpreter can
still read what the operator's user can read. ``allow_network`` is recorded and
exported to the script as ``HARNESS_SCRIPT_NETWORK`` for code that wants to
honor it; the documented limit is in ``docs/security.md``.

Jev scores the script against the request for alignment and completeness. That
score is evidence, never a gate: measured judge calibration on a good long
answer is noisy, so a low score is reported to the agent as "revise" while a
consented run still proceeds.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import osal
from .errors import HarnessError
from .events import emit

#: Defaults and hard caps. A script is a tool call, not a build; anything that
#: needs minutes belongs in the plan/apply lane.
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
DEFAULT_MAX_OUTPUT_BYTES = 65_536
MAX_OUTPUT_BYTES_CAP = 1_048_576
MAX_SOURCE_CHARS = 200_000

#: The environment a script may see. Everything else (provider keys, tokens,
#: proxy settings carrying credentials) is dropped rather than filtered, so a
#: new secret in the operator's shell never becomes reachable by accident.
_ENV_ALLOW = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONIOENCODING",
    # Windows profile directories: site.py finds the user site through
    # APPDATA, and Playwright finds its browsers through LOCALAPPDATA. Without
    # them a script silently loses whichever capability lives there.
    "APPDATA", "LOCALAPPDATA",
)

_RUN_DIR_NAME = ".harness"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ScriptPolicy:
    """Resolved posture for one run. One owner, no per-entry-point env reads."""

    enabled: bool = False
    timeout_s: int = DEFAULT_TIMEOUT_S
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    allow_network: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _truthy(value: Optional[str], default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def resolve_script_policy(settings=None) -> ScriptPolicy:
    """Resolve ``ScriptPolicy`` from settings, then env, then defaults.

    Settings win when present so a GUI toggle has one place to write; the env
    is the operator's documented escape hatch (``HARNESS_SCRIPT_*``). The
    default is **disabled**: this is arbitrary code execution, and silence is
    not consent.
    """
    def pick(attr: str, env: str):
        value = getattr(settings, attr, None) if settings is not None else None
        if value is not None:
            return value
        return os.environ.get(env)

    enabled = _truthy(pick("script_enabled", "HARNESS_SCRIPT_ENABLED"), False)
    timeout_s = _bounded_int(
        pick("script_timeout_s", "HARNESS_SCRIPT_TIMEOUT_S"),
        DEFAULT_TIMEOUT_S, 1, MAX_TIMEOUT_S)
    max_bytes = _bounded_int(
        pick("script_max_output_bytes", "HARNESS_SCRIPT_MAX_OUTPUT_BYTES"),
        DEFAULT_MAX_OUTPUT_BYTES, 1_000, MAX_OUTPUT_BYTES_CAP)
    network = _truthy(
        pick("script_allow_network", "HARNESS_SCRIPT_ALLOW_NETWORK"), False)
    return ScriptPolicy(enabled=enabled, timeout_s=timeout_s,
                        max_output_bytes=max_bytes, allow_network=network)


def script_sha256(source: str) -> str:
    """The consent handle: an approval is bound to these exact bytes."""
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()


def _package_root() -> str:
    """The directory that holds the importable ``harness`` package.

    Editable installs resolve to the checkout; wheel installs resolve to
    site-packages. Either way a script can ``import harness``.
    """
    return str(Path(__file__).resolve().parent.parent)


def _scrubbed_env(policy: ScriptPolicy) -> Dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_ALLOW if key in os.environ}
    env["PYTHONPATH"] = _package_root()
    env["HARNESS_SCRIPT_NETWORK"] = "1" if policy.allow_network else "0"
    # Keep the child honest about buffering: an interactive-looking script in a
    # pipe is the classic "my prints disappeared" report.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _refuse(reason: str) -> HarnessError:
    return HarnessError(f"script refused: {reason}")


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _compile(source: str) -> None:
    """A syntax error is a refusal, not a traceback the user has to decode."""
    try:
        compile(source, "<harness-script>", "exec")
    except SyntaxError as exc:
        raise _refuse(
            f"syntax error at line {exc.lineno}: {exc.msg}") from exc


def _capped(raw: str, limit: int) -> tuple:
    blob = raw or ""
    if len(blob) <= limit:
        return blob, False
    return blob[:limit], True


def run_script(source: str, *, root, goal: str = "", allow_exec: bool = False,
               consent_sha: Optional[str] = None, timeout_s: Optional[int] = None,
               policy: Optional[ScriptPolicy] = None,
               cancel_check=None, jev_policy=None,
               task_id: Optional[str] = None) -> Dict[str, Any]:
    """Write, compile-check, and run one script; return a receipt.

    Every refusal is a :class:`HarnessError` whose message starts with
    ``script refused:`` so a caller (CLI, MCP, chat) can classify without
    parsing prose.
    """
    policy = policy or resolve_script_policy()
    if not policy.enabled:
        raise _refuse("scripting is disabled (set HARNESS_SCRIPT_ENABLED=1)")
    if not allow_exec:
        raise _refuse("explicit consent is required (approve the script to run it)")
    if not source or not str(source).strip():
        raise _refuse("the script is empty")
    source = str(source)
    if len(source) > MAX_SOURCE_CHARS:
        raise _refuse(f"script is larger than {MAX_SOURCE_CHARS} characters")
    if root is None or not str(root).strip():
        raise _refuse("an explicit workspace root is required")
    root_path = Path(str(root)).expanduser().resolve()
    if not root_path.is_dir():
        raise _refuse(f"workspace root is not a directory: {root_path}")

    sha = script_sha256(source)
    if consent_sha and str(consent_sha).strip():
        if not _SHA_RE.match(str(consent_sha).strip().lower()):
            raise _refuse("consent sha is not a sha256 digest")
        if str(consent_sha).strip().lower() != sha:
            raise _refuse(
                "consent was granted for a different script (sha mismatch); "
                "re-approve the current source")

    _compile(source)

    if cancel_check is not None and cancel_check():
        # Before the run directory exists: a cancelled request writes nothing.
        raise HarnessError("script cancelled before it started")

    run_id = f"{sha[:8]}-{int(time.time() * 1000)}"
    run_dir = root_path / _RUN_DIR_NAME / "scripts" / run_id
    if not _within(run_dir, root_path):
        raise _refuse("run directory would escape the workspace root")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass  # Windows: ACLs, not POSIX modes
    except OSError as exc:
        raise _refuse(f"cannot create the run directory: {exc}") from exc
    script_path = run_dir / f"script_{sha[:8]}.py"
    try:
        # LF always (osal owns evidence bytes): the file a reader sees must
        # hash to the consent sha, on every platform.
        osal.write_text(str(script_path), source)
    except OSError as exc:
        raise _refuse(f"cannot write the script: {exc}") from exc

    timeout = _bounded_int(
        timeout_s if timeout_s is not None else policy.timeout_s,
        policy.timeout_s, 1, MAX_TIMEOUT_S)

    emit("script_start", path=str(script_path), sha256=sha, timeout_s=timeout,
         root=str(root_path), task_id=task_id)
    started = time.monotonic()
    # Same interpreter, scrubbed environment, PYTHONPATH pinned to the product
    # root: a script that could not ``import harness`` (or Playwright) could not
    # use the capabilities this product already owns.
    result = osal.run_tree(
        [sys.executable, str(script_path)],
        cwd=str(root_path), timeout=timeout, env=_scrubbed_env(policy))
    duration_s = round(time.monotonic() - started, 3)
    stdout, out_truncated = _capped(result.stdout, policy.max_output_bytes)
    stderr, err_truncated = _capped(result.stderr, policy.max_output_bytes)
    killed = result.returncode == 124

    receipt: Dict[str, Any] = {
        "status": "ok" if result.returncode == 0 else (
            "timeout" if killed else "failed"),
        "exit_code": int(result.returncode),
        "killed": killed,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": bool(out_truncated or err_truncated),
        "duration_s": duration_s,
        "script_path": str(script_path),
        "script_sha256": sha,
        "goal": str(goal or ""),
        "root": str(root_path),
        "timeout_s": timeout,
        "policy": policy.as_dict(),
    }

    if jev_policy is not None:
        try:
            receipt["alignment"] = score_script(jev_policy, goal, source,
                                                receipt=receipt)
        except HarnessError as exc:
            # The judge is evidence, not the safety boundary: a Jev outage
            # must not turn a completed run into a failed one.
            receipt["alignment"] = {"available": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never lose the run receipt
            receipt["alignment"] = {
                "available": False, "reason": f"{type(exc).__name__}: {exc}"}

    emit("script_end", status=receipt["status"], exit_code=receipt["exit_code"],
         duration_s=duration_s, truncated=receipt["truncated"],
         sha256=sha, task_id=task_id)
    return receipt


def score_script(jev_policy, goal: str, source: str, *,
                 receipt: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Ask Jev whether a script aligns with, and completes, the request.

    Reuses the calibrated answer-sufficiency pack with the script as the
    candidate and the request as the question, so there is exactly one judging
    contract in the product. Returns probabilities plus a single verdict; the
    caller decides what to do with them.
    """
    request = (
        "Judge whether the SCRIPT in the candidate scores as a correct and "
        "complete implementation of this request.\n\nREQUEST:\n"
        + str(goal or "").strip()[:1200]
        + "\n\nA sufficient script runs in one Python process inside the "
          "workspace, is limited to the stated task, needs no operator "
          "intervention, and does not embed secrets or destructive effects."
    )
    context = ""
    if receipt:
        context = (
            f"Exit code: {receipt.get('exit_code')}\n"
            f"Timed out: {receipt.get('killed')}\n"
            f"Stdout (first 800 chars): {str(receipt.get('stdout') or '')[:800]}\n"
            f"Stderr (first 800 chars): {str(receipt.get('stderr') or '')[:800]}"
        )
    result, structural = jev_policy.evaluate_answer(
        request, source, context, site="script",
        task_id=str((receipt or {}).get("script_sha256") or "")[:16] or None)
    answers = dict(getattr(result, "answers", {}) or {})
    sufficient = answers.get("answer_sufficient")
    if isinstance(sufficient, dict):
        sufficient = sufficient.get("noul")
    aligned = bool(isinstance(sufficient, (int, float))
                   and float(sufficient) >= 0.7)
    return {
        "available": not bool(getattr(result, "is_fallback", False)),
        "aligned": aligned,
        "sufficient": sufficient,
        # The pack's combined confidence is the minimum across all three
        # questions, so a plan-shaped request (plan_required high, correctly)
        # would report near-zero for an aligned script. Report what this score
        # is actually about -- sufficiency -- and keep the raw numbers.
        "verdict": "aligned" if aligned else "revise",
        "provided_verdict": getattr(result, "verdict", None),
        "raw_confidence": float(getattr(result, "confidence", 0.0) or 0.0),
        "model": getattr(result, "model", None),
        "reasons": list(getattr(result, "reasons", []) or []),
        "pack_version": structural.get("pack_version"),
    }
