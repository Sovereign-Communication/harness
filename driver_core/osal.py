"""The ONE place driver-core talks to the operating system.

This module exists because the alternative is always worse. A subprocess call
written inline in an executor, and the same call written in a perception
adapter, and the same call in a CLI, each develop their own idea of what an
argument list looks like on the platform they happen to be run on -- and the
divergence only ever shows up on the platform nobody tested.

So the rules are boring and total:

* **No shell, ever.** Every call is an argv list. A shell turns a path
  containing a space or a forward slash into a syntax problem on one platform
  and not another, and it turns a filename into an injection point.
* **No platform probes outside this file.** ``os.name`` and ``sys.platform``
  appear here and nowhere else; ``tests/test_osal_boundary.py`` fails the
  build if another module reaches for them.
* **Divergences are declared, not discovered.** Where platforms genuinely
  differ, the answer is a named constant and a comment, so a reader learns
  the rule instead of inferring it from a bug.

Screen capture and synthetic input are here for the same reason. Synthetic
input is *not* implemented here, and that is a decision rather than an
omission: this package declares which kinds of input exist and refuses
everything it cannot honestly perform, but the mechanism that moves a mouse
is supplied by the embedding application. Two reasons, and the second is the
one that matters:

* **A backend is a supply chain.** Synthesising input needs platform APIs --
  ``SendInput`` on Windows, Accessibility on macOS, XTest on Linux -- and each
  of those arrives as a dependency or as ``ctypes`` against a DLL whose ABI
  is not yours to depend on. A driver that acts on a machine is the last
  place to add one.
* **Registration controls existence.** What this machine is permitted to
  touch is a policy decision belonging to whoever deploys the driver, not a
  library default. A host that never registers a clicker cannot have one,
  which means no bug in this package's consent handling can conjure one.

So :func:`send_input` is a *declaration* with a pluggable backend. Unregistered
is the default state, and it refuses -- loudly, by name -- rather than
appearing to succeed while doing nothing.
"""
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

from .errors import OsalError

#: How long a child process may run before it is killed.
DEFAULT_TIMEOUT = 30

#: The largest stdout we will read from a child, in bytes. A command that
#: prints without bound must not be able to exhaust memory in the driver.
MAX_CAPTURE_BYTES = 1 << 20

#: Declared platform support. Anything absent here is refused rather than
#: attempted, and the refusal says so by name.
SUPPORTED = ("linux", "darwin", "windows")

#: Suffix for the copy ``atomic_write`` leaves beside the file it replaced.
#: A mutating action that cannot be undone by hand is a mutating action that
#: should not have been declared ``MUTATING`` rather than ``IRREVERSIBLE``,
#: so overwriting backs up first. Named, so a backup is identifiable rather
#: than an anonymous ``.tmp``.
BACKUP_SUFFIX = ".driverbak"

#: Declared divergences.
#: Windows console programs need the CREATE_NO_WINDOW flag or they flash a
#: console window on every invocation, which is unacceptable for a driver
#: that polls in a loop.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def platform_name():
    """``linux``, ``darwin`` or ``windows``. The only platform answer here."""
    if sys.platform.startswith("win") or os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return f"unknown:{sys.platform}"


def is_supported():
    return platform_name() in SUPPORTED


def run(argv, *, cwd=None, timeout=DEFAULT_TIMEOUT, env=None, input_text=None,
        max_bytes=MAX_CAPTURE_BYTES):
    """Run one command as an argv list, with no shell.

    Returns a result object rather than raising, so a caller can tell a
    non-zero exit (an answer) from a missing binary or a timeout (a failure
    to ask). Those two deserve different handling and a bare
    :class:`FileNotFoundError` does not preserve the distinction once it has
    been caught somewhere four frames up.
    """
    if isinstance(argv, str):
        raise TypeError(
            "run() takes an argv list, not a string; a string would be "
            "handed to the platform shell, which is exactly what this "
            "module exists to prevent")
    argv = list(argv)
    if not argv:
        raise ValueError("argv must not be empty")
    if input_text is not None and not isinstance(input_text, str):
        # Caught here rather than surfacing as an AttributeError from inside
        # the encode() call four lines down, which reads as a bug in this
        # module rather than as the caller's mistake.
        raise TypeError(
            f"input_text must be str or None, got {type(input_text).__name__}")

    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update(env)

    popen_kwargs = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if input_text is None:
        popen_kwargs["stdin"] = subprocess.DEVNULL
    # When input_text is given, `input=` below installs the stdin pipe itself.
    # Passing both is rejected by subprocess, and doing so meant the
    # input_text path raised ValueError on every platform -- a parameter that
    # looked like it worked and had never once been exercised.
    if platform_name() == "windows":
        popen_kwargs["creationflags"] = CREATE_NO_WINDOW

    try:
        completed = subprocess.run(
            argv, input=(input_text.encode("utf-8") if input_text is not None
                         else None),
            timeout=timeout, env=child_env, check=False, **popen_kwargs)
    except FileNotFoundError:
        return ProcessResult(0, False, "", "", f"not found: {argv[0]}",
                             reason="not_found")
    except subprocess.TimeoutExpired:
        return ProcessResult(0, False, "", "",
                             f"timed out after {timeout}s", reason="timeout")
    except OSError as exc:
        return ProcessResult(0, False, "", "", f"failed to start: {exc}",
                             reason="os_error")

    stdout = (completed.stdout or b"")[:max_bytes].decode("utf-8", "replace")
    stderr = (completed.stderr or b"")[:max_bytes].decode("utf-8", "replace")
    return ProcessResult(completed.returncode, completed.returncode == 0,
                         stdout, stderr, "")


