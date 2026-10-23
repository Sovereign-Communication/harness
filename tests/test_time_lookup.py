"""Hermetic contracts for the local-time lookup lane (no network, no model)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.errors import HarnessError
from harness import time_lookup as tl
from harness.time_lookup import (
    is_clock_question, is_time_question, is_timezone_question,
)


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
                     "country": "United States", "admin1": "Hawaii",
                     "timezone": "Pacific/Honolulu"}]}
_FC = {"timezone": "Pacific/Honolulu", "utc_offset_seconds": -36000,
       "current": {"time": "2026-10-12T07:24", "temperature_2m": 22.1}}


class PredicateTests(unittest.TestCase):
    def test_clock_questions_match(self):
        for q in ("what time is it in Pahoa?",
                  "what time is it in Pahoa, Hi 96778?",
                  "what time is it in Tokyo?",
                  "current time in London",
                  "local time in Pahoa, Hi 96778?",
                  "What's the time in Berlin right now?"):
            self.assertTrue(is_time_question(q), q)

    def test_non_clock_prompts_do_not(self):
        for q in ("What was her marathon time in Boston?",
                  "fix the timeout in harness/server.py",
                  "will it rain tomorrow?",
                  "is it time yet?",
                  "refactor the auth module",
                  ""):
            self.assertFalse(is_time_question(q), q)

    def test_event_nouns_veto_every_branch(self):
        # Audit probe: these matched and would have received a clock
        # answer with system authority.
        for q in ("screen time in kids",
                  "What time is kickoff in London?",
                  "What time is the movie in Paris?",
                  "What time does the game start in Tokyo?",
                  "When does the show start in Berlin?"):
            self.assertFalse(is_time_question(q), q)


class DisjointTests(unittest.TestCase):
    def test_zone_questions_match_zone_only(self):
        # Disjoint by construction: a zone phrasing matches the zone
        # predicate only, even though "what time zone" trips clock words.
        for q in ("what time zone is Pahoa in?",
                  "what time zone is Pahoa, Hi 96778 in?",
                  "Pahoa time zone",
                  "time zone in Tokyo"):
            self.assertTrue(is_timezone_question(q), q)
            self.assertFalse(is_time_question(q), q)
            self.assertTrue(is_clock_question(q), q)

    def test_time_questions_match_time_only(self):
        # Queries deliberately disjoint from PredicateTests' clock list:
        # overlap would double-cover the same phrasings.
        for q in ("what time is it in Paris?",
                  "what time is it?",
                  "current time in Tokyo"):
            self.assertTrue(is_time_question(q), q)
            self.assertFalse(is_timezone_question(q), q)
            self.assertTrue(is_clock_question(q), q)

    def test_non_clock_prompts_match_neither(self):
        # Likewise disjoint from test_non_clock_prompts_do_not; a place
        # alone ("... capital of Peru?") must not trip the funnel either.
        for q in ("asdfgh jkl", "tell me a joke",
                  "what is the capital of Peru?"):
            self.assertFalse(is_time_question(q), q)
            self.assertFalse(is_timezone_question(q), q)
            self.assertFalse(is_clock_question(q), q)


class TimezoneTests(unittest.TestCase):
    def test_zone_lookup_is_geo_only(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com": _GEO})
        out = tl.lookup_timezone("what time zone is Pahoa in?",
                                 fetch_json=fetch)
        self.assertEqual(out["place"], "Pahoa, Hawaii, United States")
        self.assertEqual(out["timezone"], "Pacific/Honolulu")
        self.assertIn("Pacific/Honolulu", out["summary"])
        self.assertNotIn("None", out["summary"])
        self.assertEqual(len(fetch.calls), 1)
        # The captured place must be the full query place: a lazy
        # minimum once geocoded "Pa" (Burkina Faso) on the live API.
        self.assertIn("Pahoa", fetch.calls[0])

    def test_zone_lookup_without_zone_is_honest(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com":
                             {"results": [{"name": "X", "latitude": 1.0,
                                            "longitude": 2.0}]}})
        with self.assertRaises(HarnessError):
            tl.lookup_timezone("what time zone is X in?",
                               fetch_json=fetch)

    def test_zone_lane_answers_without_a_model(self):
        canned = {"place": "Pahoa, Hawaii, United States",
                  "timezone": "Pacific/Honolulu",
                  "summary": "Pahoa, Hawaii, United States is in the "
                               "Pacific/Honolulu time zone."}
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._lookup_tz",
                            return_value=dict(canned)) as lookup_tz, \
                 mock.patch("harness.agent.chat",
                            side_effect=AssertionError(
                                "model must not be called")):
                res = agent.run_hourglass_request(
                    "what time zone is Pahoa in?")
        lookup_tz.assert_called_once()
        self.assertEqual(res["status"], "ok")
        self.assertIn("Pacific/Honolulu", res["response"])
        self.assertIsNone(res["model"])
        self.assertEqual(res["cost"], 0.0)

    def test_zone_question_never_invokes_time_branch(self):
        # Ordering negative side: a zone question must take the geo-only
        # branch even though "what time zone" trips clock words; the time
        # branch would spend a second (clock) fetch for nothing.
        canned = {"place": "Pahoa, Hawaii, United States",
                  "timezone": "Pacific/Honolulu",
                  "summary": "Pahoa, Hawaii, United States is in the "
                               "Pacific/Honolulu time zone."}
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._lookup_tz",
                            return_value=dict(canned)) as lookup_tz, \
                 mock.patch("harness.agent._lookup_time",
                            side_effect=AssertionError(
                                "time branch must not run")) as lookup_time, \
                 mock.patch("harness.agent.chat",
                            side_effect=AssertionError(
                                "model must not be called")):
                res = agent.run_hourglass_request(
                    "what time zone is Pahoa in?")
        lookup_tz.assert_called_once()
        lookup_time.assert_not_called()
        self.assertEqual(res["status"], "ok")


class OffsetTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(tl._format_offset(-36000), "-10:00")
        self.assertEqual(tl._format_offset(0), "+00:00")
        self.assertEqual(tl._format_offset(19800), "+05:30")
        self.assertEqual(tl._format_offset(None), "n/a")
        self.assertEqual(tl._format_offset("junk"), "n/a")


class LookupTests(unittest.TestCase):
    def test_pahoa_time_end_to_end_with_fakes(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com": _GEO,
                             "https://api.open-meteo.com": _FC})
        out = tl.lookup_time("what time is it in Pahoa, Hi 96778?",
                             fetch_json=fetch)
        self.assertEqual(out["place"], "Pahoa, Hawaii, United States")
        self.assertEqual(out["timezone"], "Pacific/Honolulu")
        self.assertEqual(out["local_time"], "2026-10-12T07:24")
        self.assertEqual(out["utc_offset"], "-10:00")
        self.assertIn("2026-10-12T07:24", out["summary"])
        self.assertIn("Pacific/Honolulu", out["summary"])
        self.assertNotIn("None", out["summary"])
        hosts = {u.split("/")[2] for u in fetch.calls}
        self.assertEqual(hosts, {"geocoding-api.open-meteo.com",
                                 "api.open-meteo.com"})

    def test_unknown_place_is_honest(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com":
                             {"results": []}})
        with self.assertRaises(HarnessError):
            tl.lookup_time("what time is it in Nowhereville?",
                           fetch_json=fetch)

    def test_missing_clock_is_honest(self):
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com": _GEO,
                             "https://api.open-meteo.com": {"current": {}}})
        with self.assertRaises(HarnessError):
            tl.lookup_time("what time is it in Pahoa?", fetch_json=fetch)

    def test_no_place_is_honest(self):
        with self.assertRaises(HarnessError):
            tl.lookup_time("is it time yet?", fetch_json=_fake_fetch({}))

    def test_placeless_clock_answers_utc_without_fetch(self):
        # The headline placeless path: server UTC, no geocode, no clock
        # fetch -- a fetcher that explodes proves zero network.
        def no_fetch(url):
            raise AssertionError(f"no fetch expected, got {url}")
        out = tl.lookup_time("what time is it?", fetch_json=no_fetch)
        self.assertEqual(out["place"], "UTC")
        self.assertEqual(out["timezone"], "UTC")
        self.assertEqual(out["utc_offset"], "+00:00")
        self.assertRegex(out["local_time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
        self.assertIn("UTC", out["summary"])
        self.assertNotIn("None", out["summary"])

    def test_time_lane_sends_extracted_place_to_geocode(self):
        # Boundary pin: the time lane consumes weather.geocode's contract
        # -- the extracted place must reach the geocoder verbatim. A lazy
        # minimum once geocoded "Pa" (Burkina Faso) on the live API.
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com": _GEO,
                             "https://api.open-meteo.com": _FC})
        tl.lookup_time("what time is it in Pahoa, Hi 96778?",
                       fetch_json=fetch)
        self.assertIn("Pahoa", fetch.calls[0])

    def test_geocode_without_coordinates_is_honest(self):
        # Boundary pin: weather.geocode owes {latitude, longitude}; a doc
        # without them must defer, never answer from a missing clock.
        fetch = _fake_fetch({"https://geocoding-api.open-meteo.com":
                             {"results": [{"name": "Pahoa",
                                            "timezone": "Pacific/Honolulu"}]}})
        with self.assertRaises(HarnessError) as ctx:
            tl.lookup_time("what time is it in Pahoa?", fetch_json=fetch)
        self.assertIn("coordinates", str(ctx.exception))

    def test_lane_is_in_process_no_child_no_key(self):
        import harness.time_lookup as mod
        with open(mod.__file__, encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("subprocess", src)
        self.assertNotIn("_http_worker", src)
        self.assertNotIn("api_key", src)
        self.assertNotIn("zoneinfo", src)


_CANNED = {"place": "Pahoa, Hawaii, United States",
           "timezone": "Pacific/Honolulu", "local_time": "2026-10-12T07:24",
           "utc_offset": "-10:00",
           "summary": "It is 2026-10-12T07:24 in Pahoa, Hawaii, "
                      "United States (Pacific/Honolulu, UTC-10:00)."}


def _lane_agent(tmp):
    from harness.agent import AutonomousAgent
    from harness.config import load_settings
    root = Path(tmp)
    return AutonomousAgent(settings=load_settings(), root_dir=root,
                           history_dir=root)


class LaneTests(unittest.TestCase):
    def test_hourglass_answers_deterministically_without_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._lookup_time",
                            return_value=dict(_CANNED)), \
                 mock.patch("harness.agent.chat",
                            side_effect=AssertionError(
                                "model must not be called")):
                res = agent.run_hourglass_request(
                    "what time is it in Pahoa, Hi 96778?")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["cost"], 0.0)
        self.assertIsNone(res["model"])
        self.assertIn("2026-10-12T07:24", res["response"])
        self.assertIn("Pacific/Honolulu", res["response"])

    def test_conversation_lane_answers_without_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._lookup_time",
                            return_value=dict(_CANNED)), \
                 mock.patch("harness.agent.chat",
                            side_effect=AssertionError(
                                "model must not be called")):
                res = agent._handle_conversation(
                    "what time is it in Tokyo?", "t_sess")
        self.assertEqual(res["status"], "ok")
        self.assertIn("2026-10-12T07:24", res["response"])

    def test_lookup_failure_defers_honestly_without_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch("harness.agent._lookup_time",
                            side_effect=HarnessError(
                                "time lookup: no match for 'Nowhereville'")), \
                 mock.patch("harness.agent.chat",
                            side_effect=AssertionError(
                                "model must not be called")):
                res = agent.run_hourglass_request(
                    "what time is it in Nowhereville?")
        self.assertEqual(res["status"], "deferred")
        self.assertIn("couldn't get the time", res["response"])
        self.assertIn("Nowhereville", res["defer_reason"])

    def test_non_clock_prompts_skip_the_lane(self):
        from types import SimpleNamespace
        jev_res = SimpleNamespace(
            is_fallback=False, fallback_reason=None, verdict="sufficient",
            supported=True, confidence=0.9, reasons=[], model="jev-test",
            answers={"answer_sufficient": {"confidence": 0.9}},
            cost=0.0, input_tokens=0, output_tokens=0)
        mock_policy = mock.MagicMock()
        mock_policy.evaluate_answer.return_value = (
            jev_res, {"pack_version": 1, "cost": 0.0, "native": True,
                      "answer_sufficient": 0.99,
                      "iteration_required": False, "plan_required": False,
                      "input_tokens": 0, "output_tokens": 0})
        resp = {"choices": [{"message": {"content": "plants, basically"},
                              "finish_reason": "stop"}],
                "usage": {"cost": 0.0}}
        with tempfile.TemporaryDirectory() as tmp:
            agent = _lane_agent(tmp)
            with mock.patch.object(agent, "run_time_lookup",
                                   side_effect=AssertionError(
                                       "time lane must not run")), \
                 mock.patch("harness.agent.chat", return_value=(200, resp)), \
                 mock.patch("harness.agent.governor_for",
                            return_value=(None, mock.MagicMock())), \
                 mock.patch("harness.agent.policy_for",
                            return_value=mock_policy), \
                 mock.patch("harness.agent.assess_completion",
                            return_value={"complete": True}):
                res = agent.run_hourglass_request("Explain photosynthesis")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["response"], "plants, basically")


if __name__ == "__main__":
    unittest.main()
