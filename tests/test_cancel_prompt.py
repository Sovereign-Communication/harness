"""Prompt cancellation: Stop ends in-flight work promptly; task ids reach children.

Hermetic: no network, no sleeps longer than ~0.2s. Each test bounds its own
wall time so a regression that reintroduces a full backoff fails fast.
"""
import time
import unittest
from unittest import mock

from harness import events as _events
from harness.errors import ToolCancelled


class SleepOrCancelTests(unittest.TestCase):
    def test_set_flag_raises_without_sleeping(self):
        t0 = time.monotonic()
        with self.assertRaises(ToolCancelled):
            _events.sleep_or_cancel(30.0, lambda: True)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_flag_tripping_mid_sleep_raises_promptly(self):
        state = {"n": 0}

        def _check():
            state["n"] += 1
            return state["n"] > 2

        t0 = time.monotonic()
        with self.assertRaises(ToolCancelled):
            _events.sleep_or_cancel(30.0, _check, interval=0.02)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_no_flag_sleeps_briefly(self):
        t0 = time.monotonic()
        _events.sleep_or_cancel(0.05, lambda: False)
        self.assertGreaterEqual(time.monotonic() - t0, 0.04)


class TaskPropagationTests(unittest.TestCase):
    def test_wrap_binds_parent_task_id_in_child(self):
        seen = {}
        with _events.task_context("ui/abc123"):
            fn = _events.wrap_with_task_id(
                lambda: seen.setdefault("tid", _events.current_task_id()))
            fn()
        self.assertEqual(seen["tid"], "ui/abc123")

    def test_wrap_without_id_stays_missing(self):
        self.assertIsNone(_events.current_task_id())
        seen = {}
        fn = _events.wrap_with_task_id(
            lambda: seen.setdefault("tid", _events.current_task_id()))
        fn()
        self.assertIsNone(seen["tid"])

    def test_executor_child_events_carry_parent_task_id(self):
        from harness.executor import ConcurrentExecutor
        captured = []
        sink = captured.append
        _events.add_sink(sink)
        try:
            with _events.task_context("ui/child-probe"):
                def _worker(idx, _fp):
                    _events.emit("probe_event", idx=idx)
                    return {"status": "ok"}
                ConcurrentExecutor(max_workers=2).execute_files(
                    ["a.py", "b.py"], _worker)
        finally:
            _events.remove_sink(sink)
        probes = [e for e in captured if e.get("type") == "probe_event"]
        self.assertEqual(len(probes), 2)
        for e in probes:
            self.assertEqual(e.get("task_id"), "ui/child-probe")

    def test_executor_cancel_stops_promptly(self):
        from harness.executor import ConcurrentExecutor
        import threading
        release = threading.Event()

        def _worker(idx, _fp):
            release.wait(5)
            return {"status": "ok"}

        t0 = time.monotonic()
        try:
            with self.assertRaises(ToolCancelled):
                ConcurrentExecutor(max_workers=2).execute_files(
                    ["a.py", "b.py"], _worker, cancel_check=lambda: True)
        finally:
            release.set()
        self.assertLess(time.monotonic() - t0, 2.0)


class HttpCancelTests(unittest.TestCase):
    def test_post_pre_attempt_check_stops_cancelled_run(self):
        from harness._http import HttpTransport
        opener = mock.MagicMock()
        t0 = time.monotonic()
        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaises(ToolCancelled):
                HttpTransport().post("https://x.example/", "k", {"m": 1},
                                     cancel_check=lambda: True)
        opener.assert_not_called()
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_get_pre_attempt_check_stops_cancelled_run(self):
        from harness._http import HttpTransport
        opener = mock.MagicMock()
        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaises(ToolCancelled):
                HttpTransport().get("https://x.example/", "k",
                                    cancel_check=lambda: True)
        opener.assert_not_called()

    def test_get_retry_backoff_trips_cancel_mid_wait(self):
        # First attempt fails transiently; the backoff must abort as soon
        # as the flag trips instead of sleeping the full delay.
        from harness._http import HttpTransport
        polls = {"n": 0}

        def _check():
            polls["n"] += 1
            return polls["n"] > 2

        t0 = time.monotonic()
        with mock.patch("urllib.request.urlopen",
                         side_effect=OSError("blip")):
            with self.assertRaises(ToolCancelled):
                HttpTransport().get("https://x.example/", "k",
                                    cancel_check=_check)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_post_retry_backoff_trips_cancel_mid_wait(self):
        import io
        import urllib.error
        from harness._http import HttpTransport
        polls = {"n": 0}

        def _check():
            polls["n"] += 1
            return polls["n"] > 2

        err = urllib.error.HTTPError(
            "https://x.example/", 503, "busy", {}, io.BytesIO(b"busy"))
        t0 = time.monotonic()
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ToolCancelled):
                HttpTransport().post("https://x.example/", "k", {"m": 1},
                                     cancel_check=_check)
        self.assertLess(time.monotonic() - t0, 1.0)


