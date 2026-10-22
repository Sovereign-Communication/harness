"""harness.web: deliberate, SSRF-hardened search + allowlist fetch.

Hermetic: every network seam is the patchable module-level ``_http_get``;
pins the allowlist refusals, the redirect refusal, honest failure modes,
and the prompt-side extraction helpers. No live network in this battery.
"""
import http.server
import threading
import unittest
from unittest import mock

from harness import web
from harness.errors import HarnessError


def _resp(status=200, body=b"<html></html>", final_url="https://allowed.example/x"):
    r = mock.MagicMock()
    r.status = status
    r.read.return_value = body
    r.geturl.return_value = final_url
    r.__enter__ = mock.MagicMock(return_value=r)
    r.__exit__ = mock.MagicMock(return_value=False)
    return r


class SearchWebTests(unittest.TestCase):
    def test_empty_query_refused(self):
        with self.assertRaises(HarnessError):
            web.search_web("   ")

    def test_default_search_endpoint_is_bing(self):
        # The Sep-2026 incident: DDG's html/lite endpoints answer datacenter
        # IPs with an HTTP-202 JS challenge, so "web on" searched nothing.
        # The default must stay on the keyless endpoint that answers.
        self.assertIn("bing.com", web.DEFAULT_SEARCH_URL)

    def test_bing_markup_parsed_with_ck_redirect_resolved(self):
        # Bing wraps result links in /ck/a?...&u=a1<base64url>; the parser
        # must resolve them to the plain target URL.
        import base64 as _b64
        target = "https://www.anthropic.com/research/riemann-zeta"
        u_param = "a1" + _b64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        page = (
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&p=x'
            f'&u={u_param}&ntb=1">Riemann Bound Moved</a></h2>'
            "<p>zero-free region extended</p></li>"
            '<li class="b_algo"><h2><a href="https://www.example.com/plain">'
            "Plain Link</a></h2><p>also fine</p></li>"
        ).encode()
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://www.bing.com/search?q=x")):
            results = web.search_web("riemann bound")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Riemann Bound Moved")
        self.assertEqual(results[0]["url"], target)
        self.assertEqual(results[0]["snippet"], "zero-free region extended")
        self.assertEqual(results[1]["url"], "https://www.example.com/plain")

    def test_bing_ck_link_with_unusable_target_is_dropped(self):
        # A u= param that does not decode to http(s) must not yield a result
        # pointing at the search engine's own redirect URL.
        page = (
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1'
            '!!!notbase64!!!">Junk</a></h2><p>s</p></li>'
        ).encode()
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://www.bing.com/search?q=x")):
            with self.assertRaises(HarnessError):
                web.search_web("q")

    def test_bing_parser_edge_paths(self):
        # Blocks without an h2>a are skipped; a u= param decoding to a
        # non-http scheme is dropped (no scheme confusion); a block with no
        # <p> gets an empty snippet; max_results stops the walk.
        ftp_blob = "a1" + __import__("base64").urlsafe_b64encode(
            b"ftp://bad.example/x").decode().rstrip("=")
        page = (
            '<li class="b_algo"><div>no heading at all</div></li>'
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?'
            f'u={ftp_blob}">Scheme Confusion</a></h2><p>s</p></li>'
            '<li class="b_algo"><h2><a href="https://www.example.com/nosnip">'
            "No Snippet</a></h2><div>no paragraph</div></li>"
            '<li class="b_algo"><h2><a href="https://www.example.com/never">'
            "Never Reached</a></h2><p>s</p></li>"
        ).encode()
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://www.bing.com/search?q=x")):
            results = web.search_web("q", max_results=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "No Snippet")
        self.assertEqual(results[0]["snippet"], "")

    def test_results_parsed_with_redirect_form_resolved(self):
        page = (
            '<a class="result__a" href="//www.anthropic.com/l/?uddg=https%3A%2F%2Fnews.ycombinator.com%2Fitem%3Fid%3D1">'
            "Title One</a>"
            '<a class="result__snippet" href="#">first snippet</a>'
            '<a class="result__a" href="https://www.example.com/x">Title &amp; Two</a>'
            '<a class="result__snippet" href="#">second snippet</a>'
        ).encode()
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://html.duckduckgo.com/html/?q=x")):
            results = web.search_web("riemann bound")
        self.assertEqual(results[0]["title"], "Title One")
        self.assertEqual(results[0]["url"], "https://news.ycombinator.com/item?id=1")
        self.assertEqual(results[0]["snippet"], "first snippet")
        self.assertEqual(results[1]["url"], "https://www.example.com/x")
        self.assertEqual(results[1]["title"], "Title & Two")

    LITE_PAGE = (
        '<a rel="nofollow" href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fwww.example.com%2Fa">Title A</a>'
        '<td class="result-snippet">snippet A</td>'
        # a link with no usable title is skipped, not turned into a result
        '<a rel="nofollow" href="https://www.example.com/blank"> </a>'
        '<a rel="nofollow" href="https://www.example.com/b">Title B</a>'
        # no snippet cell for this one
        '<a rel="nofollow" href="https://www.example.com/c">Title C</a>'
    )

    def test_ddg_lite_markup_is_parsed_with_snippets_optional(self):
        results = web._parse_ddg_lite_results(self.LITE_PAGE, 10)
        self.assertEqual([r["title"] for r in results],
                         ["Title A", "Title B", "Title C"])
        self.assertEqual(results[0]["url"], "https://www.example.com/a")
        self.assertEqual(results[0]["snippet"], "snippet A")
        # Snippets are matched by position; the surplus links get none.
        self.assertEqual(results[2]["snippet"], "")

    def test_ddg_lite_result_count_is_capped(self):
        self.assertEqual(len(web._parse_ddg_lite_results(self.LITE_PAGE, 1)), 1)

    def test_page_shape_selects_the_lite_parser(self):
        results = web._parse_results(self.LITE_PAGE, 2)
        self.assertEqual([r["title"] for r in results], ["Title A", "Title B"])

    def test_nofollow_page_without_lite_results_falls_through(self):
        page = '<a rel="nofollow" href="https://x/y"> </a>'
        self.assertEqual(web._parse_results(page, 5), [])

    def test_relevance_filter_drops_navigational_clutter_only(self):
        results = [{"title": "Sign in to continue", "url": "https://a/"},
                   {"title": "Find a grave for Ada", "url": "https://b/"},
                   {"title": "Zero-free region result", "url": "https://c/"}]
        kept = web.filter_search_relevance("zeta", results)
        self.assertEqual([r["url"] for r in kept], ["https://c/"])
        self.assertEqual(web.filter_search_relevance("zeta", []), [])

    def test_network_failure_raises_honest_error(self):
        with mock.patch.object(web, "_http_get", side_effect=OSError("conn refused")):
            with self.assertRaises(HarnessError) as ctx:
                web.search_web("query")
        self.assertIn("web search failed", str(ctx.exception))

    def test_http_error_status_raises(self):
        with mock.patch.object(web, "_http_get", return_value=(503, b"", "https://x/")):
            with self.assertRaises(HarnessError):
                web.search_web("query")

    def test_zero_results_is_an_error_not_a_fabrication(self):
        with mock.patch.object(web, "_http_get", return_value=(200, b"<html></html>", "https://x/")):
            with self.assertRaises(HarnessError):
                web.search_web("query")

    def test_query_length_capped_in_url(self):
        captured = {}

        def fake_get(url, timeout=12.0):
            captured["url"] = url
            page = (b'<a class="result__a" href="https://e.example/x">T</a>')
            return 200, page, "https://x/"

        with mock.patch.object(web, "_http_get", side_effect=fake_get):
            web.search_web("x" * 1000)
        from urllib.parse import urlsplit, parse_qs, unquote_plus
        qs = parse_qs(urlsplit(captured["url"]).query)
        self.assertEqual(len(unquote_plus(qs["q"][0])), web._MAX_QUERY)


