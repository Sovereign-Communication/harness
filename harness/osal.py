"""The ONE place Harness talks to the operating system.

Platform-shaped behaviour used to be sprinkled across the package, and each
copy had its own idea of what a path or a subprocess looks like:

- ``cli.py`` ran the stage gate with ``shell=True``, so on Windows
  ``cmd.exe`` parsed the string: a forward-slash path (``audits/self/audit.py``)
  or a quoted argument failed there and worked on Linux/macOS.
- ``filesafety.py`` tokenized the verify gate with POSIX ``shlex``, which
  eats backslashes: ``C:\\Users\\x\\gate.bat`` became ``C:Usersxgate.bat``.
- ``config.py`` asked ``os.name`` directly, so the one place that could
  answer "are key-file permissions meaningful?" could not be tested or
  documented from one place.
- Text writes used the platform default newline, so the ledger's bytes were
  CRLF on Windows and LF everywhere else -- two different evidence files
  from the same chain.

Everything here is deliberately boring and hermetic-testable: run argv
lists without a shell, read and write UTF-8 with a fixed newline policy,
compare paths with realpath + case normalization, and answer the key-file
question once. ``tests/test_osal_boundary.py`` fails the build when another
module reaches for ``subprocess``, ``os.name``, ``sys.platform`` or
``webbrowser`` directly, so this module stays the boundary instead of
becoming one more place with an exception.

Known, documented divergences (see ``docs/security.md`` and the README
platform table) are *declared here* rather than sprinkled at call sites:

- key-file permission bits are POSIX-only; on Windows the answer is ``None``
  ("not modelled") and the caller warns nothing rather than pretending.
- ``SO_REUSEADDR`` on a loopback bind is a hijack risk on Windows, so
  :data:`HARDEN_REUSE` asks the server class to drop it there.
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass

# ONE place that answers "which platform is this?" -- callers must not ask
# os.name / sys.platform themselves (the boundary test enforces it).
IS_WINDOWS = os.name == "nt"
IS_POSIX = not IS_WINDOWS

# Windows binds honour SO_REUSEADDR by letting a second socket take over a
# live loopback port, which would hand the UI (and its auth token) to another
# process. POSIX needs it for TIME_WAIT restarts, so it stays there only.
HARDEN_REUSE = IS_WINDOWS

# Text I/O policy: UTF-8 everywhere, and LF for machine-written evidence.
# Python 3.9 does not enable UTF-8 mode (PEP 540 landed in 3.7 but is opt-in
# on Windows until 3.15), so an explicit encoding is not optional here.
ENCODING = "utf-8"
LF = "\n"

# A path that is nothing but a root: "/", "C:/", "//server/share/".
_ROOT_RE = re.compile(r"^(?:[A-Za-z]:)?/+$")


class OsalError(OSError):
    """The OS refused an operation Harness asked for; never swallowed."""


class AtomicWriteError(OSError):
    """The target of an atomic write refused the operation (symlink, escape,
    or vanished directory) -- never follow through by writing anyway."""


# --------------------------------------------------------------------------
# commands: argv lists only, never a shell string
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandResult:
    """Outcome of a shell-free command run (a record, never a live process)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def combined(self):
        return self.stdout + self.stderr