class ProcessResult:
    """One child process's outcome."""

    __slots__ = ("returncode", "ok", "stdout", "stderr", "error", "reason")

    def __init__(self, returncode, ok, stdout, stderr, error, reason=""):
        self.returncode = returncode
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.reason = reason

    def to_dict(self):
        return {"returncode": self.returncode, "ok": self.ok,
                "stdout": self.stdout, "stderr": self.stderr,
                "error": self.error, "reason": self.reason}

    def __repr__(self):
        return f"ProcessResult(rc={self.returncode}, ok={self.ok})"


# ---- screen capture -----------------------------------------------------
# Declared per platform rather than detected, so "I could not capture" is
# always a named answer and never an empty file that looks like a blank
# screen.

def capture_screen():
    """Capture the screen. Returns ``(path, detail)``; ``path`` may be None.

    A refusal is a first-class result. A driver handed an unreadable capture
    should route to a structured source, not ask a vision model to describe
    a file that does not exist.
    """
    name = platform_name()
    if name == "windows":
        return _capture_windows()
    if name == "darwin":
        return _capture_macos()
    if name == "linux":
        return _capture_linux()
    return None, f"screen capture is not supported on {name}"


def _capture_windows():
    script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
              "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
              "$i = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
              "$g = [System.Drawing.Graphics]::FromImage($i); "
              "$g.CopyFromScreen($b.X, $b.Y, 0, 0, $i.Size); "
              "$i.Save($env:DRIVER_SCREEN_OUT, "
              "[System.Drawing.Imaging.ImageFormat]::Png)")
    out = os.environ.get("DRIVER_SCREEN_OUT", "")
    if not out:
        return None, "DRIVER_SCREEN_OUT is not set; cannot capture"
    result = run(["powershell", "-NoProfile", "-NonInteractive",
                  "-Command", script], timeout=45)
    if not result.ok or not os.path.exists(out):
        return None, f"capture failed: {result.error or result.stderr[:200]}"
    return out, ""


def _capture_macos():
    out = os.environ.get("DRIVER_SCREEN_OUT", "")
    if not out:
        return None, "DRIVER_SCREEN_OUT is not set; cannot capture"
    result = run(["screencapture", "-x", out], timeout=45)
    if not result.ok or not os.path.exists(out):
        return None, f"capture failed: {result.error or result.stderr[:200]}"
    return out, ""


def _capture_linux():
    out = os.environ.get("DRIVER_SCREEN_OUT", "")
    if not out:
        return None, "DRIVER_SCREEN_OUT is not set; cannot capture"
    result = run(["gnome-screenshot", "-f", out], timeout=45)
    if not result.ok or not os.path.exists(out):
        return None, f"capture failed: {result.error or result.stderr[:200]}"
    return out, ""


# ---- synthetic input -----------------------------------------------------
# Declared, not discovered. A driver with no registered backend returns a
# named refusal, so an unregistered driver cannot half-work its way through a
# run by doing nothing and reporting success.

#: The kinds of synthetic input that exist. This is the *contract* between
#: this package and a host-supplied backend, not a claim that any of them is
#: implemented anywhere: every one of them is refused until a backend is
#: registered.
#:
#: ``focus``, ``scroll`` and ``submit`` are declared separately from
#: ``click`` rather than folded into it. Folding them in would be a silent
#: semantic widening -- a backend asked to ``click`` cannot tell whether it
#: is nudging a cursor or activating a control whose effect cannot be undone,
#: and those deserve different handling at every layer above this one.
INPUT_KINDS = ("click", "type", "key", "focus", "scroll", "submit")

#: Registered backends, keyed by platform name. Empty by default; see the
#: module docstring for why empty is the shipped state.
_INPUT_BACKENDS = {}


