"""Gates as DATA, run by ONE runner.

A *gate* is a command Harness runs on someone's behalf: the per-apply
verify gate, the plan stage gate, and the repo's own quality gates. There
used to be two runners for the same concept:

- ``cli.py`` handed the stage gate to ``subprocess.run(..., shell=True)``.
  On Windows that is ``cmd.exe``, which rejects forward-slash paths, so
  ``audits/self/audit.py`` passed on Linux and failed on Windows.
- ``filesafety.py`` tokenized the verify gate with POSIX ``shlex``, which
  eats backslashes, so ``C:\\gate.bat`` silently became ``C:gate.bat``.

Both are now argv lists executed through :func:`harness.osal.run`, which
never uses a shell. :func:`split_command` keeps the ergonomics of a
documented command string by tokenizing it in a Windows-aware way (drive
letters, UNC prefixes and backslash paths survive intact), and the
:data:`GATES` registry holds the repo's documented gates as data with
``sys.executable`` substituted at render time -- so ``harness gates`` prints
the exact command for *this* platform instead of every doc carrying a
Windows-flavoured string.
"""
import os
import re
from dataclasses import dataclass

from .errors import HarnessError
from .paths import REPO_ROOT
from . import osal
from . import tools

# The documented verify gate budget; the stage gate gets a bigger one
# (below) because it runs a whole suite between dependent stages.
VERIFY_TIMEOUT = 300
STAGE_GATE_TIMEOUT = VERIFY_TIMEOUT * 6


# --------------------------------------------------------------------------
# tokenizing: shlex where shlex is right, protection where it is wrong
# --------------------------------------------------------------------------

