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
import base64
import binascii
import html as _html
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .errors import HarnessError, ToolCancelled
from .events import current_cancel_check, emit

# The one search endpoint. Operator-configurable via env because the choice
# of search provider is policy, not code. Only the query varies per call.
# DuckDuckGo's html/lite endpoints answer datacenter IPs with an HTTP-202
# JS challenge (the Sep-2026 "web is on but nothing searches" incident), so
# the default is Bing, which serves real result HTML keylessly. The parser
# understands both shapes.
DEFAULT_SEARCH_URL = "https://www.bing.com/search?q={query}"
SEARCH_URL = os.environ.get("HARNESS_WEB_SEARCH_URL", DEFAULT_SEARCH_URL)

# Fetch allowlist placeholder: hosts the UI may fetch pages from. Extend
# deliberately, in the open -- this is a security boundary, not a cache.
# ONE owner: the server boundary and the agent both consume this set.
DEFAULT_FETCH_HOSTS = frozenset({
    "openrouter.ai",
    "www.anthropic.com",
    "anthropic.com",
    "claude.ai",
    "en.wikipedia.org",
    "arxiv.org",
    "scientificamerican.com",
    "quantamagazine.org",
    "api.weather.gov",
    "weather.gov",
    "wttr.in",
})

# Web TLDs that mark a bare token as a fetchable site (so 'audit example.com'
# is fetched, while 'report.pdf' / 'image.png' are not). A www-prefixed host
# is always treated as a site regardless of TLD.
_FETCHABLE_TLDS = frozenset({
    "com", "org", "net", "io", "gov", "edu", "co", "uk", "de", "fr", "jp",
    "info", "app", "dev", "ai", "me", "us", "ca", "biz", "tv", "xyz",
    "site", "online", "tech", "news", "blog", "store", "cloud",
})