def register_input_backend(platform, backend):
    """Register the backend that performs synthetic input on ``platform``.

    ``backend`` is called as ``backend(kind, *, value, target)`` and returns
    ``(ok, detail)``. It is responsible for raising a clear error when the
    platform refuses the request -- an accessibility permission the host has
    not been granted, a display server nobody is connected to -- because
    that condition is not visible from inside this process.
    """
    if platform not in SUPPORTED:
        raise OsalError(
            f"cannot register an input backend for {platform!r}; declared "
            f"platforms are {list(SUPPORTED)}")
    if not callable(backend):
        raise OsalError(
            f"an input backend must be callable, got {type(backend).__name__}")
    _INPUT_BACKENDS[platform] = backend
    return backend


def disable_input_backend(platform=None):
    """Remove a backend, so the machine can be observed but never driven.

    The default state is already "no backend"; this exists so that a host
    which registered one can withdraw it without restarting, and so that the
    refusal path is reachable by policy rather than only by omission.
    """
    platform = platform or platform_name()
    return _INPUT_BACKENDS.pop(platform, None)


def input_backends():
    """Which platforms currently have a backend. For ``/health``."""
    return dict(_INPUT_BACKENDS)


# ---- reaching the network -----------------------------------------------
# Here for the same reason as ``run`` and ``capture_screen``: a driver that
# fetches from two files grows two ideas about timeouts, redirects and user
# agents, and the divergence only shows up against the one server that cares.
#
# Declared limits, because a default that trusts the far end is a default
# that will eventually be wrong in front of somebody's screen:
#
# * the scheme is restricted to http/https, so ``file://`` cannot be used to
#   read the local disk through a URL-shaped hole;
# * the redirect budget is small, because a redirect loop in a local driver
#   is a hang nobody is watching;
# * a redirect to any other scheme is refused rather than followed.
#
# Refusals raise :class:`OsalError`, which is what the filesystem and input
# sections here already raise. That type derives from ``DriverError``, so a
# caller catching the base class is unaffected -- the network tier simply
# stops hiding behind the generic name now that a specific one exists.

