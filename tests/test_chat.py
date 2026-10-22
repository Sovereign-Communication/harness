"""Response extraction: content/cost pulls and the reasoning-only fallback."""
import unittest
from unittest import mock

from harness.chat import (_extract_json, chat, extract_content_and_cost,
                          governed_completion, governed_text)
from harness.errors import HarnessError
from harness.provider_errors import ProviderSpendLimitError
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, comp, m


class ExtractionTests(unittest.TestCase):
    def test_reasoning_fallback(self):
        content, finish, cost, is_byok = extract_content_and_cost(
            comp(None, reasoning="deep thinking here"))
        self.assertIn("[NOTE]", content)
        self.assertIn("deep thinking", content)

    def test_extraction_garbage(self):
        content, finish, cost, is_byok = extract_content_and_cost({})
        self.assertIsNone(content)
        self.assertIsNone(finish)

    def test_extract_json_skips_unparseable_think_wrapped_object(self):
        """DF-LING-1: Ling-family judges wrap the verdict in a think/prose
        preamble -- ``<think>{draft}</think>{"verdict":"allow"}`` -- whose
        first ``{...}`` span is not valid JSON. The extractor must move on
        to the next ``{`` candidate instead of giving up."""
        result = _extract_json('<think>{draft}</think>{"verdict": "allow"}')
        self.assertEqual(result, {"verdict": "allow"})


def _resp(*, cost="omit", prompt_tokens=0, completion_tokens=0,
          content="ok", is_byok=False):
    usage = {"prompt_tokens": prompt_tokens,
             "completion_tokens": completion_tokens, "is_byok": is_byok}
    if cost != "omit":
        usage["cost"] = cost
    return {"choices": [{"message": {"content": content},
                         "finish_reason": "stop"}],
            "usage": usage}