def run(argv, cwd=None, timeout=None, input_text=None, env=None):
    """Run an argv list with NO shell and return a :class:`CommandResult`.

    ``argv`` is data, never a command string: that is what makes a gate
    behave the same on cmd.exe, /bin/sh and zsh. Failures are returned as
    exit codes rather than exceptions so a gate runner can report them:

    - ``124`` timed out and was killed,
    - ``126`` the target is not executable,
    - ``127`` the executable was not found.

    Output is decoded as UTF-8 with replacement so a gate that prints in
    the console's code page cannot raise inside the caller.
    """
    if isinstance(argv, str):
        raise OsalError(
            "osal.run takes an argv list, not a command string "
            "(a string is a shell waiting to happen)")
    argv = [str(a) for a in argv]
    if not argv:
        raise OsalError("osal.run: empty argv")
    kwargs = {
        "capture_output": True,
        "shell": False,
        "encoding": ENCODING,
        "errors": "replace",
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if timeout is not None:
        kwargs["timeout"] = timeout
    if input_text is not None:
        kwargs["input"] = input_text
    if env is not None:
        kwargs["env"] = env
    try:
        proc = subprocess.run(argv, **kwargs)
    except subprocess.TimeoutExpired:
        return CommandResult(124, f"command timed out after {timeout}s: {' '.join(argv)}")
    except FileNotFoundError:
        return CommandResult(127, f"executable not found: {argv[0]}")
    except PermissionError:
        return CommandResult(126, f"not executable: {argv[0]}")
    except OSError as exc:
        return CommandResult(126, f"cannot run {argv[0]}: {exc}")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def run_bounded(argv, max_bytes, timeout=10, cwd=None):
    """Run an argv list and capture at most ``max_bytes`` of stdout.

    :func:`run` is right for gates, whose output is evidence and is bounded
    by the gate itself. This is the other shape: a *query* over something
    that can be arbitrarily large (``git log`` on a big repo), where holding
    every byte in memory is the bug. The reader drains in chunks, kills the
    child the moment the cap is passed, and returns ``None`` for
    "oversized, timed out, or failed" -- a question with no safe small
    answer is a ``None``, never a truncated lie.

    stderr is discarded: a query's error text is not part of its answer, and
    a megabyte of git diagnostics is exactly the payload nobody wants.
    """
    if isinstance(argv, str):
        raise OsalError("osal.run_bounded takes an argv list, not a command string")
    argv = [str(a) for a in argv]
    if not argv or max_bytes < 0:
        return None
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, cwd=cwd)
    except OSError:
        return None
    if process.stdout is None:  # pragma: no cover - defensive
        process.kill()
        process.wait()
        return None

    captured = bytearray()
    oversized = threading.Event()

    def drain_stdout():
        while True:
            remaining = max_bytes + 1 - len(captured)
            reader = getattr(process.stdout, "read1", None) or process.stdout.read
            chunk = reader(min(65536, remaining))
            if not chunk:
                return
            captured.extend(chunk)
            if len(captured) > max_bytes:
                oversized.set()
                process.kill()
                return

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        returncode = None
    reader.join(timeout=1)
    if reader.is_alive():
        process.kill()
        process.stdout.close()
        reader.join(timeout=1)
        return None
    process.stdout.close()
    if oversized.is_set() or returncode != 0:
        return None
    return bytes(captured)


def which(name):
    """PATH lookup (the one wrapper; a missing tool is ``None``, not an error)."""
    from shutil import which as _which
    return _which(name)


# --------------------------------------------------------------------------
# text I/O: UTF-8, explicit newlines
# --------------------------------------------------------------------------

def read_text(path, errors="strict"):
    """Read a text file as UTF-8 with NO newline translation.

    ``newline=""`` is what makes bytes comparable across platforms: the
    caller sees the file's own line endings, so an evidence hash computed
    here matches the bytes on disk on Windows and on Linux alike.
    """
    with open(path, encoding=ENCODING, errors=errors, newline="") as f:
        return f.read()


def write_text(path, text, newline=LF):
    """Write UTF-8 text with an explicit newline policy (default: LF).

    Evidence files (ledger lines, JSON exports) default to ``"\\n"`` so a
    Windows run and a Linux run produce byte-identical files.
    """
    with open(path, "w", encoding=ENCODING, newline=newline) as f:
        f.write(text)
    return len(text.encode(ENCODING))


def detect_newline(path):
    """The target file's own line-ending style, or None when unknown.

    Reads the first bytes only: a ``\\r\\n`` anywhere means CRLF (mixed
    files normalize to CRLF); bare ``\\n`` means LF; no newline at all
    (or an unreadable file) means None. Model output arrives with ``\\n``
    endings -- it never saw the tree's bytes -- so writes must aim at the
    target's style explicitly instead of laundering checkouts.
    """
    try:
        with open(path, "rb") as f:
            sample = f.read(8192)
    except OSError:
        return None
    if b"\r\n" in sample:
        return "\r\n"
    if b"\n" in sample:
        return "\n"
    return None


