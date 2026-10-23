"""Deterministic local-time answers for "what time is it in X?" (ONE owner).

A clock question has a groundable place and a server-computable answer, so
it must never ride the model ladder: answering from memory is fabrication
with extra rounds. This lane resolves the place through the weather
module's keyless Open-Meteo geocoder (pinned hosts, no key, injectable
fetch seam) and reads the server's own local clock
(:func:`weather.current_time`), which is DST-correct with pure stdlib
parsing and no tz-database dependency. Zero model calls, zero spend.

A zone-only question ("what time zone is X in?") takes the cheaper
geo-only path: one call, zone name, no clock read.

Failure is an honest ``HarnessError``, never an invented time -- the agent
converts it into a deferral with a next step, mirroring the simple-action
lane's contract.
"""
import re
from datetime import datetime, timezone

from .errors import HarnessError
from .repo_scope import mentions_code_file
from .weather import current_time as _fetch_current
from .weather import extract_place, format_place, geocode

_TIME_RE = re.compile(
    r"\b(what(?:'s| is)? the time|what time|current time|local time|"
    r"time right now|time now)\b",
    re.IGNORECASE)
_TIME_IN_RE = re.compile(r"\btime\s+(in|at)\b", re.IGNORECASE)
# Event nouns veto EVERY branch below: a result/schedule question about an
# event ("kickoff", "screen time", "her marathon time") must never receive
# a deterministic clock answer with system authority.
_EVENT_RE = re.compile(
    r"\b(was|were|did|finished|ended|record|screen time|show time|"
    r"showtime|kickoff|marathon|race|flight|train|bus|schedule|movie|"
    r"game|match|score|concert|deadline|meeting|episode|halftime|"
    r"departure|arrival|premiere|finale|bedtime)\b",
    re.IGNORECASE)
_TZ_RE = re.compile(r"\btime\s*zones?\b", re.IGNORECASE)
_TZ_AFTER_RE = re.compile(
    r"time\s*zones?\s+(?:is\s+|for\s+)?([A-Za-z][A-Za-z .'\-]{1,40})",
    re.IGNORECASE)
_TZ_BEFORE_RE = re.compile(
    r"([A-Za-z][A-Za-z .'\-]{1,40}?)\s+time\s*zones?\b", re.IGNORECASE)
_TZ_STRIP_RE = re.compile(
    r"^(?:in|at|for|of|is|the|a)\s+|\s+(?:in|is|for|the|a)$", re.IGNORECASE)


def _clean_place(raw):
    """A regex-captured place candidate with edge prepositions trimmed."""
    name = re.sub(r"\s+", " ", (raw or "")).strip(" .'-\u2019")
    name = _TZ_STRIP_RE.sub("", name).strip(" .'-")
    return name if len(name) >= 2 else None


def _extract_tz_place(prompt):
    """Place from either zone-phrasing order ("X time zone", "time zone is X")."""
    for pattern in (_TZ_AFTER_RE, _TZ_BEFORE_RE):
        match = pattern.search(prompt or "")
        if match and _clean_place(match.group(1)):
            return _clean_place(match.group(1))
    return None


def is_timezone_question(prompt: str) -> bool:
    """True when a prompt asks which time zone a place is in (geo-only)."""
    text = prompt or ""
    if not text.strip():
        return False
    if mentions_code_file(text):
        return False
    if not _TZ_RE.search(text):
        return False
    if _EVENT_RE.search(text):
        return False
    try:
        return (_extract_tz_place(prompt) is not None
                or extract_place(prompt) is not None)
    except Exception:
        return False


def is_time_question(prompt: str) -> bool:
    """True when a prompt asks for the current wall time (never a zone).

    Disjoint from :func:`is_timezone_question` by construction -- a zone
    question matches zone only, even when its phrasing ("what time zone")
    also trips the clock words. A named place routes to the geocode+clock
    path; a bare clock phrasing ("what time is it?") answers in UTC.
    "time in/at" phrasing still needs a place; code-file mentions keep
    the edit lane; any event noun anywhere vetoes.
    """
    text = prompt or ""
    cleaned = text.strip().lower()
    if not cleaned:
        return False
    if is_timezone_question(text):
        return False
    if mentions_code_file(text):
        return False
    if _EVENT_RE.search(text):
        return False
    if not (_TIME_RE.search(text) or _TIME_IN_RE.search(text)):
        return False
    try:
        if extract_place(prompt) is not None:
            return True
    except Exception:
        return False
    # Placeless: only an explicit clock phrasing earns the UTC answer.
    # "time in X" without X has no target; "is it time yet?" never
    # reaches here (no TIME match).
    return bool(_TIME_RE.search(text) and not _TIME_IN_RE.search(text))


def is_clock_question(prompt: str) -> bool:
    """True when a prompt belongs to the clock lane (time or zone).

    The single funnel predicate: call sites route on this alone, and the
    time/zone split lives only in ``run_time_lookup`` (zone first), so no
    call site can order the two branches differently.
    """
    return is_timezone_question(prompt) or is_time_question(prompt)


def _format_offset(seconds):
    """UTC offset seconds as +/-HH:MM, or n/a when missing."""
    if seconds is None:
        return "n/a"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def lookup_timezone(question, *, fetch_json=None):
    """Answer-grade time zone for a question: one geocode call, no clock."""
    place = _extract_tz_place(question) or extract_place(question)
    if not place:
        raise HarnessError("time lookup: no place found in the question")
    geo = geocode(place, fetch_json=fetch_json)
    zone = geo.get("timezone")
    if not zone:
        raise HarnessError(
            f"time lookup: no time zone for '{place.strip()[:80]}'")
    where = format_place(geo, place)
    return {"place": where, "timezone": zone,
            "summary": f"{where} is in the {zone} time zone."}


def _utc_facts():
    """Server UTC clock for a placeless question: no fetch, no geocode."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return {"place": "UTC", "timezone": "UTC",
            "local_time": now, "utc_offset": "+00:00",
            "summary": f"It is {now} in UTC (UTC, UTC+00:00)."}


def lookup_time(question, *, fetch_json=None):
    """Answer-grade local time: place clock, or server UTC when placeless."""
    place = extract_place(question)
    if not place:
        text = question or ""
        if _TIME_RE.search(text) and not _TIME_IN_RE.search(text):
            return _utc_facts()
        raise HarnessError("time lookup: no place found in the question")
    geo = geocode(place, fetch_json=fetch_json)
    if geo.get("latitude") is None or geo.get("longitude") is None:
        raise HarnessError(
            f"time lookup: no coordinates for '{place.strip()[:80]}'")
    clock = _fetch_current(geo["latitude"], geo["longitude"],
                           fetch_json=fetch_json)
    zone = clock.get("timezone") or geo.get("timezone") or "n/a"
    where = format_place(geo, place)
    return {"place": where, "timezone": zone,
            "local_time": clock["local_time"],
            "utc_offset": _format_offset(clock.get("utc_offset_seconds")),
            "summary": (
                f"It is {clock['local_time']} in {where} "
                f"({zone}, UTC{_format_offset(clock.get('utc_offset_seconds'))}).")}
