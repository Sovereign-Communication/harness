"""Host-aware provisioning: probe the machine, plan the setup, ask, then run it.

Harness is the agent that lives on local hardware, so "what does this goal
need installed here?" is a question it should be able to answer instead of
deferring. This module is the CORE of that capability -- the GUI card, MCP
tool and agent intent that surface it are separate, later wiring.

The shape is four small, separately testable stages, and the safety rules
live in code at the seams between them rather than in a prompt:

1. **probe** (:func:`probe_host`) -- read-only facts: OS, architecture,
   interpreter, GPU presence, free disk, package managers, gathered with
   fixed query argvs through the ``osal`` subprocess seam.
2. **plan** (:func:`plan_provision`) -- an ordered, typed :class:`Plan` of
   :class:`Step`\\ s. A step is an argv *list* (never a shell string), a
   class (READ / MUTATING / IRREVERSIBLE, the same vocabulary as
   ``driver_core.actions``), its expected effect, a rollback hint, and
   whether it needs the network. A Jev choice pack may pick among the
   recipes the code already proved applicable; unkeyed or invalid answers
   fall back to a deterministic order and say so (``is_fallback``).
3. **approve** (:class:`ApprovalGate`) -- nothing mutating runs without an
   explicit approval record bound to the plan's content digest. Decline and
   defer are first-class, recorded outcomes, not errors; an IRREVERSIBLE
   step needs its OWN approval (a plan-level yes never covers it).
4. **execute** (:func:`execute_plan`) -- dry-run by default; real runs go
   through ``osal`` with timeouts, stop on the first failure, report rollback
   hints, and append one hash-chained ledger entry per plan, approval and
   step.

The argv allowlist (:func:`classify_argv`) is the heart of it. A step is
admissible only if it matches a declared rule -- package-manager install,
venv, pip, npm, mkdir inside an approved root, or a read-only query. There is
no escape hatch: no shell interpreters, no privilege escalation, no deletion,
no formatting, no registry edits, no URL or path installs. The model (or any
caller) can only *propose* steps; code decides whether one may exist.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from . import osal
from .errors import HarnessError
from .ledger import AutonomyLedger
from .jev_packs import (PROVISION_PACK_VERSION, PROVISION_SITE,
                        provision_selection_question_pack,
                        provision_verification_question_pack)

# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

# Same values as ``driver_core.actions`` (a test pins the parity): the
# consent classes are one vocabulary, not two.
READ = "read_only"
MUTATING = "mutating"
IRREVERSIBLE = "irreversible"
CLASSES = (READ, MUTATING, IRREVERSIBLE)
_SEVERITY = {READ: 0, MUTATING: 1, IRREVERSIBLE: 2}

APPROVE = "approve"
DECLINE = "decline"
DEFER = "defer"
DECISIONS = (APPROVE, DECLINE, DEFER)

# Step / run outcome vocabulary.
COMPLETED = "completed"
FAILED = "failed"
DRY_RUN = "dry_run"
DECLINED = "declined"
DEFERRED = "deferred"
AWAITING_APPROVAL = "awaiting_approval"
SKIPPED = "skipped"
AUDIT_FAILED = "audit_failed"
ALREADY_DONE = "already_done"

PROVISION_EVENTS = ("provision_plan", "provision_approval",
                    "provision_step_start", "provision_step", "provision_end")

DEFAULT_STEP_TIMEOUT_S = 900
DEFAULT_MAX_STEPS = 24
DEFAULT_MAX_OUTPUT_CHARS = 8000
DEFAULT_MAX_ARGV = 64
DEFAULT_MAX_ARG_CHARS = 1024
DEFAULT_MAX_SPECS = 32
_PROBE_TIMEOUT_S = 10

PROBE_PACKAGE_MANAGERS = ("winget", "choco", "scoop", "apt", "brew", "pip", "npm")
_SYSTEM_MANAGERS = ("winget", "choco", "scoop", "apt", "brew")

# Recipes the planner can build; the ids are the Jev choice vocabulary.
RECIPE_VENV_PIP = "venv-pip"
RECIPE_NPM_PREFIX = "npm-prefix"
RECIPE_SYSTEM_PM = "system-pm"
RECIPE_INVENTORY = "inventory"
_RECIPE_DESCRIPTIONS = {
    RECIPE_VENV_PIP: ("private virtual environment inside the approved root; "
                      "wheels only; reversible by deleting the directory"),
    RECIPE_NPM_PREFIX: ("package install into a private prefix inside the "
                        "approved root with install scripts disabled; "
                        "reversible by deleting the directory"),
    RECIPE_SYSTEM_PM: ("system package-manager install; changes the host "
                       "outside the approved root"),
}


class ProvisionError(HarnessError):
    """A plan, step, or approval the provisioning core refuses.

    ``problems`` carries every defect found so a reviewer sees them all at
    once rather than fixing one per round trip.
    """
    kind = "provision_error"

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = list(problems or [])


# --------------------------------------------------------------------------
# host probe (read-only)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PackageManager:
    """One package manager: where it is, and whether it answered ``--version``."""

    name: str
    path: str
    version: Optional[str]
    usable: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "path": self.path,
                "version": self.version, "usable": self.usable}


@dataclass(frozen=True)
class HostProbe:
    """Everything the planner may know about this machine (facts, not guesses)."""

    os_name: str
    arch: str
    python_version: str
    python_executable: str
    gpu_present: bool
    gpus: Tuple[str, ...]
    free_disk_bytes: Optional[int]
    package_managers: Tuple[PackageManager, ...]
    probed_path: str

    def manager(self, name: str) -> Optional[PackageManager]:
        for pm in self.package_managers:
            if pm.name == name and pm.usable:
                return pm
        return None

    def usable_managers(self) -> Tuple[str, ...]:
        return tuple(pm.name for pm in self.package_managers if pm.usable)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "os": self.os_name, "arch": self.arch,
            "python_version": self.python_version,
            "python_executable": self.python_executable,
            "gpu_present": self.gpu_present, "gpus": list(self.gpus),
            "free_disk_bytes": self.free_disk_bytes,
            "package_managers": [pm.to_dict() for pm in self.package_managers],
            "probed_path": self.probed_path,
        }

    def jev_facts(self) -> Dict[str, Any]:
        """The path-free subset sent to a judgment (no home dirs, no binaries)."""
        return {"os": self.os_name, "arch": self.arch,
                "python_version": self.python_version,
                "gpu_present": self.gpu_present,
                "package_managers": list(self.usable_managers())}


def _first_line(text: str, limit: int = 120) -> Optional[str]:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return None


def _existing_ancestor(path: str) -> str:
    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe


def _default_facts(path: str) -> Dict[str, Any]:
    try:
        free = shutil.disk_usage(_existing_ancestor(path)).free
    except OSError:
        free = None
    return {
        "os": platform.system() or "unknown",
        "arch": (platform.machine() or "unknown").lower(),
        "python_version": platform.python_version(),
        "python_executable": osal.python_exe(),
        "free_disk_bytes": free,
    }


def _parse_nvidia(output: str) -> List[str]:
    """``name, 24564 MiB`` lines from ``nvidia-smi --query-gpu`` (csv)."""
    gpus = []
    for line in (output or "").splitlines():
        line = line.strip()
        if line:
            gpus.append(" ".join(line.split()))
    return gpus


def _parse_rocm(output: str) -> List[str]:
    gpus = []
    for line in (output or "").splitlines():
        match = re.search(r"Card (?:series|model)\s*:\s*(.+)$", line, re.I)
        if match and match.group(1).strip():
            gpus.append(match.group(1).strip())
    return gpus


def probe_host(path: Optional[str] = None, *, runner: Optional[Callable] = None,
               which: Optional[Callable] = None,
               facts: Optional[Mapping[str, Any]] = None) -> HostProbe:
    """Read-only host facts, gathered through the ``osal`` seams.

    ``runner`` / ``which`` default to :func:`osal.run` / :func:`osal.which`
    and exist so tests (and any later remote probe) can substitute a double;
    ``facts`` overrides the platform facts the same way. The commands are
    fixed argv lists with a short timeout: ``<name> --version`` for each
    well-known package manager found on PATH, ``<python> -m pip --version``
    when ``pip`` is not on PATH, and the GPU vendor queries. They are
    read-only *by construction of the argv*, not by sandboxing: each runs
    whatever executable PATH resolves that name to, so a probe is only as
    trustworthy as the machine's PATH. No probe output is ever turned into
    an argv; the planner builds argv from fixed templates alone.
    """
    run = runner or osal.run
    find = which or osal.which
    target = path or os.getcwd()
    base = _default_facts(target)
    if facts:
        base.update(dict(facts))
    py_exe = str(base["python_executable"])

    def query(argv):
        result = run(list(argv), timeout=_PROBE_TIMEOUT_S)
        return result.returncode, result.stdout

    def locate(name):
        """PATH lookup that never trusts the working directory.

        The ABSOLUTE found path is what gets run (a bare name would be
        re-resolved by the OS, cwd first on Windows); a program found in the
        process's working tree was planted there, so it counts as absent.
        """
        found = find(name)
        if not found:
            return None
        found = os.path.abspath(str(found))
        return None if _in_working_directory(found) else found

    managers: List[PackageManager] = []
    for name in PROBE_PACKAGE_MANAGERS:
        found = locate(name)
        if found:
            rc, out = query([found, "--version"])
            managers.append(PackageManager(
                name, found, _first_line(out) if rc == 0 else None, rc == 0))
        elif name == "pip" and find(name) is None:
            # pip is usually reachable only as ``python -I -m pip``.
            rc, out = query([py_exe, "-I", "-m", "pip", "--version"])
            if rc == 0:
                managers.append(PackageManager(
                    name, py_exe + " -m pip", _first_line(out), True))

    gpus: List[str] = []
    smi = locate("nvidia-smi")
    if smi:
        rc, out = query([smi, "--query-gpu=name,memory.total",
                         "--format=csv,noheader"])
        if rc == 0:
            gpus.extend(_parse_nvidia(out))
    rocm = locate("rocm-smi")
    if not gpus and rocm:
        rc, out = query([rocm, "--showproductname"])
        if rc == 0:
            gpus.extend(_parse_rocm(out))
    if not gpus and base["os"] == "Darwin" and base["arch"] in ("arm64", "aarch64"):
        gpus.append("Apple silicon (integrated)")

    free = base.get("free_disk_bytes")
    return HostProbe(
        os_name=str(base["os"]), arch=str(base["arch"]),
        python_version=str(base["python_version"]), python_executable=py_exe,
        gpu_present=bool(gpus), gpus=tuple(gpus),
        free_disk_bytes=int(free) if isinstance(free, int) else None,
        package_managers=tuple(managers), probed_path=target)


# --------------------------------------------------------------------------
# typed plan
# --------------------------------------------------------------------------

_STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode(osal.ENCODING)).hexdigest()


@dataclass(frozen=True)
class Step:
    """One argv-list action with its class, effect and way back.

    ``argv`` is data, never a command string: a string is refused at
    construction so a "shell waiting to happen" cannot even be represented.
    """

    id: str
    description: str
    argv: Tuple[str, ...]
    classification: str
    expected_effect: str
    rollback_hint: str = ""
    requires_network: bool = False
    cwd: Optional[str] = None

    def __post_init__(self):
        argv = self.argv
        if isinstance(argv, (str, bytes)):
            raise ProvisionError(
                f"step {self.id!r}: argv must be a list of arguments, not a "
                "shell string")
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ProvisionError(f"step {self.id!r}: argv must be a non-empty list")
        for token in argv:
            if not isinstance(token, str) or not token:
                raise ProvisionError(
                    f"step {self.id!r}: every argv element must be a "
                    "non-empty string")
        object.__setattr__(self, "argv", tuple(argv))

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "description": self.description,
                "argv": list(self.argv), "classification": self.classification,
                "expected_effect": self.expected_effect,
                "rollback_hint": self.rollback_hint,
                "requires_network": self.requires_network, "cwd": self.cwd}

    def digest(self) -> str:
        return _sha256(_canon(self.to_dict()))


def step_from_dict(data: Any) -> Step:
    """Strict coercion of a proposed step (e.g. from JSON); refuses shape drift."""
    if not isinstance(data, Mapping):
        raise ProvisionError("a step must be an object")
    allowed = {"id", "description", "argv", "classification", "expected_effect",
               "rollback_hint", "requires_network", "cwd"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProvisionError(f"step has undeclared field(s): {unknown}")
    for required in ("id", "description", "argv", "classification",
                     "expected_effect"):
        if required not in data:
            raise ProvisionError(f"step is missing {required!r}")
    for text_field in ("id", "description", "classification", "expected_effect"):
        if not isinstance(data[text_field], str):
            raise ProvisionError(f"step {text_field!r} must be a string")
    rollback = data.get("rollback_hint", "")
    if not isinstance(rollback, str):
        raise ProvisionError("step 'rollback_hint' must be a string")
    network = data.get("requires_network", False)
    if not isinstance(network, bool):
        raise ProvisionError("step 'requires_network' must be a boolean")
    cwd = data.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ProvisionError("step 'cwd' must be a string")
    return Step(data["id"], data["description"], data["argv"],
                data["classification"], data["expected_effect"], rollback,
                network, cwd)


@dataclass(frozen=True)
class Plan:
    """An ordered, reviewable setup plan. Approval binds to :attr:`digest`."""

    goal: str
    steps: Tuple[Step, ...]
    root: str
    recipe: str
    source: str
    is_fallback: bool
    notes: Tuple[str, ...] = ()
    review: Mapping[str, Any] = field(default_factory=dict)
    review_required: bool = False

    def __post_init__(self):
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def digest(self) -> str:
        """Content hash of what would run and how it may be approved.

        ``review_required`` is part of it: a plan flagged for per-step
        approval must not hash like its unflagged twin, or the flag could be
        dropped and the same approvals replayed. Who proposed the plan
        (``source``, notes) is provenance, not content.
        """
        return _sha256(_canon({
            "goal": self.goal, "root": self.root,
            "review_required": bool(self.review_required),
            "steps": [step.to_dict() for step in self.steps]}))

    def step(self, step_id: str) -> Step:
        for candidate in self.steps:
            if candidate.id == step_id:
                return candidate
        raise ProvisionError(f"plan has no step {step_id!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "root": self.root, "recipe": self.recipe,
                "source": self.source, "is_fallback": self.is_fallback,
                "digest": self.digest, "notes": list(self.notes),
                "review": dict(self.review),
                "review_required": self.review_required,
                "steps": [step.to_dict() for step in self.steps]}


def plan_from_dict(data: Any) -> Plan:
    """Rebuild a plan from JSON; the digest is recomputed, never trusted."""
    if not isinstance(data, Mapping):
        raise ProvisionError("a plan must be an object")
    steps = data.get("steps")
    if not isinstance(steps, (list, tuple)):
        raise ProvisionError("a plan requires a steps list")
    goal, root = data.get("goal"), data.get("root")
    if not isinstance(goal, str) or not isinstance(root, str):
        raise ProvisionError("a plan requires string goal and root")
    return Plan(goal, tuple(step_from_dict(s) for s in steps), root,
                str(data.get("recipe") or "external"),
                str(data.get("source") or "external"),
                bool(data.get("is_fallback", True)),
                tuple(str(n) for n in (data.get("notes") or ())),
                dict(data.get("review") or {}),
                # A missing or malformed flag is treated as "flagged": the
                # safe default forces per-step approval; only an explicit
                # boolean False clears it.
                True if not isinstance(data.get("review_required", True), bool)
                else data.get("review_required", True))


def _safe_text(value: Any) -> str:
    """Escape control, format, bidi, zero-width and line-break characters.

    A plan is shown to a human who approves it, so no field may be able to
    repaint the screen, forge a fake step, or visually reorder text.
    """
    out = []
    for ch in str(value):
        if unicodedata.category(ch) in _UNSAFE_CATEGORIES:
            out.append(ch.encode("unicode_escape").decode("ascii"))
        else:
            out.append(ch)
    return "".join(out)


def render_plan(plan: Plan) -> str:
    """The reviewable, human-readable form (what an approval card would show).

    Every field is escaped (:func:`_safe_text`) and each argv is shown as a
    JSON list, verbatim and unambiguous -- never re-joined into a string that
    could be read as a shell line.
    """
    lines = [f"Goal: {_safe_text(plan.goal)}",
             f"Recipe: {_safe_text(plan.recipe)} ({_safe_text(plan.source)}"
             + (", fallback" if plan.is_fallback else "") + ")",
             f"Root: {_safe_text(plan.root)}",
             "Review required: " + ("yes" if plan.review_required else "no"),
             f"Digest: {plan.digest}"]
    for index, step in enumerate(plan.steps, 1):
        net = " [network]" if step.requires_network else ""
        lines.append(f"{index}. [{_safe_text(step.classification)}]{net} "
                     f"{_safe_text(step.id)}: {_safe_text(step.description)}")
        lines.append("   argv: " + json.dumps(list(step.argv), ensure_ascii=True))
        lines.append(f"   effect: {_safe_text(step.expected_effect)}")
        if step.rollback_hint:
            lines.append(f"   rollback: {_safe_text(step.rollback_hint)}")
    for note in plan.notes:
        lines.append(f"note: {_safe_text(note)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the argv allowlist
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvisionPolicy:
    """What this deployment lets provisioning touch."""

    approved_roots: Tuple[str, ...] = ()
    max_steps: int = DEFAULT_MAX_STEPS
    step_timeout_s: int = DEFAULT_STEP_TIMEOUT_S
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    allow_system_install: bool = True
    allow_source_builds: bool = False
    # Absolute interpreter paths (e.g. the probed ``sys.executable``) a step
    # may name by path. Any other executable must be a bare name resolved on
    # PATH at run time, or live inside an approved root.
    trusted_executables: Tuple[str, ...] = ()
    max_argv: int = DEFAULT_MAX_ARGV
    max_arg_chars: int = DEFAULT_MAX_ARG_CHARS
    max_specs: int = DEFAULT_MAX_SPECS

    def __post_init__(self):
        roots = tuple(self.approved_roots)
        trusted = tuple(self.trusted_executables)
        for root in roots:
            problem = _path_problem(root, strict=False)
            if problem:
                raise ProvisionError(f"approved root {root!r}: {problem}")
            absolute = os.path.abspath(root)
            if os.path.dirname(absolute) == absolute:
                raise ProvisionError(
                    f"approved root {root!r} is a filesystem root; name a directory")
        for exe in trusted:
            problem = _path_problem(exe, strict=False)
            if problem:
                raise ProvisionError(f"trusted executable {exe!r}: {problem}")
        object.__setattr__(self, "approved_roots", roots)
        object.__setattr__(self, "trusted_executables", trusted)


def policy_for_probe(probe: "HostProbe", approved_roots: Sequence[str],
                     **kw) -> ProvisionPolicy:
    """A policy that trusts exactly the probed interpreter by path.

    The probe's ``python_executable`` is normally an absolute path outside
    every approved root; naming it is only admissible because the operator
    built the policy from this probe on purpose.
    """
    trusted = tuple(kw.pop("trusted_executables", ()))
    if os.path.dirname(probe.python_executable):
        trusted += (probe.python_executable,)
    return ProvisionPolicy(approved_roots=tuple(approved_roots),
                           trusted_executables=trusted, **kw)


# Never admissible, whatever the arguments. The allowlist already refuses
# them; this table exists so the refusal names the actual danger.
_DENIED = {
    "sudo": "privilege escalation", "su": "privilege escalation",
    "doas": "privilege escalation", "runas": "privilege escalation",
    "pkexec": "privilege escalation",
    "rm": "deletion", "rmdir": "deletion", "rd": "deletion", "del": "deletion",
    "erase": "deletion", "shred": "deletion", "unlink": "deletion",
    "format": "disk formatting", "mkfs": "disk formatting",
    "dd": "raw disk writes", "diskpart": "disk partitioning",
    "fdisk": "disk partitioning", "parted": "disk partitioning",
    "reg": "registry edits", "regedit": "registry edits",
    "regedt32": "registry edits", "setx": "persistent environment edits",
    "powershell": "shell interpreter", "pwsh": "shell interpreter",
    "cmd": "shell interpreter", "sh": "shell interpreter",
    "bash": "shell interpreter", "zsh": "shell interpreter",
    "fish": "shell interpreter", "dash": "shell interpreter",
    "ksh": "shell interpreter", "csh": "shell interpreter",
    "wsl": "shell interpreter", "eval": "shell interpreter",
    "xargs": "command construction", "env": "command construction",
    "curl": "arbitrary download", "wget": "arbitrary download",
    "certutil": "arbitrary download", "bitsadmin": "arbitrary download",
    "chmod": "permission changes", "chown": "permission changes",
    "icacls": "permission changes", "takeown": "permission changes",
    "shutdown": "power state", "reboot": "power state",
    "bcdedit": "boot configuration", "netsh": "network configuration",
    "schtasks": "scheduled tasks", "sc": "service configuration",
    "wmic": "system management", "mshta": "script host",
    "rundll32": "script host", "taskkill": "process termination",
    "kill": "process termination",
}

_PYTHON_RE = re.compile(r"^(?:python[0-9.]*|py|pypy[0-9.]*)$")
_PIP_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:\[[A-Za-z0-9._,\-]+\])?"
    r"(?:(?:==|~=|>=|<=|!=|<|>)[A-Za-z0-9.*+!_\-]+)?$")
_NPM_SPEC_RE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._\-]*/)?[A-Za-z0-9][A-Za-z0-9._\-]*"
    r"(?:@[A-Za-z0-9][A-Za-z0-9.+\-]*)?$")
# One positive grammar per manager: id characters only, no extensions that
# make an id look like a file, no tap/path/URL forms, no removal syntax.
_SYSTEM_ID_RES = {
    "winget": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$"),
    "choco": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$"),
    "scoop": re.compile(
        r"^(?:[A-Za-z0-9][A-Za-z0-9._\-]*/)?[A-Za-z0-9][A-Za-z0-9._\-]*$"),
    "brew": re.compile(r"^[a-z0-9][a-z0-9@._+\-]*$"),
    # apt: a trailing '-' removes and a trailing '+' forces; '=' and '/' pick
    # a version or release. None of those is a plain install.
    "apt": re.compile(r"^[a-z0-9](?:[a-z0-9.+\-]*[a-z0-9.])?$"),
}
_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+_\-]{0,63}$")
_REGISTRY_RE = re.compile(r"^(?:HK[A-Z_]{1,20}|Registry::)[\\:]", re.I)


@dataclass(frozen=True)
class Admission:
    """The allowlist's verdict on one argv: the least class it may carry."""

    minimum_class: str
    requires_network: bool
    rule: str
    # Directories the step reads or writes, and interpreters it names inside
    # a root; ``validate_plan`` binds both to ``plan.root``.
    paths: Tuple[str, ...] = ()
    leaf_paths: Tuple[str, ...] = ()