class WebCancelTests(unittest.TestCase):
    def test_fetch_with_set_flag_never_hits_network(self):
        from harness import web
        getter = mock.MagicMock()
        with mock.patch.object(web, "_http_get", getter):
            with self.assertRaises(ToolCancelled):
                web.fetch_url("https://forecast.weather.gov/x",
                              allowed_hosts=["forecast.weather.gov"],
                              cancel_check=lambda: True)
        getter.assert_not_called()

    def test_fetch_retry_backoff_trips_cancel_mid_wait(self):
        from harness import web
        polls = {"n": 0}

        def _check():
            polls["n"] += 1
            return polls["n"] > 3

        url = "https://forecast.weather.gov/x"
        t0 = time.monotonic()
        with mock.patch.object(web, "_http_get",
                               side_effect=OSError("blip")):
            with self.assertRaises(ToolCancelled):
                web.fetch_url(url, allowed_hosts=["forecast.weather.gov"],
                              cancel_check=_check)
        # Full 1.5s retry sleep must not run out after cancel trips.
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_gather_cancel_stops_before_fetch(self):
        from harness import web
        with self.assertRaises(ToolCancelled):
            web.gather_web_context(
                "see https://www.anthropic.com/r for news",
                allowed_hosts=frozenset({"www.anthropic.com"}),
                fetch_url_fn=mock.MagicMock(),
                search_web_fn=mock.MagicMock(),
                find_urls_fn=lambda p: ["https://www.anthropic.com/r"],
                extract_query_fn=lambda p: "news",
                cancel_check=lambda: True)

    def test_gather_supports_legacy_fetch_seam_without_cancel_kwarg(self):
        # Hermetic stubs written against the old (url, allowed_hosts)
        # shape keep working through the TypeError fallback.
        from harness import web

        def _old_fetch(url, allowed_hosts=None):
            return {"url": url, "title": "t", "text": "forecast 96778"}

        out = web.gather_web_context(
            "weather near 96778",
            allowed_hosts=frozenset({"forecast.weather.gov"}),
            fetch_url_fn=_old_fetch,
            search_web_fn=lambda q: [{
                "title": "96778 forecast",
                "url": "https://forecast.weather.gov/x?zip=96778",
                "snippet": "Pahala HI 96778 highs"}],
            find_urls_fn=lambda p: [],
            extract_query_fn=lambda p: "96778 weather")
        self.assertTrue(any(s.get("ok") for s in out))

    def test_gather_drops_seattle_for_96778_and_checks_cancel(self):
        from harness import web
        results = [
            {"title": "Seattle weather", "url": "https://wx.example/sea",
             "snippet": "Seattle rain"},
            {"title": "96778 forecast", "url": "https://forecast.weather.gov/x?zip=96778",
             "snippet": "Pahala HI 96778 highs"},
        ]
        out = web.gather_web_context(
            "search weather near 96778?",
            allowed_hosts=frozenset({"forecast.weather.gov"}),
            fetch_url_fn=lambda u, allowed_hosts=None, cancel_check=None: {
                "url": u, "title": "t", "text": "Pahala HI 96778 text"},
            search_web_fn=lambda q: results,
            find_urls_fn=lambda p: [],
            extract_query_fn=lambda p: "96778 weather")
        self.assertTrue(out)
        for s in out:
            if s.get("ok"):
                self.assertNotIn("seattle",
                                 (s.get("url", "") + s.get("text", "")).lower())


class PanelCancelTests(unittest.TestCase):
    def test_cancel_during_429_backoff_raises_promptly(self):
        from harness import panel as _panel
        calls = {"n": 0}

        def _fake_chat(*a, **k):
            calls["n"] += 1
            return 429, {"error": {"message": "limited"},
                         "usage": {"cost": 0.0}}

        def _check():
            # Entry + post-chat checks pass; the backoff poll trips.
            return calls["n"] >= 1 and _backoff_started[0]

        _backoff_started = [False]
        real_sleep = _events.sleep_or_cancel

        def _spy_sleep(total, cancel=None, interval=0.05):
            _backoff_started[0] = True
            return real_sleep(total, _check, interval=interval)

        gov = mock.Mock()
        gov.is_free.return_value = True
        gov.learned_blocked.return_value = False
        gov.preflight.return_value = (0.0, [])
        gov.max_cost = 1.0
        with mock.patch.object(_panel, "chat", side_effect=_fake_chat), \
             mock.patch.object(_events, "sleep_or_cancel",
                               side_effect=_spy_sleep):
            t0 = time.monotonic()
            with self.assertRaises(ToolCancelled):
                _panel.panel_judge(
                    transport=mock.Mock(), api_key="k", governor=gov,
                    prompt="hi", panel=["m/a"], judge="m/j",
                    max_tokens=8, task_id="ui/cancel-probe",
                    max_panelists=1, cancel_check=_check)
            self.assertLess(time.monotonic() - t0, 3.0)


if __name__ == "__main__":
    unittest.main()