# shlex(posix=True) treats a backslash as an escape character, which is
# correct for /bin/sh and wrong for every Windows path. Protect the
# path-shaped spans first (quoted variants too, so a path with a space
# survives as one token), tokenize the rest, then restore.
_MARK = "\x00OSAL%d\x00"
_MARK_RE = re.compile(r"\x00OSAL(\d+)\x00")
_QUOTED_WIN_PATH = re.compile(r"""(['"])(?:[A-Za-z]:[\\/]|\\\\)[^'"]*\1""")
_BARE_WIN_PATH = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\)[^\s'"|;&<>]+""")


def _protect_windows_paths(command):
    """Swap Windows-shaped path spans for placeholders shlex cannot mangle."""
    saved = []

    def take(match):
        saved.append(match.group(0))
        return _MARK % (len(saved) - 1)

    protected = _QUOTED_WIN_PATH.sub(take, command)
    protected = _BARE_WIN_PATH.sub(take, protected)
    return protected, saved


def _restore_paths(token, saved):
    if "\x00OSAL" not in token:
        return token

    def put(match):
        raw = saved[int(match.group(1))]
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            return raw[1:-1]  # the quotes were shlex's job, not ours
        return raw

    return _MARK_RE.sub(put, token)


def split_command(command):
    """Tokenize a documented gate command into an argv list.

    Raises :class:`~harness.errors.HarnessError` when the command is empty
    or its quoting is unbalanced -- fail closed rather than guessing, since
    a mis-guessed argv list is a gate that runs the wrong thing.
    """
    if isinstance(command, (list, tuple)):
        argv = [str(a) for a in command]
        if not argv:
            raise HarnessError("gate command is empty.")
        return argv
    if not isinstance(command, str) or not command.strip():
        raise HarnessError("gate command is empty.")
    import shlex
    protected, saved = _protect_windows_paths(command)
    try:
        tokens = shlex.split(protected)
    except ValueError as e:
        raise HarnessError(
            f"gate command is not shell-tokenizable ({e}); quote it properly.") from e
    if not tokens:
        raise HarnessError("gate command is empty.")
    return [_restore_paths(t, saved) for t in tokens]


def describe_command(command):
    """Render a command for humans, quoting only what needs quoting."""
    if isinstance(command, (list, tuple)):
        return " ".join(str(a) for a in command)
    return str(command)


# --------------------------------------------------------------------------
# the one runner
# --------------------------------------------------------------------------

def run_gate(command, timeout=VERIFY_TIMEOUT, cwd=None, argv=None):
    """Run a gate WITHOUT a shell and return ``(returncode, output)``.

    `command` is the documented string (or an argv list) and `argv` may
    carry an already-tokenized list for callers that hold data. Timeouts,
    a missing executable and a non-executable target map to the same exit
    codes the previous runner used (124/127/126) so callers and their
    tests keep one vocabulary. The gate's own stdout and stderr come back
    concatenated: a gate is evidence, and evidence is what it printed.
    """
    if argv is None:
        argv = split_command(command)
    # One resolution point for every gate: a bare `python`/`ruff` is replaced
    # with the checkout's interpreter/tool before the shell-free runner sees
    # it. Explicit paths stay exactly as the caller wrote them. Resolution
    # failure keeps the runner's exit-code vocabulary (127 + the reason);
    # preflight is what turns it into a named exception before any spend.
    try:
        argv = tools.resolve_gate_argv(argv)
    except HarnessError as exc:
        return 127, str(exc)
    result = osal.run(argv, cwd=cwd, timeout=timeout)
    return result.returncode, result.combined


def validate_gate(command, require_executable=True):
    """Preflight a gate WITHOUT executing it.

    The command must tokenize, and (when require_executable) its
    interpreter/tool must be findable. Honesty, not a guarantee -- a gate
    that exists can still fail at runtime. Engine callers pass
    require_executable=False so hermetic library stubs stay usable;
    dogfood/CLI keep the PATH check.
    """
    argv = tools.resolve_gate_argv(split_command(command), strict=True)
    if require_executable and osal.which(argv[0]) is None:
        raise HarnessError(
            f"gate executable not found: {argv[0]} (the gate would fail every "
            "round; fix the gate before spending a live run)")
    return argv


# --------------------------------------------------------------------------
# the documented gates, as data
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GateSpec:
    """One documented gate: a name, what it proves, and its argv template.

    ``template`` never hardcodes an interpreter path -- ``{python}`` is
    filled from :func:`sys.executable` at render time, which is what makes
    the same registry correct on Linux, macOS and Windows. ``{root}`` is
    filled from :data:`harness.paths.REPO_ROOT`, so a gate targets the
    checkout that owns the command, not the caller's current directory.
    """

    name: str
    summary: str
    template: tuple
    requires: tuple = ()

    def argv(self, repo_root=None, **params):
        """The argv list for this platform; paths resolve from the checkout."""
        values = {"python": tools.resolve_python(),
                  "ruff": tools.ruff_argv(),
                  "root": repo_root if repo_root is not None else REPO_ROOT}
        values.update(params)
        out = []
        for part in self.template:
            if part == "{ruff}":
                out.extend(values["ruff"])
            else:
                out.append(str(part).format(**values))
        return out

    def command(self, repo_root=None, **params):
        """A copy-pasteable single-line form (quoting paths with spaces)."""
        out = []
        for part in self.argv(repo_root=repo_root, **params):
            out.append(part if not any(c.isspace() for c in part)
                       else f'"{part}"')
        return " ".join(out)

    def missing_targets(self, repo_root=None):
        """Checkout-relative targets this gate needs; empty means runnable."""
        root = repo_root if repo_root is not None else REPO_ROOT
        return [rel for rel in self.requires
                if not os.path.exists(os.path.join(root, rel))]


#: The gates CLAUDE.md tells every contributor to run before a PR. The docs
#: name the gate and point at ``harness gates``; the commands live here, so
#: a doc can no longer drift from what actually runs.
GATES = {
    spec.name: spec for spec in (
        GateSpec("ruff", "lint the package, tests, audit script and driver-core",
                 ("{ruff}", "check",
                  "{root}/harness", "{root}/tests", "{root}/audits",
                  "{root}/driver_core"),
                 requires=("harness", "tests", "audits", "driver_core")),
        GateSpec("compileall", "byte-compile every module (syntax gate)",
                 ("{python}", "-m", "compileall", "-q",
                  "{root}/harness", "{root}/tests"),
                 requires=("harness", "tests")),
        GateSpec("unittest", "hermetic suite, no network, warnings as errors",
                 ("{python}", "-W", "error::ResourceWarning", "-m", "unittest",
                  "discover", "-s", "{root}/tests"),
                 requires=("tests",)),
        GateSpec("audit", "4-dimensional self-audit (9.5+ bar on every dimension)",
                 ("{python}", "{root}/audits/self/audit.py"),
                 requires=("audits/self/audit.py",)),
        GateSpec("jev-phase", "Jev bar for one phase (hermetic, local-only)",
                 ("{python}", "-m", "harness.cli", "jev-phase", "--phase",
                  "{phase}", "--repo-root", "{root}", "--local-only"),
                 requires=("harness",)),
        GateSpec("import", "installed-package smoke: import + version",
                 ("{python}", "-c",
                  "import harness; print(harness.__version__)")),
    )
}

GATE_ORDER = ("ruff", "compileall", "unittest", "audit", "jev-phase", "import")


def gate(name):
    """Look up a documented gate by name."""
    try:
        return GATES[name]
    except KeyError:
        raise HarnessError(
            f"unknown gate: {name} (known: {', '.join(GATE_ORDER)})") from None


def gate_argv(name, repo_root=None, **params):
    """The argv list for a documented gate on this platform."""
    return gate(name).argv(repo_root=repo_root, **params)


def gate_command(name, repo_root=None, **params):
    """A copy-pasteable command line for a documented gate on this platform."""
    return gate(name).command(repo_root=repo_root, **params)


def gate_help():
    """The documented gate table (name, what it proves, this platform's command)."""
    width = max(len(n) for n in GATE_ORDER)
    lines = [f"checkout root: {REPO_ROOT}", ""]
    for name in GATE_ORDER:
        spec = GATES[name]
        lines.append(f"{spec.name:<{width}}  {spec.summary}")
    return "\n".join(lines)