_UNSAFE_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))


def _unsafe_chars(text: Any) -> List[str]:
    """Control, format (bidi / zero-width), surrogate, private and line chars."""
    if not isinstance(text, str):
        return []
    return sorted({ch for ch in text
                   if unicodedata.category(ch) in _UNSAFE_CATEGORIES})


# Characters that can read as a path separator or drive colon to a human (or
# a best-fit conversion) while being ordinary letters to Python.
_PATH_CONFUSABLES = frozenset(
    "\uff3c\uff0f\u2215\u2216\u29f5\u29f8\ufe68\u2044\uff1a\ufe55\u2236\u2571\u2572")
_DEVICE_NAMES = frozenset(
    ("con", "prn", "aux", "nul", "conin$", "conout$")
    + tuple(f"com{n}" for n in range(1, 10))
    + tuple(f"lpt{n}" for n in range(1, 10)))
_SHORT_NAME_RE = re.compile(r"^[^.~]{1,6}~[0-9]+(?:\.[^.]{0,3})?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _path_problem(path: Any, strict: bool = True) -> Optional[str]:
    """Why ``path`` cannot be used as an absolute, plain filesystem path.

    ``strict`` additionally refuses Windows 8.3 short names; policy roots are
    exempt (a CI temp dir is routinely ``RUNNER~1``) because they are only
    ever compared by realpath, never accepted from a plan.
    """
    if not isinstance(path, str) or not path.strip():
        return "must be a non-empty string"
    if "\0" in path:
        return "contains a NUL"
    if _unsafe_chars(path):
        return "contains control or invisible characters"
    if _PATH_CONFUSABLES.intersection(path):
        return "contains a look-alike path separator"
    if path.startswith("\\\\") or path.startswith("//"):
        return "UNC and device paths are not accepted"
    if not os.path.isabs(path):
        return ("must be absolute (a relative path would resolve against "
                "whatever directory the step runs in)")
    if ":" in _DRIVE_RE.sub("", path, count=1):
        return "alternate data stream / device syntax (':') is not accepted"
    for part in re.split(r"[\\/]+", path):
        if not part or part in (".", ".."):
            continue
        if part[-1] in ". ":
            return f"component {part!r} ends in a dot or space (Windows strips it)"
        if part.split(".")[0].rstrip(" ").lower() in _DEVICE_NAMES:
            return f"component {part!r} is a reserved device name"
        if strict and _SHORT_NAME_RE.match(part):
            return f"component {part!r} looks like an 8.3 short name"
    return None


# Characters cmd.exe (and so any batch shim) treats as syntax. No step needs
# them, so none may appear in any argv element. The comparison operators of a
# pip requirement (``>=``, ``!=``) are the one legitimate use, allowed only
# inside a token that fully matches the strict requirement grammar.
_CMD_META = frozenset('&|^%"!<>\n\r\0')
_SPEC_OPERATOR_CHARS = frozenset("!<>")
# Words a lenient option parser reads as the VALUE of the flag before them
# (``--ignore-scripts false`` turns the protection off).
_BOOL_WORDS = frozenset((
    "true", "false", "yes", "no", "on", "off", "0", "1", "null", "undefined"))
# A package spec is a registry name, never a file. pip builds ANY local
# archive it finds even with --only-binary, so the whole archive family is
# listed (pip's own set plus siblings and OS packages). These are refused in
# every manager, as the whole token or as its name/version part.
_ARCHIVE_SUFFIXES = (
    ".whl", ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2",
    ".tar.xz", ".txz", ".tlz", ".tar.lz", ".tar.lzma", ".tar.z", ".gz",
    ".bz2", ".xz", ".lz", ".lzma", ".z", ".egg", ".7z", ".rar", ".cab",
    ".iso", ".img", ".deb", ".rpm", ".nupkg", ".msi", ".msix", ".exe",
    ".appx", ".dmg", ".pkg", ".apk", ".jar", ".snap", ".appimage")
# Manifests and scripts: the local-file form of a system manager's spec
# (choco .config/.nuspec, scoop .json/.yml, brew .rb). pip and npm do not
# read them as specs and real names end this way (ruamel.yaml), so only the
# system managers check them.
_FILE_SUFFIXES = (
    ".nuspec", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".config",
    ".xml", ".txt", ".rb", ".sh", ".ps1", ".psm1", ".psd1", ".vbs")
_WINDOWS_SHIM_SUFFIXES = (".cmd", ".bat")
_SPEC_PART_SPLIT = re.compile(r"[=<>!~@\[\];,\s]")
_BARE_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+\-]*[A-Za-z0-9+\-])?$")