MAX_REDIRECTS = 3
FETCH_MAX_BYTES = 1 << 20
USER_AGENT = "driver-core (perception; stdlib-only)"
ALLOWED_SCHEMES = ("http", "https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Count redirects instead of following them blindly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        used = getattr(self, "_driver_core_redirects", 0)
        if used >= MAX_REDIRECTS:
            raise OsalError(
                f"refusing to follow more than {MAX_REDIRECTS} redirects "
                f"while fetching a document")
        scheme = urllib.parse.urlparse(newurl).scheme
        if scheme not in ALLOWED_SCHEMES:
            raise OsalError(
                f"refusing a redirect to {scheme!r}; only "
                f"{list(ALLOWED_SCHEMES)} are fetched")
        self._driver_core_redirects = used + 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, *, timeout=20, max_bytes=FETCH_MAX_BYTES):
    """Fetch one document. Returns ``(status, text)``; raises on refusal.

    A caller gets either an HTTP status and a body, or an exception naming
    the reason. There is deliberately no "empty document, but successfully":
    an empty body returned as a successful fetch is how a driver concludes a
    page is blank when in fact it never arrived.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise OsalError(
            f"refusing to fetch {parsed.scheme!r}; only "
            f"{list(ALLOWED_SCHEMES)} are fetched")
    if not parsed.netloc:
        raise OsalError(f"{url!r} has no host to fetch from")

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=timeout) as response:
        body = response.read(max_bytes)
        status = getattr(response, "status", None) or response.getcode()
        charset = response.headers.get_content_charset() or "utf-8"
    return status, body.decode(charset, "replace")


def send_input(kind, value=None, *, target=None):
    """Perform one synthetic input. Returns ``(ok, detail)``.

    Three refusals, in this order, each named so the operator can tell which
    one happened:

    1. the kind is not one this package declares;
    2. no backend is registered for this platform;
    3. the backend itself refused, and said why.

    There is no fourth path. A driver that reported success here without a
    backend would be indistinguishable, from the outside, from one that
    clicked the right thing -- which is the confusion this whole package
    exists to prevent.
    """
    if kind not in INPUT_KINDS:
        return False, (f"unknown input kind {kind!r}; declared kinds are "
                       f"{list(INPUT_KINDS)}")
    name = platform_name()
    backend = _INPUT_BACKENDS.get(name)
    if backend is None:
        return False, (
            f"synthetic {kind} has no registered backend on {name}; "
            f"the embedding application must register one explicitly")
    try:
        outcome = backend(kind, value=value, target=target)
    except Exception as exc:
        # A backend that raises is a backend that did not do the thing.
        # Reporting the exception as a success is the one outcome this
        # function must never produce.
        return False, f"{kind} backend on {name} raised: {exc}"
    if not isinstance(outcome, tuple) or len(outcome) != 2:
        return False, (
            f"{kind} backend on {name} returned {outcome!r}; an input backend "
            f"must return an (ok, detail) pair")
    ok, detail = outcome
    return bool(ok), str(detail)


# ---- filesystem policy ---------------------------------------------------
# Also here, for the same reason as the rest of this module: writing a file
# is a platform operation. ``os.replace`` is atomic on POSIX and on Windows,
# but the ways it can fail are not the same -- Windows refuses to replace a
# read-only destination, and refuses when another process holds the file
# open without FILE_SHARE_DELETE -- and a caller that learns that from a
# traceback instead of a named detail has learned it the expensive way.

def resolve_path(path):
    """The resolved form of a path: expanded, absolute, normalised.

    This is the form consent is bound to, and it is resolved *before* the
    operator is shown anything rather than at comparison time. Resolving
    during comparison would be a silent widening: consent given for
    ``~/notes`` would quietly authorise ``/home/someone/notes``, and the
    person who said yes never saw the second path. So the form that is
    compared, displayed and executed is the same form, computed once.
    """
    if not isinstance(path, str) or not path.strip():
        raise OsalError(f"a path must be a non-empty string, got {path!r}")
    return os.path.abspath(os.path.expanduser(path))


def atomic_write(path, content, *, encoding="utf-8", backup=True):
    """Write a file so that no reader ever sees a partial one.

    Declared divergences, both of which are answered rather than swallowed:

    * **Windows refuses to replace a read-only destination**, and refuses to
      replace a file another process holds open. Both surface as ``OSError``
      and are reported as a named failure, not a crash.
    * **The rename is fsynced but the directory entry is not.** The bytes
      are durable before the swap and the swap is atomic; a power loss
      between the two can lose the name, not the contents. Closing that gap
      needs ``O_DIRECTORY`` fsync, which is POSIX-only, and a driver that
      pretended otherwise on every platform would be lying on two of three.

    The parent directory is **not** created. Creating a path the operator
    was never shown is a side effect consent did not cover.
    """
    resolved = resolve_path(path)
    if not isinstance(content, str):
        raise OsalError(
            f"write_file takes text content, got {type(content).__name__}")
    directory = os.path.dirname(resolved)
    if not os.path.isdir(directory):
        raise OsalError(
            f"the directory {directory!r} does not exist; driver-core will "
            f"not create a path it was not given consent for")
    if os.path.isdir(resolved):
        raise OsalError(f"{resolved!r} is a directory, not a file")

    existed = os.path.exists(resolved)
    if existed and backup:
        _backup(resolved)

    temporary = os.path.join(
        directory, f".{os.path.basename(resolved)}.{os.getpid()}.tmp")
    try:
        # Same directory on purpose: os.replace is only atomic within a
        # filesystem, and a temp file in %TEMP% would be on another one.
        with open(temporary, "w", encoding=encoding, newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    except OSError as exc:
        _discard(temporary)
        raise OsalError(f"could not write {resolved!r}: {exc}") from None
    return {
        "path": resolved,
        "bytes": len(content.encode(encoding)),
        "replaced": existed,
        "backup": (resolved + BACKUP_SUFFIX) if (existed and backup) else None,
    }


def remove_file(path):
    """Delete exactly one file, and refuse every near miss.

    Three refusals, each because the alternative is an accident:

    * **a missing path** -- deleting nothing while reporting a delete is how
      a retry loop ends up deleting the thing that appeared in between;
    * **a symlink** -- "delete the link" and "delete what it points at" are
      different acts, and the caller has not said which;
    * **a directory** -- recursive deletion is not this function's job and
      must never be reachable by passing a directory here.

    No backup. The action is declared ``IRREVERSIBLE``, and a backup would
    make it recoverable, which would mean the classification was wrong.
    """
    resolved = resolve_path(path)
    if not os.path.lexists(resolved):
        raise OsalError(f"{resolved!r} does not exist; nothing was deleted")
    if os.path.islink(resolved):
        raise OsalError(
            f"{resolved!r} is a symbolic link; deleting the link and deleting "
            f"its target are different acts, so neither is performed")
    if os.path.isdir(resolved):
        raise OsalError(
            f"{resolved!r} is a directory; this action deletes one file and "
            f"never recurses")
    try:
        size = os.path.getsize(resolved)
        os.unlink(resolved)
    except OSError as exc:
        # Windows adds the read-only attribute here, which is the most
        # common cause by a wide margin.
        raise OsalError(f"could not delete {resolved!r}: {exc}") from None
    return {"path": resolved, "bytes": size, "deleted": True}


def _backup(path):
    """Copy aside, preserving mode and times, before overwriting."""
    backup = path + BACKUP_SUFFIX
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        raise OsalError(
            f"refusing to overwrite {path!r}: could not write the backup "
            f"{backup!r} ({exc})") from None
    return backup


def _discard(path):
    """Remove a leftover temporary file, reporting nothing."""
    try:
        os.unlink(path)
    except OSError:
        pass
