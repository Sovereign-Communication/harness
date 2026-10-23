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

Screen capture and synthetic input are here for the same reason. Input is
opt-in and bounded: this package implements exact visible-window focus on
Windows and refuses kinds it cannot safely perform. Other backends can still
be supplied by an embedding application. Two reasons, and the second is the
one that matters:

* **A backend is a capability.** The native implementation is deliberately
  limited to focusing one uniquely titled visible window. The current click,
  type, key, scroll and submit parameters do not identify a safe destination,
  so those requests are refused rather than guessed.
* **Opt-in controls existence.** The native backend is created only for a
  driver whose operator enables both ``DRIVER_ALLOW_WRITE`` and
  ``DRIVER_NATIVE_INPUT``. It is driver-local, not process-global. The
  explicit registration API remains available to embedding applications.

So :func:`send_input` dispatches only to a backend the host supplied or that
the operator explicitly enabled for this driver. Missing and unsupported
paths refuse -- loudly, by name -- rather than appearing to succeed while
doing nothing.
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

from .errors import OsalError
from .png import decodable as png_decodable
from .png import dimensions as png_dimensions

#: How long a child process may run before it is killed.
DEFAULT_TIMEOUT = 30

#: The largest stdout we will read from a child, in bytes. A command that
#: prints without bound must not be able to exhaust memory in the driver.
MAX_CAPTURE_BYTES = 1 << 20

#: Declared platform support. Anything absent here is refused rather than
#: attempted, and the refusal says so by name.
SUPPORTED = ("linux", "darwin", "windows")

# Measured image-request bounds ported from driver-core's current preparation
# rule: 280..1568 px per side and at least 78,400 total pixels. The longest
# side sets the pixel ceiling; padding clears the minimum without stretching.
MAX_IMAGE_SIDE = 1568
MIN_IMAGE_SIDE = 280
MIN_IMAGE_PIXELS = MIN_IMAGE_SIDE * MIN_IMAGE_SIDE

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


