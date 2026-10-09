"""Response extraction: content/cost pulls and the reasoning-only fallback."""
import unittest
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from harness.chat import (_extract_json, chat, extract_content_and_cost,
                          governed_text)
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
            receipts = [row for row in ledger._tail
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


if __name__ == "__main__":
    unittest.main()