def atomic_write_text(path, content, follow=False, newline="preserve"):
    """Atomically replace `path` with `content`, refusing unsafe targets.

    Without ``follow=True`` a pre-existing symlink is never followed (the
    classic dotfile-points-into-the-repo trick). The temp file is staged
    inside the target's directory so the final replace is atomic. The
    target's permission mode is preserved (tempfile.mkstemp creates 0600,
    which would otherwise silently strip an executable bit from a verify
    script or gate artifact and change the semantics of the working tree).

    ``newline="preserve"`` (default) translates the content to the target
    file's own detected style, so a model-written LF body never flips a
    CRLF checkout (and the failed-run rewind restores the exact original
    style, since preserved writes never change it in the first place).
    ``newline=None`` writes bytes exactly as given -- for snapshot restore,
    where the snapshot's bytes, not the tree's style, are authoritative.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    # Parent-dir symlink: realpath the directory so staging never lands
    # outside the intended tree when an intermediate component is a link.
    d_real = os.path.realpath(d)
    if os.path.islink(path) and not follow:
        raise AtomicWriteError(
            f"refusing to write through symlink: {path} "
            f"(delete the link or pass follow=True)")
    if newline == "preserve":
        style = detect_newline(path)
        if style == "\r\n":
            content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        fd, tmp = tempfile.mkstemp(prefix=".harness-", suffix=".tmp", dir=d_real)
    except OSError as e:
        raise AtomicWriteError(f"cannot stage temp file in {d_real}: {e}") from e
    try:
        # newline="" writes the string unchanged: with preserve mode the
        # translation above already ran, and with newline=None the caller
        # takes full responsibility for the bytes (snapshot restore).
        with os.fdopen(fd, "w", encoding=ENCODING, newline="") as f:
            f.write(content)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass  # new file: keep the safe 0600 default
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# --------------------------------------------------------------------------
# paths: one normalization, one containment test, one display form
# --------------------------------------------------------------------------

def norm_path(path):
    """Absolute, symlink-resolved, case-normalized path for COMPARISON.

    Windows and macOS filesystems are case-insensitive, so ``C:/Repo`` and
    ``c:/repo`` name the same file there and different files on Linux.
    ``normcase`` encodes exactly that difference, which is why every
    containment check goes through here instead of comparing raw strings.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def same_path(a, b):
    """True when two paths name the same file on this platform."""
    return norm_path(a) == norm_path(b)


def is_within(child, root):
    """True when `child` is inside `root` (or is `root` itself).

    Realpath both sides so a symlinked parent cannot smuggle a write out of
    the tree, and compare normalized so case cannot smuggle it out either.
    """
    c = norm_path(child)
    r = norm_path(root)
    if c == r:
        return True
    return c.startswith(r.rstrip(os.sep) + os.sep)


def display_path(path):
    """Human/cross-platform form of a path: forward slashes, no ``.`` noise.

    Reports, ledgers and Jev packs are compared across machines, so the
    separator must not be a platform fact. This normalizes *relative* and
    *absolute* paths alike; a machine-specific root (a drive letter, a home
    directory) is still machine-specific by design -- parity hashes pin the
    repo-relative part, which is what a shared evidence file can honestly
    promise.
    """
    if path is None:
        return None
    text = str(path).replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    # A trailing separator is noise ("C:/repo/" and "C:/repo" are the same
    # place) -- except at a root, where the separator IS the root.
    if len(text) > 1 and text.endswith("/") and not _ROOT_RE.match(text):
        text = text.rstrip("/") or "/"
    return text


def normalize_roots(roots):
    """Normalize configured allowed-roots for containment comparisons."""
    return [norm_path(r) for r in (roots or [])]


# --------------------------------------------------------------------------
# key files: the permission question, answered once
# --------------------------------------------------------------------------

def keyfile_mode(path):
    """Permission bits of a key file, or None where they are not modelled.

    POSIX gives a key file a real mode, and a group/world-readable one is a
    silent credential hazard worth a loud warning. Windows has no POSIX mode
    on a credential file (the ACL is the real control and lives outside this
    process), so the honest answer there is ``None`` -- "not modelled" --
    rather than a fabricated 0o600.
    """
    if IS_WINDOWS:
        return None
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


def keyfile_is_insecure(path):
    """True only when the mode is known AND group/world readable."""
    mode = keyfile_mode(path)
    return bool(mode is not None and mode & 0o077)


# --------------------------------------------------------------------------
# UI: the one browser hand-off
# --------------------------------------------------------------------------

def open_url(url):
    """Open `url` in the platform browser; True when a browser took it.

    Imported here (lazily) so this module is the only place that touches
    ``webbrowser``, and so importing ``harness`` never opens anything.
    """
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:  # pragma: no cover - platform browser launch is opaque
        return False


def python_exe():
    """The interpreter running this process -- the only gate executable.

    ``sys.executable`` is the same on every OS, which is what lets one
    documented gate command be data instead of three shell idioms.
    """
    return sys.executable
