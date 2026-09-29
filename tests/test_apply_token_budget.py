"""HV-5 execution dispatch reserves and honestly settles stage tokens."""
import unittest
from types import SimpleNamespace

from harness.token_budget import (
    TokenBudget, USAGE_ACTUAL, USAGE_ESTIMATED, USAGE_UNAVAILABLE,
)
from harness.errors import HarnessError
from tests._applyfixture import ApplyFixture, CHANGED, scripted_run
from tests._fake import comp
from harness.apply_state import RunState


class ApplyTokenBudgetTests(ApplyFixture):
    def _apply(self, response, budget, **kwargs):
        path = self.make_file()
        fake, _, _, engine = self.make_env(
            posts=[response], run=scripted_run([(0, "")]), renew=False)
        result = engine.apply_edit(
            task_id="token-budget", file_path=path, instruction="make change",
            require_consent=False, renew_consent=False,
            verify_cmd="python -c pass", token_budget=budget, **kwargs)
        return fake, result

    def test_success_without_provider_counts_is_estimated_and_settled(self):
        budget = TokenBudget("execution", max_input_tokens=100_000,
                             max_output_tokens=8_000)
        fake, result = self._apply(comp(CHANGED), budget)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(fake.chat_posts()), 1)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["usage_sources"][USAGE_ESTIMATED], 1)
        self.assertGreater(snapshot["used_input_tokens"], 0)
        self.assertGreater(snapshot["used_output_tokens"], 0)
        self.assertEqual(snapshot["open_allowances"], 0)

    def test_provider_counts_are_actual_and_overrun_remains_visible(self):
        response = comp(CHANGED)
        response["usage"].update(prompt_tokens=12, completion_tokens=69)
        budget = TokenBudget("execution", max_input_tokens=100_000,
                             max_output_tokens=8_000)
        _, result = self._apply(response, budget, max_tokens=64)
        self.assertEqual(result["status"], "ok")
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["usage_sources"][USAGE_ACTUAL], 1)
        self.assertEqual(snapshot["used_input_tokens"], 12)
        self.assertEqual(snapshot["used_output_tokens"], 69)
        self.assertEqual(snapshot["over_output_tokens"], 5)

    def test_unknown_error_usage_charges_the_full_reservation(self):
        budget = TokenBudget("execution", max_input_tokens=100_000,
                             max_output_tokens=8_000)
        _, result = self._apply((429, {"error": {"message": "rate limited"}}),
                                budget, max_rotations=0)
        self.assertNotEqual(result["status"], "ok")
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["usage_sources"][USAGE_UNAVAILABLE], 1)
        self.assertEqual(snapshot["used_output_tokens"], 4096)
        self.assertEqual(snapshot["open_allowances"], 0)

    def test_stage_budget_refuses_before_network_dispatch(self):
        budget = TokenBudget("execution", max_input_tokens=100_000,
                             max_output_tokens=8)
        path = self.make_file()
        fake, _, _, engine = self.make_env(posts=[], renew=False)
        with self.assertRaises(HarnessError):
            engine.apply_edit(
                task_id="token-budget", file_path=path,
                instruction="make change", require_consent=False,
                token_budget=budget)
        self.assertEqual(fake.chat_posts(), [])
        self.assertEqual(budget.snapshot()["open_allowances"], 0)

    def test_escalation_driver_refusal_happens_before_network_dispatch(self):
        from harness.escalation import EscalationDriver
        from harness.router import Router

        class Transport:
            posts = 0

            def post(self, *args, **kwargs):
                self.posts += 1
                return 200, {}

        fake = Transport()
        gov = type("Governor", (), {
            "preflight": lambda self, prompt, calls: None,
            "spent": 0.0,
            "check_byok": lambda self, model: None,
            "assert_no_tools": lambda self, payload, model: None,
        })()
        ledger = None
        driver = EscalationDriver(
            Router(["a"], "j", "a", allow_escalation=True,
                   escalation_pool=["paid/escalation"]),
            transport=fake, api_key="k", governor=gov, ledger=ledger,
            task_id="token-budget")
        budget = TokenBudget("execution", max_input_tokens=1,
                             max_output_tokens=8192)
        req = type("Req", (), {"allow_escalation": True,
                                "token_budget": budget})()
        with self.assertRaises(HarnessError):
            driver.run_with_escalation(
                req, RunState(rounds=[], history=[], current_content="x"),
                lambda state, context: "large enough escalation prompt",
                lambda model, content, cost: None)
        self.assertEqual(fake.posts, 0)
        self.assertEqual(budget.snapshot()["open_allowances"], 0)

    def test_escalation_driver_settles_actual_estimated_and_unavailable(self):
        from harness.chat import chat

        class Transport:
            def __init__(self, responses):
                self.responses = list(responses)
                self.posts = 0

            def post(self, *args, **kwargs):
                self.posts += 1
                return self.responses.pop(0)

        for response, source in (
                ((200, {"choices": [{"message": {"content": "answer"}}],
                       "usage": {"prompt_tokens": 12,
                                 "completion_tokens": 7, "cost": 0.0}}),
                 USAGE_ACTUAL),
                ((200, {"choices": [{"message": {"content": "answer"}}],
                       "usage": {"cost": 0.0}}), USAGE_ESTIMATED),
                ((429, {"error": {"message": "busy"}}),
                 USAGE_UNAVAILABLE)):
            budget = TokenBudget("execution", max_input_tokens=100_000,
                                 max_output_tokens=100)
            transport = Transport([response])
            chat(transport, "k", "paid/escalation", [
                {"role": "user", "content": "prompt"}], 20,
                 reasoning_effort="none", token_budget=budget)
            snapshot = budget.snapshot()
            self.assertEqual(snapshot["usage_sources"][source], 1)
            self.assertEqual(snapshot["open_allowances"], 0)
            if source == USAGE_ACTUAL:
                self.assertEqual(snapshot["used_input_tokens"], 12)
                self.assertEqual(snapshot["used_output_tokens"], 7)

    def test_reasoning_retry_reserves_each_http_attempt(self):
        from harness.chat import chat

        class Transport:
            def __init__(self):
                self.responses = [
                    (400, {"error": {"message": "reasoning parameter unsupported"}}),
                    (200, {"choices": [{"message": {"content": "answer"}}],
                           "usage": {"prompt_tokens": 8,
                                     "completion_tokens": 3, "cost": 0.0}}),
                ]
                self.posts = 0

            def post(self, *args, **kwargs):
                self.posts += 1
                return self.responses.pop(0)

        transport = Transport()
        budget = TokenBudget("execution", max_input_tokens=100_000,
                             max_output_tokens=20)
        chat(transport, "k", "deepseek/deepseek-chat",
             [{"role": "user", "content": "prompt"}], 10,
             reasoning_effort="high", token_budget=budget)
        snapshot = budget.snapshot()
        self.assertEqual(transport.posts, 2)
        self.assertEqual(snapshot["usage_sources"][USAGE_UNAVAILABLE], 1)
        self.assertEqual(snapshot["usage_sources"][USAGE_ACTUAL], 1)
        self.assertEqual(snapshot["used_output_tokens"], 13)
        self.assertEqual(snapshot["open_allowances"], 0)

        transport = Transport()
        budget = TokenBudget("execution", max_input_tokens=100_000,
                             max_output_tokens=10)
        with self.assertRaises(HarnessError):
            chat(transport, "k", "deepseek/deepseek-chat",
                 [{"role": "user", "content": "prompt"}], 10,
                 reasoning_effort="high", token_budget=budget)
        self.assertEqual(transport.posts, 1)
        self.assertEqual(budget.snapshot()["open_allowances"], 0)

    def test_jev_candidate_uses_execution_budget_before_evaluator_dispatch(self):
        from harness.jev import JevEvaluationResult
        from harness.jev_policy import JevPolicy

        class Evaluator:
            api_key = "jev-key"
            model = "jev-test"

            def __init__(self):
                self.dispatched = 0

            def verify_diff_mechanics(self, *args, preflight=None, **kwargs):
                preflight()
                self.dispatched += 1
                return JevEvaluationResult(
                    "pass", 0.9, 0.95, {}, [], input_tokens=12,
                    output_tokens=3, input_tokens_observed=True,
                    output_tokens_observed=True)

        class Governor:
            spent = 0.0
            outstanding = 0.0

            def preflight_jev(self, *args, **kwargs):
                pass

            def record_actual(self, *args, **kwargs):
                pass

        evaluator = Evaluator()
        policy = JevPolicy(SimpleNamespace(min_confidence=0.7),
                           evaluator=evaluator, governor=Governor())
        too_small = TokenBudget("execution", max_input_tokens=100,
                                max_output_tokens=20)
        policy.evaluate_diff(
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
            "change", "x", token_budget=too_small)
        self.assertEqual(evaluator.dispatched, 0)
        self.assertEqual(too_small.snapshot()["open_allowances"], 0)

        enough = TokenBudget("execution", max_input_tokens=5000,
                             max_output_tokens=20)
        result, _ = policy.evaluate_diff(
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
            "change", "x", token_budget=enough)
        self.assertEqual(evaluator.dispatched, 1)
        self.assertEqual(result.input_tokens, 12)
        self.assertEqual(enough.snapshot()["used_input_tokens"], 12)
        self.assertEqual(enough.snapshot()["used_output_tokens"], 3)

        class EscalationEvaluator(Evaluator):
            def evaluate(self, *args, **kwargs):
                self.dispatched += 1
                return JevEvaluationResult(
                    "pass", 0.9, 0.95, {}, [], input_tokens=14,
                    output_tokens=2, input_tokens_observed=True,
                    output_tokens_observed=True)

        escalation_evaluator = EscalationEvaluator()
        escalation_policy = JevPolicy(
            SimpleNamespace(min_confidence=0.7),
            evaluator=escalation_evaluator, governor=Governor())
        too_small = TokenBudget("execution", max_input_tokens=100,
                                max_output_tokens=20)
        escalation_policy.evaluate_escalation_decision(
            "context", token_budget=too_small)
        self.assertEqual(escalation_evaluator.dispatched, 0)
        self.assertEqual(too_small.snapshot()["open_allowances"], 0)

        enough = TokenBudget("execution", max_input_tokens=5000,
                             max_output_tokens=20)
        escalation_policy.evaluate_escalation_decision(
            "context", token_budget=enough)
        self.assertEqual(escalation_evaluator.dispatched, 1)
        self.assertEqual(enough.snapshot()["used_input_tokens"], 14)
        self.assertEqual(enough.snapshot()["used_output_tokens"], 2)

    def test_consent_and_diff_authorization_refuse_before_dispatch(self):
        from harness.attest import authorize_diff
        from harness.consent import probe_consent

        class Transport:
            posts = 0

            def post(self, *args, **kwargs):
                self.posts += 1
                return 200, {}

        class Governor:
            def check_byok(self, model):
                pass

            def fetch_pricing(self, models):
                return {model: (0.0, 0.0) for model in models}

            def learned_blocked(self, model):
                return False

            def preflight(self, *args, **kwargs):
                pass

            def assert_no_tools(self, *args, **kwargs):
                pass

        transport = Transport()
        budget = TokenBudget("execution", max_input_tokens=1,
                             max_output_tokens=10000)
        governor = Governor()
        with self.assertRaises(HarnessError):
            probe_consent(
                transport=transport, api_key="k", governor=governor,
                task_id="t", task="approve this", model="paid/model",
                token_budget=budget)
        self.assertEqual(transport.posts, 0)

        with self.assertRaises(HarnessError):
            authorize_diff(
                transport, "k", governor, None, task_id="t",
                model="paid/model", file_path="x", instruction="edit",
                current_content="before", new_content="after", round_no=1,
                token_budget=budget)
        self.assertEqual(transport.posts, 0)
        self.assertEqual(budget.snapshot()["open_allowances"], 0)

    def test_legacy_escalation_refuses_before_dispatch(self):
        budget = TokenBudget("execution", max_input_tokens=1,
                             max_output_tokens=4096)
        fake, _, _, engine = self.make_env(
            posts=[], router_kw={"allow_escalation": True,
                                 "escalation_pool": ["paid/escalation"]},
            renew=False)
        path = self.make_file()
        with self.assertRaises(HarnessError):
            engine._escalate_legacy(SimpleNamespace(
                verify_only=False, verify_cmd="check", allow_escalation=True,
                task_id="token-budget", model="base", file_path=path,
                instruction="make change", edit_snippet=None,
                continuation={}, backend="harness", max_tokens=4096,
                reasoning="high", task_start_spent=0.0,
                task_max_cost=1.0, token_budget=budget),
                RunState(rounds=[{"status": "verify_failed"}],
                         history=[], current_content="before"))
        self.assertEqual(fake.chat_posts(), [])
        self.assertEqual(budget.snapshot()["open_allowances"], 0)


if __name__ == "__main__":
    unittest.main()
