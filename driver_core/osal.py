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
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import OsalError
from .png import decodable as decodable_png
from .png import dimensions as png_dimensions_of_bytes

_INPUT_CAPABILITY_LOCK = threading.RLock()

#: Passed as ``send_input(backend=...)`` to mean "whatever is registered".
USE_REGISTERED_INPUT_BACKEND = object()

#: How long a child process may run before it is killed.
DEFAULT_TIMEOUT = 30

#: How long an application launcher may take to hand off before it is killed.
#: A launcher that has not returned in three seconds is not going to: the
#: action fails rather than occupying the step, which is what "spawn timeout:
#: 3s or fail" means in the vocabulary's own terms.
LAUNCH_TIMEOUT = 3.0

#: The largest stdout we will read from a child, in bytes. A command that
#: prints without bound must not be able to exhaust memory in the driver.
MAX_CAPTURE_BYTES = 1 << 20

#: Declared platform support. Anything absent here is refused rather than
#: attempted, and the refusal says so by name.
SUPPORTED = ("linux", "darwin", "windows")

_AUDIT_PATH_LOCKS = {}
_AUDIT_PATH_LOCKS_GUARD = threading.Lock()

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


@contextmanager
def audit_path_lock(path):
    """Lock one audit path across threads and processes.

    All reads that establish an audit snapshot and writes based on that
    snapshot use the same persistent sidecar lock. The sidecar is not removed
    after release because another process could then lock a different inode.
    """
    if not path:
        yield
        return

    canonical = os.path.normcase(
        os.path.realpath(os.path.abspath(os.fsdecode(path))))
    with _AUDIT_PATH_LOCKS_GUARD:
        thread_lock = _AUDIT_PATH_LOCKS.setdefault(
            canonical, threading.RLock())

    lock_path = canonical + ".lock"
    directory = os.path.dirname(lock_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with thread_lock:
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            platform = platform_name()
            if platform == "windows":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif platform in ("linux", "darwin"):
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                raise OsalError(
                    f"audit path locks are unsupported on {platform}")


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

#: Measured endpoint bounds: 280..1568 px per side and
#: 78,400..2,458,624 total pixels -- and since 1568 * 1568 is exactly the
#: total-pixel ceiling, fitting the per-side cap fits both bounds at the
#: top. The floor is closed here too (see :func:`fit_target`), so that
#: *any* capture leaves this module a valid request asset: an
#: under-minimum capture is padded out to the bounds rather than enlarged,
#: because enlarging invents pixels and a padded canvas invents nothing.
MAX_IMAGE_SIDE = 1568

#: The floor of the same bounds: per side, and total pixels (280 * 280).
#: Docs still say 256, but live 1-bit and RGB PNG probes on 2026-10-14
#: rejected 279-pixel sides and accepted 280 (including narrow rectangles).
MIN_IMAGE_SIDE = 280
MIN_IMAGE_PIXELS = 78400

#: PNG's eight magic bytes. Every capture here is PNG, but reading beats
#: trusting: the header is checked rather than assumed.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def capture_screen(*, max_side=MAX_IMAGE_SIDE):
    """Capture the screen, fitted to the image caps. Returns ``(path,
    detail)``; ``path`` may be None.

    A refusal is a first-class result. A driver handed an unreadable capture
    should route to a structured source, not ask a vision model to describe
    a file that does not exist.

    ``max_side`` fits the capture to the endpoint's image bounds at
    capture time (PNG in, PNG out), because the far end *rejects* an
    out-of-range image rather than resizing it -- so the file returned
    here is a valid request asset at both bounds, padded onto a canvas if
    it is small. Pass ``max_side=None`` to capture raw.
    """
    name = platform_name()
    if name == "windows":
        path, detail = _capture_windows()
    elif name == "darwin":
        path, detail = _capture_macos()
    elif name == "linux":
        path, detail = _capture_linux()
    else:
        return None, f"screen capture is not supported on {name}"
    if path is None or max_side is None:
        return path, detail
    fitted, why = fit_image(path, max_side=max_side)
    if fitted is None:
        return None, why
    return fitted, detail


def describe_screen():
    """Describe the foreground window as text. Returns ``(fields, detail)``.

    **The words mode, and the facts reader.** The endpoint takes pixels
    now, so this is no longer the only way a screen can reach a question.
    It still runs on every path: the window manager holds the title
    *exactly* rather than as a transcription of a picture, so these are the
    facts the state merges whatever the model judged -- and the evidence
    the pixels mode falls back to when no capture can be taken.

    Reading the foreground window through the window manager is also cheaper
    and more exact than describing a picture of it: the title is the title,
    not a transcription of a title. That is the same preference the tier order
    already encodes -- structured input before pixels -- applied inside the one
    tier that used to be the exception.

    The return is a dict of plain, already-typed values rather than prose, so
    a schema can consume it without parsing English. ``None`` is a refusal
    with a reason, exactly as in :func:`capture_screen`.
    """
    name = platform_name()
    if name == "windows":
        return _describe_windows()
    return None, (f"screen description is not supported on {name}; this is a "
                  f"platform gap, not a configuration one")


def launch_app(app_name, executable, *, timeout=LAUNCH_TIMEOUT):
    """Start one declared application, no shell, bounded hand-off.

    The three platforms spell "open this program" differently, and each
    spelling is declared here rather than guessed at a call site. Windows uses
    ``cmd /c start "" <exe>`` -- the empty first argument is the window title,
    without which ``start`` consumes the program name as the title. macOS uses
    ``open -a <app>``; Linux uses ``xdg-open <desktop-target>``.

    ``app_name`` is the allowlist's logical name and is used only in messages;
    ``executable`` is the value the platform launcher was declared with. A
    launcher that has not handed off within ``timeout`` is killed, so a stuck
    launcher is a failed action rather than a step that never ends.

    Returns ``(output, detail)``: ``(dict, "")`` on a hand-off,
    ``(None, reason)`` on refusal or failure.
    """
    if not executable:
        return None, f"no launch declaration for {app_name!r}"
    platform = platform_name()
    if platform == "windows":
        argv = ["cmd", "/c", "start", "", executable]
        creationflags = CREATE_NO_WINDOW
    elif platform == "darwin":
        argv = ["open", "-a", executable]
        creationflags = 0
    elif platform == "linux":
        argv = ["xdg-open", executable]
        creationflags = 0
    else:
        return None, (f"launch_app is not supported on {platform}; this is a "
                      f"platform gap, not a configuration one")
    try:
        process = subprocess.Popen(
            argv, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags)
    except OSError as exc:
        return None, f"could not start {app_name!r}: {exc}"
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return None, (f"launcher for {app_name!r} did not hand off within "
                      f"{timeout:g}s; the process was killed")
    return {"app": app_name, "argv": argv, "pid": process.pid}, ""


def _describe_windows():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, "no foreground window"

    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)

    window_class = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, window_class, 256)

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    # The process image is a *second* independent fact about the same window,
    # read through a different API. It is also the one place a path can leak a
    # username, so only the application name is kept -- the directory is
    # dropped here rather than at the call site, because a value that was
    # never assembled cannot be logged by accident.
    app = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if handle:
        try:
            image = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, image,
                                                   ctypes.byref(size)):
                app = image.value.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                if app.lower().endswith(".exe"):
                    app = app[:-4]
        finally:
            kernel32.CloseHandle(handle)

    return {
        "window_title": title.value,
        "foreground_app": app,
        "window_class": window_class.value,
    }, ""


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


