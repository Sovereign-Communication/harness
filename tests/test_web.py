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


if __name__ == "__main__":
    unittest.main()
