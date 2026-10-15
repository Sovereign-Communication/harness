"""Hermetic contracts for the bounded ordinary-answer lane."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import tempfile
import unittest

from harness.agent import AutonomousAgent, classify_prompt_intent
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
    is_simple_answer_request,
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
        "native": native, "answer_sufficient": sufficient,
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
                    "direct_answer": 0.34, "needs_tool": 0.33,
                    "ambiguous": 0.33,
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
                with patch("harness.agent.governor_for",
                           side_effect=AssertionError("no governor")), \
                     patch("harness.agent.policy_for",
                           side_effect=AssertionError("no Jev")), \
                     patch("harness.agent.governed_completion",
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
            patch("harness.agent.governor_for", return_value=("key", self.gov)),
            patch("harness.agent.policy_for", return_value=policy),
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.governed_completion", return_value=completion) as generate,
            patch("harness.agent.enumerate_repo_files", side_effect=AssertionError("no repo intake")),
            patch("harness.agent.discover_target_files", side_effect=AssertionError("no AST intake")),
            patch("harness.agent.assess_completion", side_effect=AssertionError("no model judge")),
        ):
            result = self.agent.run_prompt(
                "How do I make a peanut butter and jelly sandwich?",
                session_id="simple-test")
        return result, policy, generate

    def test_normal_path_is_pre_generation_post_once(self):
        result, policy, generate = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(policy.evaluate_simple_strategy.call_count, 1)
        self.assertEqual(policy.evaluate_answer.call_count, 1)
        self.assertEqual(result["simple_request"]["generation_calls"], 1)

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
            patch("harness.agent.governor_for", return_value=("key", self.gov)),
            patch("harness.agent.policy_for", return_value=policy),
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.governed_completion") as generate,
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
            patch("harness.agent.governor_for") as governor,
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.governed_completion") as generate,
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
            patch("harness.agent.governor_for", return_value=("key", self.gov)),
            patch("harness.agent.policy_for", return_value=policy),
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.governed_completion",
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
            patch("harness.agent.time.monotonic", side_effect=clock),
            patch("harness.agent.governor_for", return_value=("key", self.gov)),
            patch("harness.agent.policy_for", return_value=policy),
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.governed_completion", return_value=completion),
        ):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s", max_wall_seconds=1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response"], "Useful unreviewed candidate.")
        self.assertFalse(result["simple_request"]["post_review_available"])
        self.assertIn("deadline", result["simple_request"]["post_skipped"])

    def test_direct_method_cannot_let_choice_grant_mutation(self):
        with (
            patch("harness.agent.governor_for") as governor,
            patch("harness.agent.policy_for") as policy,
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.governed_completion") as generate,
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
                    "direct_answer": 1.0, "needs_tool": 0.0,
                    "ambiguous": 0.0,
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
            patch("harness.agent.governor_for", return_value=("key", gov)),
            patch("harness.agent.ledger_for", return_value=self.ledger),
            patch("harness.agent.enumerate_repo_files",
                  side_effect=AssertionError("no repo intake")),
            patch("harness.agent.assess_completion",
                  side_effect=AssertionError("no model judge")),
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
                "harness.agent.governor_for",
                return_value=("key", self.gov)),
            "policy_for": patch(
                "harness.agent.policy_for", return_value=policy),
            "ledger_for": patch(
                "harness.agent.ledger_for", return_value=self.ledger),
            "intake": patch(
                "harness.agent.enumerate_repo_files",
                side_effect=AssertionError("no repo intake")),
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
             patches["ledger_for"], patches["intake"]:
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
        with patch("harness.agent.time.monotonic", side_effect=clock), \
             patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], patches["intake"], \
             patch("harness.agent.governed_completion",
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
             patches["ledger_for"], patches["intake"], \
             patch.object(AutonomousAgent, "_chat_ladder", return_value=[]):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s")
        self.assertEqual(result["status"], "deferred")
        self.assertIn("model pool is empty", result["defer_reason"])

    def test_exhausted_shared_budget_leaves_no_answer_tokens(self):
        policy = self._policy()
        patches = self._patches(policy)
        with patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], patches["intake"], \
             patch("harness.agent._resolve_lane_max_tokens", return_value=0):
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
        with patch("harness.agent.time.monotonic", side_effect=clock), \
             patches["governor_for"], patches["policy_for"], \
             patches["ledger_for"], patches["intake"], \
             patch("harness.agent.governed_completion",
                   return_value=completion):
            result = self.agent.run_simple_request(
                "Explain ice?", session_id="s", max_wall_seconds=1)
        self.assertEqual(result["status"], "ok")
        self.assertIn("deadline", result["simple_request"]["post_skipped"])
        policy.evaluate_answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
