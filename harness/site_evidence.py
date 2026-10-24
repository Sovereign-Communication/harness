"""Rendered site evidence for an audit.

A static fetch cannot see a client-rendered progress indicator, an active mode
button, or the controls a user actually gets.  This module renders one public
HTTPS page in a headless Chromium, captures the visible text plus targeted DOM
facts, exercises the on-grid/off-grid mode controls when present, and writes a
screenshot.  It is bounded to one page and never follows a redirect to a
private address.
"""
from __future__ import annotations

import re
import socket
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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                geolocation={"latitude": 30.2672, "longitude": -97.7431},
                permissions=["geolocation"],
            )
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
