"""Deliberate, governed web access for the chat lane.

The Riemann incident (chat_history/sess_tk4073vs, 2026-09-16): the
conversation lane had NO search or fetch capability, yet the model answered
"can you search to verify?" by inventing an arXiv/Lean/Anthropic sweep and
reporting its "results" -- a fabrication a user cannot distinguish from a
real lookup. A bare URL prompt died with "I was unable to formulate a
response" because nothing can fetch a link. This module closes that gap
honestly:

- the agent never claims a lookup it did not perform (the system prompt
  states the capability boundary plainly);
- when the run opts in (``web=True``), search is a real, evidence-bearing
  tool: one GET against the configured search endpoint, results truncated
  to a context budget;
- fetch is allowlist-only: callers pass the safe-host set (the server
  boundary owns it) and every other URL is refused. Search goes to ONE
  operator-configured endpoint whose host is fixed in code/config, not
  caller-controlled, so it is not an SSRF surface.

Search and fetch are READ-ONLY: no agent loop, no writes, no model calls of
their own (the answer synthesis is the ordinary governed chat call). Redirects
are refused rather than followed, so a allowlisted URL cannot bounce the
fetcher onto an intranet address. All network seams are patchable module
functions, so every behavior here is pinned hermetically.
"""
import html as _html
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .errors import HarnessError
from .events import emit

# The one search endpoint. Operator-configurable via env because the choice
# of search provider is policy, not code. Only the query varies per call.
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
SEARCH_URL = os.environ.get("HARNESS_WEB_SEARCH_URL", DEFAULT_SEARCH_URL)

# Fetch allowlist placeholder: hosts the UI may fetch pages from. Extend
# deliberately, in the open -- this is a security boundary, not a cache.
# ONE owner: the server boundary and the agent both consume this set.
DEFAULT_FETCH_HOSTS = frozenset({"openrouter.ai", "www.anthropic.com"})

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) HarnessLocalUI/0.3 (personal local tool)"
)
_MAX_BYTES = 2_000_000        # hard read cap: never buffer an endless body
_MAX_QUERY = 300              # query length cap
_MAX_REDIRECTS_FOLLOWED = 0   # never follow: allowlist must bound the host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect can move an allowlisted https URL onto an arbitrary host
    # (including intranet). Refuse instead of following.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pragma: no cover - exercised via HTTPError path
        return None