class CostAccountingTests(unittest.TestCase):
    """A missing usage.cost must never bill a paid call as $0: free fills
    zero, paid estimates from token counts, blind fails closed."""

    def _gov(self, fake):
        return SpendGovernor(fake, "sk-test")

    def test_known_billed_overrun_is_booked_and_preserved_for_simple_answer(self):
        gov = mock.Mock()
        gov.check_byok.return_value = None
        gov.preflight.return_value = None
        gov.record_actual.side_effect = HarnessError(
            "actual running cost $0.002000 would exceed ceiling $0.001000")
        with mock.patch("harness.chat.chat", return_value=(200, _resp(cost=0.002))):
            completion = governed_completion(
                mock.Mock(), "k", gov, "paid/x", "question", 128,
                preserve_billed_overrun=True)
        gov.record_overrun.assert_called_once_with(0.002, "paid/x")
        self.assertEqual(completion["content"], "ok")
        self.assertEqual(completion["cost"], 0.002)
        self.assertIn("exceed ceiling", completion["budget_overrun"])

    def test_known_billed_overrun_exception_keeps_cost_for_other_callers(self):
        gov = mock.Mock()
        gov.check_byok.return_value = None
        gov.preflight.return_value = None
        gov.record_actual.side_effect = HarnessError("over ceiling")
        with mock.patch("harness.chat.chat", return_value=(200, _resp(cost=0.002))):
            with self.assertRaises(HarnessError) as ctx:
                governed_completion(mock.Mock(), "k", gov, "paid/x", "q", 128)
        gov.record_overrun.assert_called_once_with(0.002, "paid/x")
        self.assertEqual(ctx.exception.known_cost, 0.002)

    def test_reasoning_retry_keeps_prior_spend_when_final_usage_is_malformed(self):
        final_bodies = ({"choices": [{"message": {"content": "answer"}}],
                         "usage": None},
                        {"choices": [{"message": {"content": "answer"}}],
                         "usage": []},
                        {"choices": [{"message": {"content": "answer"}}],
                         "usage": {}},
                        {"choices": [{"message": {"content": "answer"}}]},
                        [])
        for final_body in final_bodies:
            with self.subTest(final_body=final_body):
                class RetryTransport:
                    def __init__(self):
                        self.calls = 0

                    def post(self, *_args, **_kwargs):
                        self.calls += 1
                        if self.calls == 1:
                            return 400, {
                                "error": {"message":
                                          "unsupported parameter: reasoning"},
                                "usage": {"cost": 0.004},
                            }
                        return 200, final_body

                transport = RetryTransport()
                governor = self._gov(FakeTransport(
                    models=[m("test/model", "0.000001", "0.000001")]))
                with mock.patch("harness.chat._effort_to_send",
                                return_value="low"), \
                        mock.patch("harness.chat.reasoning_param_rejected",
                                   return_value=False):
                    with self.assertRaisesRegex(
                            HarnessError, "omitted final usage|omitted usage|no usable usage"):
                        chat(transport, "test-key", "test/model",
                             [{"role": "user", "content": "hi"}], 64,
                             reasoning_effort="low", governor=governor)

                self.assertEqual(transport.calls, 2)
                self.assertAlmostEqual(governor.spent, 0.004)
                self.assertGreater(governor.snapshot()["unknown_liability"], 0.0)

    def test_transport_retry_then_unpriced_success_fails_closed(self):
        from harness._http import HttpTransport
        import json

        final_bodies = ({"choices": [{"message": {"content": "answer"}}],
                         "usage": None},
                        {"choices": [{"message": {"content": "answer"}}],
                         "usage": []},
                        {"choices": [{"message": {"content": "answer"}}],
                         "usage": {}},
                        {"choices": [{"message": {"content": "answer"}}]},
                        [])
        for final_body in final_bodies:
            with self.subTest(final_body=final_body):
                wire = HttpTransport(cancel_check=lambda: False)
                bodies = iter((
                    (429, json.dumps({"usage": {"cost": 0.004}}), None),
                    (200, json.dumps(final_body), None),
                ))
                fake = FakeTransport(models=[m("test/model", "0.000001", "0.000001")])
                governor = self._gov(fake)
                with mock.patch.object(
                        wire, "_request_once_cancellable",
                        side_effect=lambda *_args, **_kwargs: next(bodies)), \
                        mock.patch.object(wire, "_retry_wait"):
                    with self.assertRaisesRegex(
                            HarnessError, "omitted final usage|omitted usage|no usable usage"):
                        chat(wire, "test-key", "test/model",
                             [{"role": "user", "content": "hi"}], 64,
                             governor=governor)
                self.assertAlmostEqual(governor.spent, 0.004)
                self.assertGreater(governor.snapshot()["unknown_liability"], 0.0)

    def test_reported_cost_passes_through_untouched(self):
        fake = FakeTransport(models=[m("paid/x", "0.000001", "0.000002")],
                             posts=[_resp(cost=0.004)])
        gov = self._gov(fake)
        status, resp = chat(fake, "k", "paid/x",
                            [{"role": "user", "content": "hi"}], 64,
                            governor=gov)
        self.assertEqual(status, 200)
        self.assertEqual(resp["usage"]["cost"], 0.004)
        self.assertNotIn("cost_estimated", resp["usage"])

    def test_mock_governor_without_assert_no_tools(self):
        class _BareGovernor:
            def check_byok(self, model):
                pass
        fake = FakeTransport(models=[m("free/x", "0", "0")],
                             posts=[_resp(cost=0.0)])
        status, resp = chat(fake, "k", "free/x",
                            [{"role": "user", "content": "hi"}], 64,
                            governor=_BareGovernor())
        self.assertEqual(status, 200)

    def test_custom_transport_spend_limit_is_typed_and_terminal(self):
        fake = FakeTransport(posts=[
            (402, {"error": {"message": "Payment required"}}),
            _resp(cost=0.0),
        ])
        with self.assertRaises(ProviderSpendLimitError):
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64)
        self.assertEqual(len(fake.chat_posts()), 1)

    def test_spend_limit_accounts_cost_from_attempts_before_stopping(self):
        fake = FakeTransport(posts=[
            (400, {"error": {"message": "reasoning parameter unsupported"},
                   "usage": {"cost": 0.001}}),
            (402, {"error": {"code": "payment_required"},
                   "usage": {"cost": 0.002}}),
        ])
        gov = self._gov(fake)
        with self.assertRaises(ProviderSpendLimitError) as ctx:
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64,
                 reasoning_effort="high", governor=gov)
        self.assertEqual(len(fake.chat_posts()), 2)
        self.assertAlmostEqual(ctx.exception.known_cost, 0.003)
        self.assertAlmostEqual(gov.spent, 0.003)

    def test_spend_limit_known_usage_is_persisted_for_cost_reports(self):
        import os
        import tempfile
        from types import SimpleNamespace
        from harness.ledger import AutonomyLedger

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(
                ledger_path=os.path.join(temp_dir, "ledger.jsonl"))
            fake = FakeTransport(posts=[(
                402, {"error": {"code": "payment_required"},
                      "usage": {"cost": 0.003}},
            )])
            with mock.patch("harness.config.load_settings",
                            return_value=settings):
                with self.assertRaises(ProviderSpendLimitError):
                    chat(fake, "k", "paid/x",
                         [{"role": "user", "content": "hi"}], 64)

            ledger = AutonomyLedger(settings.ledger_path)
            receipts = [row for row in ledger.entries()
                        if row.get("event_note") == "provider_spend_limit"]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["event"], "model_result")
            self.assertEqual(receipts[0]["error_kind"],
                             "provider_spend_limit")
            self.assertAlmostEqual(receipts[0]["billable_cost"], 0.003)
            self.assertAlmostEqual(ledger.cost_report()["total_cost"], 0.003)

    def test_custom_transport_ordinary_429_remains_recoverable(self):
        body = {"error": {"message": "Rate limit exceeded: try later"}}
        fake = FakeTransport(posts=[(429, body)])
        status, response = chat(
            fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64)
        self.assertEqual((status, response), (429, body))

    def test_successful_answer_mentioning_billing_is_not_spend_exhaustion(self):
        content = "If you hit a spend limit, add credits in billing settings."
        fake = FakeTransport(posts=[_resp(content=content, cost=0.0)])
        status, response = chat(
            fake, "k", "free/x", [{"role": "user", "content": "Explain billing"}], 64)
        self.assertEqual(status, 200)
        self.assertEqual(response["choices"][0]["message"]["content"], content)

    def test_nested_provider_metadata_spend_limit_is_terminal(self):
        fake = FakeTransport(posts=[(429, {
            "error": {"message": "Provider returned error", "code": 429,
                      "metadata": {"raw": "Account has insufficient balance"}},
        })])
        with self.assertRaises(ProviderSpendLimitError):
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64)

    def test_spend_limit_error_does_not_echo_provider_credentials(self):
        secret = "sk-or-v1-sensitive-bearer-value"
        fake = FakeTransport(posts=[(402, {
            "error": {"message": f"Insufficient credits for Bearer {secret}"},
        })])
        with self.assertRaises(ProviderSpendLimitError) as ctx:
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64)
        rendered = str(ctx.exception)
        self.assertIn("spend limit exhausted", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("Bearer", rendered)

    def test_missing_cost_on_free_model_fills_zero(self):
        fake = FakeTransport(models=[m("free/x", "0", "0")],
                             posts=[_resp(prompt_tokens=10,
                                          completion_tokens=5)])
        gov = self._gov(fake)
        status, resp = chat(fake, "k", "free/x",
                            [{"role": "user", "content": "hi"}], 64,
                            governor=gov)
        self.assertEqual(resp["usage"]["cost"], 0.0)

    def test_missing_cost_on_paid_model_estimates_from_tokens(self):
        fake = FakeTransport(models=[m("paid/x", "0.000001", "0.000002")],
                             posts=[_resp(prompt_tokens=1000,
                                          completion_tokens=500)])
        gov = self._gov(fake)
        status, resp = chat(fake, "k", "paid/x",
                            [{"role": "user", "content": "hi"}], 64,
                            governor=gov)
        self.assertAlmostEqual(resp["usage"]["cost"], 0.002)
        self.assertTrue(resp["usage"]["cost_estimated"])

    def test_missing_final_cost_adds_known_transport_retry_charge(self):
        from harness._http import HttpTransport
        import json

        wire = HttpTransport(cancel_check=lambda: False)
        final = _resp(prompt_tokens=1000, completion_tokens=500)
        bodies = iter((
            (429, json.dumps({"usage": {"cost": 0.001}}), None),
            (200, json.dumps(final), None),
        ))
        with mock.patch.object(
                wire, "_request_once_cancellable",
                side_effect=lambda *_a, **_k: next(bodies)), \
                mock.patch.object(wire, "_retry_wait"):
            fake = FakeTransport(models=[m("paid/x", "0.000001", "0.000002")])
            gov = self._gov(fake)
            status, resp = chat(
                wire, "k", "paid/x", [{"role": "user", "content": "hi"}], 64,
                governor=gov)
        self.assertEqual(status, 200)
        self.assertAlmostEqual(resp["usage"]["cost"], 0.003)

    def test_blind_paid_response_fails_closed(self):
        fake = FakeTransport(models=[m("paid/x", "0.000001", "0.000002")],
                             posts=[_resp()])
        gov = self._gov(fake)
        with self.assertRaisesRegex(HarnessError, "omitted usage accounting"):
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}],
                 64, governor=gov)


