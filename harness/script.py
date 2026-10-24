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

The chat box and MCP are faces over this same owner, not copies. A chat message
that starts with ``/script`` is *parsed* here (:func:`parse_script_request`),
*proposed* here (:func:`propose_script`, which compiles but never writes or
runs), and only executed here (:func:`run_script`) after the operator approves
the exact source the card showed. A model reply joins the same path when it
marks one ``harness-script`` fence (:func:`extract_model_proposal`); the lane
saves the consent turn instead of the prose answer (:func:`proposal_turn`), and
a model-origin run returns its output to the model only as bounded, tagged,
untrusted evidence (:func:`untrusted_output_envelope`).
``script_evidence`` gives the chat lane the typed row it stores with the turn,
in the same ``kind``/``ok`` vocabulary the web sources already use.
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

#: The chat-box command. A message has to opt in explicitly; ordinary chat is
#: never scanned for code, so the model is not bypassed by accident.
SCRIPT_COMMAND = "/script"
_FENCE_RE = re.compile(
    r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

#: A model-authored proposal must *say* it is one. The explicit info string
#: (` ```harness-script `) or a `SCRIPT PROPOSAL:` marker above a Python fence
#: keeps ordinary code examples in an answer from becoming consent requests.
_MODEL_FENCE_RE = re.compile(
    r"```[ \t]*harness-script[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_MODEL_MARKER_RE = re.compile(
    r"(?is)^[ \t]*SCRIPT[ \t]+PROPOSAL[ \t]*:?[ \t]*\r?\n"
    r"[ \t]*```(?:python|py)?[ \t]*\r?\n(.*?)```")

#: How much script output is fed back to the model for its final answer. The
#: receipt keeps the full capped output; the model gets a bounded excerpt.
MAX_MODEL_EVIDENCE_CHARS = 8000
_OUTPUT_TAG = "untrusted-script-output"

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


class ScriptRefused(HarnessError):
    """A typed refusal from the one execution owner.

    ``reason`` is the machine-readable classification the CLI, MCP and server
    use, so none of them has to match prose to know *why* a run was refused.
    """

    kind = "script_refused"

    def __init__(self, reason: str, message: str):
        super().__init__(f"script refused: {message}")
        self.reason = str(reason)
        self.message = str(message)
        #: The terminal receipt attached by :func:`record_refusal`, so a face
        #: that catches the refusal has the same evidence the event carried.
        self.receipt: Optional[Dict[str, Any]] = None


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


def _refuse(reason: str, message: str) -> ScriptRefused:
    return ScriptRefused(reason, message)


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
            "syntax", f"syntax error at line {exc.lineno}: {exc.msg}") from exc


def _capped(raw: str, limit: int) -> tuple:
    blob = raw or ""
    if len(blob) <= limit:
        return blob, False
    return blob[:limit], True


def parse_script_request(prompt: str) -> Optional[Dict[str, str]]:
    """Read a ``/script`` chat message into ``{goal, source}``, or return None.

    Only an explicit command opt in: ordinary prompts are never scanned for
    code. The source is the single fenced Python block; the goal is the rest
    of the command line. Prose after the block is allowed and ignored, but a
    second code block is ambiguous and refused rather than guessed at.
    """
    text = (prompt or "").strip()
    if not text.startswith(SCRIPT_COMMAND):
        return None
    rest = text[len(SCRIPT_COMMAND):]
    if rest and not rest[0].isspace():
        return None  # ``/scripts`` is not the command
    first_line, _, body = rest.partition("\n")
    blocks = _FENCE_RE.findall(body)
    if len(blocks) != 1:
        raise _refuse(
            "empty",
            "a /script message needs exactly one fenced ```python block "
            f"(found {len(blocks)})")
    source = blocks[0].strip("\n")
    if not source.strip():
        raise _refuse("empty", "the fenced script block is empty")
    return {"goal": first_line.strip(), "source": source}


def refusal_receipt(reason: str, message: str, *, source: str = "",
                    goal: str = "", root=None,
                    policy: Optional[ScriptPolicy] = None,
                    timeout_s: Optional[int] = None) -> Dict[str, Any]:
    """The terminal receipt a refused run owes the caller.

    Same keys as a completed run so every reader renders one shape and only
    the status differs. Nothing is invented: a process that never started has
    no exit code, no path and no duration.
    """
    source = str(source or "")
    return {
        "kind": "script",
        "status": "refused",
        "exit_code": None,
        "killed": False,
        "cancelled": reason == "cancel",
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "duration_s": 0.0,
        "script_path": None,
        "script_sha256": script_sha256(source) if source else None,
        "goal": str(goal or ""),
        "root": str(root) if root is not None and str(root).strip() else None,
        "timeout_s": (timeout_s if timeout_s is not None
                      else (policy or ScriptPolicy()).timeout_s),
        "policy": (policy or ScriptPolicy()).as_dict(),
        "reason": str(reason),
        "error": str(message),
    }


