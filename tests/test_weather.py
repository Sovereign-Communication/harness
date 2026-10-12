"""Hermetic contracts for the lookup lane's weather facts (no network)."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