class GovernedTextTests(unittest.TestCase):
    """The single-shot governed call: preflight, billing, and fail-closed
    error paths (HTTP failure, BYOK routing, empty body, no governor)."""

    def setUp(self):
        self.fake = FakeTransport(models=[m("cheap/x")])
        self.gov = SpendGovernor(self.fake, "sk-test")

    def test_returns_content_and_bills(self):
        self.fake.posts = [comp("hello plan", cost=0.002)]
        content, cost = governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertEqual(content, "hello plan")
        self.assertEqual(cost, 0.002)
        self.assertEqual(self.gov.spent, 0.002)

    def test_requires_governor(self):
        with self.assertRaises(HarnessError):
            governed_text(self.fake, "k", None, "cheap/x", "p", 64)

    def test_http_failure_fails_closed(self):
        self.fake.posts = [(500, {"error": {"message": "boom"}})]
        with self.assertRaises(HarnessError) as ctx:
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_byok_response_refused_and_recorded(self):
        body = comp("hello")
        body["usage"]["is_byok"] = True
        self.fake.posts = [body]
        with self.assertRaises(HarnessError) as ctx:
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertIn("BYOK", str(ctx.exception))

    def test_empty_body_refused(self):
        self.fake.posts = [comp("  ")]
        with self.assertRaises(HarnessError):
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)

    def test_reasoning_param_rejected_persistence(self):
        import tempfile
        import os
        from harness.chat import (note_reasoning_param_rejection,
                                  reasoning_param_rejected,
                                  reset_reasoning_param_memory,
                                  _save_reasoning_param_rejected,
                                  _load_reasoning_param_rejected)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "sub", "rejected.json")
            with mock.patch("harness.chat.REASONING_PARAM_REJECTED_PATH", path):
                reset_reasoning_param_memory(clear_disk=True)
                self.assertFalse(reasoning_param_rejected("model/alpha"))
                note_reasoning_param_rejection("model/alpha")
                _save_reasoning_param_rejected()
                self.assertTrue(reasoning_param_rejected("model/alpha"))
                loaded = _load_reasoning_param_rejected()
                self.assertIn("model/alpha", loaded)
                reset_reasoning_param_memory(clear_disk=True)
                self.assertFalse(reasoning_param_rejected("model/alpha"))
                self.assertFalse(os.path.exists(path))
            with mock.patch("builtins.open", side_effect=OSError("disk error")):
                _save_reasoning_param_rejected()
                with mock.patch("os.path.exists", return_value=True):
                    self.assertIsInstance(_load_reasoning_param_rejected(), set)
            with mock.patch("os.remove", side_effect=OSError("remove error")):
                with mock.patch("os.path.exists", return_value=True):
                    reset_reasoning_param_memory(clear_disk=True)



if __name__ == "__main__":
    unittest.main()
