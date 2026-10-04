"""D7: a run is bounded in wall time, and a research question gets evidence.

The dogfood ran one prompt -- "can you find news about the work Claude models
did on the riemann hypothesis?" -- for about six minutes and returned nothing.
Three separate gaps fed that:

* the run was bounded in rounds but not in wall time, so a sequence of
  individually-legal rounds had no ceiling;
* a research question was routed to the conversation lane but never gathered
  sources, so every iteration re-asked a question it had no evidence for;
* when a run stopped, it said only ``needs_iteration``. ``stop_reason`` was
  computed in four places and then dropped before the envelope.

The standing requirement these tests protect: a run returns a real answer with
an honest disclosure, never a bare dead end.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agent import (
    DEFAULT_RUN_WALL_SECONDS,
    AutonomousAgent,
    is_research_question,
)
from harness.config import load_settings
from tests._fake import FakeTransport, _gov

NL = chr(10)

_TEST_GOVERNOR = _gov(FakeTransport(), max_cost=0.05)


def _settings():
    settings = load_settings()
    settings.jev_api_key = None
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    settings.hourglass_require_attestation = False
    return settings


class ResearchQuestionTests(unittest.TestCase):
    """Which prompts earn the web lane."""

    def test_the_dogfood_prompt_is_a_research_question(self):
        self.assertTrue(is_research_question(
            "can you find news about the work Claude models did on the "
            "riemann hypothesis? real progress!"))

    def test_other_research_shapes(self):
        for prompt in ("search for recent papers on riemann",
                       "look up the latest release notes for python",
                       "what is the latest news on rust?",
                       "tell me about the latest studies on sleep"):
            self.assertTrue(is_research_question(prompt), prompt)

    def test_edits_are_not_research_questions(self):
        # The subject rule is what keeps these out: "find" appears, but these
        # are asking about the working tree, not the world.
        for prompt in ("find the bug in auth.py",
                       "fix the failing test in test_waist.py",
                       "refactor auth.py to use tokens",
                       "run the tests",
                       "2+2",
                       "update the README with the new release notes"):
            self.assertFalse(is_research_question(prompt), prompt)

    def test_empty_is_not_a_research_question(self):
        for prompt in ("", "   ", None):
            self.assertFalse(is_research_question(prompt))


class _RunCase(unittest.TestCase):
    """A throwaway repo and a scoped transport/governor boundary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".hist").mkdir(exist_ok=True)
        self.addCleanup(self._tmp.cleanup)
        patcher = patch("harness.agent.governor_for",
                        return_value=(None, _TEST_GOVERNOR))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _agent(self):
        return AutonomousAgent(
            settings=_settings(), root_dir=self.root,
            transport=FakeTransport(), history_dir=self.root / ".hist")

    def _run(self, prompt, agent=None, **kwargs):
        """Run with both internals stubbed, recording the web flag seen."""
        agent = agent or self._agent()
        seen = {}

        def fake_context(p, web=False):
            seen["web"] = web
            return ("ctx", [], SimpleNamespace(estimated_tokens=0), [])

        calls = []

        def fake_answer(*a, **k):
            calls.append(1)
            return {"answer": "4", "model": "double", "cost": 0.0}

        with patch.object(agent, "_hourglass_request_context", fake_context), \
             patch.object(agent, "_hourglass_answer_once", fake_answer), \
             patch("harness.agent.assess_completion",
                   return_value={"complete": True}):
            result = agent.run_hourglass_request(prompt, **kwargs)
        return result, seen, calls


class RunWallClockTests(_RunCase):
    """A run is bounded in wall time, not only in rounds."""

    def test_an_exhausted_budget_stops_before_another_model_call(self):
        result, _seen, calls = self._run("2+2", max_wall_seconds=0.0)
        self.assertEqual(calls, [],
                         "no answer attempt may start past the deadline")
        self.assertEqual(result["status"], "needs_iteration")
        self.assertIn("wall-clock", result["stop_reason"])

    def test_a_healthy_budget_does_not_trip_the_ceiling(self):
        result, _seen, calls = self._run(
            "2+2", max_wall_seconds=DEFAULT_RUN_WALL_SECONDS)
        self.assertNotIn("wall-clock", result.get("stop_reason", ""))
        self.assertTrue(calls, "a healthy budget must still attempt an answer")

    def test_the_default_budget_is_a_positive_ceiling(self):
        self.assertGreater(DEFAULT_RUN_WALL_SECONDS, 0.0)

    def test_a_stopped_run_still_reports_a_response_field(self):
        # The standing rule: a stopped run returns an answer plus an honest
        # disclosure, never a bare dead end.
        result, _seen, _calls = self._run("2+2", max_wall_seconds=0.0)
        self.assertIn("response", result)
        self.assertTrue(result["stop_reason"])


class ResearchQuestionWebRoutingTests(_RunCase):
    """A research question gathers sources even when the caller did not ask."""

    def test_a_research_prompt_asks_for_the_web_lane(self):
        _result, seen, _calls = self._run(
            "can you find news about recent papers on riemann?",
            max_wall_seconds=0.0)
        self.assertTrue(seen.get("web"))

    def test_a_plain_question_does_not(self):
        _result, seen, _calls = self._run("2+2", max_wall_seconds=0.0)
        self.assertFalse(seen.get("web"))

    def test_an_explicit_web_request_still_works(self):
        _result, seen, _calls = self._run("2+2", web=True,
                                          max_wall_seconds=0.0)
        self.assertTrue(seen.get("web"))


if __name__ == "__main__":
    unittest.main()
