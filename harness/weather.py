"""Live weather facts for the lookup lane (ONE owner).

Open-Meteo needs no key: geocoding resolves a place name/ZIP to
coordinates, and the forecast API returns daily facts the model must
answer from instead of inventing. Network discipline mirrors
``harness/web.py`` -- https only, pinned hosts, no redirects, bounded
timeouts -- and every HTTP call goes through an injectable fetcher so
tests never touch the network.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .errors import HarnessError
from .events import emit

GEOCODE_HOST = "geocoding-api.open-meteo.com"
FORECAST_HOST = "api.open-meteo.com"
_WEATHER_HOSTS = frozenset({GEOCODE_HOST, FORECAST_HOST})

_TIMEOUT = 12.0
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_PLACE_RE = re.compile(
    r"(?:\bin\b|\bfor\b|\bnear\b|\bat\b|\bof\b)\s+"
    r"([A-Za-z][A-Za-z .'\-]{1,40}?)(?:,|\?|$)", re.IGNORECASE)
_DAY_RE = re.compile(
    r"\b(today|tonight|tomorrow|tmrw|tmr|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", re.IGNORECASE)

_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with rime", 51: "light drizzle",
    53: "drizzle", 55: "dense drizzle", 56: "freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers",
    86: "snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5,
         "sun": 6}


def _https_json(url):
    """GET one pinned-host https URL and parse its JSON body."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise HarnessError("weather fetch refused: only https:// URLs")
    if (parsed.hostname or "").lower() not in _WEATHER_HOSTS:
        raise HarnessError(
            f"weather fetch refused: host '{parsed.hostname}' is not Open-Meteo")
    req = urllib.request.Request(url, headers={"User-Agent": "harness-lookup/1.0"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise HarnessError(f"weather fetch failed: {e}") from e


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A weather fetch must stay on the pinned host; never follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def extract_place(question):
    """Best place string from a lookup question: name, else ZIP, else None."""
    q = question or ""
    m = _PLACE_RE.search(q)
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .'-")
        if len(name) >= 2:
            return name
    m = _ZIP_RE.search(q)
    return m.group(0) if m else None


def day_offset(question, *, today_weekday=None):
    """0=today, 1=tomorrow, or the offset to a named weekday (0-6)."""
    import datetime
    q = (question or "").lower()
    m = _DAY_RE.search(q)
    if not m:
        return 0
    word = m.group(1).lower()
    if word in ("today", "tonight", "now"):
        return 0
    if word in ("tomorrow", "tmrw", "tmr"):
        return 1
    if today_weekday is None:
        today_weekday = datetime.date.today().weekday()
    target = _ABBR.get(word[:3], _WEEKDAYS.index(word) if word in _WEEKDAYS else 0)
    return (target - today_weekday) % 7


def geocode(place, *, fetch_json=None):
    """Resolve a place to {name, latitude, longitude, country}."""
    if not place or not place.strip():
        raise HarnessError("weather lookup: empty place")
    fetch = fetch_json or _https_json
    url = ("https://geocoding-api.open-meteo.com/v1/search?name="
           + urllib.parse.quote_plus(place.strip()[:80])
           + "&count=1&language=en&format=json")
    emit("weather_fetch", phase="start", what="geocode", place=place.strip()[:80])
    try:
        doc = fetch(url)
    except HarnessError:
        raise
    except Exception as e:  # a fake fetcher must not crash the lane
        raise HarnessError(f"weather lookup failed: {e}") from e
    results = (doc or {}).get("results") or []
    if not results:
        emit("weather_fetch", phase="end", ok=False, what="geocode")
        raise HarnessError(f"weather lookup: no match for '{place.strip()[:80]}'")
    top = results[0]
    out = {"name": top.get("name"), "latitude": top.get("latitude"),
           "longitude": top.get("longitude"), "country": top.get("country"),
           "region": top.get("admin1")}
    emit("weather_fetch", phase="end", ok=True, what="geocode",
         name=out["name"])
    return out


def forecast(latitude, longitude, *, days=3, fetch_json=None):
    """Daily forecast facts for coordinates (at most ``days`` days)."""
    fetch = fetch_json or _https_json
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + str(latitude)
           + "&longitude=" + str(longitude)
           + "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,windspeed_10m_max"
           + "&timezone=auto&forecast_days=" + str(max(1, min(int(days), 7))))
    emit("weather_fetch", phase="start", what="forecast")
    try:
        doc = fetch(url)
    except HarnessError:
        raise
    except Exception as e:
        raise HarnessError(f"weather lookup failed: {e}") from e
    daily = (doc or {}).get("daily") or {}
    times = daily.get("time") or []
    if not times:
        emit("weather_fetch", phase="end", ok=False, what="forecast")
        raise HarnessError("weather lookup: forecast has no days")
    out = []
    for i, day in enumerate(times):
        code = (daily.get("weathercode") or [None] * len(times))[i]
        at = lambda key: (daily.get(key) or [None] * len(times))[i]  # noqa: E731
        out.append({"date": day, "summary": _WMO.get(code, "unknown"),
                    "high_c": at("temperature_2m_max"),
                    "low_c": at("temperature_2m_min"),
                    "precip_pct": at("precipitation_probability_max"),
                    "wind_kmh": at("windspeed_10m_max")})
    emit("weather_fetch", phase="end", ok=True, what="forecast", days=len(out))
    return out


def lookup_weather(question, *, fetch_json=None, today_weekday=None):
    """Answer-grade weather facts for a question: place, day, daily facts."""
    place = extract_place(question)
    if not place:
        raise HarnessError("weather lookup: no place found in the question")
    geo = geocode(place, fetch_json=fetch_json)
    offset = day_offset(question, today_weekday=today_weekday)
    days = forecast(geo["latitude"], geo["longitude"], days=offset + 2,
                    fetch_json=fetch_json)
    pick = days[min(offset, len(days) - 1)]
    where = ", ".join(p for p in (geo.get("name"), geo.get("region"),
                                  geo.get("country")) if p)
    return {"place": where or place, "day": pick["date"],
            "facts": days[:offset + 2], "summary": (
                f"{where or place} on {pick['date']}: {pick['summary']}, "
                f"high {pick['high_c']}C / low {pick['low_c']}C, "
                f"precipitation {pick['precip_pct']}%, "
                f"wind {pick['wind_kmh']} km/h.")}
