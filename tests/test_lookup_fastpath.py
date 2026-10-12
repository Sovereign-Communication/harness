"""Hermetic contracts for the lookup fast lane (no network, no plan lane).

A factual lookup (weather today) must classify as ``lookup``, fetch one
fact bundle, and answer in at most two cheap provider calls -- never
answer rounds, Jev review, or the plan lane. Edits, audits, driver work,
site checks, and ordinary conversation keep their existing routes.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness import events
from harness.agent import (AutonomousAgent, classify_prompt_intent,
                           is_lookup_question)
from harness.config import load_settings
from harness.errors import HarnessError

FACTS = {"place": "Pahoa, Hawaii, United States", "day": "2026-10-13",
         "facts": [], "summary": "Pahoa on 2026-10-13: rain."}
Q = "What's the weather supposed to be like tomorrow in Pahoa, Hi 96778?"


def _agent(tmp):
    return AutonomousAgent(settings=load_settings({}),
                           root_dir=Path(tmp), history_dir=Path(tmp))


def _ok(content="Sunny intervals, high 27C."):
    return (200, {"choices": [{"message": {"content": content},
                               "finish_reason": "stop"}],
                  "usage": {"cost": 0.000001}})


class ClassifyTests(unittest.TestCase):
    def test_weather_questions_are_lookup(self):
        for q in (Q,
                  "will it rain tomorrow in Pahoa?",
                  "Pahoa Hi 96778 forecast",
                  "how windy is it in Hilo today?"):
            with self.subTest(q=q):
                self.assertTrue(is_lookup_question(q))
                self.assertEqual(classify_prompt_intent(q), "lookup")

    def test_other_intents_unchanged(self):
        self.assertEqual(classify_prompt_intent("audit ledger"), "audit")
        self.assertEqual(
            classify_prompt_intent("fix the forecast bug in x.py"), "edit")
        self.assertEqual(
            classify_prompt_intent("refactor the weather module"), "edit")
        self.assertEqual(
            classify_prompt_intent("update the forecast page"), "edit")
        self.assertEqual(classify_prompt_intent("tell me a joke"),
                         "conversation")
        self.assertEqual(classify_prompt_intent("what's new in AI"),
                         "conversation")


class LookupLaneTests(unittest.TestCase):
    def _run(self, agent, question=Q, **kw):
        seen = []
        sink = seen.append
        events.add_sink(sink)
        self.addCleanup(events.remove_sink, sink)
        with patch("harness.weather.lookup_weather",
                   return_value=FACTS) as wx, \
             patch("harness.agent.governor_for",
                   return_value=(None, MagicMock())), \
             patch.object(AutonomousAgent, "_handle_edit",
                          side_effect=AssertionError("plan lane reached")), \
             patch.object(AutonomousAgent, "_chat_ladder",
                          return_value=["cheap-a", "cheap-b"]):
            res = agent.run_prompt(question, force_conversation=True, **kw)
        return res, seen, wx

    def test_lookup_answers_in_one_cheap_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("harness.agent.chat",
                       return_value=_ok()) as mc:
                res, seen, wx = self._run(_agent(tmp))
        wx.assert_called_once()
        self.assertEqual(mc.call_count, 1)
        self.assertEqual(mc.call_args[1]["model"], "cheap-a")
        self.assertIn("Pahoa on 2026-10-13: rain.",
                      mc.call_args[1]["messages"][0]["content"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["intent"], "lookup")
        self.assertEqual(res["response"], "Sunny intervals, high 27C.")
        self.assertTrue(res["web_used"])
        self.assertFalse([e for e in seen
                          if e.get("type") == "chat_escalated"])

    def test_lookup_rotates_once_then_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("harness.agent.chat",
                       side_effect=[(500, {"error": "x"}),
                                    _ok("Rain likely.")]) as mc:
                res, _seen, _wx = self._run(_agent(tmp))
        self.assertEqual(mc.call_count, 2)
        self.assertEqual(mc.call_args[1]["model"], "cheap-b")
        self.assertEqual(res["response"], "Rain likely.")

    def test_lookup_total_failure_is_honest_without_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("harness.agent.chat",
                       side_effect=[(500, {"error": "x"}),
                                    (500, {"error": "y"})]):
                with self.assertRaises(HarnessError):
                    self._run(_agent(tmp))

    def test_lookup_without_facts_falls_back_to_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch("harness.weather.lookup_weather",
                       side_effect=HarnessError("no place")), \
                 patch.object(AutonomousAgent, "_handle_conversation",
                              return_value={"status": "ok"}) as conv:
                res = agent.run_prompt(Q, force_conversation=True)
        conv.assert_called_once()
        self.assertTrue(conv.call_args[1]["web"])
        self.assertEqual(res["status"], "ok")

    def test_hourglass_entry_routes_lookup_to_fast_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch.object(AutonomousAgent, "_handle_lookup",
                              return_value={"status": "ok",
                                            "intent": "lookup"}) as lane:
                res = agent.run_hourglass_request(Q, session_id="hl")
        lane.assert_called_once()
        self.assertEqual(res["intent"], "lookup")


if __name__ == "__main__":
    unittest.main()
