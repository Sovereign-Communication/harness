"""harness desktop: the pywebview shell around the harness web UI.

Phase 2 parity guarantee: the desktop app IS the web app. It launches
:hfunc:`harness.server.make_server` on a loopback port (auth token by
default, since a desktop window is an unshared context) and opens a native
window pointed at it. 100% of the UI work is reused; the shell adds only:

- a native window (no browser chrome) via pywebview when installed
- an automatic fallback to the system browser when pywebview is absent
  (``pip install sovereign-harness[desktop]`` for the native shell)

The server is the same one ``harness serve`` runs; the desktop shell owns
no endpoints and no policy.
"""
import argparse
import os
import secrets
import sys
import threading

from . import osal
from .server import make_server


def _open_window(url, token):
    """Try pywebview; fall back to the system browser. Returns the mode."""
    try:
        import webview  # optional: sovereign-harness[desktop]
    except ImportError:
        osal.open_url(url + "#" + token)
        return "browser"
    # A desktop window is an unshared context; the token rides the URL
    # fragment so it is visible to the page but never sent to the network.
    webview.create_window("Harness", url + "#" + token, width=1280, height=860)
    webview.start()
    return "webview"


def _read_token_file(path):
    """The persisted desktop token, or "" when absent, blank or unreadable."""
    if os.path.islink(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return ""
    try:
        os.chmod(path, 0o600)  # tighten a file an older build left open
    except OSError:
        pass
    return token


def _write_token_file(path, token):
    """Persist the desktop token readable by its owner only (best effort).

    The token is written to a fresh sibling file created ``O_EXCL`` with mode
    0600 (POSIX; Windows ignores the mode bits) and then moved over ``path``
    with ``os.replace``. A pre-existing, wider-open file (or one an attacker
    pre-created) is therefore never written to: it is replaced, not opened.
    A symlink at ``path`` is refused rather than followed or replaced. Failure
    to persist is not fatal: the token just will not survive a restart.
    """
    tmp = None
    try:
        if os.path.islink(path):
            return
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".desktop_token.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="harness-desktop",
        description="Native desktop window over the harness web UI (loopback).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--auth-token", default=None,
                    help="UI auth token (default: generated per session)")
    ap.add_argument("--browser", action="store_true",
                    help="skip the pywebview window and open the browser")
    opts = ap.parse_args(argv)
    if opts.host not in ("127.0.0.1", "localhost", "::1"):
        print("[FATAL] loopback bind only", file=sys.stderr)
        sys.exit(1)
    token = opts.auth_token or os.environ.get("HARNESS_UI_AUTH_TOKEN")
    if not token:
        from .config import CONFIG_DIR
        token_file = os.path.join(CONFIG_DIR, "desktop_token")
        token = _read_token_file(token_file)
        if not token:
            token = secrets.token_urlsafe(24)
            _write_token_file(token_file, token)
    httpd = make_server(opts.host, opts.port, auth_token=token)
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"[OK] harness desktop at {url} (token-protected)", file=sys.stderr)

    # The server must be serving before the window opens.
    t = threading.Thread(target=httpd.serve_forever,
                         name="harness-ui-server", daemon=True)
    t.start()
    mode = "browser" if opts.browser else None
    if mode is None:
        mode = _open_window(url, token)
    else:
        osal.open_url(url + "#" + token)
    if mode == "browser":
        # No pywebview: keep the server in the foreground so the process
        # has a lifecycle (Ctrl-C stops it).
        print("[OK] browser mode; Ctrl-C to stop the server", file=sys.stderr)
        try:
            t.join()
        except KeyboardInterrupt:
            print("\n[interrupted] UI server stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