def record_refusal(exc: ScriptRefused, *, task_id: Optional[str] = None,
                   source: str = "", goal: str = "", root=None,
                   policy: Optional[ScriptPolicy] = None,
                   timeout_s: Optional[int] = None) -> Dict[str, Any]:
    """The ONE emitter for a refused script: attach its receipt, close the step.

    A refusal happens before a process exists, so there is no ``script_start``;
    this terminal ``script_end`` is what a reader waits for. Called by the two
    wrappers below and by the MCP pre-checks, so no face invents its own frame
    or a second evidence shape.
    """
    receipt = refusal_receipt(exc.reason, exc.message, source=source,
                              goal=goal, root=root, policy=policy,
                              timeout_s=timeout_s)
    exc.receipt = receipt
    emit("script_end", status="refused", reason=exc.reason,
         exit_code=None, duration_s=0.0, truncated=False,
         sha256=receipt["script_sha256"], task_id=task_id, chars=0,
         aligned=None, sufficient=None)
    return receipt


def extract_model_proposal(text: str) -> Optional[Dict[str, str]]:
    """Read at most one marked proposal out of a model reply.

    Returns ``{"source", "prose"}`` where ``prose`` is the reply with the
    proposal removed, so the explanation the model wrote survives next to the
    card. Only the two explicit forms match; an ordinary fenced code example
    never does, and two proposals in one reply are ambiguous and ignored.
    """
    raw = str(text or "")
    fences = list(_MODEL_FENCE_RE.finditer(raw))
    markers = list(_MODEL_MARKER_RE.finditer(raw))
    if len(fences) + len(markers) != 1:
        return None
    match = (fences + markers)[0]
    source = match.group(1).strip("\n")
    if not source.strip():
        return None
    before = raw[:match.start()]
    if fences and match is fences[0]:
        # The harness-script form leaves the marker line outside the fence
        # match; it labels the proposal and must not survive as prose.
        before = re.sub(
            # An all-caps label (models typo it: "SCIRPT PROPOSAL") is a
            # label, not prose, when the fence itself already marked the
            # proposal. Mixed-case mentions stay untouched.
            r"(?m)^[ \t]*[A-Z][A-Z \t]{0,40}PROPOSAL[ \t]*:?[ \t]*(?:\r?\n)?[ \t]*\Z",
            "", before)
    prose = (before + "\n" + raw[match.end():]).strip()
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return {"source": source, "prose": prose}


def propose_source(source: str, *, goal: str = "", root=None,
                   policy: Optional[ScriptPolicy] = None,
                   task_id: Optional[str] = None,
                   origin: str = "user") -> Dict[str, Any]:
    """Build one consent payload from explicit bytes, whoever authored them.

    The ``/script`` face and a model reply both land here, so the compile
    check, size bound, workspace rule and sha handle have one implementation.
    A refusal is recorded as its terminal ``script_end`` frame before it is
    raised; nothing is written or executed.
    """
    resolved = policy or resolve_script_policy()
    payload = {"goal": str(goal or "").strip(), "source": str(source or "")}
    try:
        return _proposal(payload, root=root, policy=resolved, origin=origin)
    except ScriptRefused as exc:
        record_refusal(
            exc, task_id=task_id, source=payload["source"],
            goal=payload["goal"], root=root, policy=resolved)
        raise


