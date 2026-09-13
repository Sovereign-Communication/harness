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

from .server import make_server


def _open_window(url, token):
    """Try pywebview; fall back to the system browser. Returns the mode."""
    try:
        import webview  # optional: sovereign-harness[desktop]
    except ImportError:
        import webbrowser
        webbrowser.open(url + "#" + token)
        return "browser"
    # A desktop window is an unshared context; the token rides the URL
    # fragment so it is visible to the page but never sent to the network.
    webview.create_window("Harness", url + "#" + token, width=1280, height=860)
    webview.start()
    return "webview"


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
    token = opts.auth_token or os.environ.get("HARNESS_UI_AUTH_TOKEN") \
        or secrets.token_urlsafe(24)
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
        import webbrowser
        webbrowser.open(url + "#" + token)
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