def _http_get(url, timeout=12.0):
    """One bounded GET: bounded body, no redirects, no credentials sent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200) or 200
        raw = resp.read(_MAX_BYTES + 1)
        final_url = resp.geturl()
    if len(raw) > _MAX_BYTES:
        raw = raw[:_MAX_BYTES]
    return status, raw, final_url


def _clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _resolve_result_href(href):
    """Turn a search-result href into a plain https URL.

    Handles the result-redirect form (…/l/?uddg=<encoded>) and
    scheme-relative //host/... links; returns None for junk.
    """
    if not href:
        return None
    href = _html.unescape(href.strip())
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    if parsed.scheme not in ("http", "https"):
        return None
    qs = urllib.parse.parse_qs(parsed.query)
    uddg = qs.get("uddg", [None])[0]
    if uddg:
        try:
            target = urllib.parse.urlsplit(urllib.parse.unquote(uddg))
        except ValueError:
            return None
        if target.scheme not in ("http", "https"):
            return None
        return urllib.parse.urlunsplit(target)
    return urllib.parse.urlunsplit(parsed)


def _parse_results(page_html, max_results):
    """Extract (title, url, snippet) triples from the search result page."""
    items = re.finditer(
        r"<a[^>]+class=\"[^\"]*result__a[^\"]*\"[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
        page_html, re.IGNORECASE | re.DOTALL)
    snippets = [m.group(1) for m in re.finditer(
        r"<a[^>]+class=\"[^\"]*result__snippet[^\"]*\"[^>]*>(.*?)</a>",
        page_html, re.IGNORECASE | re.DOTALL)]
    results = []
    for i, m in enumerate(items):
        url = _resolve_result_href(m.group(1))
        title = _clean_text(m.group(2))
        if not url or not title:
            continue
        snippet = _clean_text(snippets[i]) if i < len(snippets) else ""
        results.append({"title": title[:200], "url": url,
                        "snippet": snippet[:400]})
        if len(results) >= max_results:
            break
    return results


def search_web(query, max_results=5, timeout=12.0):
    """Run one web search; return [{title, url, snippet}], or raise.

    Raises HarnessError on empty query, network failure, non-200, or zero
    usable results -- the caller (agent) converts that into an honest
    statement, never into an invented narrative.
    """
    if not query or not query.strip():
        raise HarnessError("web search: empty query")
    q = query.strip()[:_MAX_QUERY]
    url = SEARCH_URL.format(query=urllib.parse.quote_plus(q))
    emit("web_search", phase="start", query=q)
    try:
        status, raw, _ = _http_get(url, timeout=timeout)
        if status != 200:
            raise HarnessError(f"web search HTTP {status}")
        results = _parse_results(raw.decode("utf-8", "replace"), max_results)
        if not results:
            raise HarnessError("web search returned no usable results")
    except HarnessError:
        emit("web_search", phase="end", ok=False, query=q, results=0)
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
        emit("web_search", phase="end", ok=False, query=q, results=0)
        raise HarnessError(f"web search failed: {e}") from e
    emit("web_search", phase="end", ok=True, query=q, results=len(results))
    return results


def _extract_text(page_html):
    # Drop scripts/styles/comments, give block tags line breaks, strip tags.
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page_html)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)</?(?:p|div|br|li|ul|ol|h[1-6]|tr|td|th|section|article)[^>]*>",
               "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _page_title(page_html):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page_html)
    return _clean_text(m.group(1))[:200] if m else ""


def fetch_url(url, allowed_hosts, timeout=12.0, max_chars=6000):
    """Fetch ONE allowlisted https URL and return {url, title, text}.

    Refusals are HarnessErrors with machine-readable messages:
    scheme, allowlist, redirect, network, and no-readable-text.
    """
    if not url or not url.strip():
        raise HarnessError("web fetch: empty url")
    u = url.strip()
    parsed = urllib.parse.urlsplit(u)
    if parsed.scheme != "https":
        raise HarnessError("web fetch refused: only https:// URLs are fetched")
    host = (parsed.hostname or "").lower()
    allowed = {h.lower() for h in (allowed_hosts or [])}
    if not host or host not in allowed:
        raise HarnessError(
            f"web fetch refused: host '{host or '?'}' is not on the fetch allowlist")
    emit("web_fetch", phase="start", url=u)
    try:
        status, raw, final_url = _http_get(u, timeout=timeout)
        if status != 200:
            raise HarnessError(f"web fetch HTTP {status}")
        final_host = (urllib.parse.urlsplit(final_url).hostname or "").lower()
        if final_host != host:
            raise HarnessError(
                f"web fetch refused: redirect to non-allowlisted host '{final_host}'")
        text = _extract_text(raw.decode("utf-8-sig", "replace"))[:max_chars]
        if not text:
            raise HarnessError("web fetch returned no readable text")
    except HarnessError:
        emit("web_fetch", phase="end", ok=False, url=u)
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
        emit("web_fetch", phase="end", ok=False, url=u)
        raise HarnessError(f"web fetch failed: {e}") from e
    emit("web_fetch", phase="end", ok=True, url=final_url, chars=len(text))
    return {"url": final_url, "title": _page_title(raw.decode("utf-8-sig", "replace")),
            "text": text}


# ---- prompt-side helpers (query + URL extraction) --------------------------

_URL_RE = re.compile(r"https?://[^\s)>\]'\"]+")

_QUERY_STOPWORDS = frozenset({
    "what", "how", "why", "when", "where", "who", "which", "is", "are", "was",
    "were", "did", "do", "does", "can", "could", "would", "should", "you",
    "your", "it", "its", "the", "a", "an", "of", "on", "in", "to", "and",
    "or", "for", "about", "with", "that", "this", "really", "actually",
    "please", "verify", "search", "hear", "heard", "tell", "me", "my", "we",
    "i", "has", "have", "had", "there", "their", "them", "they",
})


def find_urls(prompt):
    """All URLs mentioned in a prompt, in order, capped in length."""
    return [u[:500] for u in _URL_RE.findall(prompt or "")]


def extract_query(prompt, max_words=10):
    """Reduce a natural-language prompt to a search query (stopwords out)."""
    words = re.findall(r"[a-zA-Z0-9_']+", (prompt or "").lower())
    kept = [w for w in words if w not in _QUERY_STOPWORDS and len(w) > 1]
    return " ".join(kept[:max_words]).strip()
