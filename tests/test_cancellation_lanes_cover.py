"""Hermetic D12 coverage: chat/jev/agent cancellation paths (#198).

Drives the unknown-usage accounting, the Jev evaluator's cancellation
mapping, and the agent hourglass lanes with fake transports and raising
doubles. No network, no operator config, no ambient credentials.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.chat import _merge_retry_cost, _retry_cost_value, chat
from harness.errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from harness.jev import JevEvaluator
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


def _body(content="hi", **usage):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": "stop"}],
            "usage": dict(usage)}


def _paid_gov():
    return SpendGovernor(
        FakeTransport(models=[m("paid/x", "0.000001", "0.000002")]),
        "sk-test")


class _RaisingTransport:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


class TestChatCostFnsCover(unittest.TestCase):
    def test_retry_cost_value_rejects(self):
        self.assertEqual(_retry_cost_value(None), 0.0)
        self.assertEqual(_retry_cost_value({"retry_cost": float("nan")}),
                         0.0)
        self.assertEqual(_retry_cost_value({"retry_cost": -1.0}), 0.0)
        self.assertEqual(_retry_cost_value({"retry_cost": True}), 0.0)
        self.assertEqual(_retry_cost_value({"retry_cost": 0.01}), 0.01)

    def test_merge_retry_cost_drops_bad_cost(self):
        out = _merge_retry_cost({"usage": {"cost": "bad"}}, 1.0)
        self.assertNotIn("cost", out["usage"])
        self.assertEqual(out["usage"]["retry_cost"], 1.0)


class TestChatAccountingCover(unittest.TestCase):
    def _msgs(self):
        return [{"role": "user", "content": "hi"}]

    def test_unpriced_retry_charge_fails_closed_without_governor(self):
        class NoGovTransport:
            def post(self, *args, **kwargs):
                return 200, _body(retry_cost=0.01)

        with self.assertRaises(ProviderUsageUnknown):
            chat(NoGovTransport(), "k", "paid/x", self._msgs(), 64,
                 governor=None)

    def test_unpriced_success_returns_without_governor(self):
        class NoGovTransport:
            def post(self, *args, **kwargs):
                return 200, _body()

        status, resp = chat(NoGovTransport(), "k", "paid/x", self._msgs(),
                            64, governor=None)
        self.assertEqual(status, 200)

    def test_missing_usage_fails_closed_for_paid_model(self):
        class NoUsageTransport:
            def post(self, *args, **kwargs):
                return 200, {"choices": [{"message": {"content": "hi"},
                                          "finish_reason": "stop"}]}

        with self.assertRaisesRegex(ProviderUsageUnknown, "bill blind"):
            chat(NoUsageTransport(), "k", "paid/x", self._msgs(), 64,
                 governor=_paid_gov())

    def test_missing_usage_after_billed_retry_fails_closed(self):
        class RetryTransport:
            def __init__(self):
                self.calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return 400, {
                        "error": {"message": "unsupported parameter: "
                                             "reasoning"},
                        "usage": {"cost": 0.004}}
                return 200, {"choices": [{"message": {"content": "hi"},
                                          "finish_reason": "stop"}]}

        with mock.patch("harness.chat._effort_to_send",
                        return_value="low"), \
                mock.patch("harness.chat.reasoning_param_rejected",
                           return_value=False):
            with self.assertRaises(ProviderUsageUnknown):
                chat(RetryTransport(), "k", "paid/x", self._msgs(), 64,
                     reasoning_effort="low", governor=None)

    def test_os_error_becomes_unknown_usage(self):
        governor = _paid_gov()
        with self.assertRaises(ProviderUsageUnknown) as raised:
            chat(_RaisingTransport(OSError("down")), "k", "paid/x",
                 self._msgs(), 64, governor=governor)
        self.assertTrue(raised.exception.usage_unknown)

    def test_plain_cancellation_skips_accounting_and_reraises(self):
        governor = _paid_gov()
        with self.assertRaises(ToolCancelled):
            chat(_RaisingTransport(ToolCancelled("stop")), "k", "paid/x",
                 self._msgs(), 64, governor=governor)

    def test_retry_unknown_usage_keeps_prior_cost(self):
        class RetryLostTransport:
            def __init__(self):
                self.calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return 400, {
                        "error": {"message": "unsupported parameter: "
                                             "reasoning"},
                        "usage": {"cost": 0.004}}
                raise ProviderUsageUnknown("lost", known_cost=0.001)

        governor = _paid_gov()
        with mock.patch("harness.chat._effort_to_send",
                        return_value="low"), \
                mock.patch("harness.chat.reasoning_param_rejected",
                           return_value=False):
            with self.assertRaises(ProviderUsageUnknown) as raised:
                chat(RetryLostTransport(), "k", "paid/x", self._msgs(), 64,
                     reasoning_effort="low", governor=governor)
        self.assertAlmostEqual(raised.exception.known_cost, 0.005)

    def test_accounting_overrun_and_pricing_fallbacks(self):
        governor = _paid_gov()
        with mock.patch.object(governor, "record_actual",
                               side_effect=HarnessError("ceiling")), \
                mock.patch.object(governor, "fetch_pricing",
                                  side_effect=RuntimeError("no pricing")), \
                mock.patch.object(governor, "remaining",
                                  return_value=0.02):
            with self.assertRaises(ProviderUsageUnknown):
                chat(_RaisingTransport(
                    ProviderUsageUnknown("lost", known_cost=0.01)),
                    "k", "paid/x", self._msgs(), 64, governor=governor)
        self.assertGreater(governor.snapshot().get("unknown_liability",
                                                   0.0), 0.0)

    def test_accounting_budget_fallback(self):
        governor = _paid_gov()
        with mock.patch.object(governor, "record_actual",
                               side_effect=HarnessError("ceiling")), \
                mock.patch.object(governor, "fetch_pricing",
                                  side_effect=RuntimeError("no pricing")), \
                mock.patch.object(governor, "remaining",
                                  side_effect=RuntimeError("no budget")):
            with self.assertRaises(ProviderUsageUnknown):
                chat(_RaisingTransport(
                    ProviderUsageUnknown("lost", known_cost=0.01)),
                    "k", "paid/x", self._msgs(), 64, governor=governor)
        self.assertGreater(governor.snapshot().get("unknown_liability",
                                                   0.0), 0.0)


class TestJevCover(unittest.TestCase):
    def _evaluator(self, transport):
        return JevEvaluator(api_key="key", transport=transport)

    def test_fallback_unparseable_reported_cost(self):
        fake = FakeTransport(posts=[(500, {"usage": {"cost": "bad",
                                                     "input_tokens": 50}})])
        result = self._evaluator(fake).evaluate(
            {"code": "x = 1"},
            {"q": {"type": "noul", "instructions": "Is x valid?"}})
        self.assertTrue(result.is_fallback)

    def test_fallback_bad_token_counts(self):
        fake = FakeTransport(posts=[(500, {"usage": {"cost": "bad",
                                                     "input_tokens": "x"}})])
        result = self._evaluator(fake).evaluate(
            {"code": "x = 1"},
            {"q": {"type": "noul", "instructions": "Is x valid?"}})
        self.assertTrue(result.is_fallback)

    def test_fallback_reported_cost_caps_retry(self):
        fake = FakeTransport(posts=[(500, {"usage": {"cost": 0.002,
                                                     "input_tokens": 10}})])
        result = self._evaluator(fake).evaluate(
            {"code": "x = 1"},
            {"q": {"type": "noul", "instructions": "Is x valid?"}})
        self.assertTrue(result.is_fallback)
        self.assertGreaterEqual(result.cost, 0.002)

    def test_evaluate_maps_unknown_usage(self):
        fake = FakeTransport()
        questions = {"q": {"type": "noul", "instructions": "Is x?"}}
        with mock.patch.object(fake, "post",
                               side_effect=ProviderUsageUnknown("lost")):
            with self.assertRaises(ProviderUsageUnknown):
                self._evaluator(fake).evaluate({"code": "x = 1"},
                                                questions)

    def test_evaluate_maps_cancellation(self):
        fake = FakeTransport()
        questions = {"q": {"type": "noul", "instructions": "Is x?"}}
        with mock.patch.object(fake, "post",
                               side_effect=ToolCancelled("stop")):
            with self.assertRaises(ToolCancelled):
                self._evaluator(fake).evaluate({"code": "x = 1"},
                                                questions)

    def test_evaluate_once_maps_unknown_usage(self):
        questions = {"q": {"type": "noul", "instructions": "Is x?"}}

        class OneShotBoom:
            def post(self, *args, **kwargs):
                raise ProviderUsageUnknown("lost")

            def post_once(self, *args, **kwargs):
                raise ProviderUsageUnknown("lost")

        with self.assertRaises(ProviderUsageUnknown):
            self._evaluator(OneShotBoom()).evaluate_once(
                {"code": "x = 1"}, questions)

    def test_evaluate_once_maps_cancellation(self):
        questions = {"q": {"type": "noul", "instructions": "Is x?"}}

        class OneShotBoom:
            def post(self, *args, **kwargs):
                raise ToolCancelled("stop")

            def post_once(self, *args, **kwargs):
                raise ToolCancelled("stop")

        with self.assertRaises(ToolCancelled):
            self._evaluator(OneShotBoom()).evaluate_once(
                {"code": "x = 1"}, questions)

    def test_retry_cost_rejects_bad_value(self):
        evaluator = JevEvaluator()
        self.assertEqual(evaluator._retry_cost({"retry_cost": "bad"}), 0.0)

    def test_parse_rejects_bad_retry_cost(self):
        resp = {"answers": {"supported": {"noul": 0.9}},
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "retry_cost": "bad"}}
        with self.assertRaisesRegex(ValueError, "retry_cost"):
            JevEvaluator()._parse_jev_response(
                resp, {"supported": {"type": "noul"}})


class TestAgentCover(unittest.TestCase):
    def _agent(self, tmp):
        from harness.agent import AutonomousAgent
        agent = AutonomousAgent(history_dir=Path(tmp))
        agent.root_dir = Path(tmp)
        return agent

    def test_hourglass_context_cancellation_propagates(self):
        from harness.agent import AutonomousAgent  # noqa: F401

        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with mock.patch.object(agent, "_gather_web_context",
                                   side_effect=ToolCancelled("stop")):
                with self.assertRaises(ToolCancelled):
                    agent._hourglass_request_context("What is 2+2?",
                                                     web=True)

    def _hourglass_mocks(self, agent, completion):
        gov = mock.MagicMock()
        policy = mock.MagicMock()
        policy.evaluate_answer.return_value = (
            mock.MagicMock(),
            {"cost": 0.0, "iteration_required": True})
        brief = mock.MagicMock()
        brief.estimated_tokens = 10
        patches = [
            mock.patch.object(agent, "_hourglass_request_context",
                              return_value=("ctx", {}, brief, [])),
            mock.patch("harness.agent.governor_for",
                       return_value=("k", gov)),
            mock.patch("harness.agent.policy_for", return_value=policy),
            mock.patch.object(agent, "_hourglass_answer_once",
                              return_value={"answer": "Paris", "model": "m",
                                            "cost": 0.0}),
            mock.patch.object(agent, "_jev_answer_envelope",
                              return_value={"native": True,
                                            "confidence": {
                                                "passed": False,
                                                "observed": 0.9},
                                            "reasons": []}),
            mock.patch("harness.agent.assess_completion",
                       return_value=completion),
        ]
        return patches

    def test_completion_cancellation_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            patches = self._hourglass_mocks(agent, None)
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            with mock.patch("harness.agent.assess_completion",
                           side_effect=ToolCancelled("stop")):
                with self.assertRaises(ToolCancelled):
                    agent.run_hourglass_request("What is the capital?",
                                                max_rounds=1)

    def test_escalation_cancellation_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            patches = self._hourglass_mocks(agent, {"complete": False})
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            with mock.patch.object(agent, "_escalation_authorized",
                                   return_value=True), \
                    mock.patch.object(agent, "_handle_edit",
                                      side_effect=ToolCancelled("stop")):
                with self.assertRaises(ToolCancelled):
                    agent.run_hourglass_request("What is the capital?",
                                                max_rounds=1)

    def test_edit_second_plan_cancellation_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            refused = {"status": "refused",
                       "confirmation": {
                           "reason": "validated restart to planning x"}}
            plan_eval = mock.MagicMock()
            plan_eval.answers = {}
            plan_policy = mock.MagicMock()
            plan_policy.evaluate_plan.return_value = (plan_eval, None)
            with mock.patch.object(agent, "_triage_scope",
                                   return_value=["a.py"]), \
                    mock.patch("harness.agent.distill_context") as distill, \
                    mock.patch("harness.agent.governor_for",
                               return_value=("k", mock.MagicMock())), \
                    mock.patch("harness.agent.policy_for",
                               return_value=plan_policy), \
                    mock.patch("harness.agent.budget_from_settings",
                               return_value=None), \
                    mock.patch.object(agent, "_plan_round",
                                      side_effect=[refused,
                                                   ToolCancelled("stop")]):
                distill.return_value.ledger_fields.return_value = {}
                with self.assertRaises(ToolCancelled):
                    agent._handle_edit("Refactor the login helper", "sid",
                                       False)
