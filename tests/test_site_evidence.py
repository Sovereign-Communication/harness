"""Rendered site evidence: the DOM facts an audit is allowed to cite."""
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from harness import site_evidence
from harness.errors import HarnessError
from harness.site_evidence import evidence_summary, render_site_evidence


class EvidenceSummaryTests(unittest.TestCase):
    def test_progress_modes_and_driven_actions_are_reported(self):
        summary = evidence_summary({
            "engine": "chromium headless",
            "final_url": "https://example.test/",
            "title": "Example",
            "progress": [{"id": "guidedProgress",
                          "class": "guided-progress",
                          "text": "4. Your result"}],
            "modes": [{"label": "Off-grid / Backup", "class": "active"}],
            "actions": [{"target": "Off-grid / Backup",
                         "result": "class=active"}],
            "steps": [{"step": "1. Location", "status": "initial"},
                      {"step": "4. Your result", "status": "done"}],
            "status": "ready",
            "result_excerpt": "Best pick",
            "input_count": 12,
            "screenshot": "shot.png",
            "text": "body",
        })
        self.assertIn("guidedProgress='4. Your result'", summary)
        self.assertIn("Off-grid / Backup -> class=active", summary)
        self.assertIn("1. Location (initial)", summary)
        self.assertIn("Screenshot: shot.png", summary)

    def test_empty_evidence_summarizes_to_none_markers(self):
        summary = evidence_summary({})
        self.assertIn("Progress/DOM markers: none", summary)
        self.assertIn("Mode controls: none", summary)
        self.assertIn("Driven actions: none", summary)
        self.assertIn("Progress transitions: none", summary)
        self.assertIn("Interactive controls: 0", summary)

    def test_plain_http_is_refused_before_a_browser_starts(self):
        with self.assertRaises(HarnessError):
            render_site_evidence("http://example.test/")


class _Element:
    def __init__(self, text="", attrs=None, fail_text=False, fail_click=False):
        self.text = text
        self.attrs = attrs or {}
        self.fail_text = fail_text
        self.fail_click = fail_click
        self.clicked = False

    def inner_text(self):
        if self.fail_text:
            raise RuntimeError("text unavailable")
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name, "")

    def click(self, timeout=0):
        if self.fail_click:
            raise RuntimeError("click failed")
        self.clicked = True

    @property
    def first(self):
        return self


class _Locator:
    def __init__(self, items=None, count=None, fail_count=False, fail_nth=False):
        self.items = list(items or [])
        self._count = len(self.items) if count is None else count
        self.fail_count = fail_count
        self.fail_nth = fail_nth

    def count(self):
        if self.fail_count:
            raise RuntimeError("count unavailable")
        return self._count

    def nth(self, index):
        if self.fail_nth:
            raise RuntimeError("nth unavailable")
        return self.items[index]

    @property
    def first(self):
        return self.items[0]

    def inner_text(self):
        return self.items[0].inner_text()

    def click(self, timeout=0):
        self.items[0].click(timeout)


class _Page:
    def __init__(self, *, progress=None, buttons=None, precise=False, run=False,
                 guided=(), statuses=(), body="", title="Example",
                 url="https://example.test/#result", input_count=12,
                 progress_fail=False, buttons_fail=False,
                 precise_fail_click=False, run_fail_click=False,
                 guided_fail=False, status_fail=False):
        self.progress = list(progress or [])
        self.buttons = list(buttons or [])
        self.precise = precise
        self.run = run
        self.guided = list(guided)
        self.statuses = list(statuses)
        self.body = body
        self._title = title
        self.url = url
        self.input_count = input_count
        self.progress_fail = progress_fail
        self.buttons_fail = buttons_fail
        self.precise_fail_click = precise_fail_click
        self.run_fail_click = run_fail_click
        self.guided_fail = guided_fail
        self.status_fail = status_fail
        self.guided_index = 0
        self.status_index = 0
        self.screenshots = []
        self.visited = []

    def goto(self, url, wait_until=None, timeout=None):
        self.visited.append((url, wait_until, timeout))

    def wait_for_timeout(self, _milliseconds):
        return None

    def title(self):
        return self._title

    def inner_text(self, selector):
        self.assert_body_selector = selector
        return self.body

    def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"png")
        self.screenshots.append((path, full_page))

    def locator(self, selector):
        if selector == site_evidence._PROGRESS_SELECTOR:
            return _Locator(self.progress, fail_count=self.progress_fail)
        if selector == "button":
            return _Locator(self.buttons, fail_count=self.buttons_fail)
        if selector == "#btnRunSizing":
            return _Locator([_Element(fail_click=self.run_fail_click)]
                            if self.run else [])
        if selector == "#guidedProgress":
            if self.guided_fail:
                return _Locator([_Element(fail_text=True)])
            if not self.guided:
                return _Locator([])
            index = min(self.guided_index, len(self.guided) - 1)
            self.guided_index += 1
            return _Locator([_Element(text=self.guided[index])])
        if selector == "#sizingStatus":
            if self.status_fail:
                return _Locator([_Element(fail_text=True)])
            if not self.statuses:
                return _Locator([])
            index = min(self.status_index, len(self.statuses) - 1)
            self.status_index += 1
            return _Locator([_Element(text=self.statuses[index])])
        if selector == "input,select,textarea":
            return _Locator(count=self.input_count)
        return _Locator([])

    def get_by_role(self, _role, name=None, exact=False):
        for button in self.buttons:
            if name and name.lower() in button.text.lower():
                return _Locator([button])
        return _Locator([])

    def get_by_text(self, _text, exact=False):
        if self.precise:
            return _Locator([_Element(fail_click=self.precise_fail_click)])
        return _Locator([])


