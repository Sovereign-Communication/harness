"""Hermetic contracts for the bounded ordinary-answer lane."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import tempfile
import unittest

from harness.agent import AutonomousAgent
from harness.request_intent import classify_prompt_intent
from harness.config import load_settings
from harness.errors import (
    HarnessError,
    ProviderUsageUnknown,
    ToolCancelled,
)
from harness.jev_policy import policy_for
from harness.simple_request import (
    answer_clock,
    is_current_information_request,
    is_comprehensive_workspace_inspection_request,
    is_simple_answer_request,
    is_workspace_inspection_request,
    parse_clock_request,
    relevant_history,
)
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, comp, jev_resp, m


def _settings(root: Path):
    return load_settings({
        "panel_pool": "cheap-test-model",
        "ledger_path": str(root / "ledger.jsonl"),
        "jev_api_key": "jev-test",
        "max_cost": 0.01,
    })


def _pre(*, native=True, strategy="direct_answer"):
    return SimpleNamespace(is_fallback=not native), {
        "native": native, "strategy": strategy,
        "probabilities": ({strategy: 1.0} if native else None),
        "concentration": (1.0 if native else None),
        "cost": 0.000001,
    }


def _post(*, native=True, sufficient=0.95):
    return SimpleNamespace(is_fallback=not native), {
        "native": native, "verdict": "pass" if native else "fail",
        "answer_sufficient": sufficient,
        "iteration_required": False, "plan_required": False,
        "cost": 0.000001,
    }


class RequestKindTests(unittest.TestCase):
    def test_generic_ordinary_requests_are_simple(self):
        for prompt in (
            "How do I make a peanut butter and jelly sandwich?",
            "Explain why ice floats in a few sentences.",
            "Rewrite this: Thanks for waiting.",
            "What is 17 * 23?",
        ):
            with self.subTest(prompt=prompt):
                intent = classify_prompt_intent(prompt)
                self.assertEqual(intent, "conversation")
                self.assertTrue(is_simple_answer_request(prompt, intent=intent))

    def test_imperative_calculation_and_conversion_are_simple(self):
        for prompt in (
            "Calculate 17 times 23.",
            "Compute 17 times 23.",
            "Convert 3 miles to kilometers.",
            "Please calculate 17 times 23.",
            "Could you compute 17 times 23?",
            "Can you convert 3 miles to kilometers?",
        ):
            with self.subTest(prompt=prompt):
                intent = classify_prompt_intent(prompt)
                self.assertEqual(intent, "conversation")
                self.assertTrue(is_simple_answer_request(prompt, intent=intent))

    def test_tool_current_edit_and_ambiguous_requests_stay_out(self):
        for prompt in (
            "What is the weather tomorrow?",
            "Find the latest release notes.",
            "Rewrite harness/agent.py to be shorter.",
            "Fix the bug in executor.py.",
            "sandwich",
        ):
            with self.subTest(prompt=prompt):
                intent = classify_prompt_intent(prompt)
                self.assertFalse(is_simple_answer_request(prompt, intent=intent))
        self.assertTrue(is_current_information_request(
            "What is the weather tomorrow?"))

    def test_workspace_review_is_local_intent_not_general_repository_question(self):
        local = "Can you audit this repo and find the biggest remaining issues?"
        general = "How do I audit a software repository?"
        self.assertTrue(is_workspace_inspection_request(local))
        self.assertTrue(is_comprehensive_workspace_inspection_request(local))
        self.assertFalse(is_simple_answer_request(local))
        self.assertFalse(is_workspace_inspection_request(general))
        self.assertTrue(is_workspace_inspection_request(
            "What does router.py do?"))
        self.assertFalse(is_workspace_inspection_request(
            "Update router.py to change its timeout."))
        self.assertFalse(is_comprehensive_workspace_inspection_request(
            "What does router.py do?"))

    def test_general_review_question_without_workspace_reference_is_not_local(self):
        prompt = "How do I audit software repositories effectively?"
        self.assertFalse(is_workspace_inspection_request(prompt))
        self.assertFalse(is_comprehensive_workspace_inspection_request(prompt))

    def test_history_is_relevant_and_bounded(self):
        turns = [
            {"prompt": "Tell me about bicycles", "response": "old bicycle answer"},
            {"prompt": "Explain sourdough starter", "response": "bread context"},
            {"prompt": "Why does sourdough rise?", "response": "fermentation"},
        ]
        selected = relevant_history("How does sourdough rise?", turns,
                                    max_turns=1)
        self.assertEqual(selected[0]["response"], "fermentation")

    def test_empty_and_file_target_prompts_are_not_current(self):
        self.assertFalse(is_current_information_request(""))
        self.assertFalse(is_current_information_request(
            "Summarize the latest changes in harness/agent.py"))
        self.assertFalse(is_simple_answer_request(
            "What is in harness/agent.py?"))

    def test_simple_answer_guards_intent_and_length(self):
        self.assertFalse(is_simple_answer_request(
            "What is this?", intent="edit"))
        self.assertFalse(is_simple_answer_request(
            "What is " + "x" * 2000 + "?", intent="conversation"))
        self.assertFalse(is_simple_answer_request(
            "Fix the bug?", intent="conversation"))

    def test_history_skips_lexically_unrelated_turns(self):
        turns = [{"prompt": "pasta recipe",
                  "response": "boil water"}]
        self.assertEqual(
            relevant_history("quantum tunneling explained", turns), [])

    def test_history_skips_turns_beyond_the_char_budget(self):
        turns = [{"prompt": "pasta", "response": "x" * 5000}]
        self.assertEqual(
            relevant_history("pasta", turns, max_chars=100), [])

    def test_history_retains_immediate_prior_turn_on_anaphora(self):
        turns = [
            {"prompt": "Tell me about bicycles", "response": "old bicycle answer"},
            {"prompt": "what is the longest road in the world?",
             "response": "The Pan-American Highway is roughly 19,000 miles long."},
        ]
        # Follow-up with pronoun 'that' refers to the immediate prior turn
        kept = relevant_history("how many nanometers long is that?", turns, max_turns=2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["prompt"], "what is the longest road in the world?")

    def test_history_matches_terms_in_prior_response(self):
        turns = [
            {"prompt": "Explain baking science",
             "response": "Sourdough fermentation relies on lactobacillus cultures."},
        ]
        # Term 'lactobacillus' appears only in response, not in prior prompt
        kept = relevant_history("Tell me more about lactobacillus", turns, max_turns=1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["prompt"], "Explain baking science")


class SimpleStrategyPolicyTests(unittest.TestCase):
    def test_missing_key_falls_back_without_dispatch(self):
        settings = _settings(Path("."))
        settings.jev_api_key = None
        transport = FakeTransport()
        policy = policy_for(settings, transport=transport,
                            governor=MagicMock(), ledger=MagicMock())
        result, structural = policy.evaluate_simple_strategy("Explain ice")
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["strategy"], "direct_answer")
        self.assertEqual(transport.jev_calls(), [])

    def test_choice_concentration_is_observed_not_a_correctness_gate(self):
        answer = {
            "strategy": {
                "type": "choice", "choice": "direct_answer",
                "probabilities": {
                    "direct_answer": 0.34, "web_research": 0.0,
                    "workspace_inspection": 0.0, "simple_action": 0.0,
                    "needs_tool": 0.0, "plan": 0.0, "ambiguous": 0.66,
                },
                "confidence": 0.01,
            }
        }
        transport = FakeTransport(jev_posts=[jev_resp(answers=answer)])
        gov = SpendGovernor(FakeTransport(), "key", max_cost=0.01)
        ledger = MagicMock()
        policy = policy_for(_settings(Path(".")), transport=transport,
                            governor=gov, ledger=ledger)
        result, structural = policy.evaluate_simple_strategy("Explain ice")
        self.assertFalse(result.is_fallback)
        self.assertTrue(structural["native"])
        self.assertEqual(structural["strategy"], "direct_answer")
        self.assertEqual(structural["concentration"], 0.01)
        self.assertEqual(len(transport.jev_calls()), 1)

    def test_malformed_choice_degrades_to_deterministic_candidate(self):
        transport = FakeTransport(jev_posts=[jev_resp(answers={})])
        gov = SpendGovernor(FakeTransport(), "key", max_cost=0.01)
        policy = policy_for(_settings(Path(".")), transport=transport,
                            governor=gov, ledger=MagicMock())
        result, structural = policy.evaluate_simple_strategy("Explain ice")
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural["native"])
        self.assertEqual(structural["strategy"], "direct_answer")

    def test_transport_and_auth_failures_degrade_without_strategy_authority(self):
        cases = [
            FakeTransport(jev_posts=[(401, {"error": {"message": "unauthorized"}})]),
            MagicMock(),
        ]
        cases[1].post.side_effect = TimeoutError("timed out")
        for transport in cases:
            with self.subTest(transport=type(transport).__name__):
                gov = SpendGovernor(FakeTransport(), "key", max_cost=0.01)
                policy = policy_for(
                    _settings(Path(".")), transport=transport,
                    governor=gov, ledger=MagicMock())
                result, structural = policy.evaluate_simple_strategy(
                    "Explain ice")
                self.assertTrue(result.is_fallback)
                self.assertFalse(structural["native"])
                self.assertEqual(structural["strategy"], "direct_answer")

    def _live_strategy_policy(self):
        gov = SpendGovernor(FakeTransport(), "key", max_cost=0.01)
        return policy_for(_settings(Path(".")), transport=FakeTransport(),
                          governor=gov, ledger=MagicMock())

    def test_strategy_harness_error_degrades_to_direct_answer(self):
        policy = self._live_strategy_policy()
        with patch.object(policy.evaluator, "evaluate",
                           side_effect=HarnessError("strategy refused")):
            result, structural = policy.evaluate_simple_strategy(
                "Explain ice")
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural["native"])
        self.assertEqual(structural["strategy"], "direct_answer")
        self.assertIn("strategy refused", result.reasons[0])

    def test_strategy_provider_usage_unknown_propagates(self):
        policy = self._live_strategy_policy()
        with patch.object(policy.evaluator, "evaluate",
                           side_effect=ProviderUsageUnknown("usage unknown")), \
             self.assertRaises(ProviderUsageUnknown):
            policy.evaluate_simple_strategy("Explain ice")

    def test_strategy_cancellation_propagates(self):
        policy = self._live_strategy_policy()
        with patch.object(policy.evaluator, "evaluate",
                           side_effect=ToolCancelled("stop")), \
             self.assertRaises(ToolCancelled):
            policy.evaluate_simple_strategy("Explain ice")


class ClockFastPathTests(unittest.TestCase):
    def test_exact_user_query_routes_to_hst(self):
        self.assertEqual(
            parse_clock_request("What is the current date and time in HST?"),
            "HST")

    def test_bare_clock_questions_use_machine_local(self):
        for prompt in ("What time is it?", "What's today's date?",
                       "What day is it?", "Tell me the time."):
            with self.subTest(prompt=prompt):
                self.assertEqual(parse_clock_request(prompt), "")

    def test_event_time_questions_are_not_clock_questions(self):
        for prompt in ("What time is the meeting?",
                       "What date was the treaty signed?",
                       "What time does the store open?",
                       "What is the meeting time in Boston?",
                       "Fix the current time now?",
                       "Fix the bug in executor.py."):
            with self.subTest(prompt=prompt):
                self.assertIsNone(parse_clock_request(prompt))
                self.assertIsNone(answer_clock(prompt))

    def test_naive_injected_clock_value_is_treated_as_utc(self):
        from datetime import datetime
        answer = answer_clock(
            "What is the current time in HST?", now=datetime(2026, 10, 15, 16))
        self.assertIn("6:00 AM", answer)
        self.assertIn("HST", answer)

    def test_local_clock_answer_names_its_source(self):
        from datetime import datetime, timezone
        answer = answer_clock(
            "What time is it?", now=datetime(2026, 10, 15, 16,
                                             tzinfo=timezone.utc))
        self.assertIn("machine-local time", answer)
        self.assertIn("machine's clock", answer)
        self.assertIn("no lookup was run", answer)

    def test_hst_answer_is_fixed_utc_minus_10(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
        answer = answer_clock("What is the current date and time in HST?",
                              now=now)
        self.assertIn("6:00 AM", answer)
        self.assertIn("HST", answer)
        self.assertIn("UTC-10", answer)
        self.assertIn("machine's clock", answer)

    def test_requested_timezone_abbreviation_uses_its_fixed_offset(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
        answer = answer_clock("What is the current time in PST?", now=now)
        self.assertIn("PST", answer)
        self.assertIn("UTC-8", answer)

    def test_clock_answer_makes_zero_model_or_jev_calls(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from harness.agent import AutonomousAgent
        for force_conversation in (False, True):
            with self.subTest(force_conversation=force_conversation), \
                 tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                agent = AutonomousAgent(
                    settings=_settings(root), root_dir=root,
                    history_dir=root, transport=MagicMock())
                with patch("harness.simple_answer_lifecycle.governor_for",
                           side_effect=AssertionError("no governor")), \
                     patch("harness.simple_answer_lifecycle.policy_for",
                           side_effect=AssertionError("no Jev")), \
                     patch("harness.simple_answer_lifecycle.governed_completion",
                           side_effect=AssertionError("no model")):
                    result = agent.run_prompt(
                        "What is the current date and time in HST?",
                        session_id="clock-test",
                        force_conversation=force_conversation)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["cost"], 0.0)
                self.assertTrue(result["simple_request"]["clock"])
                self.assertIn("HST", result["response"])


class SimpleLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agent = AutonomousAgent(
            settings=_settings(self.root), root_dir=self.root,
            history_dir=self.root, transport=MagicMock())
        self.gov = MagicMock()
        self.ledger = MagicMock()

    def _run(self, answer="Spread peanut butter, add jelly, and close the bread.",
             pre=None, post=None):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = pre or _pre()
        policy.evaluate_answer.return_value = post or _post()
        completion = {
            "content": answer, "model": "cheap-test-model",
            "cost": 0.000002, "input_tokens": 21, "output_tokens": 9,
            "cost_estimated": False,
        }
        with (
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for", return_value=policy),
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
            patch("harness.simple_answer_lifecycle.governed_completion", return_value=completion) as generate,
        ):
            result = self.agent.run_prompt(
                "How do I make a peanut butter and jelly sandwich?",
                session_id="simple-test")
        return result, policy, generate

    def test_normal_path_is_pre_generation_post_once(self):
        result, policy, generate = self._run()
        state = self.agent.lifecycle.load("simple-test")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(policy.evaluate_simple_strategy.call_count, 1)
        self.assertEqual(policy.evaluate_answer.call_count, 1)
        self.assertEqual(result["simple_request"]["generation_calls"], 1)
        self.assertTrue(result["simple_request"]["post_review_passed"])
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["review"]["rounds"], 1)
        self.assertEqual(state["review"]["decision"], "pass")
        self.assertEqual(len(state["usage"]["provider_calls"]), 3)
        self.assertTrue(all(
            call["status"] == "completed"
            for call in state["usage"]["provider_calls"].values()))

    def test_failed_review_and_plan_signal_are_not_reported_as_passed(self):
        failed_judgment = SimpleNamespace(verdict="fail")
        failed_review = {
            "native": True,
            "verdict": "fail",
            "answer_sufficient": 0.96,
            "iteration_required": False,
            "plan_required": True,
            "cost": 0.000001,
        }
        result, _, _ = self._run(post=(failed_judgment, failed_review))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["simple_request"]["post"]["verdict"], "fail")
        self.assertTrue(result["simple_request"]["post"]["plan_required"])
        self.assertFalse(result["simple_request"]["post_review_passed"])

    def test_shared_prompt_entry_routes_imperative_conversion_to_simple_lane(self):
        with (
            patch.object(self.agent, "_live_jev_available", return_value=True),
            patch.object(self.agent, "run_simple_request",
                         return_value={"status": "ok", "workflow_tier": "simple-answer"}) as simple,
            patch.object(self.agent, "run_hourglass_request") as hourglass,
        ):
            result = self.agent.run_prompt(
                "Convert 3 miles to kilometers.", session_id="conversion")
        self.assertEqual(result["workflow_tier"], "simple-answer")
        simple.assert_called_once()
        hourglass.assert_not_called()

    def test_shared_prompt_entry_falls_through_on_pre_generation_deferral(self):
        with (
            patch.object(self.agent, "_live_jev_available", return_value=True),
            patch.object(self.agent, "run_simple_request",
                         return_value={"status": "deferred", "simple_request": {"generation_calls": 0}}),
            patch.object(self.agent, "run_hourglass_request",
                         return_value={"status": "ok", "response": "grounded answer"}) as hourglass,
        ):
            result = self.agent.run_prompt(
                "Explain the universe?", session_id="fallthrough")
        self.assertEqual(result["status"], "ok")
        hourglass.assert_called_once()

    def test_force_conversation_falls_through_on_pre_generation_deferral(self):
        with (
            patch.object(self.agent, "_live_jev_available", return_value=True),
            patch.object(self.agent, "run_simple_request",
                         return_value={"status": "deferred", "simple_request": {"generation_calls": 0}}),
            patch.object(self.agent, "run_hourglass_request",
                         return_value={"status": "ok", "response": "grounded answer"}) as hourglass,
        ):
            result = self.agent.run_prompt(
                "Explain the universe?", session_id="fallthrough-force", force_conversation=True)
        self.assertEqual(result["status"], "ok")
        hourglass.assert_called_once()

    def test_jev_unavailable_before_and_after_keeps_useful_answer(self):
        result, _, _ = self._run(pre=_pre(native=False),
                                 post=_post(native=False))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["simple_request"]["post_review_available"])
        self.assertIn("peanut butter", result["response"])

    def test_pre_strategy_defer_makes_no_generation(self):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = _pre(
            native=True, strategy="ambiguous")
        with (
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for", return_value=policy),
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
            patch("harness.simple_answer_lifecycle.governed_completion") as generate,
        ):
            result = self.agent.run_simple_request("Explain it?", session_id="s")
        self.assertEqual(result["status"], "deferred")
        generate.assert_not_called()

    def test_valid_refusal_is_a_result_not_an_outage(self):
        result, policy, generate = self._run(
            answer="I can't help with that request.")
        self.assertEqual(result["status"], "deferred")
        self.assertIn("can't help", result["defer_reason"])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(policy.evaluate_answer.call_count, 1)

    def test_receipt_carries_provider_usage(self):
        self._run()
        _, kwargs = self.ledger.append.call_args_list[-1]
        self.assertEqual(kwargs["input_tokens"], 21)
        self.assertEqual(kwargs["output_tokens"], 9)
        self.assertEqual(kwargs["generation_calls"], 1)
        self.assertEqual(kwargs["usage_source"], "provider")

    def test_zero_deadline_dispatches_nothing_and_ledgers_failure(self):
        with (
            patch("harness.simple_answer_lifecycle.governor_for") as governor,
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
            patch("harness.simple_answer_lifecycle.governed_completion") as generate,
        ):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s", max_wall_seconds=0)
        self.assertEqual(result["status"], "deferred")
        governor.assert_not_called()
        generate.assert_not_called()
        self.assertIn("deadline", result["defer_reason"])

    def test_generation_failure_is_ledgered_without_retry(self):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = _pre()
        with (
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for", return_value=policy),
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
            patch("harness.simple_answer_lifecycle.governed_completion",
                  side_effect=HarnessError("provider unavailable")) as generate,
        ):
            result = self.agent.run_simple_request("Explain ice?", session_id="s")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(generate.call_count, 1)
        self.assertIn("provider unavailable", result["defer_reason"])

    def test_deadline_during_post_preserves_generated_candidate(self):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = _pre()
        policy.evaluate_answer.side_effect = ToolCancelled("deadline")
        clock_calls = {"n": 0}

        def clock():
            clock_calls["n"] += 1
            return 0.0 if clock_calls["n"] <= 5 else 2.0

        completion = {
            "content": "Useful unreviewed candidate.",
            "model": "cheap-test-model", "cost": 0.000002,
            "input_tokens": 5, "output_tokens": 4,
            "cost_estimated": False,
        }
        with (
            patch("harness.simple_answer_lifecycle.time.monotonic", side_effect=clock),
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for", return_value=policy),
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
            patch("harness.simple_answer_lifecycle.governed_completion", return_value=completion),
        ):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s", max_wall_seconds=1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], "Useful unreviewed candidate.")
        self.assertFalse(result["simple_request"]["post_review_available"])
        self.assertIn("deadline", result["simple_request"]["post_skipped"])

    def test_deadline_unknown_usage_is_checkpointed_with_candidate_or_without(self):
        from harness.history import load_chat_checkpoint

        for has_candidate in (False, True):
            with self.subTest(has_candidate=has_candidate):
                policy = MagicMock()
                policy.evaluate_simple_strategy.return_value = _pre()
                exc = ToolCancelled("provider stopped", usage_unknown=True)
                exc.reserved_cost = 0.004
                if has_candidate:
                    policy.evaluate_answer.side_effect = exc
                    completion = {
                        "content": "Useful candidate.",
                        "model": "cheap-test-model", "cost": 0.000002,
                        "input_tokens": 5, "output_tokens": 4,
                        "cost_estimated": False,
                    }
                    provider = patch(
                        "harness.simple_answer_lifecycle.governed_completion",
                        return_value=completion)
                else:
                    provider = patch(
                        "harness.simple_answer_lifecycle.governed_completion",
                        side_effect=exc)
                clock_calls = {"n": 0}
                deadline_check = 5 if has_candidate else 4

                def clock():
                    clock_calls["n"] += 1
                    return (0.0 if clock_calls["n"] <= deadline_check
                            else 2.0)

                with patch("harness.simple_answer_lifecycle.time.monotonic", side_effect=clock), \
                     patch("harness.simple_answer_lifecycle.governor_for",
                           return_value=("key", self.gov)), \
                     patch("harness.simple_answer_lifecycle.policy_for", return_value=policy), \
                     patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger), \
                     provider:
                    result = self.agent.run_simple_request(
                        "Explain ice?", session_id=f"unknown-{has_candidate}",
                        max_wall_seconds=1)

                checkpoint = load_chat_checkpoint(
                    f"unknown-{has_candidate}", self.root)
                self.assertTrue(result.get("usage_unknown"))
                self.assertTrue(checkpoint["usage_unknown"])
                self.assertAlmostEqual(checkpoint["unknown_liability"], 0.004)
                if has_candidate:
                    self.assertEqual(result["response"], "Useful candidate.")
                    self.assertEqual(checkpoint["stage"], "answer_review")
                else:
                    self.assertEqual(result["status"], "deferred")

    def test_direct_method_cannot_let_choice_grant_mutation(self):
        with (
            patch("harness.simple_answer_lifecycle.governor_for") as governor,
            patch("harness.simple_answer_lifecycle.policy_for") as policy,
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
            patch("harness.simple_answer_lifecycle.governed_completion") as generate,
        ):
            result = self.agent.run_simple_request(
                "Rewrite harness/agent.py to delete the checks.", session_id="s")
        self.assertEqual(result["status"], "deferred")
        governor.assert_not_called()
        policy.assert_not_called()
        generate.assert_not_called()

    def test_real_shared_helpers_make_two_jev_posts_and_one_chat_post(self):
        strategy = {
            "strategy": {
                "type": "choice", "choice": "direct_answer",
                "probabilities": {
                    "direct_answer": 1.0, "web_research": 0.0,
                    "workspace_inspection": 0.0, "simple_action": 0.0,
                    "needs_tool": 0.0, "plan": 0.0, "ambiguous": 0.0,
                },
                "confidence": 1.0,
            }
        }
        post = {
            "answer_sufficient": {"type": "noul", "noul": 0.95},
            "iteration_required": {"type": "noul", "noul": 0.01},
            "plan_required": {"type": "noul", "noul": 0.01},
        }
        transport = FakeTransport(
            posts=[comp("Use two slices of bread.", cost=0.000002)],
            jev_posts=[jev_resp(answers=strategy), jev_resp(answers=post)],
        )
        pricing = FakeTransport(models=[m("cheap-test-model")])
        gov = SpendGovernor(pricing, "key", max_cost=0.01)
        agent = AutonomousAgent(
            settings=_settings(self.root), root_dir=self.root,
            history_dir=self.root, transport=transport)
        with (
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", gov)),
            patch("harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
        ):
            result = agent.run_prompt(
                "How do I make a simple sandwich?", session_id="shared-path")
        post_calls = [call for call in transport.calls if call[0] == "POST"]
        jev_calls = transport.jev_calls()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(post_calls), 3)
        self.assertEqual(len(jev_calls), 2)
        self.assertEqual(len(post_calls) - len(jev_calls), 1)


class SimpleRequestEdgeTests(unittest.TestCase):
    """Cover the cancellation, pool, budget, and deadline edges."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agent = AutonomousAgent(
            settings=_settings(self.root), root_dir=self.root,
            history_dir=self.root, transport=MagicMock())
        self.gov = MagicMock()
        self.ledger = MagicMock()

    def _patches(self, policy, **overrides):
        patches = {
            "governor_for": patch(
                "harness.simple_answer_lifecycle.governor_for",
                return_value=("key", self.gov)),
            "policy_for": patch(
                "harness.simple_answer_lifecycle.policy_for", return_value=policy),
            "ledger_for": patch(
                "harness.simple_answer_lifecycle.ledger_for", return_value=self.ledger),
        }
        patches.update(overrides)
        return patches

    def _policy(self):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = _pre()
        policy.evaluate_answer.return_value = _post()
        return policy

    def test_immediate_cancel_is_ledgered_and_reraised(self):
        policy = self._policy()
        patches = self._patches(policy)
        with patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"]:
            with self.assertRaises(ToolCancelled):
                self.agent.run_simple_request(
                    "Explain ice?", session_id="s", cancel_check=lambda: True)
        _, kwargs = self.ledger.append.call_args_list[-1]
        self.assertEqual(kwargs["status"], "cancelled")

    def test_cancel_after_deadline_without_answer_defers(self):
        policy = self._policy()
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            return 0.0 if calls["n"] <= 4 else 2.0

        patches = self._patches(policy)
        with patch("harness.simple_answer_lifecycle.time.monotonic", side_effect=clock), \
             patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], \
             patch("harness.simple_answer_lifecycle.governed_completion",
                   side_effect=ToolCancelled("provider stopped")):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s", max_wall_seconds=1)
        self.assertEqual(result["status"], "deferred")
        self.assertIn("deadline exhausted", result["defer_reason"])
        _, kwargs = self.ledger.append.call_args_list[-1]
        self.assertEqual(kwargs["status"], "deferred")

    def test_empty_model_pool_is_a_terminal_deferral(self):
        policy = self._policy()
        self.agent.settings.panel_pool = []
        patches = self._patches(policy)
        with patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], \
             patch.object(AutonomousAgent, "_chat_ladder", return_value=[]):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s")
        self.assertEqual(result["status"], "deferred")
        self.assertIn("model pool is empty", result["defer_reason"])

    def test_exhausted_shared_budget_leaves_no_answer_tokens(self):
        policy = self._policy()
        patches = self._patches(policy)
        with patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], \
             patch("harness.simple_answer_lifecycle._resolve_lane_max_tokens", return_value=0):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s")
        self.assertEqual(result["status"], "deferred")
        self.assertIn("no answer tokens", result["defer_reason"])

    def test_deadline_after_generation_skips_post_review(self):
        policy = self._policy()
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            return 0.0 if calls["n"] <= 4 else 2.0

        completion = {
            "content": "Ice floats.", "model": "cheap-test-model",
            "cost": 0.000002, "input_tokens": 5, "output_tokens": 4,
            "cost_estimated": False,
        }
        patches = self._patches(policy)
        with patch("harness.simple_answer_lifecycle.time.monotonic", side_effect=clock), \
             patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], \
             patch("harness.simple_answer_lifecycle.governed_completion",
                   return_value=completion):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s", max_wall_seconds=1)
        self.assertEqual(result["status"], "ok")
        self.assertIn("deadline", result["simple_request"]["post_skipped"])
        policy.evaluate_answer.assert_not_called()

    def test_is_simple_answer_request_recognizes_leading_question_mark(self):
        from harness.simple_request import is_simple_answer_request
        self.assertTrue(is_simple_answer_request("? simple question - answer please"))
        self.assertTrue(is_simple_answer_request("? what is ice"))

    def test_strategy_needs_tool_escalates_to_hourglass_when_live_jev_available(self):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = _pre(
            native=True, strategy="needs_tool")
        with (
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for", return_value=policy),
            patch.object(self.agent, "_live_jev_available", return_value=True),
            patch.object(self.agent, "run_hourglass_request",
                         return_value={"status": "ok", "response": "world records fact",
                                       "workflow_tier": "hourglass"}) as hourglass,
        ):
            result = self.agent.run_simple_request(
                "how many attempts at Guinness world records have there been in total?",
                session_id="guinness")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], "world records fact")
        hourglass.assert_called_once()
        self.assertTrue(hourglass.call_args[1].get("web"))

    def test_strategy_needs_tool_escalates_to_handle_conversation_when_live_jev_unavailable(self):
        policy = MagicMock()
        policy.evaluate_simple_strategy.return_value = _pre(
            native=True, strategy="needs_tool")
        with (
            patch("harness.simple_answer_lifecycle.governor_for", return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for", return_value=policy),
            patch.object(self.agent, "_live_jev_available", return_value=False),
            patch.object(self.agent, "_handle_conversation",
                         return_value={"status": "ok", "response": "direct convo fact"}) as convo,
        ):
            result = self.agent.run_simple_request(
                "how many attempts at Guinness world records have there been in total?",
                session_id="guinness")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], "direct convo fact")
        convo.assert_called_once()
        self.assertTrue(convo.call_args[1].get("web"))

    def test_unknown_strategy_dispatch_is_never_replayed_by_simple_answer(self):
        prompt = "Explain ice?"
        state = self.agent.lifecycle.begin(
            "chat", "saved-unknown", prompt, stage="strategy",
            data={"route": "simple-answer"})
        call_id = state["request_id"] + ":simple-strategy"
        self.agent.lifecycle.provider_started(state, call_id, 0.003)
        policy = MagicMock()
        checkpoint = {
            "route": "simple-answer", "status": "interrupted",
            "stage": "strategy", "prompt": prompt,
            "known_cost": 0.0, "unknown_liability": 0.0,
            "usage_unknown": False,
        }
        with (
            patch("harness.simple_answer_lifecycle.governor_for",
                  return_value=("key", self.gov)),
            patch("harness.simple_answer_lifecycle.policy_for",
                  return_value=policy),
            patch("harness.simple_answer_lifecycle.ledger_for",
                  return_value=self.ledger),
        ):
            result = self.agent.run_simple_request(
                prompt, session_id="saved-unknown", resume_state=checkpoint)

        saved = self.agent.lifecycle.load("saved-unknown")
        self.assertEqual(result["status"], "deferred")
        self.assertIn("did not replay it", result["response"])
        self.assertEqual(result["next_step"], "restate the request as a new task")
        self.assertEqual(saved["status"], "deferred")
        self.assertEqual(saved["usage"]["provider_calls"][call_id]["status"], "unknown")
        self.assertAlmostEqual(saved["usage"]["unknown_liability"], 0.003)
        policy.evaluate_simple_strategy.assert_not_called()

    def test_simple_answer_rejects_lifecycle_owned_by_another_lane(self):
        prompt = "Explain ice?"
        self.agent.lifecycle.begin("driver", "wrong-lane", prompt)
        with (
            patch("harness.simple_answer_lifecycle.ledger_for",
                  return_value=self.ledger),
        ):
            result = self.agent.run_simple_request(
                prompt, session_id="wrong-lane", resume_state={
                    "route": "simple-answer", "status": "interrupted",
                    "stage": "strategy", "prompt": prompt,
                })

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(
            result["defer_reason"], "chat request lifecycle could not be restored")

    def test_corrupt_shared_state_is_cleared_then_legacy_chat_is_adopted(self):
        prompt = "Explain ice?"
        self.agent.lifecycle._path("corrupt-chat", create=True).write_text(
            "{not valid lifecycle", encoding="utf-8")
        with patch("harness.simple_answer_lifecycle.ledger_for",
                   return_value=self.ledger):
            with self.assertRaises(ToolCancelled):
                self.agent.run_simple_request(
                    prompt, session_id="corrupt-chat",
                    cancel_check=lambda: True,
                    resume_state={
                        "route": "simple-answer", "status": "interrupted",
                        "stage": "strategy", "prompt": prompt,
                    })

        recovered = self.agent.lifecycle.load("corrupt-chat")
        self.assertEqual(recovered["lane"], "chat")
        self.assertEqual(recovered["original_request"], prompt)
        self.assertFalse(self.agent.lifecycle._path("corrupt-chat").read_text(
            encoding="utf-8").startswith("{not valid lifecycle"))


if __name__ == "__main__":
    unittest.main()