class HttpGetLiveLoopbackTests(unittest.TestCase):
    """Exercise the REAL _http_get against a loopback server: bounded read,
    no-redirect opener, status passthrough. Hermetic (localhost only), and
    keeps the transport body suite-executed for the changed-line gate."""

    def setUp(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://elsewhere.example/x")
                    self.end_headers()
                    return
                body = b"<html><body>loopback ok</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def test_real_get_returns_status_body_and_final_url(self):
        status, raw, final_url = web._http_get(
            f"http://127.0.0.1:{self.port}/page", timeout=5)
        self.assertEqual(status, 200)
        self.assertIn(b"loopback ok", raw)
        self.assertTrue(final_url.endswith("/page"))

    def test_real_get_refuses_redirect_instead_of_following(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            web._http_get(f"http://127.0.0.1:{self.port}/redirect", timeout=5)
        self.assertEqual(ctx.exception.code, 302)
        # The refused redirect's HTTPError owns the (already-consumed) loopback
        # response; unclosed, it warns at GC time and trips the R13 leak scan.
        ctx.exception.close()


class FetchUrlTests(unittest.TestCase):
    def test_empty_url_refused(self):
        with self.assertRaises(HarnessError):
            web.fetch_url("  ", allowed_hosts=["a.example"])

    def test_non_https_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            web.fetch_url("http://www.anthropic.com/x", allowed_hosts=["www.anthropic.com"])
        self.assertIn("https", str(ctx.exception))

    def test_non_allowlisted_host_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            web.fetch_url("https://internal.intranet.local/secret",
                          allowed_hosts=["www.anthropic.com"])
        self.assertIn("allowlist", str(ctx.exception))

    def test_happy_path_returns_title_and_text(self):
        body = (b"<html><head><title>Anthropic &amp; Riemann</title></head><body>"
                b"<script>var tracker=1;</script>"
                b"<p>Zero-free region extended.</p><p>Second paragraph.</p>"
                b"</body></html>")
        url = "https://www.anthropic.com/research/riemann-zeta"
        with mock.patch.object(web, "_http_get",
                               return_value=(200, body, url)):
            page = web.fetch_url(url, allowed_hosts=["www.anthropic.com"])
        self.assertEqual(page["title"], "Anthropic & Riemann")
        self.assertIn("Zero-free region extended.", page["text"])
        self.assertNotIn("tracker", page["text"])
        self.assertEqual(page["url"], url)

    def test_redirect_to_other_host_refused(self):
        url = "https://www.anthropic.com/research/riemann-zeta"
        with mock.patch.object(web, "_http_get",
                               return_value=(200, b"<html><body>x</body></html>",
                                             "https://internal.intranet.local/x")):
            with self.assertRaises(HarnessError) as ctx:
                web.fetch_url(url, allowed_hosts=["www.anthropic.com"])
        self.assertIn("redirect", str(ctx.exception))

    def test_network_failure_raises_honest_error(self):
        with mock.patch.object(web, "_http_get", side_effect=OSError("no route")):
            with self.assertRaises(HarnessError) as ctx:
                web.fetch_url("https://www.anthropic.com/x", allowed_hosts=["www.anthropic.com"])
        self.assertIn("web fetch failed", str(ctx.exception))

    def test_script_only_page_is_no_readable_text(self):
        body = b"<html><head><script>var a=1;</script></head><body></body></html>"
        url = "https://www.anthropic.com/x"
        with mock.patch.object(web, "_http_get", return_value=(200, body, url)):
            with self.assertRaises(HarnessError) as ctx:
                web.fetch_url(url, allowed_hosts=["www.anthropic.com"])
        self.assertIn("no readable text", str(ctx.exception))

    def test_transient_failure_retried_once_then_ok(self):
        # The fetch-failed incident: an upstream blip (challenge / 5xx /
        # timeout) killed the turn's only evidence. Transient statuses and
        # network errors get exactly one retry.
        url = "https://www.anthropic.com/research/riemann-zeta"
        body = b"<html><body><p>Zero-free region.</p></body></html>"
        getter = mock.MagicMock(side_effect=[(503, b"", url), (200, body, url)])
        with mock.patch.object(web, "_http_get", getter):
            page = web.fetch_url(url, allowed_hosts=["www.anthropic.com"])
        self.assertEqual(getter.call_count, 2)
        self.assertIn("Zero-free region.", page["text"])

    def test_permanent_status_not_retried(self):
        url = "https://www.anthropic.com/x"
        getter = mock.MagicMock(return_value=(404, b"", url))
        with mock.patch.object(web, "_http_get", getter):
            with self.assertRaises(HarnessError) as ctx:
                web.fetch_url(url, allowed_hosts=["www.anthropic.com"])
        self.assertEqual(getter.call_count, 1)
        self.assertIn("HTTP 404", str(ctx.exception))


class ExtractionHelpersTests(unittest.TestCase):
    def test_find_urls_finds_and_stops_at_paren(self):
        urls = web.find_urls("see https://www.anthropic.com/research/riemann-zeta) and http://x.example")
        self.assertEqual(urls[0], "https://www.anthropic.com/research/riemann-zeta")
        self.assertEqual(len(urls), 2)

    def test_find_urls_empty(self):
        self.assertEqual(web.find_urls("no links here"), [])

    def test_extract_query_drops_stopwords_and_caps_words(self):
        q = web.extract_query("What did you hear about the Riemann hypothesis and Claude moving the bound on it?")
        self.assertNotIn("what", q.split())
        self.assertNotIn("the", q.split())
        self.assertIn("riemann", q.split())
        self.assertIn("hypothesis", q.split())
        self.assertLessEqual(len(q.split()), 10)

    def test_extract_query_empty(self):
        self.assertEqual(web.extract_query("can you verify?"), "")



class GatherWebContextTests(unittest.TestCase):
    """The search fallback: results on an allowlisted host are upgraded to a
    fetched page; everything else stays a snippet; failures stay honest."""

    HOSTS = frozenset({"www.anthropic.com"})

    def _gather(self, results, fetch):
        return web.gather_web_context(
            "any news on the riemann bound",
            allowed_hosts=self.HOSTS,
            fetch_url_fn=fetch,
            search_web_fn=lambda q: results,
            find_urls_fn=lambda p: [],
            extract_query_fn=lambda p: "riemann bound")

    def test_allowlisted_result_is_enriched_with_the_fetched_page(self):
        results = [
            {"title": "Plain", "url": "https://www.example.com/p", "snippet": "s1"},
            {"title": "Primary", "url": "https://www.anthropic.com/r",
             "snippet": "s2"},
        ]
        fetched = []

        def fetch(url, allowed_hosts=None):
            fetched.append(url)
            return {"url": url, "title": "Primary page", "text": "full text"}
        sources = self._gather(results, fetch)
        self.assertEqual(fetched, ["https://www.anthropic.com/r"])
        self.assertEqual([s["kind"] for s in sources], ["search", "fetch"])
        self.assertEqual(sources[1]["text"], "full text")
        self.assertEqual(sources[0]["text"], "s1")

    def test_failed_enrichment_keeps_the_snippet(self):
        results = [{"title": "Primary", "url": "https://www.anthropic.com/r",
                    "snippet": "s2"}]

        def fetch(url, allowed_hosts=None):
            raise RuntimeError("network down")
        sources = self._gather(results, fetch)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["kind"], "search")
        self.assertEqual(sources[0]["text"], "s2")

    def test_search_failure_is_reported_not_swallowed(self):
        def boom(q):
            raise HarnessError("web search failed: offline")
        sources = web.gather_web_context(
            "q", allowed_hosts=self.HOSTS, search_web_fn=boom,
            find_urls_fn=lambda p: [], extract_query_fn=lambda p: "q")
        self.assertEqual(len(sources), 1)
        self.assertFalse(sources[0]["ok"])
        self.assertIn("offline", sources[0]["note"])


class WeatherAllowlistTests(unittest.TestCase):
    def test_weather_hosts_in_default_fetch_hosts(self):
        for host in ("api.weather.gov", "weather.gov", "wttr.in"):
            self.assertIn(host, web.DEFAULT_FETCH_HOSTS)


class BareDomainAndSiteQueryTests(unittest.TestCase):
    """A site named without a scheme is still fetched (not mangled into a
    search query), and a site-focused search leads with the host."""

    def test_find_urls_recognizes_www_bare_domain(self):
        urls = web.find_urls(
            "audit www.freeoffgridcalculator.com for user friendliness")
        self.assertEqual(urls, ["https://www.freeoffgridcalculator.com"])

    def test_find_urls_recognizes_bare_domain_with_known_tld(self):
        urls = web.find_urls("check freeoffgridcalculator.com today")
        self.assertEqual(urls, ["https://freeoffgridcalculator.com"])

    def test_find_urls_keeps_scheme_urls_and_dedupes(self):
        urls = web.find_urls(
            "see https://example.com/a and also example.com/b")
        self.assertEqual(urls, ["https://example.com/a",
                                "https://example.com/b"])

    def test_find_urls_ignores_file_names_not_sites(self):
        self.assertEqual(web.find_urls("compare report.pdf and image.png"), [])

    def test_extract_query_leads_with_the_host_not_audit(self):
        q = web.extract_query(
            "audit www.freeoffgridcalculator.com for user friendliness")
        self.assertTrue(q.startswith("freeoffgridcalculator.com"), q)
        self.assertNotIn("audit", q.split())
        self.assertNotIn("www", q.split())
        self.assertIn("friendliness", q.split())

    def test_extract_query_without_site_is_unchanged(self):
        q = web.extract_query("what is the longest road in the world")
        self.assertIn("longest", q.split())
        self.assertNotIn("the", q.split())


class SubjectRelevanceGateTests(unittest.TestCase):
    """A search whose query names a specific site must not feed the model
    off-subject navigational junk as evidence (the Sep-2026 Riemann-class
    fabrication vector): when Bing has no real result for an obscure site it
    answers with an unrelated fallback, which the parser would otherwise
    hand over as if it were about the subject."""

    @staticmethod
    def _bing(items):
        blocks = "".join(
            f'<li class="b_algo"><h2><a href="{u}">{t}</a></h2>'
            f"<p>{s}</p></li>" for t, u, s in items)
        return ("<html><body><ol id=\"b_results\">" + blocks
                + "</ol></body></html>").encode()

    def test_off_subject_junk_is_refused_not_fabricated(self):
        # The real failing shape: query names the site; Bing returns movie
        # times / shower-rod articles that never mention it.
        page = self._bing([
            ("Movie Showtimes near Seattle", "https://www.fandango.com/s", "c"),
            ("How to Fix a Shower Curtain Rod", "https://r.example/fix", "t"),
            ("Contact Microsoft Support", "https://support.example/c", "h"),
        ])
        q = web.extract_query(
            "audit www.freeoffgridcalculator.com for user friendliness")
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://bing/")):
            with self.assertRaises(HarnessError) as ctx:
                web.search_web(q)
        self.assertIn("off-subject", str(ctx.exception))

    def test_a_result_mentioning_the_subject_keeps_the_evidence(self):
        page = self._bing([
            ("Movie Showtimes near Seattle", "https://www.fandango.com/s", "c"),
            ("Free Off Grid Calculator review", "https://blog.example/f",
             "the freeoffgridcalculator.com interface"),
        ])
        q = web.extract_query(
            "audit www.freeoffgridcalculator.com for user friendliness")
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://bing/")):
            results = web.search_web(q)
        self.assertEqual(len(results), 2)
        self.assertIn("freeoffgridcalculator",
                      (results[1]["snippet"] + results[1]["url"]))

    def test_topical_query_without_a_site_subject_is_unaffected(self):
        # No bare domain in the query -> the gate is inert (existing topical
        # searches keep their behavior).
        page = self._bing([("Riemann Bound Moved", "https://a.example/x",
                            "zero-free region extended")])
        with mock.patch.object(web, "_http_get",
                               return_value=(200, page, "https://bing/")):
            results = web.search_web("riemann bound")
        self.assertEqual(len(results), 1)

    def test_subject_core_label_extraction(self):
        self.assertEqual(
            web._subject_core_label("audit www.freeoffgridcalculator.com now"),
            "freeoffgridcalculator")
        self.assertEqual(web._subject_core_label("check example.com"),
                         "example")
        self.assertEqual(web._subject_core_label("is bbc.co.uk up"), "bbc")
        self.assertIsNone(web._subject_core_label("riemann hypothesis"))

    def test_non_fetchable_tld_token_yields_no_subject(self):
        # 'report.pdf' matches the bare-domain shape but its TLD is not a
        # web TLD, so it is skipped (defensive continue) -> no subject.
        self.assertIsNone(web._subject_core_label("audit report.pdf now"))

    def test_generic_only_host_yields_no_subject(self):
        # A host made entirely of generic labels (www/com/net/org) has no
        # distinctive brand label to gate on -> skipped (defensive continue).
        self.assertIsNone(web._subject_core_label("check www.com now"))
        self.assertIsNone(web._subject_core_label("see net.net"))

    def test_result_mentions_subject_treats_absent_label_as_match(self):
        # No subject label means the gate cannot judge relevance; the helper
        # reports a match rather than refusing (defensive short-circuit).
        self.assertTrue(web._result_mentions_subject([{"title": "x"}], None))
        self.assertTrue(web._result_mentions_subject([], ""))


class AllowlistRelaxationTests(unittest.TestCase):
    """Disabling the allowlist lets any PUBLIC https host be fetched, but the
    SSRF public-address guard is retained -- never an internal probe."""

    def test_default_path_still_enforces_the_allowlist(self):
        with self.assertRaises(HarnessError):
            web.fetch_url("https://freeoffgridcalculator.com/",
                          allowed_hosts=["en.wikipedia.org"])

    def test_allow_any_public_refuses_non_public_address(self):
        with mock.patch.object(
                web, "_resolved_addresses_public",
                side_effect=HarnessError(
                    "site probe refused: resolves to non-public address")):
            with self.assertRaises(HarnessError):
                web.fetch_url("https://internal.example/secret",
                              allowed_hosts=[], allow_any_public=True)

    def test_allow_any_public_fetches_a_public_host(self):
        body = b"<html><title>P</title><body>hello world</body></html>"
        with mock.patch.object(web, "_resolved_addresses_public",
                               return_value=["93.184.216.34"]), \
             mock.patch.object(
                 web, "_http_get",
                 return_value=(200, body,
                               "https://freeoffgridcalculator.com/")):
            page = web.fetch_url("https://freeoffgridcalculator.com/",
                                 allowed_hosts=[], allow_any_public=True)
        self.assertIn("hello world", page["text"])


class FetchAllowlistConfigTests(unittest.TestCase):
    """The GUI-tunable allowlist posture resolves through the ONE owner
    (web.resolve_fetch_allowlist), with hostile host values sanitized."""

    def _settings(self, **kw):
        from harness.config import load_settings
        return load_settings(overrides=dict(kw))

    def test_default_is_enabled_with_builtin_hosts(self):
        hosts, any_public = web.resolve_fetch_allowlist(self._settings())
        self.assertFalse(any_public)
        self.assertIn("en.wikipedia.org", hosts)

    def test_disabled_allowlist_allows_any_public(self):
        _, any_public = web.resolve_fetch_allowlist(
            self._settings(fetch_allowlist_enabled=False))
        self.assertTrue(any_public)

    def test_custom_hosts_replace_defaults_and_are_sanitized(self):
        hosts, any_public = web.resolve_fetch_allowlist(self._settings(
            fetch_allowlist_hosts=(
                "FreeOffGridCalculator.com, https://evil.com, a.com/path")))
        self.assertEqual(hosts, {"freeoffgridcalculator.com"})
        self.assertFalse(any_public)

    def test_host_sanitizer_drops_hostile_values(self):
        self.assertIsNone(web._sanitize_host("https://evil.com"))
        self.assertIsNone(web._sanitize_host("a.com/path"))
        self.assertIsNone(web._sanitize_host("user@bad.com"))
        self.assertIsNone(web._sanitize_host("nodots"))
        self.assertEqual(web._sanitize_host(" Example.COM "), "example.com")


if __name__ == "__main__":
    unittest.main()

