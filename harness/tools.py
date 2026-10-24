"""One owner for resolving external tools used by gates.

A gate command written as ``python check.py`` used to be executed literally.
On a python3-only host there is no ``python`` alias, so every round failed
identically and the task ended as ``gate_broken`` after the model had already
been billed. The audit's ruff lookup had a second, ad-hoc resolver with the
same failure class.

Resolution is deliberately boring data:

* the interpreter is ``HARNESS_PYTHON`` when set, else ``sys.executable``,
  then ``python``, ``python3`` and ``py`` on PATH;
* ruff is ``HARNESS_RUFF`` when set, else ``ruff`` on PATH, then
  ``<interpreter> -m ruff``.

Explicit overrides are authoritative: if one is set and unusable, resolution
fails loudly instead of silently falling back to a different tool. Resolved
argv is executed without a shell, so a path containing spaces stays one
argument.
"""
import importlib.util
import os
import re

from . import osal
from .errors import HarnessError

PYTHON_ENV = "HARNESS_PYTHON"
RUFF_ENV = "HARNESS_RUFF"

_PYTHON_NAMES = ("python", "python3", "py")
_PYTHON_VERSIONED = re.compile(r"^python3(?:\.\d+)?$")


def _tool_name(token):
    """The bare, case-folded tool name for a command's first token."""
    base = os.path.basename(str(token)).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _is_bare(token):
    """True for a PATH name, false for an explicit path the caller chose."""
    return os.path.dirname(str(token)) == ""


def _override(env_name):
    """A validated explicit override, or ``None`` when the variable is unset."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    candidate = raw
    if os.path.dirname(raw):
        candidate = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(candidate):
            raise HarnessError(
                f"{env_name}={raw!r} is not a file; fix the override or unset "
                f"{env_name}")
        return candidate
    found = osal.which(raw)
    if found is None:
        raise HarnessError(
            f"{env_name}={raw!r} was not found on PATH; fix the override or "
            f"unset {env_name}")
    return found


def resolve_python():
    """The interpreter every gate should use.

    ``sys.executable`` is first because it is the interpreter running this
    process (and therefore the one that has Harness installed). The PATH
    names exist so the same policy works from a launcher that cannot report a
    usable executable. A caller that explicitly named an absolute interpreter
    is left alone by :func:`resolve_gate_argv`.
    """
    override = _override(PYTHON_ENV)
    if override is not None:
        return override
    running = osal.python_exe()
    if running and os.path.isfile(running):
        return running
    for name in _PYTHON_NAMES:
        found = osal.which(name)
        if found is not None:
            return found
    raise HarnessError(
        "no Python interpreter found: sys.executable is unusable and "
        + ", ".join(_PYTHON_NAMES)
        + " are not on PATH; install Python or set HARNESS_PYTHON to an "
          "interpreter")


def python_token():
    """The interpreter as one shell-free command token (quoted if needed)."""
    exe = resolve_python()
    return f'"{exe}"' if any(ch.isspace() for ch in exe) else exe


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def ruff_argv(*, strict=False):
    """The argv prefix for ruff: a PATH/override binary or the module form.

    ``strict`` turns "neither a binary nor the module is available" into the
    one preflight error a caller should see before spending model rounds.
    Listing the documented gates does not need ruff installed, so it uses the
    module form without checking.
    """
    override = _override(RUFF_ENV)
    if override is not None:
        return (override,)
    found = osal.which("ruff")
    if found is not None:
        return (found,)
    interpreter = resolve_python()
    if _module_available("ruff"):
        return (interpreter, "-m", "ruff")
    if strict:
        raise HarnessError(
            "ruff not found: put ruff on PATH, install the dev extra "
            "(pip install -e '.[dev]'), or set HARNESS_RUFF")
    return (interpreter, "-m", "ruff")


def resolve_gate_argv(argv, *, strict=False):
    """Resolve a gate command's tool token; leave everything else untouched.

    Only a bare interpreter/ruff name is rewritten. An explicit path (a
    venv's ``/path/bin/python``, a pinned ``C:\\...\\ruff.exe``) is the
    caller's choice and must not be silently replaced.

    ``strict`` belongs to preflight: a missing ruff is then the one named
    error instead of a module fallback that would fail every gate round.
    Execution uses the non-strict shape so a runner still returns an exit
    code rather than raising.
    """
    resolved = [str(part) for part in argv]
    if not resolved:
        raise HarnessError("gate command is empty")
    first = resolved[0]
    if not _is_bare(first):
        return resolved
    name = _tool_name(first)
    if name in _PYTHON_NAMES or _PYTHON_VERSIONED.match(name):
        resolved[0] = resolve_python()
    elif name == "ruff":
        resolved = list(ruff_argv(strict=strict)) + resolved[1:]
    return resolved