def _looks_like_artifact(text: str, git: bool = True, files: bool = True) -> bool:
    """A local file / repo, not a registry name: installing it runs unreviewed
    bytes. Checks the whole token AND its name and version parts, so
    ``evil.whl==1`` and ``evil==1.0.tbz`` are both caught. ``git`` also
    refuses a ``.git`` suffix (a repository); winget ids such as ``Git.Git``
    are real names and turn it off.
    """
    lowered = str(text).lower()
    whole = (_ARCHIVE_SUFFIXES + (_FILE_SUFFIXES if files else ())
             + ((".git",) if git else ()))
    if lowered.endswith(whole):
        return True
    return any(part.endswith(_ARCHIVE_SUFFIXES)
               for part in _SPEC_PART_SPLIT.split(lowered) if part)


def _is_bare_name(token: str) -> bool:
    return not any(ch in token for ch in ("/", "\\", ":"))


def _check_executable(argv0: str, policy: "ProvisionPolicy"):
    """argv[0]: a plain PATH name, a trusted interpreter, or inside a root."""
    if argv0.lower().endswith(_WINDOWS_SHIM_SUFFIXES):
        raise ProvisionError(
            f"{argv0!r} is a cmd.exe batch shim; shims re-parse their "
            "arguments and are never run")
    if _is_bare_name(argv0):
        if not _BARE_NAME_RE.match(argv0):
            raise ProvisionError(
                f"executable name {argv0!r} is not a plain ASCII program name")
        return
    problem = _path_problem(argv0)
    if problem:
        raise ProvisionError(f"executable path {argv0!r}: {problem}")
    if _within_roots_leaf(argv0, policy):
        return
    if any(osal.same_path(argv0, trusted) for trusted in policy.trusted_executables):
        return
    raise ProvisionError(
        f"executable path {argv0!r} is outside every approved root and is not "
        "a trusted executable (use a bare name, or policy_for_probe)")