def propose_script(prompt: str, *, root=None,
                   policy: Optional[ScriptPolicy] = None,
                   task_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Turn a ``/script`` message into a consent request -- never a run.

    Compiling here means the card never asks the operator to approve a script
    that cannot start. Nothing is written and nothing executes; the sha in the
    payload is the handle the run must present. A refusal is recorded as the
    terminal ``script_end`` frame before it is raised.
    """
    resolved = policy or resolve_script_policy()
    try:
        request = parse_script_request(prompt)
    except ScriptRefused as exc:
        record_refusal(exc, task_id=task_id, root=root, policy=resolved)
        raise
    if request is None:
        return None
    return propose_source(
        request["source"], goal=request["goal"], root=root, policy=resolved,
        task_id=task_id, origin="user")


def proposal_turn(prompt: str, result: Dict[str, Any], *, root=None,
                  policy: Optional[ScriptPolicy] = None,
                  task_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Convert a model reply with a marked script into the chat turn it saves.

    The model's prose is kept, the proposal is attached, and the turn is a
    consent request like any other. A draft that cannot compile stays prose
    with an honest note rather than becoming a run or a failed turn. Returns
    None when the reply contains no proposal (or the draft was refused).
    """
    result = result if isinstance(result, dict) else {}
    found = extract_model_proposal(str(result.get("response") or ""))
    if found is None:
        return None
    resolved = policy or resolve_script_policy()
    try:
        proposal = propose_source(
            found["source"], goal=str(prompt or ""), root=root,
            policy=resolved, task_id=task_id, origin="model")
    except ScriptRefused as exc:
        result["response"] = (
            found["prose"].rstrip()
            + f"\n\n[Script proposal refused ({exc.reason}): {exc.message}]"
        ).strip()
        return None
    info = dict(proposal["script"])
    emit("script_proposed", task_id=task_id, sha256=info["sha256"],
         chars=info["chars"], goal=info["goal"], enabled=True, origin="model")
    response = "\n\n".join(
        part for part in (found["prose"].strip(), proposal["response"])
        if part)
    return {
        "status": "awaiting_consent",
        "intent": "script",
        "kind": "script",
        "script_state": "proposed",
        "proposal_origin": "model",
        "prompt": prompt,
        "response": response,
        "model": result.get("model"),
        "cost": round(float(result.get("cost") or 0.0), 6),
        "script": info,
    }


def untrusted_output_envelope(receipt: Optional[Dict[str, Any]], *,
                             limit: int = MAX_MODEL_EVIDENCE_CHARS) -> str:
    """Wrap a receipt's output as untrusted evidence for a follow-up answer.

    The closing tag is neutralized in the payload, so text produced by the
    script cannot end the envelope and start issuing instructions. The receipt
    itself keeps the full capped output; only this excerpt is bounded.
    """
    receipt = receipt or {}
    budget = max(500, int(limit))

    def clean(value, cap):
        body = str(value or "")
        body = body.replace(
            f"</{_OUTPUT_TAG}>", f"<\\/{_OUTPUT_TAG}>")
        body = body.replace(f"<{_OUTPUT_TAG}>", f"<\\{_OUTPUT_TAG}>")
        if len(body) > cap:
            body = body[:cap] + "\n[truncated for the model]"
        return body or "(empty)"

    return (
        f"<{_OUTPUT_TAG}>\n"
        f"status: {receipt.get('status')}\n"
        f"exit_code: {receipt.get('exit_code')}\n"
        f"timed_out: {bool(receipt.get('killed'))}\n"
        f"truncated: {bool(receipt.get('truncated'))}\n"
        f"script_sha256: {receipt.get('script_sha256')}\n"
        "stdout:\n" + clean(receipt.get("stdout"), budget) + "\n"
        "stderr:\n" + clean(receipt.get("stderr"), max(500, budget // 2)) + "\n"
        f"</{_OUTPUT_TAG}>"
    )


def _proposal(request: Dict[str, str], *, root=None,
              policy: ScriptPolicy, origin: str = "user") -> Dict[str, Any]:
    """Build the consent payload for an already-parsed request."""
    if not policy.enabled:
        raise _refuse(
            "disabled", "scripting is disabled (set HARNESS_SCRIPT_ENABLED=1)")
    if root is None or not str(root).strip():
        raise _refuse(
            "root", "select a workspace folder before running a script")
    root_path = Path(str(root)).expanduser().resolve()
    if not root_path.is_dir():
        raise _refuse("root", f"workspace root is not a directory: {root_path}")
    source = request["source"]
    if len(source) > MAX_SOURCE_CHARS:
        raise _refuse(
            "too_large", f"script is larger than {MAX_SOURCE_CHARS} characters")
    _compile(source)
    sha = script_sha256(source)
    goal = request["goal"]
    return {
        "status": "awaiting_consent",
        "kind": "script",
        "response": (
            "Script ready for approval -- nothing has run yet.\n\n"
            f"sha256: `{sha}`\n"
            f"size: {len(source)} characters\n"
            f"workspace: `{root_path}`\n"
            + (f"goal: {goal}\n\n" if goal else "\n")
            + "Approve the exact source shown to run it once; editing it "
              "invalidates the approval."
        ),
        "script": {
            "goal": goal,
            "source": source,
            "sha256": sha,
            "chars": len(source),
            "root": str(root_path),
            "timeout_s": policy.timeout_s,
            "enabled": True,
            "origin": origin,
        },
    }


def script_evidence(receipt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The typed evidence row the chat lane stores with the turn.

    Same ``kind``/``ok`` vocabulary as the web sources, so a transcript has
    one evidence shape rather than a second, script-specific one. A refusal is
    a terminal row too: ``ok`` is false and ``reason`` says why.
    """
    receipt = receipt or {}
    return {
        "kind": "script",
        "ok": receipt.get("status") == "ok",
        "sha256": receipt.get("script_sha256"),
        "status": receipt.get("status"),
        "reason": receipt.get("reason"),
        "exit_code": receipt.get("exit_code"),
        "duration_s": receipt.get("duration_s"),
        "chars": len(str(receipt.get("stdout") or "")),
        "path": receipt.get("script_path"),
    }


def run_script(source: str, *, root, goal: str = "", allow_exec: bool = False,
               consent_sha: Optional[str] = None, timeout_s: Optional[int] = None,
               policy: Optional[ScriptPolicy] = None,
               cancel_check=None, jev_policy=None,
               task_id: Optional[str] = None) -> Dict[str, Any]:
    """Write, compile-check, and run one script; return a receipt.

    Every refusal is a :class:`ScriptRefused` (a ``HarnessError`` with
    ``kind="script_refused"`` and a ``reason``), so a caller can classify
    without parsing prose. Each refusal also leaves exactly one terminal
    ``script_end`` frame, emitted here and nowhere else.
    """
    resolved = policy or resolve_script_policy()
    try:
        return _execute_script(
            source, root=root, goal=goal, allow_exec=allow_exec,
            consent_sha=consent_sha, timeout_s=timeout_s, policy=resolved,
            cancel_check=cancel_check, jev_policy=jev_policy, task_id=task_id)
    except ScriptRefused as exc:
        record_refusal(exc, task_id=task_id, source=str(source or ""),
                       goal=goal, root=root, policy=resolved,
                       timeout_s=timeout_s)
        raise


def _execute_script(source: str, *, root, goal: str = "",
                    allow_exec: bool = False,
                    consent_sha: Optional[str] = None,
                    timeout_s: Optional[int] = None,
                    policy: ScriptPolicy, cancel_check=None,
                    jev_policy=None,
                    task_id: Optional[str] = None) -> Dict[str, Any]:
    """The run itself; ``run_script`` owns the refusal evidence around it."""
    if not policy.enabled:
        raise _refuse(
            "disabled", "scripting is disabled (set HARNESS_SCRIPT_ENABLED=1)")
    if not allow_exec:
        raise _refuse(
            "no_consent",
            "explicit consent is required (approve the script to run it)")
    if not source or not str(source).strip():
        raise _refuse("empty", "the script is empty")
    source = str(source)
    if len(source) > MAX_SOURCE_CHARS:
        raise _refuse(
            "too_large", f"script is larger than {MAX_SOURCE_CHARS} characters")
    if root is None or not str(root).strip():
        raise _refuse("root", "an explicit workspace root is required")
    root_path = Path(str(root)).expanduser().resolve()
    if not root_path.is_dir():
        raise _refuse("root", f"workspace root is not a directory: {root_path}")

    sha = script_sha256(source)
    if consent_sha and str(consent_sha).strip():
        if not _SHA_RE.match(str(consent_sha).strip().lower()):
            raise _refuse("sha_invalid", "consent sha is not a sha256 digest")
        if str(consent_sha).strip().lower() != sha:
            raise _refuse(
                "sha_mismatch",
                "consent was granted for a different script (sha mismatch); "
                "re-approve the current source")

    _compile(source)

    if cancel_check is not None and cancel_check():
        # Before the run directory exists: a cancelled request writes nothing.
        raise _refuse("cancel", "the request was cancelled before it started")

    run_id = f"{sha[:8]}-{int(time.time() * 1000)}"
    run_dir = root_path / _RUN_DIR_NAME / "scripts" / run_id
    if not _within(run_dir, root_path):
        raise _refuse("write", "run directory would escape the workspace root")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass  # Windows: ACLs, not POSIX modes
    except OSError as exc:
        raise _refuse(
            "write", f"cannot create the run directory: {exc}") from exc
    script_path = run_dir / f"script_{sha[:8]}.py"
    try:
        # LF always (osal owns evidence bytes): the file a reader sees must
        # hash to the consent sha, on every platform.
        osal.write_text(str(script_path), source)
    except OSError as exc:
        raise _refuse("write", f"cannot write the script: {exc}") from exc

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
        cwd=str(root_path), timeout=timeout, env=_scrubbed_env(policy),
        cancel_check=cancel_check)
    duration_s = round(time.monotonic() - started, 3)
    stdout, out_truncated = _capped(result.stdout, policy.max_output_bytes)
    stderr, err_truncated = _capped(result.stderr, policy.max_output_bytes)
    killed = result.returncode == 124
    cancelled = result.returncode == 125

    receipt: Dict[str, Any] = {
        "kind": "script",
        "status": ("ok" if result.returncode == 0 else
                   "cancelled" if cancelled else
                   "timeout" if killed else "failed"),
        "exit_code": int(result.returncode),
        "killed": killed,
        "cancelled": cancelled,
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

    alignment = receipt.get("alignment") or {}
    emit("script_end", status=receipt["status"], exit_code=receipt["exit_code"],
         duration_s=duration_s, truncated=receipt["truncated"],
         sha256=sha, task_id=task_id, chars=len(stdout),
         aligned=alignment.get("aligned"),
         sufficient=alignment.get("sufficient"))
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
