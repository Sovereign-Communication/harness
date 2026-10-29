"""Rendered site evidence with a fail-closed browser egress boundary.

A static fetch cannot see a client-rendered progress indicator, an active mode
button, or the controls a user actually gets. This module renders one public
HTTPS page in headless Chromium, captures visible text and targeted DOM facts,
exercises the on-grid/off-grid mode controls when present, and writes a
screenshot. Browser HTTP(S) requests and secure WebSockets must pass through an
in-process HTTP CONNECT proxy. At each connection the proxy resolves the target
once, rejects the entire answer set if any address is not globally routable,
and opens the socket to one of those exact numeric addresses. Chromium's
implicit loopback and link-local proxy bypass is removed; the proxy has no
direct fallback. Non-HTTPS URLs, QUIC, service workers, and WebRTC are blocked
or disabled so the page cannot use those paths to bypass the proxy.
"""
from __future__ import annotations

import ipaddress
import re
import select
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import HarnessError
from .events import emit

_MODE_RE = re.compile(r"cut my electric bill|off[- ]?grid", re.IGNORECASE)
_PROGRESS_SELECTOR = (
    "[class*=progress], [id*=progress], [role=progressbar], progress")
_MAX_TEXT = 7000
_EGRESS_CONNECT_TIMEOUT = 10.0
_CHROMIUM_EGRESS_ARGS = (
    "--proxy-bypass-list=<-loopback>",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
_DISABLE_WEBRTC_SCRIPT = """
for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection"]) {
  try {
    Object.defineProperty(globalThis, name, {
      value: undefined, configurable: false, writable: false
    });
  } catch (_) {}
}
"""


def _parse_connect_target(authority: str):
    """Return a normalized ``(host, port)`` from an HTTP CONNECT authority."""
    if not authority or any(char.isspace() or ord(char) < 0x20
                            for char in authority):
        raise ValueError("invalid CONNECT authority")
    parsed = urllib.parse.urlsplit("//" + authority)
    if (parsed.netloc != authority or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password
            or "@" in authority or not parsed.hostname):
        raise ValueError("invalid CONNECT authority")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError:
        raise ValueError("invalid CONNECT port") from None
    if not 1 <= port <= 65535:
        raise ValueError("invalid CONNECT port")

    host = parsed.hostname
    if "%" in host:
        raise ValueError("scoped IP literals are not allowed")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("invalid CONNECT host") from None
        if not host or len(host) > 253:
            raise ValueError("invalid CONNECT host") from None
    return host, port


def _connect_pinned_address(address: str, port: int, timeout: float):
    """Open a socket to a validated numeric address without resolving again."""
    if "%" in address:
        raise OSError("scoped IP address refused")
    parsed = ipaddress.ip_address(address)
    if not parsed.is_global:
        raise OSError("non-public IP address refused")
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sockaddr = ((str(parsed), port, 0, 0) if parsed.version == 6
                    else (str(parsed), port))
        sock.connect(sockaddr)
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


class _BrowserEgressProxy:
    """Loopback-only CONNECT proxy pinning each tunnel to public DNS results."""

    def __init__(self, *, resolve_fn=None, connect_fn=None):
        self._resolve_fn = resolve_fn or socket.getaddrinfo
        self._connect_fn = connect_fn or _connect_pinned_address
        self._server = None
        self._thread = None

    @property
    def url(self):
        if self._server is None:
            raise RuntimeError("browser egress proxy has not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        proxy = self

        class ProxyServer(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

        class ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def _response(self, status):
                self.send_response_only(status)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.close_connection = True

            def _reject(self):
                self._response(403)

            def do_CONNECT(self):
                try:
                    host, port = _parse_connect_target(self.path)
                    from .web import _resolved_addresses_public

                    addresses = _resolved_addresses_public(
                        host, proxy._resolve_fn)
                except Exception:
                    self._reject()
                    return

                upstream = None
                for address in addresses:
                    try:
                        upstream = proxy._connect_fn(
                            address, port, _EGRESS_CONNECT_TIMEOUT)
                        break
                    except OSError:
                        continue
                if upstream is None:
                    self._response(502)
                    return

                try:
                    self.send_response_only(200, "Connection Established")
                    self.end_headers()
                    self.wfile.flush()
                    _relay_tunnel(self.connection, upstream)
                except OSError:
                    pass
                finally:
                    self.close_connection = True
                    upstream.close()

            def do_GET(self):
                self._reject()

            def do_POST(self):
                self._reject()

            def do_PUT(self):
                self._reject()

            def do_DELETE(self):
                self._reject()

            def do_OPTIONS(self):
                self._reject()

            def do_HEAD(self):
                self._reject()

            def do_PATCH(self):
                self._reject()

        self._server = ProxyServer(("127.0.0.1", 0), ProxyHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="harness-site-egress-proxy", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        return False


def _relay_tunnel(client: socket.socket, upstream: socket.socket):
    """Copy bytes in both directions until either side closes the tunnel."""
    peers = (client, upstream)
    while True:
        try:
            readable, _writable, _exceptional = select.select(peers, [], [], 0.5)
        except (OSError, ValueError):
            return
        for source in readable:
            destination = upstream if source is client else client
            try:
                data = source.recv(65536)
                if not data:
                    return
                destination.sendall(data)
            except OSError:
                return


def _guard_browser_request(route):
    """Permit only public-HTTPS-capable URL forms; proxy validates each host."""
    try:
        parsed = urllib.parse.urlsplit(route.request.url)
        scheme = parsed.scheme.lower()
        if scheme in {"about", "blob", "data"}:
            route.continue_()
            return
        if (scheme not in {"https", "wss"} or not parsed.hostname
                or parsed.username or parsed.password or "@" in parsed.netloc):
            route.abort("blockedbyclient")
            return
        _ = parsed.port  # Fail closed on malformed ports.
        route.continue_()
    except Exception:
        try:
            route.abort("blockedbyclient")
        except Exception:
            pass


def _public_https(url: str) -> str:
    parsed = urllib.parse.urlsplit(url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        raise HarnessError("site evidence refused: only https:// URLs are rendered")
    from .web import _resolved_addresses_public

    _resolved_addresses_public(parsed.hostname.lower(), socket.getaddrinfo)
    return parsed.hostname.lower()


def _safe_slug(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "site").lower()
    return re.sub(r"[^a-z0-9.-]+", "-", host)[:80] or "site"


def _screenshot_path(url: str, directory: Optional[Path]) -> Path:
    base = Path(directory) if directory else Path.cwd() / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / f"site-{_safe_slug(url)}-{stamp}.png"


def _screenshot_dir(configured: Optional[Path]) -> Path:
    if configured:
        return Path(configured)
    from .paths import get_config_dir

    return Path(get_config_dir()) / "site_evidence"


def _element_facts(page, selector: str, limit: int = 12) -> List[Dict[str, str]]:
    try:
        count = page.locator(selector).count()
    except Exception:
        return []
    facts: List[Dict[str, str]] = []
    for index in range(min(count, limit)):
        item = page.locator(selector).nth(index)
        try:
            facts.append({
                "id": item.get_attribute("id") or "",
                "class": item.get_attribute("class") or "",
                "text": " ".join((item.inner_text() or "").split())[:200],
            })
        except Exception:
            continue
    return facts


def _mode_facts(page) -> List[Dict[str, str]]:
    try:
        count = page.locator("button").count()
    except Exception:
        return []
    modes: List[Dict[str, str]] = []
    for index in range(min(count, 60)):
        item = page.locator("button").nth(index)
        try:
            text = " ".join((item.inner_text() or "").split())
        except Exception:
            continue
        if not _MODE_RE.search(text):
            continue
        modes.append({
            "label": text[:120],
            "class": item.get_attribute("class") or "",
            "pressed": item.get_attribute("aria-pressed") or "",
        })
    return modes


def _click_mode(page, label: str) -> Dict[str, str]:
    """Click one mode button and return what the DOM shows afterwards."""
    try:
        button = page.get_by_role("button", name=label, exact=False)
        if button.count() == 0:
            return {"action": "click", "target": label, "result": "not found"}
        target = button.first
        target.click(timeout=4000)
        page.wait_for_timeout(500)
        classes = target.get_attribute("class") or ""
        pressed = target.get_attribute("aria-pressed") or ""
        return {
            "action": "click",
            "target": label,
            "result": f"class={classes} aria-pressed={pressed}",
        }
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        return {"action": "click", "target": label,
                "result": f"failed: {type(exc).__name__}"}


def evidence_summary(evidence: Dict[str, Any]) -> str:
    """Compact, model-readable summary of one rendered page."""
    progress = evidence.get("progress") or []
    modes = evidence.get("modes") or []
    actions = evidence.get("actions") or []
    steps = evidence.get("steps") or []
    lines = [
        f"RENDERED SITE EVIDENCE ({evidence.get('engine') or 'browser'}):"
        f" {evidence.get('final_url') or evidence.get('url')}",
        f"Title: {evidence.get('title') or ''}",
        "Progress/DOM markers: " + (
            "; ".join(
                f"{item.get('id') or item.get('class') or 'progress'}="
                f"{item.get('text')!r}" for item in progress) or "none"),
        "Mode controls: " + (
            "; ".join(
                f"{item.get('label')!r} class={item.get('class')!r}"
                for item in modes) or "none"),
        "Driven actions: " + (
            "; ".join(
                f"{item.get('target')} -> {item.get('result')}"
                for item in actions) or "none"),
        "Progress transitions: " + (
            "; ".join(
                f"{item.get('step')} ({item.get('status')})"
                for item in steps) or "none"),
        f"Interactive controls: {evidence.get('input_count', 0)} inputs/selects",
    ]
    if evidence.get("status"):
        lines.append("Final status: " + str(evidence["status"])[:500])
    if evidence.get("result_excerpt"):
        lines.append("Result excerpt: " + str(evidence["result_excerpt"])[:1200])
    if evidence.get("screenshot"):
        lines.append(f"Screenshot: {evidence['screenshot']}")
    if evidence.get("text"):
        lines.append("Visible page text (excerpt):\n"
                     + str(evidence["text"])[:_MAX_TEXT])
    return "\n".join(lines)


def render_site_evidence(
    url: str,
    *,
    screenshot_dir: Optional[Path] = None,
    timeout: float = 30.0,
    drive_modes: bool = True,
) -> Dict[str, Any]:
    """Render one public site and return DOM/action evidence.

    Headless first: an audit must not flash a browser window on the operator's
    desktop.  The page is closed in every path.  A missing renderer is an
    honest HarnessError, never a silent empty result.
    """
    _public_https(url)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise HarnessError(
            "site evidence unavailable: playwright is not installed") from exc
    emit("site_evidence", phase="start", url=url)
    final_url = url
    title = ""
    text = ""
    screenshot = ""
    with sync_playwright() as p, _BrowserEgressProxy() as egress_proxy:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": egress_proxy.url},
            args=list(_CHROMIUM_EGRESS_ARGS),
        )
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                geolocation={"latitude": 30.2672, "longitude": -97.7431},
                permissions=["geolocation"],
                service_workers="block",
            )
            context.route("**/*", _guard_browser_request)
            context.add_init_script(_DISABLE_WEBRTC_SCRIPT)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded",
                      timeout=int(timeout * 1000))
            page.wait_for_timeout(2500)
            title = (page.title() or "").strip()
            progress = _element_facts(page, _PROGRESS_SELECTOR)
            modes = _mode_facts(page)
            actions: List[Dict[str, str]] = []
            steps: List[Dict[str, str]] = []
            status = ""
            if progress:
                steps.append({"step": progress[0].get("text") or "",
                              "status": "initial"})
            if drive_modes:
                for label in ("Off-grid / Backup", "Cut My Electric Bill"):
                    if any(label.lower() in (mode.get("label") or "").lower()
                           for mode in modes):
                        actions.append(_click_mode(page, label))
            if drive_modes:
                precise = page.get_by_text(
                    "Use my precise location", exact=False)
                if precise.count():
                    try:
                        precise.first.click(timeout=4000)
                        page.wait_for_timeout(1600)
                        actions.append({
                            "action": "click",
                            "target": "Use my precise location",
                            "result": "simulated Austin geolocation applied",
                        })
                    except Exception as exc:  # noqa: BLE001
                        actions.append({
                            "action": "click",
                            "target": "Use my precise location",
                            "result": f"failed: {type(exc).__name__}",
                        })
                run = page.locator("#btnRunSizing")
                if run.count():
                    try:
                        run.first.click(timeout=5000)
                        actions.append({
                            "action": "click", "target": "Size My System",
                            "result": "sizing started",
                        })
                        deadline = time.monotonic() + min(timeout, 35.0)
                        last_step = ""
                        while time.monotonic() < deadline:
                            page.wait_for_timeout(500)
                            try:
                                step = " ".join(
                                    (page.locator("#guidedProgress").inner_text()
                                     or "").split())
                            except Exception:
                                step = ""
                            try:
                                status = " ".join(
                                    (page.locator("#sizingStatus").inner_text()
                                     or "").split())
                            except Exception:
                                status = ""
                            if step and step != last_step:
                                steps.append({"step": step, "status": status[:240]})
                                last_step = step
                            if "✅" in status or "Best pick" in status:
                                break
                    except Exception as exc:  # noqa: BLE001
                        actions.append({
                            "action": "click", "target": "Size My System",
                            "result": f"failed: {type(exc).__name__}",
                        })
            text = " ".join((page.inner_text("body") or "").split())
            progress = _element_facts(page, _PROGRESS_SELECTOR) or progress
            screenshot_file = _screenshot_path(
                url, _screenshot_dir(screenshot_dir))
            page.screenshot(path=str(screenshot_file), full_page=False)
            screenshot = str(screenshot_file)
            final_url = (page.url or url).split("#", 1)[0]
            input_count = page.locator("input,select,textarea").count()
            result_excerpt = ""
            for marker in ("Best pick", "Your result", "Recommended"):
                index = text.find(marker)
                if index >= 0:
                    result_excerpt = text[index:index + 1200]
                    break
        finally:
            browser.close()
    evidence = {
        "rendered": True,
        "engine": "chromium headless",
        "url": url,
        "final_url": final_url,
        "title": title,
        "text": text[:_MAX_TEXT],
        "progress": progress,
        "modes": modes,
        "actions": actions,
        "steps": steps,
        "status": status,
        "result_excerpt": result_excerpt,
        "input_count": input_count,
        "screenshot": screenshot,
    }
    emit("site_evidence", phase="done", url=url,
         progress=len(progress), modes=len(modes),
         screenshot=bool(screenshot))
    return evidence


__all__ = ["evidence_summary", "render_site_evidence"]