def _exe_name(token: str) -> str:
    base = osal._ascii_fold(os.path.basename(token.replace("\\", "/")))
    for suffix in (".exe", ".cmd", ".bat"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _within_roots(path: str, policy: ProvisionPolicy) -> bool:
    return any(osal.is_within(path, root) for root in policy.approved_roots)


def _within_leaf(path: str, root: str) -> bool:
    """``path`` sits directly in a directory inside ``root``.

    For an interpreter: a venv's ``bin/python`` is a symlink to the base
    interpreter on POSIX, so its realpath is outside the venv. What must stay
    inside the root is the DIRECTORY it lives in (a swapped directory
    junction/symlink resolves out and is refused); the leaf may be a link.
    """
    return osal.is_within(os.path.dirname(path), root)


def _within_roots_leaf(path: str, policy: ProvisionPolicy) -> bool:
    return any(_within_leaf(path, root) for root in policy.approved_roots)


def _require_in_roots(path: str, policy: ProvisionPolicy, what: str):
    if not policy.approved_roots:
        raise ProvisionError(f"{what} {path!r}: no approved root is configured")
    problem = _path_problem(path)
    if problem:
        raise ProvisionError(f"{what} {path!r}: {problem}")
    if not _within_roots(path, policy):
        raise ProvisionError(
            f"{what} {path!r} is outside every approved root "
            f"{list(policy.approved_roots)}")


def _split_flags(args: Sequence[str], valued: Mapping[str, str],
                 boolean: Iterable[str], where: str, specs_first: bool = False):
    """Split ``args`` into ({flag: value}, [flag...], [positionals]).

    ``valued`` flags consume one value token, ``boolean`` flags none; any
    other ``-x`` token is refused -- an unknown flag is an unreviewed
    capability (``--index-url``, ``--user``, ``-r`` ...). With
    ``specs_first`` every positional must precede every flag, so no bare
    word can follow a boolean flag (a lenient option parser would swallow
    ``--ignore-scripts false`` as the flag's value and switch it off).
    """
    booleans = set(boolean)
    values: Dict[str, str] = {}
    flags: List[str] = []
    positionals: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-"):
            name, eq, inline = token.partition("=")
            if name in valued:
                if eq:
                    value = inline
                else:
                    index += 1
                    if index >= len(args):
                        raise ProvisionError(f"{where}: {name} needs a value")
                    value = args[index]
                if name in values:
                    raise ProvisionError(f"{where}: {name} was given twice")
                values[name] = value
            elif token in booleans:
                flags.append(token)
            else:
                raise ProvisionError(f"{where}: flag {token!r} is not allowed")
        else:
            if specs_first and (values or flags):
                raise ProvisionError(
                    f"{where}: {token!r} must come before the flags")
            positionals.append(token)
        index += 1
    return values, flags, positionals


def _check_plain(value: str, pattern, where: str, git: bool = True,
                 files: bool = True):
    """One package id / spec: registry-shaped, never a flag word or a file."""
    if value.lower() in _BOOL_WORDS:
        raise ProvisionError(
            f"{where}: {value!r} is a boolean word, not a package (a lenient "
            "option parser would read it as a flag value)")
    if _looks_like_artifact(value, git, files):
        raise ProvisionError(
            f"{where}: {value!r} looks like a local file; only registry "
            "package names are installed")
    if not pattern.match(value):
        raise ProvisionError(
            f"{where}: package spec {value!r} is not a plain "
            "name[==version] (no URLs, paths or VCS references)")


def _check_specs(specs: Sequence[str], pattern, where: str,
                 policy: Optional["ProvisionPolicy"] = None, git: bool = True,
                 files: bool = True):
    if not specs:
        raise ProvisionError(f"{where}: at least one package is required")
    limit = policy.max_specs if policy is not None else DEFAULT_MAX_SPECS
    if len(specs) > limit:
        raise ProvisionError(
            f"{where}: {len(specs)} packages exceeds the limit of {limit}")
    for spec in specs:
        _check_plain(spec, pattern, where, git, files)


def _admit_python(argv, policy):
    exe, rest = argv[0], list(argv[1:])
    if rest in (["--version"], ["-V"]):
        return Admission(READ, False, "python-version")
    # Anything that runs code runs ISOLATED (-I): no working-directory or
    # sys.path[0] module shadowing, no PYTHON* environment, no user site.
    if rest[:3] == ["-I", "-m", "venv"]:
        if len(rest) != 4 or rest[3].startswith("-"):
            raise ProvisionError(
                "python -I -m venv takes exactly one directory and no flags")
        _require_in_roots(rest[3], policy, "venv directory")
        return Admission(MUTATING, False, "venv", (rest[3],))
    if rest[:3] == ["-I", "-m", "pip"] and len(rest) >= 4:
        return _admit_pip(exe, rest[3:], policy)
    raise ProvisionError(
        "python may only run '-I -m venv <dir>', '-I -m pip ...' or "
        "'--version' (always isolated; no -c, no scripts, no other modules)")


def _admit_pip(exe, rest, policy):
    where = "pip"
    if not rest:
        raise ProvisionError("pip needs a subcommand")
    sub, args = rest[0], list(rest[1:])
    if sub == "--version":
        if args:
            raise ProvisionError("pip --version takes no further arguments")
        return Admission(READ, False, "pip-version")
    if sub == "list":
        _, _, extra = _split_flags(
            args, {}, ("--format=json", "--disable-pip-version-check",
                       "--isolated"), where)
        if extra:
            raise ProvisionError("pip list takes no packages")
        return Admission(READ, False, "pip-list")
    if sub == "show":
        _, _, specs = _split_flags(
            args, {}, ("--disable-pip-version-check", "--isolated"), where)
        _check_specs(specs, _PIP_SPEC_RE, where + " show", policy, True, False)
        return Admission(READ, False, "pip-show")
    if sub != "install":
        raise ProvisionError(f"pip {sub!r} is not allowed (install, list, show only)")
    boolean = ["--no-input", "--disable-pip-version-check", "--no-deps",
               "--upgrade", "-U", "--quiet", "-q", "--only-binary=:all:",
               "--isolated"]
    values, flags, specs = _split_flags(args, {"--target": "dir"}, boolean, where)
    _check_specs(specs, _PIP_SPEC_RE, where + " install", policy, True, False)
    if "--isolated" not in flags:
        raise ProvisionError(
            "pip install must pass --isolated (ignores user/site pip config "
            "and PIP_* environment)")
    if not policy.allow_source_builds and "--only-binary=:all:" not in flags:
        raise ProvisionError(
            "pip install must pass --only-binary=:all: (source builds run "
            "arbitrary build scripts)")
    in_root_interpreter = os.path.dirname(exe) != "" and _within_roots_leaf(exe, policy)
    if "--target" in values:
        _require_in_roots(values["--target"], policy, "pip --target")
    elif not in_root_interpreter:
        raise ProvisionError(
            "pip install must target an approved root: use the interpreter of "
            "a venv inside the root, or pass --target <dir in root>")
    paths = (values["--target"],) if "--target" in values else ()
    return Admission(MUTATING, True, "pip-install", paths,
                     (exe,) if in_root_interpreter else ())


def _admit_npm(rest, policy):
    where = "npm"
    if not rest:
        raise ProvisionError("npm needs a subcommand")
    sub, args = rest[0], list(rest[1:])
    if sub == "--version":
        if args:
            raise ProvisionError("npm --version takes no further arguments")
        return Admission(READ, False, "npm-version")
    if sub in ("ls", "list"):
        values, _, specs = _split_flags(args, {"--prefix": "dir"}, ("--depth=0",),
                                        where, specs_first=True)
        if "--prefix" not in values:
            raise ProvisionError("npm ls must pass --prefix <dir in root>")
        _require_in_roots(values["--prefix"], policy, "npm --prefix")
        if specs:
            _check_specs(specs, _NPM_SPEC_RE, where + " ls", policy, True, False)
        return Admission(READ, False, "npm-ls", (values["--prefix"],))
    if sub not in ("install", "i"):
        raise ProvisionError(f"npm {sub!r} is not allowed (install, ls only)")
    values, flags, specs = _split_flags(
        args, {"--prefix": "dir"},
        ("--ignore-scripts", "--no-audit", "--no-fund", "--save-exact"), where,
        specs_first=True)
    if "--prefix" not in values:
        raise ProvisionError("npm install must pass --prefix <dir in root> (no global installs)")
    _require_in_roots(values["--prefix"], policy, "npm --prefix")
    if "--ignore-scripts" not in flags:
        raise ProvisionError(
            "npm install must pass --ignore-scripts (install scripts run "
            "arbitrary code)")
    _check_specs(specs, _NPM_SPEC_RE, where + " install", policy, True, False)
    return Admission(MUTATING, True, "npm-install", (values["--prefix"],))


_PACKAGE_SOURCES = ("winget", "msstore")


def _check_source(values, where):
    """A package source is a closed vocabulary, never a URL or a flag word."""
    source = values.get("--source")
    if source is not None and source not in _PACKAGE_SOURCES:
        raise ProvisionError(
            f"{where}: --source {source!r} is not one of {list(_PACKAGE_SOURCES)}")


def _admit_system(name, rest, policy):
    where = name
    key = "apt" if name == "apt-get" else name
    id_re = _SYSTEM_ID_RES[key]
    git = key != "winget"  # winget ids such as Git.Git are real names
    if not rest:
        raise ProvisionError(f"{name} needs a subcommand")
    sub, args = rest[0], list(rest[1:])
    if sub == "--version":
        if args:
            raise ProvisionError(f"{name} --version takes no further arguments")
        return Admission(READ, False, f"{name}-version")
    if sub in {"list", "show", "info"}:
        values, _, specs = _split_flags(
            args, {"--id": "id", "--source": "src"},
            ("--installed", "--local-only", "-e", "--exact"), where)
        _check_source(values, where)
        for value in [v for k, v in values.items() if k == "--id"] + list(specs):
            _check_plain(value, id_re, f"{where} {sub}", git)
        return Admission(READ, False, f"{name}-{sub}")
    if sub != "install":
        raise ProvisionError(f"{name} {sub!r} is not allowed (install and queries only)")
    if not policy.allow_system_install:
        raise ProvisionError(
            f"{name} install changes the host outside the approved root and "
            "this policy forbids system installs")
    if name == "winget":
        values, _, specs = _split_flags(
            args, {"--id": "id", "--version": "ver", "--source": "src"},
            ("-e", "--exact"), where)
        if "--id" not in values or specs:
            raise ProvisionError("winget install takes --id <package id> and no bare names")
        _check_source(values, where)
        packages = [values["--id"]]
    else:
        values, _, packages = _split_flags(
            args, {"--version": "ver"}, ("-y", "--yes"), where)
    _check_specs(packages, id_re, where + " install", policy, git)
    version = values.get("--version")
    if version is not None and not _VERSION_RE.match(version):
        raise ProvisionError(f"{where} install: version {version!r} is not plain")
    return Admission(IRREVERSIBLE, True, f"{name}-install")


def _admit_mkdir(argv, policy):
    if len(argv) != 2 or argv[1].startswith("-"):
        raise ProvisionError("mkdir takes exactly one directory and no flags")
    _require_in_roots(argv[1], policy, "mkdir directory")
    return Admission(MUTATING, False, "mkdir", (argv[1],))


def classify_argv(argv: Sequence[str], policy: ProvisionPolicy) -> Admission:
    """Admit an argv or raise: the single allowlist every step passes.

    Returns the LEAST class the argv may carry. A planner may label a step
    more severe than this (never less), and may not hide network use.
    """
    if isinstance(argv, (str, bytes)):
        raise ProvisionError("argv must be a list of arguments, not a shell string")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ProvisionError("argv must be a non-empty list")
    if len(argv) > policy.max_argv:
        raise ProvisionError(
            f"argv has {len(argv)} elements; the limit is {policy.max_argv}")
    for token in argv:
        if not isinstance(token, str) or not token:
            raise ProvisionError("every argv element must be a non-empty string")
        if len(token) > policy.max_arg_chars:
            raise ProvisionError(
                f"an argv element is {len(token)} characters; the limit is "
                f"{policy.max_arg_chars}")
        syntax = _CMD_META.intersection(token)
        if syntax and not (not syntax - _SPEC_OPERATOR_CHARS
                           and _PIP_SPEC_RE.match(token)):
            raise ProvisionError(
                f"argv element {token!r} contains shell syntax "
                f"{sorted(syntax)!r}; no step needs it")
        if _unsafe_chars(token):
            raise ProvisionError(
                f"argv element {token!r} contains control or invisible characters")
        if _REGISTRY_RE.match(token):
            raise ProvisionError(f"registry path {token!r} is never allowed")
    if _is_bare_name(argv[0]) and not _BARE_NAME_RE.match(argv[0]):
        raise ProvisionError(
            f"executable name {argv[0]!r} is not a plain ASCII program name")
    name = _exe_name(argv[0])
    if name in _DENIED:
        raise ProvisionError(f"{argv[0]!r} is never allowed ({_DENIED[name]})")
    _check_executable(argv[0], policy)
    if name == "mkdir" and _is_bare_name(argv[0]):
        return _admit_mkdir(argv, policy)
    if _PYTHON_RE.match(name):
        return _admit_python(argv, policy)
    if name == "pip":
        return _admit_pip(argv[0], list(argv[1:]), policy)
    if name == "npm":
        return _admit_npm(list(argv[1:]), policy)
    if name in _SYSTEM_MANAGERS or name == "apt-get":
        return _admit_system(name, list(argv[1:]), policy)
    raise ProvisionError(
        f"{argv[0]!r} is not on the provisioning allowlist (package-manager "
        "install, venv, pip, npm, mkdir in an approved root, or a read-only query)")


def validate_step(step: Step, policy: ProvisionPolicy) -> Admission:
    """Validate one step against the allowlist and its own declarations."""
    problems: List[str] = []
    if not isinstance(step.id, str) or not _STEP_ID_RE.match(step.id):
        problems.append("id must be lowercase letters, digits, '-' or '_'")
    for text_field in ("description", "expected_effect"):
        value = getattr(step, text_field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{text_field} must be non-empty")
    for text_field in ("description", "expected_effect", "rollback_hint"):
        if _unsafe_chars(getattr(step, text_field)):
            problems.append(
                f"{text_field} contains control, line-break or invisible characters")
    if step.classification not in CLASSES:
        problems.append(
            f"classification {step.classification!r} is not one of {list(CLASSES)}")
    if not isinstance(step.requires_network, bool):
        problems.append("requires_network must be a boolean")
    if step.cwd is not None:
        problems.append(
            "steps run in a private empty directory the executor creates; "
            "cwd is not selectable")
    admission = None
    try:
        admission = classify_argv(step.argv, policy)
    except ProvisionError as exc:
        problems.append(str(exc))
    if admission is not None and step.classification in CLASSES:
        if _SEVERITY[step.classification] < _SEVERITY[admission.minimum_class]:
            problems.append(
                f"classified {step.classification} but the {admission.rule} "
                f"rule is at least {admission.minimum_class}")
        if admission.requires_network and not step.requires_network:
            problems.append(
                f"the {admission.rule} rule uses the network; "
                "requires_network must be true")
    if (step.classification in (MUTATING, IRREVERSIBLE)
            and not (isinstance(step.rollback_hint, str)
                     and step.rollback_hint.strip())):
        problems.append("a mutating step needs a rollback_hint (say so if there is none)")
    if problems:
        raise ProvisionError(f"step {step.id!r} rejected: " + "; ".join(problems),
                             problems)
    return admission


def validate_plan(plan: Plan, policy: ProvisionPolicy) -> Tuple[Admission, ...]:
    """Validate every step and the plan's own shape; raises with ALL defects."""
    problems: List[str] = []
    if not isinstance(plan.goal, str) or not plan.goal.strip():
        problems.append("goal must be non-empty")
    if _unsafe_chars(plan.goal):
        problems.append("goal contains control, line-break or invisible characters")
    if not plan.steps:
        problems.append("a plan needs at least one step")
    if len(plan.steps) > policy.max_steps:
        problems.append(f"{len(plan.steps)} steps exceeds the limit of {policy.max_steps}")
    if plan.root:
        try:
            _require_in_roots(plan.root, policy, "plan root")
        except ProvisionError as exc:
            problems.append(str(exc))
    elif any(step.classification != READ for step in plan.steps):
        problems.append("a plan with mutating steps must name its root")
    seen = set()
    admissions: List[Admission] = []
    for step in plan.steps:
        if step.id in seen:
            problems.append(f"duplicate step id {step.id!r}")
        seen.add(step.id)
        try:
            admission = validate_step(step, policy)
        except ProvisionError as exc:
            problems.extend(exc.problems or [str(exc)])
            continue
        admissions.append(admission)
        if plan.root:
            # Every path a step touches is bound to the plan's own root, so a
            # plan cannot advertise one directory and work in another.
            for path in admission.paths:
                if not osal.is_within(path, plan.root):
                    problems.append(
                        f"step {step.id!r}: {path!r} is outside the plan root")
            for path in admission.leaf_paths:
                if not _within_leaf(path, plan.root):
                    problems.append(
                        f"step {step.id!r}: {path!r} is outside the plan root")
    if problems:
        raise ProvisionError("plan rejected: " + "; ".join(problems), problems)
    return tuple(admissions)


def needs_approval(step: Step) -> bool:
    """Everything that mutates, or leaves the machine, needs a recorded yes."""
    return step.classification != READ or step.requires_network


# --------------------------------------------------------------------------
# approval gate (decline / defer are first-class)
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ApprovalRecord:
    """One recorded decision, bound to the exact content it covers."""

    decision: str
    scope: str
    plan_digest: str
    step_id: Optional[str]
    step_digest: Optional[str]
    approver: str
    reason: str
    ts: str

    def to_dict(self) -> Dict[str, Any]:
        return {"decision": self.decision, "scope": self.scope,
                "plan_digest": self.plan_digest, "step_id": self.step_id,
                "step_digest": self.step_digest, "approver": self.approver,
                "reason": self.reason, "ts": self.ts}


class ApprovalGate:
    """The record of who said yes, no, or not-now to what.

    Approvals are bound to the plan's content digest (and, per step, the
    step's digest), so a plan edited after approval simply has no matching
    record and nothing runs. A plan-level approve covers READ and MUTATING
    steps; an IRREVERSIBLE step needs its own step-level approve. Plan-level
    decline / defer is terminal until a later plan-level record replaces it.

    ``ask`` is the optional interactive path: ``ask(plan, step)`` returns
    ``(decision, reason)`` or ``None``. Anything malformed, or an exception,
    is recorded as a DEFER -- ambiguity never becomes consent.
    """

    def __init__(self, ledger=None, task_id: Optional[str] = None,
                 ask: Optional[Callable] = None, ask_approver: str = "interactive"):
        self.ledger = ledger
        self.task_id = task_id
        self.ask = ask
        self.ask_approver = ask_approver
        self._records: List[ApprovalRecord] = []

    @property
    def records(self) -> Tuple[ApprovalRecord, ...]:
        """Read-only view: callers add decisions only through :meth:`record`."""
        return tuple(self._records)

    def record(self, plan: Plan, decision: str, *, approver: str,
               step_id: Optional[str] = None, reason: str = "") -> ApprovalRecord:
        if decision not in DECISIONS:
            raise ProvisionError(
                f"decision {decision!r} is not one of {list(DECISIONS)}")
        if not isinstance(approver, str) or not approver.strip():
            raise ProvisionError("an approval needs a named approver")
        step = plan.step(step_id) if step_id is not None else None
        if (step is None and decision == APPROVE and plan.review_required):
            raise ProvisionError(
                "this plan was flagged by review: approve its steps one at a "
                "time instead of the whole plan")
        entry = ApprovalRecord(
            decision, "step" if step is not None else "plan", plan.digest,
            step.id if step is not None else None,
            step.digest() if step is not None else None,
            approver.strip(), str(reason or ""), _now())
        if self.ledger is not None:
            fields = entry.to_dict()
            # The ledger stamps its own ``ts``; keep the decision time apart.
            fields["decided_at"] = fields.pop("ts")
            # Ledger FIRST: if the evidence cannot be written, no approval
            # exists. A consent that was never recorded must not be usable.
            self.ledger.append("provision_approval", task_id=self.task_id,
                               **fields)
        self._records.append(entry)
        return entry

    def approve_plan(self, plan, approver, reason=""):
        return self.record(plan, APPROVE, approver=approver, reason=reason)

    def decline_plan(self, plan, approver, reason=""):
        return self.record(plan, DECLINE, approver=approver, reason=reason)

    def defer_plan(self, plan, approver, reason=""):
        return self.record(plan, DEFER, approver=approver, reason=reason)

    def approve_step(self, plan, step_id, approver, reason=""):
        return self.record(plan, APPROVE, approver=approver,
                           step_id=step_id, reason=reason)

    def decline_step(self, plan, step_id, approver, reason=""):
        return self.record(plan, DECLINE, approver=approver,
                           step_id=step_id, reason=reason)

    def defer_step(self, plan, step_id, approver, reason=""):
        return self.record(plan, DEFER, approver=approver,
                           step_id=step_id, reason=reason)

    def resolve(self, plan: Plan, step: Step) -> Optional[ApprovalRecord]:
        """The decision that governs ``step`` now, or ``None`` (no consent)."""
        digest = plan.digest
        plan_level = None
        step_level = None
        for entry in self._records:
            if entry.plan_digest != digest:
                continue
            if entry.scope == "plan":
                plan_level = entry
            elif entry.step_id == step.id and entry.step_digest == step.digest():
                step_level = entry
        if plan_level is not None and plan_level.decision != APPROVE:
            return plan_level
        if step_level is not None:
            return step_level
        if plan_level is not None and step.classification != IRREVERSIBLE:
            return plan_level
        return None

    def resolve_or_ask(self, plan: Plan, step: Step) -> Optional[ApprovalRecord]:
        found = self.resolve(plan, step)
        if found is not None or self.ask is None:
            return found
        try:
            answer = self.ask(plan, step)
        except Exception as exc:
            return self.record(
                plan, DEFER, approver=self.ask_approver, step_id=step.id,
                reason=f"approval prompt failed: {type(exc).__name__}")
        if answer is None:
            return None
        try:
            decision, reason = answer
        except (TypeError, ValueError):
            decision, reason = None, "malformed approval answer"
        if decision not in DECISIONS:
            return self.record(
                plan, DEFER, approver=self.ask_approver, step_id=step.id,
                reason=reason if decision is None else
                f"unrecognised decision {decision!r}")
        return self.record(plan, decision, approver=self.ask_approver,
                           step_id=step.id, reason=str(reason or ""))


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StepResult:
    step_id: str
    status: str
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    approval: Optional[str] = None
    approver: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "status": self.status,
                "returncode": self.returncode, "stdout": self.stdout,
                "stderr": self.stderr, "duration_s": self.duration_s,
                "approval": self.approval, "approver": self.approver,
                "note": self.note}


@dataclass(frozen=True)
class ExecutionReport:
    plan_digest: str
    dry_run: bool
    outcome: str
    results: Tuple[StepResult, ...]
    rollback_hints: Tuple[Tuple[str, str], ...] = ()
    # True when the ledger could not record something. The report is still
    # returned (a step that ran must never lose its result) and the run stops.
    audit_failed: bool = False

    @property
    def resumable(self) -> bool:
        """A deferred or still-unapproved run can continue where it stopped."""
        return self.outcome in (DEFERRED, AWAITING_APPROVAL)

    def completed_ids(self) -> Tuple[str, ...]:
        return tuple(r.step_id for r in self.results
                     if r.status in (COMPLETED, ALREADY_DONE))

    def to_dict(self) -> Dict[str, Any]:
        return {"plan_digest": self.plan_digest, "dry_run": self.dry_run,
                "outcome": self.outcome,
                "results": [r.to_dict() for r in self.results],
                "rollback_hints": [list(h) for h in self.rollback_hints],
                "resumable": self.resumable, "audit_failed": self.audit_failed}


def _tail(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else "...[truncated]\n" + text[-limit:]


def _builtin_mkdir(step: Step, policy: ProvisionPolicy) -> osal.CommandResult:
    target = step.argv[1]
    try:
        # Re-check at the moment of creation: the tree may have changed
        # (a link planted in the root) since the plan was reviewed.
        _require_in_roots(target, policy, "mkdir directory")
        os.makedirs(target, exist_ok=True)
    except (OSError, ProvisionError) as exc:
        return osal.CommandResult(1, "", f"mkdir failed: {exc}")
    return osal.CommandResult(0, f"directory ready: {target}\n", "")


# Config an executed step must not inherit: pip/npm/uv settings (index URLs,
# scripts policy), interpreter and loader hooks, TLS trust and proxy
# redirection. Matching is case-insensitive (Windows environments are).
_ENV_DROP_PREFIXES = ("PIP_", "NPM_", "PYTHON", "NODE_", "LD_", "DYLD_", "UV_",
                      "SSL_CERT_")
_ENV_DROP_NAMES = frozenset((
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"))


def _scrubbed_env(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A clean environment for an executed step.

    Dropped: every pip / npm / uv / python / node / loader variable, proxies
    and CA-bundle overrides. Set: explicit defaults. pip's user and site
    config files are NOT ignored by the environment -- the allowlist makes
    ``pip install`` carry ``--isolated`` for that. npm's user and global
    config are pointed at an empty file; a project ``.npmrc`` inside the
    ``--prefix`` directory is the one config npm still reads, which is why
    steps run from an empty private directory and the prefix lives in a
    root the operator controls.
    """
    source = dict(os.environ if environ is None else environ)
    env = {key: value for key, value in source.items()
           if not key.upper().startswith(_ENV_DROP_PREFIXES)
           and key.upper() not in _ENV_DROP_NAMES}
    env.update({
        "PIP_CONFIG_FILE": os.devnull,       # belt; --isolated is the control
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "NPM_CONFIG_USERCONFIG": os.devnull,  # empty user config
        "NPM_CONFIG_GLOBALCONFIG": os.devnull,
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "PYTHONNOUSERSITE": "1",
    })
    return env


def _in_working_directory(path: str) -> bool:
    """``path`` is in, or under, the process's current directory.

    Windows resolves bare names (and ``shutil.which``) against the current
    directory first, so a program found there was planted, not installed. A
    filesystem-root cwd would exclude the whole drive, so only its own
    directory counts then.
    """
    here = os.path.abspath(os.getcwd())
    if osal.same_path(os.path.dirname(os.path.abspath(path)), here):
        return True
    at_fs_root = os.path.dirname(here) == here
    return (not at_fs_root) and osal.is_within(path, here)


def _resolve_command(step: Step, which: Callable) -> Tuple[Optional[List[str]], str]:
    """The argv to run, with a bare executable name pinned to a real path.

    A bare name is resolved here, once, and the ABSOLUTE resolved path is
    what runs -- never the name, which the OS could resolve against the
    working directory. A name that resolves to a relative path, anywhere in
    the process's working tree, or to a batch shim, is refused: the argv's
    shape was reviewed, so the program must be too.
    """
    exe = step.argv[0]
    if not _is_bare_name(exe):
        return list(step.argv), ""  # a path: already confined by the allowlist
    found = which(exe)
    if not found:
        return None, f"executable {exe!r} was not found on PATH"
    found = str(found)
    if not os.path.isabs(found):
        return None, f"{exe!r} resolved to a relative path ({found}); refusing it"
    if found.lower().endswith(_WINDOWS_SHIM_SUFFIXES):
        return None, f"{exe!r} resolves to a batch shim ({found}); refusing to run it"
    if _in_working_directory(found):
        return None, (f"{exe!r} resolves to {found}, inside the working "
                      "directory; refusing a planted executable")
    return [found] + list(step.argv[1:]), ""


def _is_builtin_mkdir(step: Step) -> bool:
    return (_exe_name(step.argv[0]) == "mkdir"
            and _is_bare_name(step.argv[0]))


def _require_real_ledger(ledger) -> None:
    """A real run needs the harness's hash-chained ledger, with an intact chain.

    Any object with an ``append`` would do for a dry run's evidence; for a
    run that changes the host, a no-op or in-memory stand-in is no audit.
    """
    if not isinstance(ledger, AutonomyLedger):
        raise ProvisionError(
            "a real run needs the harness's hash-chained ledger "
            "(AutonomyLedger): an unaudited provisioning run is refused")
    ok, bad = ledger.verify()
    if not ok:
        raise ProvisionError(
            f"the ledger's hash chain does not verify (first bad seq {bad}); "
            "refusing to extend broken evidence")


def _private_cwd_parent(plan: Plan, policy: ProvisionPolicy) -> str:
    """The approved root that hosts every step's private working directory."""
    roots = policy.approved_roots
    if not roots:
        raise ProvisionError(
            "a real run needs an approved root to host each step's private "
            "working directory")
    base = next((r for r in roots if plan.root and osal.is_within(plan.root, r)),
                roots[0])
    if not os.path.isdir(base):
        raise ProvisionError(f"approved root {base!r} does not exist")
    return base


def execute_plan(plan: Plan, gate: ApprovalGate, *, policy: ProvisionPolicy,
                 ledger=None, task_id: Optional[str] = None,
                 dry_run: bool = True, runner: Optional[Callable] = None,
                 done: Iterable[str] = (), which: Optional[Callable] = None,
                 environ: Optional[Mapping[str, str]] = None) -> ExecutionReport:
    """Run (or, by default, only preview) a validated plan.

    ``dry_run=True`` is the default and executes NOTHING -- not a command,
    not a mkdir; it reports each step's would-run argv and approval state.
    ``dry_run`` must be exactly ``True`` or ``False``: a falsy-looking
    ``None`` / ``0`` / ``""`` is a caller bug, refused rather than guessed
    into a real run.

    A real run requires ``dry_run=False``, the harness's own verified
    :class:`AutonomyLedger`, and a recorded approval for every step that
    mutates or uses the network. Each step then runs like this:

    * the allowlist, the paths and the executable are re-validated
      IMMEDIATELY before it runs (a directory swapped for a junction since
      the plan was reviewed is refused);
    * a ``provision_step_start`` entry is chained BEFORE it runs;
    * it runs in a FRESH, EMPTY, private directory the executor creates
      inside the approved root -- never the process's directory -- so a
      bare filename can only name a registry package, a module can only come
      from the interpreter, and a program can only come from PATH;
    * with an explicit scrubbed environment (:func:`_scrubbed_env`) and a
      bare executable name pinned to its absolute PATH-resolved path.

    The first decline, defer, missing approval or failure stops the run;
    later steps are reported ``skipped`` and the report carries rollback
    hints for what already changed. If the ledger fails after a step ran the
    report is still returned, with ``audit_failed=True``, and the run stops.
    ``done`` lists step ids a previous (deferred) run already completed;
    ``which`` / ``environ`` are substitution seams for tests.
    """
    if dry_run is not True and dry_run is not False:
        raise ProvisionError(
            f"dry_run must be True or False, not {dry_run!r}")
    if not dry_run:
        _require_real_ledger(ledger)
    validate_plan(plan, policy)
    base_root = None if dry_run else _private_cwd_parent(plan, policy)
    run = runner or osal.run
    find = which or osal.which
    finished = set(done)
    digest = plan.digest
    state = {"stop": None, "audit": False}

    def record_event(event, **fields) -> bool:
        """Append one entry; a ledger failure is recorded, never swallowed."""
        if ledger is None:
            return True
        if state["audit"]:
            return False
        try:
            ledger.append(event, task_id=task_id, plan_digest=digest, **fields)
        except Exception:  # the evidence store failed: stop, keep the result
            state["audit"] = True
            return False
        return True

    if ledger is not None:
        ledger.append(
            "provision_plan", task_id=task_id, plan_digest=digest,
            goal=plan.goal, recipe=plan.recipe, source=plan.source,
            is_fallback=plan.is_fallback, dry_run=dry_run, root=plan.root,
            steps=[{"id": s.id, "classification": s.classification,
                    "argv": list(s.argv), "requires_network": s.requires_network}
                   for s in plan.steps])

    results: List[StepResult] = []
    hints: List[Tuple[str, str]] = []
    changed: List[Step] = []

    def finish(step, status, **kw) -> bool:
        result = StepResult(step.id, status, **kw)
        results.append(result)
        logged = record_event(
            "provision_step", step_id=step.id,
            classification=step.classification, argv=list(step.argv),
            status=status, dry_run=dry_run, returncode=result.returncode,
            approval=result.approval, approver=result.approver,
            duration_s=result.duration_s, note=result.note,
            stdout=_tail(result.stdout, policy.max_output_chars),
            stderr=_tail(result.stderr, policy.max_output_chars),
            output_sha256=_sha256(result.stdout + "\x00" + result.stderr))
        if not logged and state["stop"] is None:
            state["stop"] = AUDIT_FAILED
        return logged

    for step in plan.steps:
        if state["stop"] is not None:
            finish(step, SKIPPED, note=f"run stopped: {state['stop']}")
            continue
        if step.id in finished:
            finish(step, ALREADY_DONE, note="completed by an earlier run")
            continue
        record = None
        if needs_approval(step):
            record = (gate.resolve(plan, step) if dry_run
                      else gate.resolve_or_ask(plan, step))
        approval = record.decision if record is not None else None
        approver = record.approver if record is not None else None
        if dry_run:
            if not needs_approval(step):
                note_state = "not required"
            else:
                note_state = {APPROVE: "approved", DECLINE: "declined",
                              DEFER: "deferred"}.get(approval, "missing")
            finish(step, DRY_RUN, approval=approval, approver=approver,
                   note=f"would run: {json.dumps(list(step.argv))} "
                        f"(approval: {note_state})")
            continue
        if needs_approval(step):
            if record is None:
                finish(step, AWAITING_APPROVAL,
                       note="no approval record; nothing was run")
                state["stop"] = AWAITING_APPROVAL
                continue
            if record.decision != APPROVE:
                status = DECLINED if record.decision == DECLINE else DEFERRED
                finish(step, status, approval=approval, approver=approver,
                       note=record.reason)
                state["stop"] = status
                continue

        # ---- re-validate everything, now, immediately before running ----
        builtin = _is_builtin_mkdir(step)
        try:
            validate_step(step, policy)
            command, refusal = ((list(step.argv), "") if builtin
                                else _resolve_command(step, find))
        except ProvisionError as exc:
            command, refusal = None, str(exc)
        started_logged = record_event(
            "provision_step_start", step_id=step.id,
            classification=step.classification, argv=list(step.argv),
            resolved=command, approval=approval, approver=approver,
            refused=refusal or None)
        if not started_logged:
            results.append(StepResult(
                step.id, AUDIT_FAILED, approval=approval, approver=approver,
                note="the ledger could not record the step start; not run"))
            state["stop"] = AUDIT_FAILED
            continue

        workdir = None
        leftover = ""
        started = time.monotonic()
        try:
            if command is None:
                outcome = osal.CommandResult(127, "", refusal)
            elif builtin:
                outcome = _builtin_mkdir(step, policy)
            else:
                workdir = tempfile.mkdtemp(prefix=".provision-cwd-", dir=base_root)
                if os.listdir(workdir):
                    outcome = osal.CommandResult(
                        126, "", "the private working directory is not empty; "
                        "refusing to run")
                else:
                    outcome = run(command, cwd=workdir,
                                  timeout=policy.step_timeout_s,
                                  env=_scrubbed_env(environ))
        except Exception as exc:  # a runner may fail in any way; keep the record
            outcome = osal.CommandResult(126, "", f"runner error: {exc}")
        finally:
            if workdir is not None:
                try:
                    os.rmdir(workdir)
                except OSError:
                    leftover = f"; the step left files in {workdir}"
        elapsed = round(time.monotonic() - started, 3)
        if outcome.returncode == 0:
            logged = finish(step, COMPLETED, returncode=0, stdout=outcome.stdout,
                            stderr=outcome.stderr, duration_s=elapsed,
                            approval=approval, approver=approver,
                            note=leftover.lstrip("; "))
            if step.classification != READ:
                changed.append(step)
        else:
            note = "timed out" if outcome.returncode == 124 else "non-zero exit"
            logged = finish(step, FAILED, returncode=outcome.returncode,
                            stdout=outcome.stdout, stderr=outcome.stderr,
                            duration_s=elapsed, approval=approval,
                            approver=approver, note=note + leftover)
            if state["stop"] is None:
                state["stop"] = FAILED
            if step.rollback_hint:
                hints.append((step.id, step.rollback_hint))
        if not logged:
            state["stop"] = AUDIT_FAILED

    if dry_run:
        outcome_name = DRY_RUN
    elif state["audit"]:
        outcome_name = AUDIT_FAILED
    elif state["stop"] is not None:
        outcome_name = state["stop"]
    else:
        outcome_name = COMPLETED
    if outcome_name in (FAILED, DECLINED, DEFERRED, AWAITING_APPROVAL,
                        AUDIT_FAILED):
        # A run that stopped part-way reports how to undo what it changed,
        # newest first. A finished run invites no undo nobody asked for.
        hints.extend((s.id, s.rollback_hint) for s in reversed(changed)
                     if s.rollback_hint)
    completed = [r.step_id for r in results
                 if r.status in (COMPLETED, ALREADY_DONE)]
    if not record_event("provision_end", outcome=outcome_name, dry_run=dry_run,
                        completed=completed,
                        rollback_hints=[list(h) for h in hints]):
        outcome_name = AUDIT_FAILED if not dry_run else outcome_name
    return ExecutionReport(digest, dry_run, outcome_name, tuple(results),
                           tuple(hints), audit_failed=state["audit"])


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PackageRequest:
    """One thing to install; ``ecosystem`` is python / node / system / None."""

    name: str
    version: Optional[str] = None
    ecosystem: Optional[str] = None

    def pip_spec(self) -> str:
        return f"{self.name}=={self.version}" if self.version else self.name

    def npm_spec(self) -> str:
        return f"{self.name}@{self.version}" if self.version else self.name


_PINNED_PY_RE = re.compile(
    r"(?<![\w@/.\-])([A-Za-z][A-Za-z0-9._\-]*)==([0-9][A-Za-z0-9.+!_\-]*)")
_PINNED_NODE_RE = re.compile(
    r"(?<![\w@/.\-])(@?[A-Za-z][A-Za-z0-9._\-]*(?:/[A-Za-z][A-Za-z0-9._\-]*)?)"
    r"@([0-9][A-Za-z0-9.+\-]*)")
_VERB_RE = re.compile(
    r"\b(?:install|provision|set\s*up|setup)\s+(?:(?:the|a|an|my)\s+)?"
    r"([A-Za-z][A-Za-z0-9._\-]*)", re.I)
_MAX_NAME_CHARS = 100
MAX_GOAL_CHARS = 4000
_STOPWORDS = frozenset((
    "the", "a", "an", "my", "it", "this", "that", "tool", "tools", "package",
    "packages", "dev", "development", "environment", "env", "venv", "pinned",
    "pip", "npm", "python", "node", "system", "globally", "in", "into", "for",
    "on", "to", "with", "and", "or", "using", "via", "from", "latest",
    "winget", "choco", "scoop", "apt", "brew", "locally", "local"))
_ECOSYSTEM_HINTS = (
    ("python", re.compile(r"\b(?:pip|python|venv|pypi)\b", re.I)),
    ("node", re.compile(r"\b(?:npm|node|nodejs)\b", re.I)),
    ("system", re.compile(
        r"\b(?:winget|choco|scoop|apt|apt-get|brew|system|globally)\b", re.I)),
)


def extract_requests(goal: str) -> Tuple[PackageRequest, ...]:
    """Deterministic goal -> packages: pinned ``name==1.2`` / ``name@1.2``
    tokens first, otherwise the word after ``install`` / ``set up``.

    This is deliberately dumb counting, not understanding: callers with
    structured intent pass ``packages=`` to :func:`plan_provision` instead.
    """
    text = goal or ""
    hinted = [eco for eco, pattern in _ECOSYSTEM_HINTS if pattern.search(text)]
    hint = hinted[0] if len(hinted) == 1 else None
    found: List[PackageRequest] = []
    seen = set()

    def add(name, version, eco):
        key = (name.lower(), version)
        if name.lower() in _STOPWORDS or key in seen:
            return
        # A local file or an absurd token is not a package name to install.
        if (len(name) > _MAX_NAME_CHARS
                or _looks_like_artifact(name, False, False)
                or (version and (len(version) > _MAX_NAME_CHARS
                                 or _looks_like_artifact(version, False, False)))):
            return
        seen.add(key)
        found.append(PackageRequest(name, version, eco))

    pinned = list(_PINNED_PY_RE.finditer(text))
    for match in pinned:
        add(match.group(1), match.group(2), "python")
    node_pinned = list(_PINNED_NODE_RE.finditer(text))
    for match in node_pinned:
        add(match.group(1), match.group(2), "node")
    # A pinned token that was rejected (a local file) must not fall through
    # to "install the word before it": that would invent a different package.
    if not found and not pinned and not node_pinned:
        for match in _VERB_RE.finditer(text):
            add(match.group(1), None, hint)
    return tuple(found)


def _venv_python(probe: HostProbe, venv_dir: str) -> str:
    sub = ("Scripts", "python.exe") if probe.os_name == "Windows" else ("bin", "python")
    return os.path.join(venv_dir, *sub)


def _mkdir_step(root: str) -> Step:
    return Step(
        "mkdir-root", "Create the private working directory",
        ("mkdir", root), MUTATING, f"creates the directory {root}",
        f"delete the directory {root} (manually; Harness never deletes)")


def _recipe_venv_pip(requests, probe, root):
    venv_dir = os.path.join(root, "venv")
    py = _venv_python(probe, venv_dir)
    steps = [
        _mkdir_step(root),
        Step("create-venv", "Create an isolated virtual environment",
             (probe.python_executable, "-I", "-m", "venv", venv_dir), MUTATING,
             f"creates {venv_dir} with its own interpreter",
             f"delete the directory {venv_dir}"),
        Step("pip-install", "Install the packages into the venv (wheels only)",
             (py, "-I", "-m", "pip", "install", "--isolated",
              "--only-binary=:all:", "--disable-pip-version-check",
              "--no-input", *[r.pip_spec() for r in requests]),
             MUTATING, "downloads wheels and installs them inside the venv only",
             f"delete the directory {venv_dir}", requires_network=True),
    ]
    for index, req in enumerate(requests):
        steps.append(Step(
            f"verify-{index + 1}-{_slug(req.name)}",
            f"Confirm {req.name} is installed",
            (py, "-I", "-m", "pip", "show", "--isolated", req.name), READ,
            "prints the installed package metadata"))
    return steps


def _recipe_npm_prefix(requests, probe, root):
    exe = "npm"  # a bare name, pinned to a PATH-resolved path at run time
    steps = [
        _mkdir_step(root),
        Step("npm-install", "Install the packages into a private prefix",
             (exe, "install", *[r.npm_spec() for r in requests],
              "--prefix", root, "--ignore-scripts", "--no-audit", "--no-fund"),
             MUTATING, "downloads packages into the prefix; install scripts "
             "are disabled", f"delete the directory {root}",
             requires_network=True),
    ]
    for index, req in enumerate(requests):
        steps.append(Step(
            f"verify-{index + 1}-{_slug(req.name)}",
            f"Confirm {req.name} is installed",
            (exe, "ls", req.name, "--prefix", root), READ,
            "lists the installed package"))
    return steps


def _system_manager(probe: HostProbe) -> Optional[PackageManager]:
    order = {"Windows": ("winget", "choco", "scoop"),
             "Darwin": ("brew",)}.get(probe.os_name, ("apt", "brew"))
    for name in order:
        found = probe.manager(name)
        if found is not None:
            return found
    return None


def _recipe_system_pm(requests, probe, root):
    pm = _system_manager(probe)
    exe = pm.name
    steps = []
    for index, req in enumerate(requests):
        tag = f"{index + 1}-{_slug(req.name)}"
        if exe == "winget":
            install = ["winget", "install", "--id", req.name, "--exact"]
            query = ["winget", "list", "--id", req.name]
            if req.version:
                install += ["--version", req.version]
        else:
            install = [exe, "install"]
            if req.version and exe == "choco":
                install += ["--version", req.version]
            if exe in ("choco", "apt"):
                install.append("-y")
            install.append(req.name)
            query = [exe, "list"]
            if exe == "choco":
                query.append("--local-only")
            elif exe == "apt":
                query.append("--installed")
            query.append(req.name)
        steps.append(Step(
            f"install-{tag}", f"Install {req.name} with {exe}", tuple(install),
            IRREVERSIBLE,
            f"changes the host outside the approved root via {exe}; Harness "
            "never escalates privileges, so this needs the rights it already has",
            f"uninstall {req.name} with {exe} (manually)", requires_network=True))
        steps.append(Step(f"verify-{tag}", f"Confirm {req.name} is installed",
                          tuple(query), READ, f"{exe} lists the package"))
    return steps


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "pkg"


def _inventory_steps(probe: HostProbe) -> List[Step]:
    steps = [Step("python-version", "Report the interpreter version",
                  (probe.python_executable, "--version"), READ,
                  "prints the interpreter version")]
    for pm in probe.package_managers:
        if pm.usable:
            steps.append(Step(f"{pm.name}-version", f"Report the {pm.name} version",
                              (pm.name, "--version"), READ,
                              f"prints the {pm.name} version"))
    return steps


_BUILDERS = {RECIPE_VENV_PIP: _recipe_venv_pip,
             RECIPE_NPM_PREFIX: _recipe_npm_prefix,
             RECIPE_SYSTEM_PM: _recipe_system_pm}


def applicable_recipes(requests: Sequence[PackageRequest], probe: HostProbe,
                       policy: ProvisionPolicy) -> List[str]:
    """Recipe ids that fit EVERY request on this host, most reversible first."""
    if not requests:
        return []

    def fits(ecosystem):
        return all(r.ecosystem in (None, ecosystem) for r in requests)

    out = []
    if probe.python_version and probe.python_executable and fits("python") \
            and policy.approved_roots:
        out.append(RECIPE_VENV_PIP)
    npm = probe.manager("npm")
    # An npm that is only a cmd.exe batch shim cannot be run safely (the shim
    # re-parses its arguments), so it is not a usable recipe.
    if (npm is not None and not npm.path.lower().endswith(_WINDOWS_SHIM_SUFFIXES)
            and fits("node") and policy.approved_roots):
        out.append(RECIPE_NPM_PREFIX)
    if (policy.allow_system_install and fits("system")
            and _system_manager(probe) is not None):
        out.append(RECIPE_SYSTEM_PM)
    return out


def _answer(result: Any, key: str, field_name: str):
    answers = getattr(result, "answers", None)
    entry = answers.get(key) if isinstance(answers, dict) else None
    return entry.get(field_name) if isinstance(entry, dict) else None


def _jev_select(jev, goal, probe, candidates, task_id):
    """A live Jev choice among ``candidates`` -- or ``None`` with reasons."""
    ids = [cid for cid, _ in candidates]
    try:
        questions = provision_selection_question_pack(candidates)
        result, _ = jev.evaluate_provision(
            {"goal": goal, "host": probe.jev_facts(), "candidates": ids,
             "pack_version": PROVISION_PACK_VERSION},
            questions, site=PROVISION_SITE, task_id=task_id)
    except Exception as exc:  # any judgment failure degrades to the fixed order
        return None, [f"jev selection unavailable: {exc}"]
    if getattr(result, "is_fallback", True):
        return None, ["jev selection fell back; using the deterministic order"]
    choice = _answer(result, "recipe", "choice")
    if isinstance(choice, str) and choice in ids:
        return choice, [f"jev chose {choice}"]
    return None, [f"jev answer {choice!r} is not a declared recipe; refused"]


def _jev_review(jev, plan, task_id):
    """Advisory review of a built plan; never loosens the allowlist."""
    state = {"goal": plan.goal, "recipe": plan.recipe,
             "steps": [{"id": s.id, "description": s.description,
                        "class": s.classification, "argv": list(s.argv)}
                       for s in plan.steps]}
    try:
        result, _ = jev.evaluate_provision(
            state, provision_verification_question_pack(),
            site=PROVISION_SITE, task_id=task_id)
    except Exception as exc:  # advisory only: any failure is a recorded fallback
        return {"is_fallback": True, "reason": str(exc)}, False
    if getattr(result, "is_fallback", True):
        return {"is_fallback": True, "reason": "jev review fell back"}, False
    satisfies = _answer(result, "satisfies_goal", "noul")
    exceeds = _answer(result, "exceeds_goal", "noul")
    if not isinstance(satisfies, (int, float)) or not isinstance(exceeds, (int, float)):
        return {"is_fallback": True, "reason": "jev review unparseable"}, False
    flagged = float(exceeds) >= 0.5 or float(satisfies) < 0.5
    return {"is_fallback": False, "satisfies_goal": float(satisfies),
            "exceeds_goal": float(exceeds)}, flagged


def plan_provision(goal: str, probe: HostProbe, *, policy: ProvisionPolicy,
                   packages: Optional[Sequence[PackageRequest]] = None,
                   root: Optional[str] = None, jev=None,
                   task_id: Optional[str] = None) -> Plan:
    """Build a reviewable plan for ``goal`` on this host. Never executes.

    Recipe applicability is decided by code from the probe; Jev (when ``jev``
    is a keyed policy) only chooses among those declared ids and reviews the
    finished plan. Unkeyed, failing or out-of-vocabulary answers fall back to
    the deterministic order with ``is_fallback=True``. A goal that names
    nothing installable yields a READ-only inventory plan, never a guess.
    The result has already passed :func:`validate_plan`.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise ProvisionError("a goal is required")
    if len(goal) > MAX_GOAL_CHARS:
        raise ProvisionError(f"goal is {len(goal)} characters; the limit is {MAX_GOAL_CHARS}")
    requests = tuple(packages) if packages is not None else extract_requests(goal)
    if len(requests) > policy.max_specs:
        raise ProvisionError(
            f"{len(requests)} packages exceeds the limit of {policy.max_specs}")
    work_root = root
    if work_root is None and policy.approved_roots:
        work_root = os.path.join(
            policy.approved_roots[0],
            "provision-" + (_slug(requests[0].name) if requests else "inventory"))
    work_root = work_root or ""
    if work_root:
        _require_in_roots(work_root, policy, "plan root")

    notes: List[str] = []
    candidates = applicable_recipes(requests, probe, policy)
    recipe, is_fallback, source = RECIPE_INVENTORY, True, "deterministic"
    if not requests:
        notes.append("no installable package was identified in the goal; "
                     "emitting a read-only inventory. Name a package (for "
                     "example name==1.2.3) to get an install plan.")
    elif not candidates:
        notes.append("no setup recipe fits this host and policy; emitting a "
                     "read-only inventory.")
    else:
        recipe = candidates[0]
        if len(candidates) > 1 and jev is not None:
            chosen, reasons = _jev_select(
                jev, goal, probe,
                [(cid, _RECIPE_DESCRIPTIONS[cid]) for cid in candidates], task_id)
            notes.extend(reasons)
            if chosen is not None:
                recipe, is_fallback, source = chosen, False, "jev"
        elif len(candidates) > 1:
            notes.append("no judgment source supplied; using the most "
                         "reversible recipe")
        else:
            notes.append(f"only one recipe fits this host: {recipe}")
    if recipe == RECIPE_INVENTORY:
        steps = _inventory_steps(probe)
    else:
        steps = _BUILDERS[recipe](requests, probe, work_root)
    plan = Plan(goal.strip(), tuple(steps), work_root, recipe, source,
                is_fallback, tuple(notes))
    validate_plan(plan, policy)
    if jev is not None and recipe != RECIPE_INVENTORY:
        review, flagged = _jev_review(jev, plan, task_id)
        live_review = not review["is_fallback"]
        plan = Plan(plan.goal, plan.steps, plan.root, plan.recipe,
                    "jev-review" if live_review and plan.source != "jev"
                    else plan.source,
                    plan.is_fallback and not live_review, plan.notes,
                    review, flagged)
    return plan