class _Browser:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.context_kwargs = {}
        self.context = None

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        self.context = _Context(self.page)
        return self.context

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, page):
        self.page = page
        self.route_pattern = None
        self.route_handler = None

    def route(self, pattern, handler):
        self.route_pattern = pattern
        self.route_handler = handler

    def new_page(self):
        return self.page


class _Chromium:
    def __init__(self, page):
        self.page = page
        self.headless = None
        self.launch_kwargs = {}
        self.browser = None

    def launch(self, headless=False, **kwargs):
        self.headless = headless
        self.launch_kwargs = kwargs
        self.browser = _Browser(self.page)
        return self.browser


class _Playwright:
    def __init__(self, page):
        self.chromium = _Chromium(page)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_modules(page):
    playwright = _Playwright(page)
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: playwright
    module._playwright = playwright
    parent = types.ModuleType("playwright")
    parent.__path__ = []
    parent.sync_api = module
    return {"playwright": parent, "playwright.sync_api": module}


def _render(page, tmp, record=None, **kwargs):
    modules = _fake_modules(page)
    if record is not None:
        record["playwright"] = modules["playwright.sync_api"]._playwright
    with mock.patch.dict(sys.modules, modules), \
         mock.patch.object(site_evidence, "_public_https",
                           return_value="example.test"), \
         mock.patch.object(site_evidence, "emit") as events:
        result = render_site_evidence(
            "https://example.test/", screenshot_dir=Path(tmp), **kwargs)
    return result, events