def fit_target(width, height, *, max_side=MAX_IMAGE_SIDE,
               min_side=MIN_IMAGE_SIDE, min_pixels=MIN_IMAGE_PIXELS):
    """Choose a provider-valid canvas, shrinking content before padding."""
    for name, value in (("width", width), ("height", height),
                        ("max_side", max_side)):
        if not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    longest = max(width, height)
    for name, value in (("min_side", min_side), ("min_pixels", min_pixels)):
        if not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if min_side > max_side or min_pixels > max_side * max_side:
        raise ValueError("image floor cannot fit within max_side")
    if longest > max_side:
        scale = max_side / longest
        width = max(1, round(width * scale))
        height = max(1, round(height * scale))
    canvas_width = max(width, min_side)
    canvas_height = max(height, min_side)
    if canvas_width * canvas_height < min_pixels:
        grow = math.sqrt(min_pixels / (canvas_width * canvas_height))
        canvas_width = min(max_side, math.ceil(canvas_width * grow))
        canvas_height = min(max_side, math.ceil(canvas_height * grow))
        canvas_width = min(max_side, max(
            canvas_width, (min_pixels + canvas_height - 1) // canvas_height))
        canvas_height = max(
            canvas_height, (min_pixels + canvas_width - 1) // canvas_width)
    return canvas_width, canvas_height


def prepare_image_bytes(data, *, scaler=None):
    """Validate and fit a PNG capture, returning immutable prepared bytes.

    The original is never modified. PNG structure and every scanline are
    checked before a platform decoder is invoked; the composed output is
    decoded and dimension-checked again before it can become an asset.
    ``scaler`` is an internal test seam; production uses the platform scaler.
    """
    dimensions = png_dimensions(data)
    if dimensions is None or not png_decodable(data):
        return None, "capture is not a complete, decodable PNG"
    width, height = dimensions
    try:
        canvas_width, canvas_height = fit_target(width, height)
        scale = min(canvas_width / width, canvas_height / height, 1.0)
        draw_width = max(1, round(width * scale))
        draw_height = max(1, round(height * scale))
    except ValueError as exc:
        return None, f"cannot fit image: {exc}"

    try:
        with tempfile.TemporaryDirectory(prefix="driver-image-") as directory:
            path = os.path.join(directory, "prepared.png")
            with open(path, "wb") as handle:
                handle.write(data)
            compose = scaler or _scale_image
            try:
                scaled, detail = compose(
                    path, canvas_width, canvas_height,
                    draw_width, draw_height)
            except Exception as exc:
                return None, f"image scaling failed: {exc}"
            if scaled is None:
                return None, detail or "platform image preparation failed"
            if scaled != path:
                return None, "image scaler returned an unexpected output path"
            with open(scaled, "rb") as handle:
                prepared = handle.read()
    except (OSError, ValueError) as exc:
        return None, f"image preparation failed: {exc}"

    dimensions, detail = validate_prepared_image(prepared)
    if dimensions != (canvas_width, canvas_height):
        return None, detail or "prepared PNG dimensions do not match the fit"
    return prepared, ""


def validate_prepared_image(data):
    """Return dimensions only for a complete PNG inside the measured bounds."""
    dimensions = png_dimensions(data)
    if dimensions is None or not png_decodable(data):
        return None, "prepared image is not a complete, decodable PNG"
    try:
        if fit_target(*dimensions) != dimensions:
            return None, "prepared image dimensions are outside the image bounds"
    except ValueError as exc:
        return None, f"prepared image dimensions are invalid: {exc}"
    return dimensions, ""


def _scale_image(path, width, height, draw_width, draw_height):
    """Compose one PNG in place; platform tools only receive computed sizes."""
    name = platform_name()
    if name == "windows":
        return _scale_windows(path, width, height, draw_width, draw_height)
    if name == "darwin":
        return _scale_darwin(path, width, height, draw_width, draw_height)
    if name == "linux":
        return _scale_linux(path, width, height, draw_width, draw_height)
    return None, f"image scaling is not supported on {name}"


def _scale_windows(path, width, height, draw_width, draw_height):
    script = ("$ErrorActionPreference = 'Stop'; "
              "Add-Type -AssemblyName System.Drawing; "
              "$i = [System.Drawing.Image]::FromFile($env:DRIVER_SCALE_IN); "
              "$w = [int]$env:DRIVER_SCALE_W; "
              "$h = [int]$env:DRIVER_SCALE_H; "
              "$dw = [int]$env:DRIVER_SCALE_DW; "
              "$dh = [int]$env:DRIVER_SCALE_DH; "
              "$b = New-Object System.Drawing.Bitmap $w, $h; "
              "$g = [System.Drawing.Graphics]::FromImage($b); "
              "$g.Clear([System.Drawing.Color]::Black); "
              "$g.DrawImage($i, [int][math]::Floor(($w - $dw) / 2), "
              "[int][math]::Floor(($h - $dh) / 2), $dw, $dh); "
              "$g.Dispose(); $i.Dispose(); "
              "$b.Save($env:DRIVER_SCALE_IN, "
              "[System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()")
    result = run(["powershell", "-NoProfile", "-NonInteractive",
                  "-Command", script], timeout=45,
                 env={"DRIVER_SCALE_IN": path,
                      "DRIVER_SCALE_W": str(width),
                      "DRIVER_SCALE_H": str(height),
                      "DRIVER_SCALE_DW": str(draw_width),
                      "DRIVER_SCALE_DH": str(draw_height)})
    if not result.ok:
        return None, f"scaling failed: {result.error or result.stderr[:200]}"
    return path, ""


def _scale_darwin(path, width, height, draw_width, draw_height):
    result = run(["sips", "-z", str(draw_height), str(draw_width), path],
                 timeout=45)
    if not result.ok:
        return None, f"scaling failed: {result.error or result.stderr[:200]}"
    if (draw_width, draw_height) == (width, height):
        return path, ""
    result = run(["sips", "--padToHeightWidth", str(height), str(width),
                  "--padColor", "000000", path], timeout=45)
    if not result.ok:
        return None, f"scaling failed: {result.error or result.stderr[:200]}"
    return path, ""


def _scale_linux(path, width, height, draw_width, draw_height):
    result = run(["convert", path,
                  "-resize", f"{draw_width}x{draw_height}!",
                  "-background", "black", "-gravity", "center",
                  "-extent", f"{width}x{height}", path], timeout=45)
    if not result.ok:
        return None, f"scaling failed: {result.error or result.stderr[:200]}"
    return path, ""


# ---- synthetic input -----------------------------------------------------
# Declared, not discovered. A driver with no registered backend returns a
# named refusal, so an unregistered driver cannot half-work its way through a
# run by doing nothing and reporting success.

#: The kinds of synthetic input that exist. This is the contract between the
#: executor tier and a backend. Native Windows currently implements only
#: exact-title focus; remaining kinds need a backend that safely supports them.
#:
#: ``focus``, ``scroll`` and ``submit`` are declared separately from
#: ``click`` rather than folded into it. Folding them in would be a silent
#: semantic widening -- a backend asked to ``click`` cannot tell whether it
#: is nudging a cursor or activating a control whose effect cannot be undone,
#: and those deserve different handling at every layer above this one.
INPUT_KINDS = ("click", "type", "key", "focus", "scroll", "submit")

# The executor registry can either use a backend explicitly supplied to that
# driver or fall back to the embedding application's process registration.
# A sentinel keeps an explicit ``None`` fail-closed instead of falling back.
USE_REGISTERED_INPUT_BACKEND = object()

#: Embedding-application backends, keyed by platform name. Empty by default.
#: The optional native focus backend is driver-local and never stored here.
_INPUT_BACKENDS = {}
_INPUT_CAPABILITY_LOCK = threading.RLock()


class _FrozenInputBackend:
    """Immutable registry record for a backend and its declared capabilities."""

    __slots__ = ("_backend", "_supported_kinds")

    def __init__(self, backend, supported_kinds):
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_supported_kinds", supported_kinds)

    def __setattr__(self, name, value):
        raise AttributeError("registered input backend capabilities are immutable")

    @property
    def supported_kinds(self):
        return self._supported_kinds

    def __call__(self, kind, *, value=None, target=None):
        return self._backend(kind, value=value, target=target)


class _WindowsWindowApi:
    """Small, exact-title window adapter over the Windows user32 API."""

    def __init__(self):
        if platform_name() != "windows":
            raise OsalError("native window focus is supported only on Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.EnumWindows.argtypes = [
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        ]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int,
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL

    def _title(self, hwnd):
        length = self._user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        text = self._ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, text, len(text))
        return text.value

    def find_exact_visible(self, title):
        matches = []
        callback_type = self._ctypes.WINFUNCTYPE(
            self._wintypes.BOOL, self._wintypes.HWND, self._wintypes.LPARAM)

        @callback_type
        def visit(hwnd, _):
            if (self._user32.IsWindowVisible(hwnd)
                    and self._title(hwnd) == title):
                matches.append(hwnd)
            return True

        if not self._user32.EnumWindows(visit, 0):
            error = self._ctypes.get_last_error()
            raise OsalError(f"could not enumerate windows (Windows error {error})")
        return matches

    def focus(self, hwnd, expected_title):
        # Recheck the handle immediately before using it; a closed window may
        # have disappeared or the numeric handle may have been recycled.
        if (not self._user32.IsWindow(hwnd)
                or not self._user32.IsWindowVisible(hwnd)
                or self._title(hwnd) != expected_title):
            return False
        if self._user32.GetForegroundWindow() == hwnd:
            return True
        self._user32.SetForegroundWindow(hwnd)
        return self._user32.GetForegroundWindow() == hwnd


def native_input_backend(platform=None, *, window_api=None):
    """Return the opt-in native backend available on ``platform``.

    This backend intentionally supports only ``focus``: the current action
    vocabulary gives clicks no coordinates or control identifier, and gives
    typing/keys/scroll no window target. Guessing those destinations would
    widen the exact consent the operator supplied. Other platforms and kinds
    remain refused until they have equally bounded implementations.

    ``window_api`` is a test seam for the small window operations used below.
    """
    platform = platform or platform_name()
    if platform != "windows":
        return None
    api = window_api or _WindowsWindowApi()

    def focus_backend(kind, *, value=None, target=None):
        if kind != "focus":
            return False, (f"native Windows input does not support {kind!r}; "
                           "only exact-title focus is implemented")
        if value is not None:
            return False, "focus accepts a window title and no value"
        if not isinstance(target, str) or not target:
            return False, "focus requires a non-empty exact window title"
        matches = api.find_exact_visible(target)
        if len(matches) != 1:
            return False, (f"expected one visible window titled {target!r}; "
                           f"found {len(matches)}")
        if not api.focus(matches[0], target):
            return False, f"Windows refused to focus the window titled {target!r}"
        return True, "focused exact-title window"

    focus_backend.supported_kinds = ("focus",)
    return freeze_input_backend(focus_backend)


def freeze_input_backend(backend):
    """Capture declared kinds in a separate immutable registry record."""
    if backend is None or backend is USE_REGISTERED_INPUT_BACKEND:
        return backend
    if isinstance(backend, _FrozenInputBackend):
        return backend
    with _INPUT_CAPABILITY_LOCK:
        for registered in _INPUT_BACKENDS.values():
            if (isinstance(registered, _FrozenInputBackend)
                    and registered._backend is backend):
                return registered

        declared = INPUT_KINDS
        try:
            declared = getattr(backend, "supported_kinds", INPUT_KINDS)
            if isinstance(declared, (str, bytes, bytearray)):
                snapshot = ()
            else:
                snapshot = tuple(declared)
        except Exception:
            snapshot = ()
        if any(not isinstance(kind, str) for kind in snapshot):
            snapshot = ()
        return _FrozenInputBackend(backend, snapshot)


def register_input_backend(platform, backend):
    """Register the backend that performs synthetic input on ``platform``.

    ``backend`` is called as ``backend(kind, *, value, target)`` and returns
    ``(ok, detail)``. It is responsible for raising a clear error when the
    platform refuses the request -- an accessibility permission the host has
    not been granted, a display server nobody is connected to -- because
    that condition is not visible from inside this process. An optional
    ``supported_kinds`` iterable declares a narrower capability; if absent,
    the backend supports the full :data:`INPUT_KINDS` contract.
    """
    if platform not in SUPPORTED:
        raise OsalError(
            f"cannot register an input backend for {platform!r}; declared "
            f"platforms are {list(SUPPORTED)}")
    if not callable(backend):
        raise OsalError(
            f"an input backend must be callable, got {type(backend).__name__}")
    with _INPUT_CAPABILITY_LOCK:
        backend = freeze_input_backend(backend)
        _INPUT_BACKENDS[platform] = backend
    return backend


def disable_input_backend(platform=None):
    """Remove a backend, so the machine can be observed but never driven.

    The default state is already "no backend"; this exists so that a host
    which registered one can withdraw it without restarting, and so that the
    refusal path is reachable by policy rather than only by omission.
    """
    platform = platform or platform_name()
    with _INPUT_CAPABILITY_LOCK:
        return _INPUT_BACKENDS.pop(platform, None)


def input_backend_supports(backend, kind):
    """Whether a backend declares this input kind as executable.

    Backends predating the optional capability declaration continue to
    support the full contract. A backend that declares a narrower set is
    checked both before execution and again at the OS boundary, where the
    current registered backend is actually selected.
    """
    backend = freeze_input_backend(backend)
    if isinstance(backend, _FrozenInputBackend):
        supported = backend._supported_kinds
    else:
        supported = ()
    return kind in supported


def input_backends():
    """Which platforms currently have a backend. For ``/health``."""
    with _INPUT_CAPABILITY_LOCK:
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


def send_input(kind, value=None, *, target=None,
               backend=USE_REGISTERED_INPUT_BACKEND):
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
    if backend is USE_REGISTERED_INPUT_BACKEND:
        backend = input_backends().get(name)
    if backend is None:
        return False, (
            f"synthetic {kind} has no registered backend on {name}; "
            f"the embedding application must register one explicitly")
    backend = freeze_input_backend(backend)
    if not input_backend_supports(backend, kind):
        return False, f"input backend on {name} does not support {kind!r}"
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
    if not isinstance(ok, bool):
        return False, (
            f"{kind} backend on {name} returned a non-boolean success flag")
    return ok, str(detail)


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
