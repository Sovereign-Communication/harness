"""Rendered site evidence for an audit.

A static fetch cannot see a client-rendered progress indicator, an active mode
button, or the controls a user actually gets.  This module renders one public
HTTPS page in a headless Chromium, captures the visible text plus targeted DOM
facts, exercises the on-grid/off-grid mode controls when present, and writes a
screenshot.  It is bounded to one page.

The network boundary is enforced for the whole browser session, not only the
initial URL.  Chromium runs behind a loopback CONNECT proxy that resolves each
destination once, requires every answer to be globally public, and opens the
tunnel to that validated address -- so a redirect, iframe, script, image,
fetch, or a DNS answer that changes between check and connection cannot reach
loopback, private, link-local, reserved, or metadata addresses.  Port 443 is
the only tunnel and HTTPS the only scheme; a route guard refuses a disallowed
request before the browser even asks the proxy.
"""
from __future__ import annotations

import re
import select
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import HarnessError
from .events import emit

_MODE_RE = re.compile(r"cut my electric bill|off[- ]?grid", re.IGNORECASE)
_PROGRESS_SELECTOR = (
    "[class*=progress], [id*=progress], [role=progressbar], progress")
_MAX_TEXT = 7000


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


#: Largest CONNECT head accepted from the browser.  A real one is a few
#: hundred bytes; the cap keeps a hostile request from growing memory.
_MAX_TUNNEL_HEAD = 64 * 1024


def _public_addresses(host, port=443, resolve=None):
    """Resolve *host* once and return every answer, all globally public.

    The same predicate the fetch lane uses (:mod:`harness.web`), applied to one
    resolution whose answers the caller then connects to.  Resolving once and
    connecting to that exact answer is what makes the boundary immune to a DNS
    reply that changes between the check and the connection.
    """
    from .web import _validate_public_addresses

    resolver = resolve or socket.getaddrinfo
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:  # noqa: BLE001 - a DNS failure is a refusal
        raise HarnessError(
            f"site evidence refused: DNS resolution failed for '{host}': "
            f"{exc}") from exc
    addresses = []
    for info in infos or []:
        sockaddr = info[4] if len(info) > 4 else None
        address = sockaddr[0] if sockaddr else None
        if address:
            addresses.append(address)
    return _validate_public_addresses(host, addresses)


def _split_authority(authority):
    """Split ``host[:port]`` from a CONNECT line, including ``[v6]:port``."""
    authority = (authority or "").strip()
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ValueError("unterminated IPv6 literal")
        host = authority[1:end]
        rest = authority[end + 1:]
        if rest and not rest.startswith(":"):
            raise ValueError("malformed authority")
        return host, int(rest[1:]) if rest else 443
    host, sep, port = authority.rpartition(":")
    if not sep:
        return authority, 443
    return host, int(port)


class _ValidatedEgressProxy:
    """A loopback CONNECT proxy that validates and pins every destination.

    Chromium is pointed at this proxy for the whole session, with the implicit
    localhost bypass disabled, so redirects, iframes, scripts, images and
    fetches all arrive here.  A CONNECT is refused unless the authority is
    hostname-or-IP on port 443, every resolved answer is globally public, and
    the tunnel opens to that validated answer rather than being re-resolved by
    the browser.  Nothing outside the proxy can be reached.
    """

    def __init__(self, *, resolve=None, connect=None):
        self._resolve = resolve or socket.getaddrinfo
        self._connect = connect or socket.create_connection
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._closed = threading.Event()
        self._workers = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def server_url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def close(self):
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        for worker in list(self._workers):
            worker.join(timeout=1)

    def _serve(self):
        while not self._closed.is_set():
            try:
                client, _address = self._server.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._handle, args=(client,),
                                      daemon=True)
            self._workers.append(worker)
            worker.start()

    @staticmethod
    def _read_head(client):
        """Read one request head, bounded; None when the peer sends nothing."""
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = client.recv(4096)
            if not chunk:
                return None
            buffer += chunk
            if len(buffer) > _MAX_TUNNEL_HEAD:
                raise ValueError("request head too large")
        return buffer.split(b"\r\n\r\n", 1)[0].decode("latin-1")

    @staticmethod
    def _refuse(client, reason):
        body = reason.encode("ascii", "replace")
        client.sendall(
            b"HTTP/1.1 403 Forbidden\r\n"
            b"Content-Type: text/plain; charset=ascii\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") +
            b"\r\nConnection: close\r\n\r\n" + body)

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(10)
            head = self._read_head(client)
            if head is None:
                return
            first_line = head.split("\r\n", 1)[0]
            parts = first_line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                self._refuse(client, "only HTTPS tunnels are allowed")
                return
            try:
                host, port = _split_authority(parts[1])
            except ValueError:
                self._refuse(client, "malformed destination")
                return
            if not host or port != 443:
                self._refuse(client, "only port 443 may be reached")
                return
            try:
                addresses = _public_addresses(host, port, self._resolve)
            except HarnessError:
                self._refuse(client, "destination is not public")
                return
            upstream = self._connect((addresses[0], port), timeout=10)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(client, upstream)
        except Exception:  # noqa: BLE001 - one tunnel never kills the proxy
            pass
        finally:
            for sock in (upstream, client):
                if sock is None:
                    continue
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _relay(client, upstream):
        """Copy bytes both ways until either side closes or goes idle."""
        peers = {client: upstream, upstream: client}
        while True:
            try:
                readable, _writable, errored = select.select(
                    list(peers), [], list(peers), 30)
            except OSError:
                return
            if errored:
                return
            if not readable:
                return  # idle beyond the bound: close, never grow forever
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                peers[source].sendall(data)


def _request_guard():
    """A route handler that revalidates every browser request.

    The proxy is the enforcement -- it pins the address it validated -- and
    this guard refuses a disallowed URL before the tunnel is even requested,
    so a redirect, iframe, script, image, fetch or popup aimed at a private
    destination ends as a blocked request rather than a silent load.  The
    verdict is cached per host: a page with fifty images resolves once.
    """
    verdicts = {}

    def guard(route):
        try:
            url = route.request.url
            parsed = urllib.parse.urlsplit(url)
            scheme = (parsed.scheme or "").lower()
            if scheme in ("data", "blob", "about"):
                route.continue_()
                return
            host = (parsed.hostname or "").lower()
            if scheme != "https" or not host:
                emit("site_evidence", phase="blocked", url=url,
                     reason="only https requests are rendered")
                route.abort("blockedbyclient")
                return
            if host in verdicts:
                reason = verdicts[host]
            else:
                try:
                    _public_https(url)
                except HarnessError as exc:
                    reason = verdicts[host] = str(exc)
                else:
                    reason = verdicts[host] = None
            if reason is not None:
                emit("site_evidence", phase="blocked", url=url,
                     reason=reason)
                route.abort("blockedbyclient")
                return
            route.continue_()
        except Exception:  # noqa: BLE001 - an unhandled route hangs the page
            try:
                route.abort("failed")
            except Exception:  # noqa: BLE001
                pass

    return guard


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
    with sync_playwright() as p, _ValidatedEgressProxy() as proxy:
        browser = p.chromium.launch(
            headless=True,
            # Every request -- redirects and subresources included -- is
            # forced through the validated proxy.  `<-loopback>` cancels
            # Chromium's implicit localhost bypass, which would otherwise
            # let a loopback destination skip the proxy entirely.
            proxy={"server": proxy.server_url, "bypass": "<-loopback>"})
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                geolocation={"latitude": 30.2672, "longitude": -97.7431},
                permissions=["geolocation"],
                service_workers="block",
            )
            context.route("**/*", _request_guard())
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