class RenderSiteEvidenceTests(unittest.TestCase):
    def test_full_render_drives_modes_location_and_sizing(self):
        page = _Page(
            progress=[_Element(text="1. Location",
                               attrs={"id": "guidedProgress",
                                      "class": "progress"})],
            buttons=[
                _Element(text="Off-grid / Backup",
                         attrs={"class": "mode active", "aria-pressed": "true"}),
                _Element(text="Cut My Electric Bill", attrs={"class": "mode"}),
                _Element(text="Other"),
            ],
            precise=True, run=True,
            guided=["2. Usage", "2. Usage"],
            statuses=["working", "✅ Best pick"],
            body="Intro Best pick 12 kW Recommended",
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, events = _render(page, tmp)
            screenshot_exists = Path(result["screenshot"]).exists()

        self.assertTrue(result["rendered"])
        self.assertEqual(result["title"], "Example")
        self.assertEqual(result["final_url"], "https://example.test/")
        self.assertEqual(result["input_count"], 12)
        self.assertEqual(result["status"], "✅ Best pick")
        self.assertTrue(result["result_excerpt"].startswith("Best pick"))
        self.assertEqual(len(result["actions"]), 4)
        self.assertEqual(result["steps"][0],
                         {"step": "1. Location", "status": "initial"})
        self.assertEqual(result["steps"][1]["step"], "2. Usage")
        self.assertTrue(screenshot_exists)
        self.assertEqual([call.args[0] for call in events.call_args_list],
                         ["site_evidence", "site_evidence"])
        self.assertTrue(page.screenshots)

    def test_drive_modes_false_only_observes(self):
        page = _Page(progress=[_Element(text="1. Location")],
                     buttons=[_Element(text="Off-grid / Backup")],
                     precise=True, run=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, _events = _render(page, tmp, drive_modes=False)
        self.assertEqual(result["actions"], [])
        self.assertTrue(result["screenshot"])

    def test_missing_playwright_is_an_honest_error(self):
        with mock.patch.object(site_evidence, "_public_https",
                               return_value="example.test"), \
             mock.patch.dict(sys.modules,
                             {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(HarnessError) as caught:
                render_site_evidence("https://example.test/")
        self.assertIn("playwright is not installed", str(caught.exception))

    def test_absent_controls_and_failed_lookups_still_render(self):
        page = _Page(progress_fail=True, buttons_fail=True, body="nothing")
        with tempfile.TemporaryDirectory() as tmp:
            result, _events = _render(page, tmp)
        self.assertEqual(result["progress"], [])
        self.assertEqual(result["modes"], [])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["result_excerpt"], "")

    def test_precise_location_click_failure_is_recorded(self):
        page = _Page(precise=True, precise_fail_click=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, _events = _render(page, tmp)
        self.assertIn("failed: RuntimeError", result["actions"][0]["result"])

    def test_run_sizing_click_failure_is_recorded(self):
        page = _Page(run=True, run_fail_click=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, _events = _render(page, tmp)
        self.assertIn("failed: RuntimeError", result["actions"][0]["result"])

    def test_progress_and_status_read_errors_do_not_abort_the_loop(self):
        page = _Page(run=True, guided_fail=True, status_fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            result, _events = _render(page, tmp, timeout=0.05)
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["status"], "")


class SiteEvidenceHelperTests(unittest.TestCase):
    def test_public_https_resolves_only_https(self):
        with mock.patch("harness.web._resolved_addresses_public") as resolved:
            self.assertEqual(
                site_evidence._public_https("https://Example.COM/path"),
                "example.com")
            resolved.assert_called_once()
        with self.assertRaises(HarnessError):
            site_evidence._public_https("http://example.com/")

    def test_slug_and_screenshot_paths(self):
        self.assertEqual(
            site_evidence._safe_slug("https://Example.COM:8443/x"),
            "example.com")
        self.assertEqual(site_evidence._safe_slug("not a url"), "site")
        with tempfile.TemporaryDirectory() as tmp:
            path = site_evidence._screenshot_path(
                "https://example.com/", Path(tmp))
            self.assertEqual(path.parent, Path(tmp))
            self.assertTrue(path.name.startswith("site-example.com-"))
            self.assertTrue(path.name.endswith(".png"))
            self.assertEqual(site_evidence._screenshot_dir(Path(tmp)),
                             Path(tmp))
            with mock.patch("harness.paths.get_config_dir",
                            return_value=tmp):
                self.assertEqual(site_evidence._screenshot_dir(None),
                                 Path(tmp) / "site_evidence")

    def test_element_facts_success_count_failure_and_item_failure(self):
        page = _Page(progress=[_Element(text=" 4. Result ",
                                        attrs={"id": "p", "class": "bar"})])
        selector = site_evidence._PROGRESS_SELECTOR
        self.assertEqual(site_evidence._element_facts(page, selector),
                         [{"id": "p", "class": "bar", "text": "4. Result"}])
        self.assertEqual(
            site_evidence._element_facts(_Page(progress_fail=True),
                                         selector), [])
        self.assertEqual(
            site_evidence._element_facts(
                _Page(progress=[_Element(fail_text=True)]), selector), [])

    def test_mode_facts_success_skip_and_count_failure(self):
        page = _Page(buttons=[
            _Element(text="Off-grid / Backup", attrs={"class": "mode"}),
            _Element(text="Other"),
            _Element(fail_text=True),
        ])
        modes = site_evidence._mode_facts(page)
        self.assertEqual(len(modes), 1)
        self.assertEqual(modes[0]["label"], "Off-grid / Backup")
        self.assertEqual(site_evidence._mode_facts(_Page(buttons_fail=True)),
                         [])

    def test_click_mode_success_not_found_and_exception(self):
        success = _Page(buttons=[
            _Element(text="Off-grid / Backup",
                     attrs={"class": "active", "aria-pressed": "true"}),
        ])
        self.assertIn("class=active",
                      site_evidence._click_mode(success, "Off-grid")["result"])
        self.assertEqual(
            site_evidence._click_mode(_Page(), "missing")["result"],
            "not found")
        failure = _Page(buttons=[_Element(text="Off-grid",
                                          fail_click=True)])
        self.assertIn("failed: RuntimeError",
                      site_evidence._click_mode(failure, "Off-grid")["result"])


class _FakeRoute:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.outcome = None

    def continue_(self):
        self.outcome = "continue"

    def abort(self, reason=None):
        self.outcome = f"abort:{reason}"


class EgressProxyTests(unittest.TestCase):
    """The browser's network boundary, exercised with real sockets."""

    def _proxy(self, **kwargs):
        proxy = site_evidence._ValidatedEgressProxy(**kwargs)
        self.addCleanup(proxy.close)
        return proxy

    @staticmethod
    def _resolver(answers):
        calls = []

        def resolve(host, port, **_kwargs):
            calls.append(host)
            address = answers[min(len(calls), len(answers)) - 1]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (address, port))]

        resolve.calls = calls
        return resolve

    def _connect(self, proxy, authority):
        client = socket.create_connection(("127.0.0.1", proxy.port),
                                          timeout=5)
        self.addCleanup(client.close)
        client.sendall(
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n"
            .encode("ascii"))
        return client, client.recv(4096)

    def test_loopback_private_link_local_and_metadata_are_refused(self):
        proxy = self._proxy()
        for authority in ("127.0.0.1:443", "[::1]:443", "10.0.0.7:443",
                          "169.254.169.254:443", "[fe80::1]:443"):
            with self.subTest(authority=authority):
                _client, reply = self._connect(proxy, authority)
                self.assertIn(b" 403 ", reply)
                self.assertNotIn(b" 200 ", reply)

    def test_a_dns_answer_that_turns_private_never_connects(self):
        connected = []

        def connector(address, timeout=None):
            connected.append(address)
            raise AssertionError("a disallowed destination was connected")

        resolver = self._resolver(["127.0.0.1", "93.184.216.34"])
        proxy = self._proxy(resolve=resolver, connect=connector)
        _client, reply = self._connect(proxy, "public.test:443")
        self.assertIn(b" 403 ", reply)
        self.assertEqual(connected, [])
        self.assertEqual(resolver.calls, ["public.test"])

    def test_no_request_reaches_a_local_listener(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        listener.setblocking(False)
        proxy = self._proxy()
        _client, reply = self._connect(
            proxy, f"127.0.0.1:{listener.getsockname()[1]}")
        self.assertIn(b" 403 ", reply)
        with self.assertRaises(BlockingIOError):
            listener.accept()

    def test_the_tunnel_uses_the_address_it_validated(self):
        connected = []
        resolver = self._resolver(["93.184.216.34", "127.0.0.1"])

        def connector(address, timeout=None):
            connected.append(address)
            client_end, peer = socket.socketpair()
            self.addCleanup(peer.close)
            return client_end

        proxy = self._proxy(resolve=resolver, connect=connector)
        _client, reply = self._connect(proxy, "public.test:443")
        self.assertIn(b" 200 ", reply)
        self.assertEqual(connected, [("93.184.216.34", 443)])
        self.assertEqual(resolver.calls, ["public.test"])

    def test_non_connect_and_non_443_are_refused(self):
        proxy = self._proxy()
        client = socket.create_connection(("127.0.0.1", proxy.port),
                                          timeout=5)
        self.addCleanup(client.close)
        client.sendall(b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b" 403 ", client.recv(4096))
        _client, reply = self._connect(proxy, "example.test:8443")
        self.assertIn(b" 403 ", reply)


class RequestGuardTests(unittest.TestCase):
    """Every request type is refused before the tunnel is requested."""

    def test_public_https_continues_and_private_destinations_abort(self):
        guard = site_evidence._request_guard()

        def public(url):
            if "127.0.0.1" in url or "169.254" in url or "fe80" in url:
                raise HarnessError("site evidence refused: non-public")
            return "example.test"

        with mock.patch.object(site_evidence, "_public_https", public), \
                mock.patch.object(site_evidence, "emit"):
            allowed = _FakeRoute("https://example.test/app.js")
            guard(allowed)
            self.assertEqual(allowed.outcome, "continue")
            for url in ("https://127.0.0.1/x", "https://169.254.169.254/",
                        "http://example.test/", "https://[fe80::1]/"):
                route = _FakeRoute(url)
                guard(route)
                self.assertTrue(route.outcome.startswith("abort:"), url)
            data = _FakeRoute("data:image/png;base64,AA")
            guard(data)
            self.assertEqual(data.outcome, "continue")

    def test_a_refused_host_is_not_resolved_again(self):
        guard = site_evidence._request_guard()
        calls = []

        def public(url):
            calls.append(url)
            raise HarnessError("site evidence refused: non-public")

        with mock.patch.object(site_evidence, "_public_https", public), \
                mock.patch.object(site_evidence, "emit"):
            first = _FakeRoute("https://private.test/a")
            second = _FakeRoute("https://private.test/b")
            guard(first)
            guard(second)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first.outcome, "abort:blockedbyclient")
        self.assertEqual(second.outcome, "abort:blockedbyclient")


class EgressWiringTests(unittest.TestCase):
    def test_the_browser_is_forced_through_the_validated_proxy(self):
        page = _Page(body="rendered")
        record = {}
        with tempfile.TemporaryDirectory() as tmp:
            _render(page, tmp, record=record)
        chromium = record["playwright"].chromium
        browser = chromium.browser

        self.assertEqual(chromium.launch_kwargs["proxy"]["bypass"],
                         "<-loopback>")
        self.assertTrue(chromium.launch_kwargs["proxy"]["server"]
                        .startswith("http://127.0.0.1:"))
        self.assertEqual(browser.context_kwargs["service_workers"], "block")
        self.assertEqual(browser.context.route_pattern, "**/*")
        self.assertTrue(callable(browser.context.route_handler))


if __name__ == "__main__":
    unittest.main()
