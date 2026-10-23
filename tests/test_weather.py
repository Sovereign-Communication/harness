"""Hermetic contracts for the lookup lane's weather facts (no network)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.errors import HarnessError
from harness import weather as wx


def _fake_fetch(mapping):
    calls = []

    def fetch(url):
        calls.append(url)
        for prefix, doc in mapping.items():
            if url.startswith(prefix):
                if isinstance(doc, Exception):
                    raise doc
                return doc
        raise AssertionError(f"unexpected url {url}")
    fetch.calls = calls
    return fetch


_GEO = {"results": [{"name": "Pahoa", "latitude": 19.5, "longitude": -154.95,
                     "country": "United States", "admin1": "Hawaii"}]}
_FC = {"daily": {"time": ["2026-10-12", "2026-10-13"],
                 "weathercode": [2, 61],
                 "temperature_2m_max": [27.1, 26.0],
                 "temperature_2m_min": [19.2, 19.0],
                 "precipitation_probability_max": [10, 80],
                 "windspeed_10m_max": [15.0, 22.0]}}


class ExtractTests(unittest.TestCase):
    def test_place_prefers_name_over_zip(self):
        self.assertEqual(
            wx.extract_place("What's the weather tomorrow in Pahoa, Hi 96778?"),
            "Pahoa")

    def test_zip_only_falls_back_to_zip(self):
        self.assertEqual(wx.extract_place("weather 96778 tomorrow"), "96778")

    def test_no_place_is_none(self):
        self.assertIsNone(wx.extract_place("will it rain?"))

    def test_day_offsets(self):
        self.assertEqual(wx.day_offset("weather today"), 0)
        self.assertEqual(wx.day_offset("weather tomorrow"), 1)
        # Monday(0) -> Friday = +4; Saturday(5) -> Monday = +2.
        self.assertEqual(wx.day_offset("forecast friday", today_weekday=0), 4)
        self.assertEqual(wx.day_offset("forecast monday", today_weekday=5), 2)
        self.assertEqual(wx.day_offset("weather"), 0)


class FetchTests(unittest.TestCase):
    def test_lookup_weather_end_to_end_with_fakes(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com": _GEO,
                             "https://api.open-meteo.com": _FC})
        out = wx.lookup_weather(
            "What's the weather supposed to be like tomorrow in Pahoa, Hi 96778?",
            fetch_json=fetch, today_weekday=6)
        self.assertEqual(out["place"], "Pahoa, Hawaii, United States")
        self.assertEqual(out["day"], "2026-10-13")
        self.assertIn("rain", out["summary"])
        self.assertIn("80", out["summary"])
        self.assertTrue(all(u.startswith("https://") for u in fetch.calls))
        hosts = {u.split("/")[2] for u in fetch.calls}
        self.assertEqual(hosts, {"geocoding-api.open-meteo.com",
                                 "api.open-meteo.com"})

    def test_unknown_place_is_honest(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com":
                             {"results": []}})
        with self.assertRaises(HarnessError):
            wx.lookup_weather("weather tomorrow in Nowhereville",
                              fetch_json=fetch)

    def test_broken_fetcher_is_honest(self):
        with self.assertRaises(HarnessError):
            wx.lookup_weather("weather tomorrow in Pahoa",
                              fetch_json=_fake_fetch({}))

    def test_no_place_is_honest(self):
        with self.assertRaises(HarnessError):
            wx.lookup_weather("will it rain?",
                              fetch_json=_fake_fetch({}))

    def test_real_fetcher_refuses_off_hosts_without_network(self):
        with self.assertRaises(HarnessError):
            wx._https_json("https://example.com/x")
        with self.assertRaises(HarnessError):
            wx._https_json("http://api.open-meteo.com/v1/forecast")

    def test_next_week_fetches_seven_days(self):
        days = [f"2026-10-{12 + i:02d}" for i in range(7)]
        fetch = _fake_fetch({
            "https://geocoding-api.open-meteo.com": _GEO,
            "https://api.open-meteo.com": {
                "daily": {
                    "time": days,
                    "weathercode": [2] * 7,
                    "temperature_2m_max": [27.0] * 7,
                    "temperature_2m_min": [19.0] * 7,
                    "precipitation_probability_max": [10] * 7,
                    "windspeed_10m_max": [15.0] * 7,
                }}})
        out = wx.lookup_weather(
            "What's the weather like over the next week in Pahoa, Hi 96778?",
            fetch_json=fetch)
        self.assertEqual(len(out["facts"]), 7)
        self.assertIn("7-day outlook", out["summary"])
        self.assertIn("2026-10-18", out["summary"])
        self.assertIn("forecast_days=7", fetch.calls[1])

    def test_week_outlook_predicate(self):
        self.assertTrue(wx.is_week_outlook(
            "What's the weather like over the next week in Pahoa, Hi 96778?"))
        self.assertTrue(wx.is_week_outlook("7-day forecast for Pahoa"))
        self.assertFalse(wx.is_week_outlook("weather tomorrow in Pahoa"))
        self.assertFalse(wx.is_week_outlook(""))

    def test_null_fields_render_na_never_none_literal(self):
        fetch = _fake_fetch({
            "https://geocoding-api.open-meteo.com": _GEO,
            "https://api.open-meteo.com": {
                "daily": {
                    "time": ["2026-10-12"],
                    "weathercode": [None],
                    "temperature_2m_max": [None],
                    "temperature_2m_min": [19.2],
                    "precipitation_probability_max": [None],
                    "windspeed_10m_max": [None],
                }}})
        out = wx.lookup_weather("weather today in Pahoa", fetch_json=fetch)
        self.assertNotIn("None", out["summary"])
        self.assertNotIn("unknown", out["summary"])
        self.assertIn("n/a", out["summary"])
        self.assertIn("19.2", out["summary"])

    def test_short_payload_labels_actual_day_count(self):
        fetch = _fake_fetch({
            "https://geocoding-api.open-meteo.com": _GEO,
            "https://api.open-meteo.com": _FC})
        out = wx.lookup_weather(
            "What's the weather like over the next week in Pahoa, Hi 96778?",
            fetch_json=fetch)
        self.assertEqual(len(out["facts"]), 2)
        self.assertIn("2-day outlook", out["summary"])
        self.assertNotIn("7-day outlook", out["summary"])

    def test_format_facts_is_the_single_field_renderer(self):
        day = {"date": "2026-10-12", "summary": "partly cloudy",
               "high_c": 27.1, "low_c": None, "precip_pct": None,
               "wind_kmh": 15.0}
        body = wx.format_facts([day])
        self.assertIn("2026-10-12: partly cloudy", body)
        self.assertIn("27.1", body)
        self.assertNotIn("None", body)
        self.assertEqual(wx.format_facts([]), "")
        """The reported crash spawned a child worker file that does not
        exist; this lane must never spawn one -- plain in-process urllib,
        no subprocess, no key material on the call."""
        import harness.weather as mod
        with open(mod.__file__, encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("subprocess", src)
        self.assertNotIn("_http_worker", src)
        self.assertNotIn("api_key", src)
        self.assertNotIn("API_KEY", src)


class LanePredicateTests(unittest.TestCase):
    def test_pahoa_week_query_is_a_weather_question(self):
        from harness.agent import is_weather_question
        self.assertTrue(is_weather_question(
            "What's the weather like over the next week in Pahoa, Hi 96778?"))

    def test_placeless_and_code_prompts_are_not(self):
        from harness.agent import is_weather_question
        self.assertFalse(is_weather_question("will it rain?"))
        self.assertFalse(is_weather_question(
            "fix the weather parser in harness/weather.py"))
        self.assertFalse(is_weather_question(""))
        self.assertFalse(is_weather_question("refactor the auth module"))


def _lane_agent(tmp):
    from harness.agent import AutonomousAgent
    from harness.config import load_settings
    root = Path(tmp)
    return AutonomousAgent(settings=load_settings(), root_dir=root,
                           history_dir=root)


_PAHOA_FACTS = {
    "place": "Pahoa, Hawaii, United States", "day": "2026-10-12",
    "facts": [{"date": "2026-10-12", "summary": "partly cloudy",
                 "high_c": 27.1, "low_c": 19.2, "precip_pct": 10,
                 "wind_kmh": 15.0}],
    "summary": "Pahoa, Hawaii, United States on 2026-10-12: partly cloudy.",
}


class LaneContextTests(unittest.TestCase):
    def test_hourglass_context_grounds_the_pahoa_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._weather_lookup",
                            return_value=dict(_PAHOA_FACTS)) as lookup:
                context, _files, _brief, _web = \
                    agent._hourglass_request_context(
                        "What's the weather like over the next week "
                        "in Pahoa, Hi 96778?")
        lookup.assert_called_once()
        self.assertIn("WEATHER FACTS for Pahoa, Hawaii, United States",
                      context)
        self.assertIn("2026-10-12: partly cloudy", context)
        # Single owner: the lane body IS weather.format_facts output.
        self.assertIn(wx.format_facts(_PAHOA_FACTS["facts"]), context)

    def test_hourglass_context_failure_is_honest_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._weather_lookup",
                            side_effect=HarnessError("weather fetch failed")):
                context, _files, _brief, _web = \
                    agent._hourglass_request_context("weather tomorrow in Pahoa")
        self.assertIn("WEATHER LOOKUP FAILED", context)
        self.assertIn("do not invent", context)

    def test_hourglass_context_ignores_non_weather_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._weather_lookup") as lookup:
                context, _files, _brief, _web = \
                    agent._hourglass_request_context("Explain widget.py")
        lookup.assert_not_called()
        self.assertNotIn("WEATHER", context)

    def test_conversation_lane_grounds_the_pahoa_query(self):
        from harness.agent import AutonomousAgent
        from harness.config import load_settings
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=load_settings(),
                                    history_dir=Path(tmp))
            resp = {"choices": [{"message": {"content": "Pahoa: partly cloudy"},
                                  "finish_reason": "stop"}],
                    "usage": {"cost": 0.0}}
            with mock.patch("harness.agent._weather_lookup",
                            return_value=dict(_PAHOA_FACTS)), \
                 mock.patch("harness.agent.chat",
                            return_value=(200, resp)) as mock_chat, \
                 mock.patch("harness.agent.governor_for",
                            return_value=(None, mock.MagicMock())), \
                 mock.patch("harness.agent.assess_completion",
                            return_value={"complete": True}):
                res = agent._handle_conversation(
                    "What's the weather like over the next week "
                    "in Pahoa, Hi 96778?", "wx_sess")
            sysmsg = mock_chat.call_args[1]["messages"][0]["content"]
        self.assertEqual(res["status"], "ok")
        self.assertIn("WEATHER FACTS for Pahoa", sysmsg)


if __name__ == "__main__":
    unittest.main()
