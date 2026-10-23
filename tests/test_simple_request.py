"""Agnostic simple-request lane: ordinary questions get cheap answers.

No network, no model spend beyond canned fakes. Covers the operator ruling
that weather is one regression example, not the product: the fast path must
trigger on held-out paraphrases across everyday instructions, factual
explanations, calculations, and rewrites -- never per-topic routing.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agent import (
    AutonomousAgent,
    is_current_info_request,
    is_simple_answer_request,
)
from harness.errors import HarnessError
from tests._fake import FakeTransport, _gov, m


def _settings():
    from harness.config import load_settings

    settings = load_settings()
    settings.jev_api_key = None
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    settings.hourglass_require_attestation = False
    return settings


class SimpleClassifierTests(unittest.TestCase):
    def test_everyday_paraphrases_are_simple(self):
        for prompt in (
            "How do I make a peanut butter and jelly sandwich?",
            "Explain why ice floats in a few sentences.",
            "Convert 5 miles to kilometers.",
            "What is 17 * 23?",
            "Rewrite this in a friendlier tone: thanks for waiting.",
            "Summarize this paragraph in one sentence: the cat sat.",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_simple_answer_request(prompt), prompt)

    def test_supplied_prose_rewrites_are_conversation(self):
        from harness.agent import classify_prompt_intent
        for prompt in (
            "Rewrite this in a friendlier tone: thanks for waiting.",
            "Replace this word with a kinder one: stupid.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_prompt_intent(prompt), "conversation")
                self.assertTrue(is_simple_answer_request(prompt), prompt)

    def test_rewrite_with_file_or_second_verb_is_edit(self):
        from harness.agent import classify_prompt_intent
        for prompt in (
            "Rewrite executor.py to use pathlib.",
            "Rewrite and delete this paragraph from the notes.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_prompt_intent(prompt), "edit")

    def test_tool_requests_are_not_simple(self):
        for prompt in (
            "check if example.com is up right now",
            "can you find news about recent papers on riemann?",
            "Fix the bug in executor.py",
            "Refactor harness/calc.py",
            "verify chain",
            "How can I refactor executor.py?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_simple_answer_request(prompt), prompt)


class SimpleContextTests(unittest.TestCase):
    def test_simple_context_skips_repo_enumeration(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=_settings(),
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )
            with (
                patch(
                    "harness.agent.enumerate_repo_files", side_effect=AssertionError("no repo scan")
                ),
                patch(
                    "harness.agent.discover_target_files",
                    side_effect=AssertionError("no target discovery"),
                ),
                patch("harness.agent.keyword_fallback", side_effect=AssertionError("no fallback")),
            ):
                context, files, brief, web_sources = agent._hourglass_request_context(
                    "How do I make a peanut butter and jelly sandwich?"
                )
            self.assertEqual(files, {})
            self.assertEqual(web_sources, [])
            self.assertEqual(brief.estimated_tokens, 0)

    def test_non_simple_still_uses_repo_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=_settings(),
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )
            seen = {}

            def fake_discover(prompt, root):
                seen["called"] = True
                return []

            with (
                patch("harness.agent.discover_target_files", fake_discover),
                patch("harness.agent.enumerate_repo_files", return_value=[]),
                patch("harness.agent.keyword_fallback", return_value=[]),
            ):
                agent._hourglass_request_context("Fix the bug in executor.py")
            self.assertTrue(seen.get("called"))


class SimpleJevReuseTests(unittest.TestCase):
    def test_simple_path_keeps_independent_judge_authoritative(self):
        gov = _gov(FakeTransport(), max_cost=0.05)
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=_settings(),
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )

            def fake_context(prompt, web=False):
                ns = SimpleNamespace(estimated_tokens=0)
                ns.to_prompt_context = lambda: ""
                ns.ledger_fields = lambda: {"estimated_tokens": 0, "sources": 0}
                return ("", {}, ns, [])

            def fake_answer(*a, **k):
                return {
                    "answer": "Spread peanut butter, add jelly, close.",
                    "model": "m",
                    "cost": 0.0,
                }

            passed = {
                "confidence": {
                    "observed": 0.99,
                    "passed": True,
                    "threshold": 0.99,
                    "source": "jev.noul.answer_sufficient",
                },
                "native": True,
                "is_fallback": False,
                "verdict": "pass",
                "supported": 0.99,
                "answers": {},
                "reasons": [],
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "model": "jev",
                "pack_version": "v1",
            }
            structural = {
                "answer_sufficient": 0.99,
                "iteration_required": False,
                "plan_required": False,
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "pack_version": "v1",
            }

            judge_calls = []

            def fake_judge(prompt, state, chat_fn):
                judge_calls.append(prompt)
                return {"complete": True, "remaining": "", "reason": "fulfilled"}

            with (
                patch.object(agent, "_hourglass_request_context", fake_context),
                patch.object(agent, "_hourglass_answer_once", fake_answer),
                patch("harness.agent.governor_for", return_value=(None, gov)),
                patch("harness.agent.policy_for") as mock_policy,
                patch("harness.agent.assess_completion", side_effect=fake_judge),
                patch("harness.agent.load_chat_history", return_value=[]),
            ):
                mock_policy.return_value.evaluate_answer.return_value = (
                    SimpleNamespace(
                        answers={},
                        is_fallback=False,
                        verdict="pass",
                        supported=0.99,
                        confidence=0.99,
                        reasons=[],
                        cost=0.0,
                        input_tokens=1,
                        output_tokens=1,
                        model="jev",
                        discarded=False,
                        cache_hit=False,
                        fallback_reason=None,
                    ),
                    structural,
                )
                with patch.object(AutonomousAgent, "_jev_answer_envelope", return_value=passed):
                    result = agent.run_hourglass_request(
                        "How do I make a peanut butter and jelly sandwich?"
                    )
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["hourglass"]["simple_path"])
            # Safety contract: even on the simple path with Jev passed, the
            # independent judge still runs and stays authoritative.
            self.assertTrue(judge_calls)
            self.assertTrue(result["hourglass"]["rounds"][0]["completion"]["complete"])


class CurrentInfoTests(unittest.TestCase):
    """Time-window questions earn retrieval without per-topic routing."""

    def test_pahoa_week_weather_earns_web_not_simple(self):
        prompt = "What's the weather like over the next week in Pahoa, Hi 96778?"
        self.assertTrue(is_current_info_request(prompt))
        self.assertFalse(is_simple_answer_request(prompt))

    def test_neighbors_across_subjects(self):
        for prompt in (
            "Will it rain tomorrow?",
            "What is happening downtown this weekend?",
            "What is the traffic like tomorrow morning?",
            "Give me the outlook for next week.",
            "Show me upcoming concerts.",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_current_info_request(prompt), prompt)
                self.assertFalse(is_simple_answer_request(prompt), prompt)

    def test_timeless_requests_are_not_current_info(self):
        for prompt in (
            "How do I make a peanut butter and jelly sandwich?",
            "Explain why ice floats in a few sentences.",
            "What is 17 * 23?",
            "Rewrite this in a friendlier tone: thanks for waiting.",
            "Fix the bug in executor.py",
            "check if example.com is up right now",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_current_info_request(prompt), prompt)

    def test_current_info_skips_repo_enumeration_but_gathers_web(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=_settings(),
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )
            with (
                patch(
                    "harness.agent.enumerate_repo_files", side_effect=AssertionError("no repo scan")
                ),
                patch(
                    "harness.agent.discover_target_files",
                    side_effect=AssertionError("no target discovery"),
                ),
                patch("harness.agent.keyword_fallback", side_effect=AssertionError("no fallback")),
                patch.object(
                    agent,
                    "_gather_web_context",
                    return_value=[{"kind": "search", "ok": False, "note": "no usable results"}],
                ) as gather,
            ):
                context, files, brief, web_sources = agent._hourglass_request_context(
                    "What's the weather like over the next week in Pahoa, Hi 96778?",
                    web=True,
                )
            self.assertEqual(files, {})
            self.assertEqual(gather.call_count, 1)
            self.assertEqual(len(web_sources), 1)
            self.assertFalse(web_sources[0]["ok"])


class TransportSurvivalTests(unittest.TestCase):
    """A broken model transport degrades; auth failures stay fail-closed."""

    _PAHOA = "What's the weather like over the next week in Pahoa, Hi 96778?"
    _WORKER_BOOM = (
        "could not verify key limit: HTTP transport unavailable (HTTP child "
        "returned an invalid response: C:\\Python314\\python.exe: can't open "
        "file 'C:\\Temp\\pr213-rebase\\harness\\_http_worker.py': [Errno 2] "
        "No such file or directory). The key was not checked -- reinstall/restart "
        "Harness from the repo root."
    )

    def _agent(self, tmp):
        return AutonomousAgent(
            settings=_settings(),
            root_dir=Path(tmp),
            transport=FakeTransport(),
            history_dir=Path(tmp),
        )

    def test_broken_transport_degrades_honestly_with_zero_model_calls(self):
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with (
                patch("harness.agent.governor_for", side_effect=HarnessError(self._WORKER_BOOM)),
                patch.object(
                    agent, "_hourglass_answer_once", side_effect=AssertionError("no model calls")
                ),
                patch("harness.agent.policy_for", side_effect=AssertionError("no jev policy")),
                patch.object(
                    agent,
                    "_gather_web_context",
                    return_value=[{"kind": "search", "ok": False, "note": "no usable results"}],
                ),
                patch("harness.agent.ledger_for", return_value=MagicMock()),
            ):
                result = agent.run_hourglass_request(self._PAHOA)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["cost"], 0.0)
        self.assertIn("transport", result["response"].lower())
        self.assertNotIn("could not verify key limit", result["response"])
        self.assertEqual(result["hourglass"]["transport"], "unavailable")
        self.assertEqual(result["defer_reason"], "model transport unavailable")

    def test_auth_failure_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with (
                patch(
                    "harness.agent.governor_for",
                    side_effect=HarnessError("could not verify key limit: unauthorized"),
                ),
            ):
                with self.assertRaises(HarnessError):
                    agent.run_hourglass_request("How do I make a peanut butter and jelly sandwich?")

    def test_zero_hit_search_still_reaches_an_answer(self):
        gov = _gov(FakeTransport(), max_cost=0.05)
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)

            def fake_answer(*a, **k):
                return {"answer": "No current data found.", "model": "m", "cost": 0.0}

            passed = {
                "confidence": {
                    "observed": 0.99,
                    "passed": True,
                    "threshold": 0.99,
                    "source": "jev.noul.answer_sufficient",
                },
                "native": True,
                "is_fallback": False,
                "verdict": "pass",
                "supported": 0.99,
                "answers": {},
                "reasons": [],
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "model": "jev",
                "pack_version": "v1",
            }
            structural = {
                "answer_sufficient": 0.99,
                "iteration_required": False,
                "plan_required": False,
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "pack_version": "v1",
            }
            with (
                patch.object(agent, "_hourglass_answer_once", fake_answer),
                patch("harness.agent.governor_for", return_value=(None, gov)),
                patch("harness.agent.policy_for") as mock_policy,
                patch(
                    "harness.agent.assess_completion",
                    return_value={"complete": True, "remaining": "", "reason": "fulfilled"},
                ),
                patch("harness.agent.load_chat_history", return_value=[]),
                patch.object(
                    agent,
                    "_gather_web_context",
                    return_value=[{"kind": "search", "ok": False, "note": "no usable results"}],
                ),
            ):
                mock_policy.return_value.evaluate_answer.return_value = (
                    SimpleNamespace(
                        answers={},
                        is_fallback=False,
                        verdict="pass",
                        supported=0.99,
                        confidence=0.99,
                        reasons=[],
                        cost=0.0,
                        input_tokens=1,
                        output_tokens=1,
                        model="jev",
                        discarded=False,
                        cache_hit=False,
                        fallback_reason=None,
                    ),
                    structural,
                )
                with patch.object(AutonomousAgent, "_jev_answer_envelope", return_value=passed):
                    result = agent.run_hourglass_request(self._PAHOA)
            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["web_used"])


class SimpleEscalationGuardTests(unittest.TestCase):
    """A simple-path answer never auto-escalates into plan/execute.

    Regression for the live GUI failure where a sandwich question earned
    a Jev plan_required advisory and was then dragged through the full
    edit lane (repo scans, LLM decomposition, waist planning) for ~9
    minutes, ending refused. The direct answer must be returned with a
    manual-resume note and _handle_edit must never run."""

    def _run_plan_required(self, armed, web=False):
        gov = _gov(FakeTransport(), max_cost=0.05)
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings()
            settings.allow_escalation = armed
            agent = AutonomousAgent(
                settings=settings,
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )

            def fake_context(prompt, web=False):
                ns = SimpleNamespace(estimated_tokens=0)
                ns.to_prompt_context = lambda: ""
                ns.ledger_fields = lambda: {"estimated_tokens": 0, "sources": 0}
                return ("", {}, ns, [])

            def fake_answer(*a, **k):
                return {
                    "answer": "Spread peanut butter, add jelly, close.",
                    "model": "m",
                    "cost": 0.0,
                }

            structural = {
                "answer_sufficient": 0.0,
                "iteration_required": False,
                "plan_required": True,
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "pack_version": "v1",
            }
            passed = {
                "confidence": {
                    "observed": 0.0,
                    "passed": False,
                    "threshold": 0.99,
                    "source": "jev.noul.answer_sufficient",
                },
                "native": True,
                "is_fallback": False,
                "verdict": "fail",
                "supported": 0.0,
                "answers": {},
                "reasons": [],
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "model": "jev",
                "pack_version": "v1",
            }
            with (
                patch.object(agent, "_hourglass_request_context", fake_context),
                patch.object(agent, "_hourglass_answer_once", fake_answer),
                patch("harness.agent.governor_for", return_value=(None, gov)),
                patch("harness.agent.policy_for") as mock_policy,
                patch("harness.agent.resolve_api_key", return_value="k"),
                patch.object(
                    agent, "_handle_edit",
                    side_effect=AssertionError("simple path must not escalate"),
                ),
                patch.object(
                    AutonomousAgent, "_jev_answer_envelope", return_value=passed
                ),
                patch.object(agent, "_gather_web_context", return_value=[]),
            ):
                mock_policy.return_value.evaluate_answer.return_value = (
                    SimpleNamespace(
                        answers={},
                        is_fallback=False,
                        verdict="fail",
                        supported=0.0,
                        reasons=[],
                        cost=0.0,
                        input_tokens=1,
                        output_tokens=1,
                        model="jev",
                    ),
                    structural,
                )
                return agent.run_hourglass_request(
                    "How do I make a peanut butter and jelly sandwich?",
                    web=web,
                )

    def test_plan_required_returns_answer_without_escalation(self):
        result = self._run_plan_required(armed=True)
        self.assertEqual(result["status"], "plan_required")
        self.assertIn("peanut butter", result["response"])
        self.assertTrue(result["hourglass"]["simple_path"])
        self.assertIn("resubmit it as an edit request",
                      result.get("remaining_scope") or "")

    def test_plan_required_with_web_attached_still_returns_answer(self):
        # The Web toggle only adds pre-gathered sources; a no-tool
        # question with web=True must not escalate on the advisory
        # signal either.
        result = self._run_plan_required(armed=True, web=True)
        self.assertEqual(result["status"], "plan_required")
        self.assertIn("peanut butter", result["response"])
        self.assertFalse(result["hourglass"]["simple_path"])
        self.assertIn("resubmit it as an edit request",
                      result.get("remaining_scope") or "")

    def test_quiet_prompts_never_escalate_marker_does(self):
        # Escalation fires only on the model's own deferral marker --
        # neither a simple question nor a lookup that needs web evidence
        # (the Pahoa failure) routes to plan/execute on its own.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings()
            settings.allow_escalation = True
            agent = AutonomousAgent(
                settings=settings,
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )
            with patch("harness.agent.resolve_api_key", return_value="k"):
                for prompt in ("How do I make a sandwich?",
                               "What is the weather next week in Pahoa?"):
                    with self.subTest(prompt=prompt):
                        self.assertFalse(
                            agent._escalation_authorized(prompt, False))
                self.assertTrue(
                    agent._escalation_authorized("anything", True))


class LookupPlanAdvisoryTests(unittest.TestCase):
    """Pahoa-class lookups never terminate as plan_required.

    A Jev planning advisory on a lookup-class request (current-info
    or research: no write content) is set aside and disclosed, and
    the answer loop judges the response on its merits -- the run
    ends ok or needs_iteration with an honest scope, never with a
    resubmit-as-edit note for a question."""

    PAHOA = ("What is the weather like over the next week in Pahoa, "
             "HI 96778?")

    def _run_lookup(self, judge_complete=False):
        gov = _gov(FakeTransport(), max_cost=0.05)
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=_settings(),
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )

            def fake_context(prompt, web=False):
                ns = SimpleNamespace(estimated_tokens=0)
                ns.to_prompt_context = lambda: ""
                ns.ledger_fields = lambda: {"estimated_tokens": 0, "sources": 0}
                return ("", {}, ns, [])

            def fake_answer(*a, **k):
                return {
                    "answer": ("No Pahoa forecast data in this run; "
                                 "check forecast.weather.gov for 96778."),
                    "model": "m",
                    "cost": 0.0,
                }

            structural = {
                "answer_sufficient": 0.0,
                "iteration_required": False,
                "plan_required": True,
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "pack_version": "v1",
            }
            envelope = {
                "confidence": {
                    "observed": 0.99 if judge_complete else 0.0,
                    "passed": judge_complete,
                    "threshold": 0.99,
                    "source": "jev.noul.answer_sufficient",
                },
                "native": True,
                "is_fallback": False,
                "verdict": "pass" if judge_complete else "fail",
                "supported": 0.99 if judge_complete else 0.0,
                "answers": {},
                "reasons": [],
                "cost": 0.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "model": "jev",
                "pack_version": "v1",
            }
            completion = ({"complete": True, "remaining": "",
                           "reason": "honest limitation stated"}
                          if judge_complete else None)
            with (
                patch.object(agent, "_hourglass_request_context", fake_context),
                patch.object(agent, "_hourglass_answer_once", fake_answer),
                patch("harness.agent.governor_for", return_value=(None, gov)),
                patch("harness.agent.policy_for") as mock_policy,
                patch("harness.agent.assess_completion",
                      return_value=completion),
                patch.object(
                    agent, "_handle_edit",
                    side_effect=AssertionError("lookup must not escalate"),
                ),
                patch.object(agent, "_gather_web_context", return_value=[]),
                patch.object(
                    AutonomousAgent, "_jev_answer_envelope",
                    return_value=envelope
                ),
            ):
                mock_policy.return_value.evaluate_answer.return_value = (
                    SimpleNamespace(
                        answers={},
                        is_fallback=False,
                        verdict=envelope["verdict"],
                        supported=envelope["supported"],
                        reasons=[],
                        cost=0.0,
                        input_tokens=1,
                        output_tokens=1,
                        model="jev",
                    ),
                    structural,
                )
                return agent.run_hourglass_request(self.PAHOA, web=True)

    def test_lookup_plan_advisory_ends_honest_not_plan_required(self):
        result = self._run_lookup(judge_complete=False)
        self.assertEqual(result["status"], "needs_iteration")
        self.assertIn("forecast.weather.gov", result["response"])
        self.assertNotIn("resubmit it as an edit request",
                         result.get("remaining_scope") or "")
        self.assertTrue(
            result["hourglass"].get("plan_advisory_set_aside"))

    def test_lookup_plan_advisory_does_not_block_ok(self):
        result = self._run_lookup(judge_complete=True)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(
            result["hourglass"].get("plan_advisory_set_aside"))


class HistoryHygieneTests(unittest.TestCase):
    """Refused runs leave no assistant-history residue.

    Regression for the live GUI failure where a refused edit saga
    (recipe answer plus waist-gate guard block) was persisted as an
    assistant turn and replayed into the next run, whose model had to
    disclaim the stale refusal text as injected context."""

    def test_refused_turn_is_not_persisted(self):
        from harness.history import load_chat_history, save_chat_turn
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp)
            self.assertIsNone(save_chat_turn(
                "sess_ref", {"status": "refused", "prompt": "p",
                               "response": "no"}, history_dir=hdir))
            self.assertEqual(load_chat_history("sess_ref", history_dir=hdir), [])
            self.assertFalse((hdir / "sess_ref.jsonl").exists())

    def test_refused_turn_after_ok_turn_keeps_only_ok(self):
        from harness.history import load_chat_history, save_chat_turn
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp)
            save_chat_turn("sess_mix", {"status": "ok", "prompt": "p",
                                          "response": "r"}, history_dir=hdir)
            save_chat_turn("sess_mix", {"status": "refused", "prompt": "q",
                                          "response": "no"}, history_dir=hdir)
            turns = load_chat_history("sess_mix", history_dir=hdir)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["status"], "ok")

    def test_other_terminals_still_persist(self):
        from harness.history import load_chat_history, save_chat_turn
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp)
            for status in ("ok", "deferred", "failed", "needs_iteration",
                           "plan_required"):
                save_chat_turn("sess_all", {"status": status, "prompt": "p",
                                              "response": "r"}, history_dir=hdir)
            self.assertEqual(
                [t["status"] for t in load_chat_history("sess_all",
                                                          history_dir=hdir)],
                ["ok", "deferred", "failed", "needs_iteration",
                 "plan_required"])

    def test_refused_edit_saga_leaves_no_residue(self):
        from harness.history import load_chat_history
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                settings=_settings(),
                root_dir=Path(tmp),
                transport=FakeTransport(),
                history_dir=Path(tmp),
            )
            plan = {
                "confirmation": {"reason": "consent no longer covers this "
                                              "assignment",
                                 "cost": 0.0},
                "dag": {"nodes": []},
                "stages": [],
            }
            with patch.object(agent, "_handle_conversation",
                              side_effect=HarnessError("no model")):
                result = agent._refused_edit(plan, "Do the thing",
                                             [], "sess_saga")
            self.assertEqual(result["status"], "refused")
            self.assertEqual(
                load_chat_history("sess_saga", history_dir=Path(tmp)), [])


class VerifyKeyHardeningTests(unittest.TestCase):
    def test_missing_worker_reports_transport_not_key(self):
        gov = _gov(FakeTransport(), max_cost=0.05)

        class BrokenTransport:
            def get(self, url, api_key, timeout=15):
                raise OSError(
                    "HTTP child returned an invalid response: "
                    "C:\\Python314\\python.exe: can't open file "
                    "'C:\\Temp\\pr213-rebase\\harness\\_http_worker.py': "
                    "[Errno 2] No such file or directory"
                )

        gov.transport = BrokenTransport()
        with self.assertRaises(HarnessError) as ctx:
            gov.verify_key()
        msg = str(ctx.exception)
        self.assertIn("HTTP transport unavailable", msg)
        self.assertIn("key was not checked", msg)

    def test_session_catalog_refresh_forces_live_pricing(self):
        models_v1 = [m("elevenlabs/elevenlabs-v1", "0.00000010", "0.00000020")]
        models_v2 = [m("elevenlabs/elevenlabs-v1", "0.00000005", "0.00000010")]
        fake = FakeTransport(models=models_v1)
        gov = _gov(fake, max_cost=0.05)
        before = gov.fetch_pricing(["elevenlabs/elevenlabs-v1"])
        fake.models = models_v2
        gov.refresh_session_catalog()
        after = gov.fetch_pricing(["elevenlabs/elevenlabs-v1"])
        self.assertNotEqual(before, after)
        self.assertEqual(after["elevenlabs/elevenlabs-v1"][0], 5e-08)


if __name__ == "__main__":
    unittest.main()