# ---- image preparation --------------------------------------------------
# The endpoint takes a PNG or JPEG inside hard pixel bounds and *rejects*
# anything outside them rather than resizing it, so fitting a capture to
# those bounds is part of capture rather than a caller's problem: a file
# this module returns is a valid request asset or it is a named refusal,
# never a capture that will fail later at the far end.
#
# The arithmetic lives here, in Python: the canvas a capture must become
# and the size its content is drawn at on that canvas. The platform step
# only composes -- "fill a WxH canvas with the content drawn at DxH,
# centred". Two owners of one fit rule is how a Windows capture and a
# Linux capture end up obeying different caps.


def fit_within(width, height, max_side=MAX_IMAGE_SIDE):
    """The size to send so an image fits the caps; aspect preserved.

    Uniform scale toward the cap: an image already inside it is returned
    unchanged, and an oversized one comes back with its longest side
    exactly at ``max_side`` and the other side rounded to whole pixels.
    This is the only place the shrink decision is made -- platform scalers
    receive their target from here and get no say in it.
    """
    for name, value in (("width", width), ("height", height),
                        ("max_side", max_side)):
        if not isinstance(value, int) or value < 1:
            raise ValueError(
                f"{name} must be a positive integer, got {value!r}")
    longest = max(width, height)
    if longest <= max_side:
        return width, height
    scale = max_side / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def fit_target(width, height, *, max_side=MAX_IMAGE_SIDE,
               min_side=MIN_IMAGE_SIDE, min_pixels=MIN_IMAGE_PIXELS):
    """The canvas that makes this image a valid asset at *both* bounds.

    Shrinks first (:func:`fit_within`), then grows the canvas to clear the
    floor. Growing was deliberately absent while preparation only had to
    shrink -- enlarging invents pixels -- but "a valid asset for any
    capture" has to give somewhere, and the choice is padding over
    upscaling: a padded canvas is always constructible and invents
    nothing, while a uniform upscale cannot always reach the floor without
    breaching the cap (1000x20 would have to be 1980 px wide at its own
    aspect to reach 78,400 pixels). The content keeps its own size on
    that canvas; see :func:`fit_size`.

    The result always satisfies the endpoint's bounds: every side within
    ``min_side``..``max_side``, and at least ``min_pixels`` in total.
    """
    for name, value in (("min_side", min_side), ("min_pixels", min_pixels)):
        if not isinstance(value, int) or value < 1:
            raise ValueError(
                f"{name} must be a positive integer, got {value!r}")
    width, height = fit_within(width, height, max_side)
    if min_side > max_side or min_pixels > max_side * max_side:
        raise ValueError("image floor cannot fit within max_side")
    canvas_width = max(width, min_side)
    canvas_height = max(height, min_side)
    if canvas_width * canvas_height < min_pixels:
        grow = math.sqrt(min_pixels / (canvas_width * canvas_height))
        canvas_width = min(max_side, math.ceil(canvas_width * grow))
        canvas_height = min(max_side, math.ceil(canvas_height * grow))
        # A long side may hit its cap before proportional growth clears
        # the area floor. Add padding on the remaining axis using exact
        # integer division; feasible floors always fit in max_side squared.
        canvas_width = min(max_side, max(
            canvas_width, (min_pixels + canvas_height - 1) // canvas_height))
        canvas_height = max(
            canvas_height, (min_pixels + canvas_width - 1) // canvas_width)
    return canvas_width, canvas_height


@dataclass(frozen=True)
class ImageGeometry:
    """The original image and its centred, uniformly fitted content rectangle."""

    width: int
    height: int
    canvas_width: int
    canvas_height: int
    draw_width: int
    draw_height: int

    @property
    def offset(self):
        return ((self.canvas_width - self.draw_width) // 2,
                (self.canvas_height - self.draw_height) // 2)

    def source_box(self, box):
        """Canvas fractions -> original image/crop fractions, excluding padding.

        Crop coordinates remain relative to the crop supplied by the caller;
        preparation neither guesses a parent-frame origin nor stretches it.
        A box entirely in padding has no location in the original image.
        """
        x, y = self.offset
        result = {
            edge: max(0.0, min(1.0, (box[edge] * canvas - offset) / draw))
            for edge, canvas, offset, draw in (
                ("left", self.canvas_width, x, self.draw_width),
                ("right", self.canvas_width, x, self.draw_width),
                ("top", self.canvas_height, y, self.draw_height),
                ("bottom", self.canvas_height, y, self.draw_height))
        }
        if result["left"] >= result["right"] or result["top"] >= result["bottom"]:
            return None
        return result


@dataclass(frozen=True)
class PreparedImage:
    """One decoded snapshot: dispatch bytes and their source geometry."""

    data: bytes
    geometry: ImageGeometry


def fit_size(width, height, canvas_width, canvas_height):
    """The size the content is drawn at on its canvas.

    The largest uniform fit of ``width`` x ``height`` inside the canvas,
    never larger than the content itself: a canvas bigger than its content
    is padding, not an upscale.
    """
    for name, value in (("width", width), ("height", height),
                        ("canvas_width", canvas_width),
                        ("canvas_height", canvas_height)):
        if not isinstance(value, int) or value < 1:
            raise ValueError(
                f"{name} must be a positive integer, got {value!r}")
    scale = min(canvas_width / width, canvas_height / height, 1.0)
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def png_dimensions(path):
    """``(width, height)`` from a PNG's IHDR, or None if it is not a PNG.

    Reads the complete 33-byte IHDR, not the raster. Dispatch preparation
    separately forces decoding through the platform compositor.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(33)
    except OSError:
        return None
    if (len(head) < 33 or head[:8] != PNG_SIGNATURE
            or head[8:12] != b"\x00\x00\x00\x0d" or head[12:16] != b"IHDR"):
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def fit_image(path, *, max_side=MAX_IMAGE_SIDE, reader=None, scaler=None):
    """Fit one captured PNG to the image bounds, in place. ``(path, detail)``.

    Shrinks an oversized capture, pads an undersized one onto a canvas
    sized by :func:`fit_target`, and draws the content at the size
    :func:`fit_size` chose -- never enlarged. A capture already inside the
    bounds is left untouched.

    The seams are injectable so the fit-or-not decision is testable
    without a screen, a display server, or a platform image tool -- the
    same rule the rest of this package uses.
    """
    fitted, _geometry, detail = _fit_image(
        path, max_side=max_side, reader=reader, scaler=scaler)
    return fitted, detail


def prepare_crop(path, region):
    """Prepare an integer source crop without modifying the owned source PNG."""
    size = png_dimensions(path)
    if size is None or len(region) != 4:
        return None, "invalid crop source or region"
    with open(path, "rb") as handle:
        if not decodable_png(handle.read()):
            return None, "crop source PNG raster is not decodable"
    x, y, width, height = region
    if (any(type(v) is not int for v in region) or min(x, y) < 0
            or min(width, height) < 1 or x + width > size[0]
            or y + height > size[1]):
        return None, "crop outside source bounds"
    with tempfile.TemporaryDirectory(prefix="driver-crop-") as directory:
        output = os.path.join(directory, "crop.png")
        name = platform_name()
        if name == "windows":
            script = ("$ErrorActionPreference='Stop'; "
                      "Add-Type -AssemblyName System.Drawing; "
                      "$i=[System.Drawing.Bitmap]::FromFile($env:DRIVER_CROP_IN); "
                      "$r=New-Object System.Drawing.Rectangle "
                      "([int]$env:DRIVER_CROP_X),([int]$env:DRIVER_CROP_Y),"
                      "([int]$env:DRIVER_CROP_W),([int]$env:DRIVER_CROP_H); "
                      "$b=$i.Clone($r,$i.PixelFormat); $i.Dispose(); "
                      "$b.Save($env:DRIVER_CROP_OUT,"
                      "[System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()")
            result = run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", script], timeout=45, env={
                              "DRIVER_CROP_IN": path, "DRIVER_CROP_OUT": output,
                              "DRIVER_CROP_X": str(x), "DRIVER_CROP_Y": str(y),
                              "DRIVER_CROP_W": str(width),
                              "DRIVER_CROP_H": str(height)})
        elif name == "darwin":
            result = run(["sips", "--cropToHeightWidth", str(height), str(width),
                          "--cropOffset", str(y), str(x), path, "--out", output],
                         timeout=45)
        elif name == "linux":
            result = run(["convert", path, "-crop", f"{width}x{height}+{x}+{y}",
                          "+repage", output], timeout=45)
        else:
            return None, f"cropping is not supported on {name}"
        if not result.ok:
            return None, f"crop failed: {result.error or result.stderr[:200]}"
        if png_dimensions(output) != (width, height):
            return None, "crop dimensions do not match source region"
        return prepare_image(output)


def prepare_image(path, *, max_side=MAX_IMAGE_SIDE, reader=None, scaler=None):
    """Return (PreparedImage, detail), or a refusal, without changing source.

    Decode/re-encode even in-range files through the platform compositor.
    The temporary snapshot is removed before returning; callers own only
    immutable bytes and geometry from the same preparation operation.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="driver-image-") as directory:
            snapshot = os.path.join(directory, "prepared.png")
            shutil.copyfile(path, snapshot)
            with open(snapshot, "rb") as handle:
                if not decodable_png(handle.read()):
                    return None, "PNG raster is not decodable"
            fitted, geometry, detail = _fit_image(
                snapshot, max_side=max_side, reader=reader, scaler=scaler,
                decode=True)
            if fitted is None:
                return None, detail
            with open(fitted, "rb") as handle:
                data = handle.read()
            if not decodable_png(data):
                return None, "prepared PNG raster is not decodable"
            return PreparedImage(data, geometry), detail
    except OSError as exc:
        return None, f"image preparation failed: {exc}"


def _fit_image(path, *, max_side, reader=None, scaler=None, decode=False):
    """Compose a private snapshot, or the legacy fit_image in-place target."""
    reader = reader or png_dimensions
    scaler = scaler or _scale_image
    size = reader(path)
    if size is None:
        return None, None, f"not a readable PNG: {path}"
    width, height = size
    try:
        target = fit_target(width, height, max_side=max_side)
        if max_side > MAX_IMAGE_SIDE:
            raise ValueError(f"max_side must not exceed {MAX_IMAGE_SIDE}")
    except ValueError as exc:
        return None, None, f"cannot fit {path}: {exc}"
    draw_width, draw_height = fit_size(width, height, *target)
    geometry = ImageGeometry(width, height, *target, draw_width, draw_height)
    if target == (width, height) and not decode:
        return path, geometry, f"already within the image bounds: {width}x{height}"
    scaled, detail = scaler(path, target[0], target[1],
                            draw_width, draw_height)
    if scaled is None:
        return None, None, detail
    if reader(scaled) != target:
        return None, None, "prepared PNG dimensions do not match the fit target"
    return scaled, geometry, (
        f"fitted {width}x{height} into {target[0]}x{target[1]}"
        + (f"; {detail}" if detail else ""))


def _scale_image(path, width, height, draw_width, draw_height):
    """Compose one PNG in place, platform-native. ``(path, detail)``/refusal.

    A ``width`` x ``height`` canvas with the content drawn at
    ``draw_width`` x ``draw_height``, centred. Composition, not policy:
    both sizes were chosen in Python and the platform step gets no say in
    them.
    """
    name = platform_name()
    if name == "windows":
        return _scale_windows(path, width, height, draw_width, draw_height)
    if name == "darwin":
        return _scale_darwin(path, width, height, draw_width, draw_height)
    if name == "linux":
        return _scale_linux(path, width, height, draw_width, draw_height)
    return None, f"image scaling is not supported on {name}"


def _scale_windows(path, width, height, draw_width, draw_height):
    # System.Drawing, the same toolset the capture step already uses. The
    # values travel through the child environment rather than being
    # formatted into the script, so a path with a space or a quote can
    # only ever be a value. The script composes only: a black canvas with
    # the content centred at the draw size Python chose.
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
              "[System.Drawing.Imaging.ImageFormat]::Png); "
              "$b.Dispose()")
    result = run(["powershell", "-NoProfile", "-NonInteractive",
                  "-Command", script], timeout=45,
                 env={"DRIVER_SCALE_IN": path,
                      "DRIVER_SCALE_W": str(width),
                      "DRIVER_SCALE_H": str(height),
                      "DRIVER_SCALE_DW": str(draw_width),
                      "DRIVER_SCALE_DH": str(draw_height)})
    if not result.ok:
        return None, (f"scaling failed: "
                      f"{result.error or result.stderr[:200]}")
    return path, ""


def _scale_darwin(path, width, height, draw_width, draw_height):
    # sips ships with macOS and resizes in place; -z takes height, width.
    # Two steps because the draw size is Python's and the pad is sips':
    # the content first at its own ratio, then the canvas around it.
    result = run(["sips", "-z", str(draw_height), str(draw_width), path],
                 timeout=45)
    if not result.ok:
        return None, (f"scaling failed: "
                      f"{result.error or result.stderr[:200]}")
    if (draw_width, draw_height) == (width, height):
        return path, ""
    result = run(["sips", "--padToHeightWidth", str(height), str(width),
                  "--padColor", "000000", path], timeout=45)
    if not result.ok:
        return None, (f"scaling failed: "
                      f"{result.error or result.stderr[:200]}")
    return path, ""


def _scale_linux(path, width, height, draw_width, draw_height):
    # ImageMagick's convert, the declared Linux scaler. The trailing "!"
    # pins the exact draw size -- the aspect is already right, fit_size
    # chose a uniform scale -- and the canvas is a centred black extent.
    # A missing binary comes back from run() as a named refusal rather
    # than a crash.
    result = run(["convert", path,
                  "-resize", f"{draw_width}x{draw_height}!",
                  "-background", "black", "-gravity", "center",
                  "-extent", f"{width}x{height}", path],
                 timeout=45)
    if not result.ok:
        return None, (f"scaling failed: "
                      f"{result.error or result.stderr[:200]}")
    return path, ""


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
    with _INPUT_CAPABILITY_LOCK:
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
    if backend is None or backend is USE_REGISTERED_INPUT_BACKEND:
        backend = _INPUT_BACKENDS.get(name)
    if backend is None:
        return False, (
            f"synthetic {kind} has no registered backend on {name}; "
            f"the embedding application must register one explicitly")
    supported = tuple(getattr(backend, "supported_kinds", INPUT_KINDS))
    if kind not in supported:
        return False, (f"{name} input backend does not support {kind!r}; "
                       f"it supports {list(supported)}")
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


def atomic_write(path, content, *, encoding="utf-8", backup=True,
                 commit=True):
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

    ``commit=False`` performs every check and returns the *same* record,
    then touches nothing. Every field is already computed before the
    mutation -- whether the file existed, the byte count, the backup path
    -- so a rehearsal is this one code path with one branch closed rather
    than a second implementation free to disagree with the first. That is
    what makes a dry run a rehearsal instead of a report that says
    nothing.
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
    # Built once, before either exit, so the rehearsal record and the real
    # record are the same expression rather than two that can drift.
    record = {
        "path": resolved,
        "bytes": len(content.encode(encoding)),
        "replaced": existed,
        "backup": ((resolved + BACKUP_SUFFIX)
                   if (existed and backup) else None),
    }
    if not commit:
        # Every field is already known, so this is the real record. Returning
        # here is the whole rehearsal: one branch closed, no second path.
        return record
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
    return record


def remove_file(path, *, commit=True):
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

    ``commit=False`` performs all three refusals and reports the size that
    would be freed, without unlinking. The size is read before the unlink
    either way, so a rehearsal costs nothing extra and cannot drift from
    the real record.
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
        if commit:
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


def native_input_backend(platform=None, *, window_api=None):
    """Return the opt-in native backend available on ``platform``.

    This backend intentionally supports only ``focus``: the current action
    vocabulary gives clicks no coordinates or control identifier, and gives
    typing/keys/scroll no window target. Guessing those destinations would
    widen the exact consent the operator supplied. Other platforms and kinds
    remain refused until they have equally bounded implementations.

    ``window_api`` supplies the small window operations used below. There is
    no default implementation, so omitting it fails closed with a named error
    instead of a ``NameError`` from a class that does not exist.
    """
    platform = platform or platform_name()
    if platform != "windows":
        return None
    if window_api is None:
        raise RuntimeError(
            "native Windows input needs an injected window API; "
            "driver_core ships no default implementation")
    api = window_api

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


def validate_prepared_image(data):
    """Return dimensions only for a complete PNG inside the measured bounds."""
    dimensions = png_dimensions_of_bytes(data)
    if dimensions is None or not decodable_png(data):
        return None, "prepared image is not a complete, decodable PNG"
    try:
        if fit_target(*dimensions) != dimensions:
            return None, "prepared image dimensions are outside the image bounds"
    except ValueError as exc:
        return None, f"prepared image dimensions are invalid: {exc}"
    return dimensions, ""


def prepare_image_bytes(data, *, scaler=None):
    """Validate and fit a PNG capture, returning immutable prepared bytes.

    The original is never modified. PNG structure and every scanline are
    checked before a platform decoder is invoked; the composed output is
    decoded and dimension-checked again before it can become an asset.
    ``scaler`` is an internal test seam; production uses the platform scaler.
    """
    dimensions = png_dimensions_of_bytes(data)
    if dimensions is None or not decodable_png(data):
        return None, "capture is not a complete, decodable PNG"
    width, height = dimensions
    try:
        canvas_width, canvas_height = fit_target(width, height)
        draw_width, draw_height = fit_size(
            width, height, canvas_width, canvas_height)
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