# A bare domain (labels + a 2+-letter TLD), optionally with a path, not
# preceded by a scheme/word/dot/slash/@ so it is not re-matched inside an
# existing URL or an email address.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w./@])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+)([a-z]{2,})"
    r"(/[^\s)>\]'\"]*)?",
    re.IGNORECASE)

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Generic request scaffolding dropped from a site-focused search query (the
# site is the subject; these words only add noise that drifts the search).
_SITE_SCAFFOLD = frozenset({
    "audit", "www", "com", "http", "https", "site", "website", "webpage",
    "page", "url", "domain", "review", "check", "test", "look", "visit",
})


def _is_fetchable_domain(host):
    """True when a bare host looks like a real web site, not a file name."""
    host = (host or "").lower()
    return host.startswith("www.") or host.rsplit(".", 1)[-1] in _FETCHABLE_TLDS


def _sanitize_host(host):
    """Return a normalized hostname if well-formed, else None.

    Drops anything that could smuggle a scheme, path, port, or credential
    into the host set, so a hostile config value cannot widen the boundary.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h or len(h) > 253:
        return None
    if any(c in h for c in "/:@ ?#"):
        return None
    labels = h.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL_RE.match(label) for label in labels):
        return None
    return h


def resolve_fetch_allowlist(settings):
    """Derive ``(allowed_hosts, allow_any_public)`` from settings.

    The web-fetch posture is ONE owner (this module); settings only tune it.
    An empty/absent host list means "use the built-in DEFAULT_FETCH_HOSTS".
    When the allowlist is disabled, any PUBLIC https host is fetchable -- but
    ``fetch_url`` still runs the SSRF public-address guard, so relaxing the
    allowlist never turns the fetcher into an internal-network probe.
    """
    enabled = bool(getattr(settings, "fetch_allowlist_enabled", True))
    raw = getattr(settings, "fetch_allowlist_hosts", None) or []
    hosts = set()
    for h in raw:
        clean = _sanitize_host(str(h))
        if clean:
            hosts.add(clean)
    if not hosts:
        hosts = set(DEFAULT_FETCH_HOSTS)
    return hosts, (not enabled)


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
    cancel_check = current_cancel_check()
    if cancel_check is not None:
        from ._http import cancellable_request
        status, raw, _retry_after, final_url = cancellable_request(
            "GET", url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            timeout=timeout, cancel_check=cancel_check,
            max_bytes=_MAX_BYTES + 1, no_redirect=True)
        if len(raw) > _MAX_BYTES:
            raw = raw[:_MAX_BYTES]
        return status, raw, final_url
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

    Handles the result-redirect forms (.../l/?uddg=<encoded> for DuckDuckGo,
    /ck/a?...&u=a1<base64url> for Bing) and scheme-relative //host/... links;
    returns None for junk.
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
    bing_u = qs.get("u", [None])[0]
    if bing_u and bing_u.startswith("a1"):
        blob = bing_u[2:]
        blob += "=" * (-len(blob) % 4)
        try:
            target_url = base64.urlsafe_b64decode(blob).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            return None
        target = urllib.parse.urlsplit(target_url)
        if target.scheme in ("http", "https"):
            return urllib.parse.urlunsplit(target)
        return None
    return urllib.parse.urlunsplit(parsed)


def _parse_bing_results(page_html, max_results):
    """Bing's <li class="b_algo"><h2><a href>... blocks."""
    results = []
    for block in re.finditer(
            r"<li class=\"b_algo\".*?</li>", page_html,
            re.IGNORECASE | re.DOTALL):
        m = re.search(
            r"<h2[^>]*><a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
            block.group(0), re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        url = _resolve_result_href(m.group(1))
        title = _clean_text(m.group(2))
        if not url or not title:
            continue
        sm = re.search(r"<p[^>]*>(.*?)</p>", block.group(0),
                       re.IGNORECASE | re.DOTALL)
        snippet = _clean_text(sm.group(1)) if sm else ""
        results.append({"title": title[:200], "url": url,
                        "snippet": snippet[:400]})
        if len(results) >= max_results:
            break
    return results


def _parse_ddg_lite_results(page_html, max_results):
    """Extract (title, url, snippet) triples from DuckDuckGo Lite HTML."""
    results = []
    link_matches = list(re.finditer(
        r'<a[^>]+rel=[\'"]nofollow[\'"][^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',
        page_html, re.I | re.DOTALL))
    snippet_matches = list(re.finditer(
        r'<td[^>]+class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>',
        page_html, re.I | re.DOTALL))
    for i, lm in enumerate(link_matches):
        raw_href = lm.group(1)
        url = _resolve_result_href(raw_href)
        title = _clean_text(lm.group(2))
        if not url or not title:
            continue
        snippet = _clean_text(snippet_matches[i].group(1)) if i < len(snippet_matches) else ""
        results.append({"title": title[:200], "url": url, "snippet": snippet[:400]})
        if len(results) >= max_results:
            break
    return results


_IRRELEVANT_PATTERNS = (
    re.compile(r"\bfind\s+(?:your\s+)?(?:phone|device|hub)\b", re.I),
    re.compile(r"\bfind\s+a\s+grave\b", re.I),
    re.compile(r"\b(?:log\s*in|sign\s*in|download\s+claude|claude\s+help\s+center)\b", re.I),
)


def filter_search_relevance(query, results):
    """Filter out non-informational spam or navigational clutter."""
    if not results:
        return results
    filtered = [
        r for r in results
        if not any(pat.search(r.get("title", "")) for pat in _IRRELEVANT_PATTERNS)
    ]
    return filtered


def _subject_core_label(query):
    """The distinctive brand label when a query names a bare web domain.

    ``'audit www.freeoffgridcalculator.com ...'`` -> ``'freeoffgridcalculator'``.
    Returns ``None`` when the query names no fetchable bare domain, so the
    subject-relevance gate in :func:`search_web` stays inert for ordinary
    topical searches (which have no single host subject).
    """
    text = (query or "").strip().lower()
    for m in _BARE_DOMAIN_RE.finditer(text):
        host = (m.group(1) + m.group(2)).lower()
        if not _is_fetchable_domain(host):
            continue
        labels = [lb for lb in host.split(".")
                  if lb not in ("www", "com", "net", "org")]
        if not labels:
            continue
        # The brand label is the longest remaining component (handles co.uk /
        # com.au second-level TLDs without hardcoding a country list).
        return max(labels, key=len)
    return None


def _result_mentions_subject(results, label):
    """True when at least one result references the subject brand label."""
    if not label:
        return True
    needle = label.lower()
    for r in results:
        blob = " ".join(str(r.get(k) or "") for k in ("title", "url", "snippet"))
        if needle in blob.lower():
            return True
    return False


def _parse_results(page_html, max_results):
    """Extract (title, url, snippet) triples from the search result page.

    Shape is chosen by the page itself, not by which endpoint sent it:
    Bing b_algo blocks when present, DuckDuckGo Lite rows when present,
    else DuckDuckGo's result__a markup.
    """
    if re.search(r"<li class=\"b_algo\"", page_html, re.IGNORECASE):
        return _parse_bing_results(page_html, max_results)
    if "result-snippet" in page_html or "nofollow" in page_html:
        lite_res = _parse_ddg_lite_results(page_html, max_results)
        if lite_res:
            return lite_res
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
    emit("web_search", phase="start", query=q)
    url = (os.environ.get("HARNESS_WEB_SEARCH_URL") or DEFAULT_SEARCH_URL).format(
        query=urllib.parse.quote_plus(q)
    )
    try:
        status, raw, _ = _http_get(url, timeout=timeout)
    except ToolCancelled:
        emit("web_search", phase="end", ok=False, query=q,
             reason="cancelled")
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as e:
        emit("web_search", phase="end", ok=False, query=q)
        raise HarnessError(f"web search failed: {e}") from e
    if status != 200:
        emit("web_search", phase="end", ok=False, query=q)
        raise HarnessError(f"web search HTTP {status}")
    results = _parse_results(raw.decode("utf-8", "replace"), max_results)
    results = filter_search_relevance(q, results)
    if not results:
        emit("web_search", phase="end", ok=False, query=q)
        raise HarnessError("web search returned no usable results")
    # Subject-relevance gate: when the query names a specific site, a result
    # set that never mentions that site is off-subject navigational junk
    # (search engines answer an obscure query with an unrelated fallback).
    # Handing that to the model as "evidence" is the fabrication vector this
    # module exists to prevent, so refuse honestly instead.
    subject = _subject_core_label(q)
    if subject and not _result_mentions_subject(results, subject):
        emit("web_search", phase="end", ok=False, query=q,
             reason="off_subject_results")
        raise HarnessError(
            f"web search returned no results about '{subject}' "
            "(the results were off-subject)")

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


def fetch_url(url, allowed_hosts, timeout=12.0, max_chars=6000,
              allow_any_public=False):
    """Fetch ONE https URL and return {url, title, text}.

    Refusals are HarnessErrors with machine-readable messages:
    scheme, allowlist, redirect, network, and no-readable-text. A transient
    failure (timeout/network error, HTTP 429 or 5xx) is retried once after a
    short backoff; policy refusals and other statuses are not.

    When ``allow_any_public`` is set (the operator disabled the fetch
    allowlist), the host need not be on the allowlist -- but it MUST resolve
    to a globally public address, so the fetcher can never reach a private,
    loopback, link-local, or metadata address (the SSRF guard is retained
    even when the allowlist is relaxed).
    """
    if not url or not url.strip():
        raise HarnessError("web fetch: empty url")
    u = url.strip()
    parsed = urllib.parse.urlsplit(u)
    if parsed.scheme != "https":
        raise HarnessError("web fetch refused: only https:// URLs are fetched")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HarnessError("web fetch refused: no host in URL")
    if allow_any_public:
        import socket as _socket
        try:
            _resolved_addresses_public(host, _socket.getaddrinfo)
        except HarnessError as e:
            raise HarnessError(
                f"web fetch refused: {str(e).split(':', 1)[-1].strip()}") from e
    else:
        allowed = {h.lower() for h in (allowed_hosts or [])}
        if host not in allowed:
            raise HarnessError(
                f"web fetch refused: host '{host}' is not on the fetch allowlist")
    emit("web_fetch", phase="start", url=u)
    raw = final_url = None
    error_note = None
    for attempt in (1, 2):
        try:
            status, raw, final_url = _http_get(u, timeout=timeout)
            if status == 200:
                error_note = None
                break
            error_note = f"web fetch HTTP {status}"
            if status not in (429, 500, 502, 503, 504):
                break  # deterministic status: a retry cannot change it
        except ToolCancelled:
            emit("web_fetch", phase="end", ok=False, url=u,
                 reason="cancelled")
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError) as e:
            error_note = f"web fetch failed: {e}"
        if attempt == 1:
            emit("web_fetch", phase="retry", url=u)
            from ._http import interruptible_sleep
            interruptible_sleep(1.5, current_cancel_check())
    if error_note is not None or raw is None:
        emit("web_fetch", phase="end", ok=False, url=u)
        raise HarnessError(error_note or "web fetch failed: empty response")
    final_host = (urllib.parse.urlsplit(final_url).hostname or "").lower()
    if final_host != host:
        emit("web_fetch", phase="end", ok=False, url=u)
        raise HarnessError(
            f"web fetch refused: redirect to non-allowlisted host '{final_host}'")
    text = _extract_text(raw.decode("utf-8-sig", "replace"))[:max_chars]
    if not text:
        emit("web_fetch", phase="end", ok=False, url=u)
        raise HarnessError("web fetch returned no readable text")
    emit("web_fetch", phase="end", ok=True, url=final_url, chars=len(text))
    return {"url": final_url, "title": _page_title(raw.decode("utf-8-sig", "replace")),
            "text": text}


def gather_web_context(prompt: str, *, allowed_hosts=DEFAULT_FETCH_HOSTS, allow_any_public=False, fetch_url_fn=None, search_web_fn=None, find_urls_fn=None, extract_query_fn=None, max_sources=3):
    """Single-owner web evidence gathering for one chat turn.

    Fetches allowlisted URLs in the prompt (one per URL, up to max_sources);
    if none succeed, falls back to one search. If search results include
    pages on allowed_hosts, automatically fetches the primary page so deep
    text evidence is available to the model.
    """
    from typing import Dict, List, Any
    fetch_fn = fetch_url_fn if fetch_url_fn is not None else fetch_url
    search_fn = search_web_fn if search_web_fn is not None else search_web
    find_fn = find_urls_fn if find_urls_fn is not None else find_urls
    extract_fn = extract_query_fn if extract_query_fn is not None else extract_query
    sources: List[Dict[str, Any]] = []
    urls = find_fn(prompt)[:max_sources]
    if urls:
        for u in urls:
            try:
                # Pass allow_any_public only when set, so the injectable seam
                # keeps its original (url, allowed_hosts=...) shape for the
                # default allowlisted path and existing test doubles.
                if allow_any_public:
                    page = fetch_fn(u, allowed_hosts=allowed_hosts,
                                    allow_any_public=True)
                else:
                    page = fetch_fn(u, allowed_hosts=allowed_hosts)
                sources.append({"kind": "fetch", "ok": True, "url": page["url"], "title": page["title"], "text": page["text"]})
            except HarnessError as e:
                sources.append({"kind": "fetch", "ok": False, "url": u, "note": str(e)})
        if any(s["ok"] for s in sources):
            return sources
    try:
        results = search_fn(extract_fn(prompt))
        for r in results[:max_sources]:
            r_url = r.get("url", "")
            r_host = (urllib.parse.urlsplit(r_url).hostname or "").lower()
            allowed = {h.lower() for h in (allowed_hosts or [])}
            # If the discovered source is from an allowlisted host, enrich snippet with primary page text
            if r_host in allowed and not any(s.get("kind") == "fetch" and s.get("ok") for s in sources):
                try:
                    page = fetch_fn(r_url, allowed_hosts=allowed_hosts)
                    sources.append({"kind": "fetch", "ok": True, "url": page["url"], "title": page["title"], "text": page["text"]})
                    continue
                except ToolCancelled:
                    raise
                except Exception:
                    pass
            sources.append({"kind": "search", "ok": True, "url": r["url"], "title": r["title"], "text": r["snippet"]})
    except HarnessError as e:
        sources.append({"kind": "search", "ok": False, "note": str(e)})
    return sources


# ---- prompt-side helpers (query + URL extraction) --------------------------

_URL_RE = re.compile(r"https?://[^\s)>\]'\"]+")

_QUERY_STOPWORDS = frozenset({
    "what", "how", "why", "when", "where", "who", "which", "is", "are", "was",
    "were", "did", "do", "does", "can", "could", "would", "should", "you",
    "your", "it", "its", "the", "a", "an", "of", "on", "in", "to", "and",
    "or", "for", "about", "with", "that", "this", "really", "actually",
    "please", "verify", "search", "hear", "heard", "tell", "me", "my", "we",
    "i", "has", "have", "had", "there", "their", "them", "they",
    "find", "news", "work", "didn't", "didnt", "dont", "don't", "prove",
    "proven", "but", "made", "make", "get", "got", "know", "looking",
    "look", "check", "see", "give", "show", "any", "some", "real", "progress",
})

_QUERY_SPELLING_FIXES = {
    "rimann": "riemann",
    "rieman": "riemann",
    "reimann": "riemann",
}


def find_urls(prompt):
    """Every fetchable URL or bare domain in a prompt, normalized to https.

    Recognizes scheme URLs (https://...), www-prefixed hosts, and bare domains
    with a known web TLD -- so a site named without a scheme (the common
    'audit example.com' shape) is fetched directly instead of being mangled
    into a search query. File names (report.pdf) are excluded by the TLD
    allowlist. Results are capped in length and de-duplicated.
    """
    text = prompt or ""
    out = [u[:500] for u in _URL_RE.findall(text)]
    seen = set(out)
    for m in _BARE_DOMAIN_RE.finditer(text):
        host = (m.group(1) + m.group(2)).lower()
        if not _is_fetchable_domain(host):
            continue
        # A hostname over 253 chars is invalid -- never a fetch target, so
        # extract_site_target's length guard still sees it as no candidate.
        if len(host) > 253:
            continue
        url = "https://" + host + (m.group(3) or "")
        if url not in seen:
            seen.add(url)
            out.append(url[:500])
    return out


# ---- bounded public-site liveness probe (request-lifecycle #202) -----------
# A read-only uptime check must not enter repo triage, DAG decomposition, or
# waist confirmation, and it must never treat search snippets as liveness
# evidence. This probe answers one question -- does this public host answer
# HTTPS -- with SSRF guards: https only, no credentials, default port only,
# every resolved address must be globally public (DNS pinned through the
# connection with TLS hostname validation), no redirects followed, short
# timeout, small body cap, no cookies or auth. All network seams are
# injectable so hermetic tests never touch the network.
_SITE_TARGET_RE = re.compile(
    r"(?P<scheme>https?://)?(?P<host>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,})(?::(?P<port>\d{1,5}))?(?P<path>/[^\s)>\]'\"]*)?"
)
_PROBE_CONNECT_TIMEOUT = 5.0
_PROBE_READ_TIMEOUT = 4.0
_PROBE_MAX_BYTES = 8192


def extract_site_target(prompt):
    """Return a normalized ``https://host[/path]`` for an uptime-check prompt.

    Prefers an explicit URL from :func:`find_urls`; otherwise accepts a bare
    public hostname (``example.com``, ``www.example.com/path``). Returns
    ``None`` when no candidate is present. Never raises for prompt text."""
    urls = find_urls(prompt or "")
    if urls:
        return urls[0][:500]
    m = _SITE_TARGET_RE.search(prompt or "")
    if not m:
        return None
    host = m.group("host")
    if len(host) > 253:
        return None
    path = m.group("path") or ""
    return f"https://{host.lower()}{path[:200]}"


def _resolved_addresses_public(host, resolve_fn):
    """Resolve *host* and require every address to be globally public.

    Returns the address list on success. Raises :class:`HarnessError` when
    resolution fails, yields nothing, or any address is private, loopback,
    link-local, multicast, reserved, or unspecified -- the SSRF refusal."""
    try:
        infos = resolve_fn(host, 443)
    except Exception as e:
        raise HarnessError(f"site probe refused: DNS resolution failed for '{host}': {e}") from e
    addrs = []
    for info in infos or []:
        sockaddr = info[4] if len(info) > 4 else None
        ip = sockaddr[0] if sockaddr else None
        if ip:
            addrs.append(ip)
    return _validate_public_addresses(host, addrs)


def _validate_public_addresses(host, addrs):
    import ipaddress as _ipaddress
    if not addrs:
        raise HarnessError(f"site probe refused: no addresses for '{host}'")
    for ip in addrs:
        try:
            parsed = _ipaddress.ip_address(ip)
        except ValueError:
            raise HarnessError(f"site probe refused: unparseable address '{ip}'") from None
        if not parsed.is_global:
            raise HarnessError(
                f"site probe refused: '{host}' resolves to non-public address")
    return addrs


def preflight_public_site(url, resolve_fn=None, cancel_check=None):
    """Validate one HTTPS target and pin its public DNS addresses.

    This is the no-HTTP capability/security preflight used before Jev
    workflow selection. The resulting plan lets the later request use the
    same DNS answers, so a hostname cannot change between preflight and probe.
    """
    import socket as _socket
    if cancel_check and cancel_check():
        from .errors import ToolCancelled as _Cancelled
        raise _Cancelled("site probe cancelled by user")
    if not url or not url.strip():
        raise HarnessError("site probe refused: empty url")
    u = url.strip()[:500]
    try:
        parsed = urllib.parse.urlsplit(u if "://" in u else f"https://{u}")
    except ValueError:
        raise HarnessError("site probe refused: malformed URL") from None
    if parsed.scheme != "https":
        raise HarnessError("site probe refused: only https:// URLs are probed")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise HarnessError("site probe refused: credentials in URL are rejected")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HarnessError("site probe refused: no host in URL")
    try:
        port = parsed.port or 443
    except ValueError:
        raise HarnessError("site probe refused: malformed port") from None
    if port != 443:
        raise HarnessError("site probe refused: only the default https port is probed")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    resolve = resolve_fn or _socket.getaddrinfo
    addrs = _resolved_addresses_public(host, resolve)
    return {"url": u, "host": host, "port": port,
            "path": path, "addresses": tuple(addrs)}


def probe_public_site(url, timeout=8.0, cancel_check=None, resolve_fn=None,
                      connect_fn=None, preflight_plan=None):
    """Perform one bounded HTTPS liveness probe against a public site.

    Returns a plain dict ``{ok, http_status, verdict, reason, latency_s,
    host, url}`` where *verdict* is one of ``up``, ``denied``, ``redirect``,
    ``timeout``, ``network``, or ``refused``. Environmental outcomes are
    returned, never raised; only policy refusals (credentials, unsafe
    scheme/port, non-public address) raise :class:`HarnessError` so the
    caller can render an honest terminal refusal before any model call.
    """
    import socket as _socket
    import ssl as _ssl
    import time as _time
    if cancel_check and cancel_check():
        from .errors import ToolCancelled as _Cancelled
        raise _Cancelled("site probe cancelled by user")
    plan = (preflight_plan if preflight_plan is not None
            else preflight_public_site(url, resolve_fn=resolve_fn,
                                       cancel_check=cancel_check))
    raw_url = url.strip()[:500] if isinstance(url, str) else ""
    if not isinstance(plan, dict) or plan.get("url") != raw_url:
        raise HarnessError("site probe refused: preflight plan does not match url")
    host = plan["host"]
    port = plan["port"]
    path = plan["path"]
    u = plan["url"]
    addrs = _validate_public_addresses(host, list(plan.get("addresses") or ()))
    if cancel_check and cancel_check():
        from .errors import ToolCancelled as _Cancelled
        raise _Cancelled("site probe cancelled by user")
    deadline = _time.monotonic() + max(1.0, float(timeout))
    start = _time.monotonic()
    raw_sock = tls_sock = None
    try:
        if connect_fn is not None:
            status, reason, final_host = connect_fn(host, path, timeout)
            latency = _time.monotonic() - start
            return _probe_verdict(host, u, status, reason, latency, final_host)
        ctx = _ssl.create_default_context()
        conn_timeout = min(_PROBE_CONNECT_TIMEOUT, max(1.0, deadline - _time.monotonic()))
        raw_sock = _socket.create_connection((addrs[0], port), timeout=conn_timeout)
        raw_sock.settimeout(max(1.0, deadline - _time.monotonic()))
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        request = (f"GET {path[:200]} HTTP/1.1\r\nHost: {host}\r\n"
                   f"User-Agent: {_USER_AGENT}\r\nAccept: text/html,*/*;q=0.8\r\n"
                   f"Connection: close\r\n\r\n")
        tls_sock.sendall(request.encode("ascii", "replace"))
        chunks = []
        received = 0
        while received < _PROBE_MAX_BYTES:
            remaining = max(0.5, deadline - _time.monotonic())
            tls_sock.settimeout(remaining)
            try:
                data = tls_sock.recv(min(4096, _PROBE_MAX_BYTES - received))
            except (_socket.timeout, TimeoutError):
                latency = _time.monotonic() - start
                return {"ok": False, "http_status": None, "verdict": "timeout",
                        "reason": f"'{host}' did not answer within the probe budget",
                        "latency_s": round(latency, 2), "host": host, "url": u}
            if not data:
                break
            chunks.append(data)
            received += len(data)
            if b"\r\n\r\n" in b"".join(chunks):
                break
        latency = _time.monotonic() - start
        head = b"".join(chunks).decode("iso-8859-1", "replace")
        status_line = head.splitlines()[0] if head.splitlines() else ""
        parts = status_line.split()
        try:
            status = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            status = 0
        location = ""
        for line in head.splitlines()[1:]:
            if line.lower().startswith("location:"):
                location = line.split(":", 1)[1].strip()[:200]
                break
        return _probe_verdict(host, u, status, location, latency, host)
    except (HarnessError, Exception) as e:
        from .errors import HarnessError as _HarnessError
        if isinstance(e, _HarnessError):
            raise
        latency = _time.monotonic() - start
        text = str(e)
        if "timed out" in text or "timeout" in text.lower():
            return {"ok": False, "http_status": None, "verdict": "timeout",
                    "reason": f"'{host}' timed out", "latency_s": round(latency, 2),
                    "host": host, "url": u}
        return {"ok": False, "http_status": None, "verdict": "network",
                "reason": f"'{host}' unreachable: {text[:160]}",
                "latency_s": round(latency, 2), "host": host, "url": u}
    finally:
        for sock in (tls_sock, raw_sock):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass


def _probe_verdict(host, url, status, location, latency, final_host):
    """Map one observed HTTP status to an honest liveness verdict."""
    base = {"latency_s": round(float(latency), 2), "host": host,
            "url": url, "reachable": True}
    if 200 <= status < 300:
        return {**base, "ok": True, "http_status": status, "verdict": "up",
                "reason": f"'{host}' is up (HTTPS {status})"}
    if status in (401, 402, 403, 407):
        return {**base, "ok": True, "http_status": status, "verdict": "denied",
                "reason": (f"'{host}' responds but denied page access "
                           f"(HTTPS {status}); the host is reachable")}
    if 300 <= status < 400:
        dest = f" -> {location}" if location else ""
        return {**base, "ok": True, "http_status": status, "verdict": "redirect",
                "reason": (f"'{host}' answered with HTTPS {status}{dest}; "
                           f"redirects are not followed, so the destination "
                           f"is not claimed reachable")}
    if status == 429:
        return {**base, "ok": False, "http_status": status,
                "verdict": "rate_limited",
                "reason": f"'{host}' responded but rate-limited the probe (HTTPS {status})"}
    if 500 <= status < 600:
        return {**base, "ok": False, "http_status": status,
                "verdict": "server_error",
                "reason": f"'{host}' responded with a server error (HTTPS {status})"}
    if 400 <= status < 500:
        return {**base, "ok": False, "http_status": status,
                "verdict": "http_error",
                "reason": f"'{host}' responded with an HTTP error (HTTPS {status})"}
    return {**base, "ok": False, "http_status": status or None,
            "verdict": "network",
            "reason": f"'{host}' gave an unusable response (HTTPS {status or '?'})"}


def extract_query(prompt, max_words=10):
    """Reduce a natural-language prompt to a search query.

    Stopwords and spellings are normalized. When the prompt names a fetchable
    site, that site is the subject: the query leads with the bare host (never
    tokenized into 'www x com') and drops generic request scaffolding (audit,
    review, site ...) so the search targets the site instead of drifting onto
    a keyword like 'audit' and returning unrelated results.
    """
    text = (prompt or "").strip()
    words = re.findall(r"[a-zA-Z0-9_']+", text.lower())
    host = None
    for m in _BARE_DOMAIN_RE.finditer(text):
        h = (m.group(1) + m.group(2)).lower()
        if _is_fetchable_domain(h):
            host = h.replace("www.", "", 1)
            break
    if host:
        host_tokens = set(host.split("."))
        kept = [w for w in words
                if w not in _QUERY_STOPWORDS and w not in _SITE_SCAFFOLD
                and w not in host_tokens and len(w) > 1]
        return (host + " " + " ".join(kept[:max_words])).strip()
    kept = []
    for w in words:
        w_fixed = _QUERY_SPELLING_FIXES.get(w, w)
        if w_fixed not in _QUERY_STOPWORDS and len(w_fixed) > 1:
            kept.append(w_fixed)
    return " ".join(kept[:max_words]).strip()
