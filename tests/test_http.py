"""Transport retries: transient-attempt spend must survive to the bill."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from harness._http import HttpTransport
from harness.events import task_context
from harness.provider_errors import ProviderSpendLimitError


def _ok(body):
    resp = mock.MagicMock()
    resp.getcode.return_value = 200
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.headers = {}
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = resp
    ctx.__exit__.return_value = False
    return ctx


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://openrouter.ai/api/v1/chat/completions", code, "err",
        {"Retry-After": "0"}, io.BytesIO(json.dumps(body).encode("utf-8")))


class RetryCostTests(unittest.TestCase):
    def test_get_spend_limit_429_is_terminal_before_retry(self):
        capped = _http_error(429, {
            "error": {"code": "billing_hard_limit_reached",
                      "message": "billing hard limit reached"},
        })
        with mock.patch("urllib.request.urlopen", side_effect=[capped]) as get, \
             mock.patch("time.sleep") as sleep:
            with self.assertRaises(ProviderSpendLimitError):
                HttpTransport().get("https://openrouter.ai/api/v1/key", "k")
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_spend_limit_429_is_terminal_without_same_key_retry(self):
        capped = _http_error(429, {
            "error": {"code": "insufficient_credits",
                      "message": "Insufficient credits; add more credits"}})
        with mock.patch("urllib.request.urlopen", side_effect=[capped]) as post, \
             mock.patch("time.sleep") as sleep:
            with self.assertRaises(ProviderSpendLimitError) as ctx:
                HttpTransport().post(
                    "https://openrouter.ai/api/v1/chat/completions", "k",
                    {"model": "m"})
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(ctx.exception.http_status, 429)

    def test_spend_limit_preserves_billed_attempts(self):
        capped = _http_error(402, {
            "error": {"code": "payment_required", "message": "add credits"},
            "usage": {"cost": 0.002},
        })
        with mock.patch("urllib.request.urlopen", side_effect=[
                _http_error(429, {"usage": {"cost": 0.001}}), capped]), \
             mock.patch("time.sleep"):
            with self.assertRaises(ProviderSpendLimitError) as ctx:
                HttpTransport().post(
                    "https://openrouter.ai/api/v1/chat/completions", "k",
                    {"model": "m"})
        self.assertAlmostEqual(ctx.exception.known_cost, 0.003)
        self.assertFalse(ctx.exception.cost_accounted)

    def test_sibling_limit_during_retry_backoff_keeps_this_workers_cost(self):
        transient = _http_error(429, {"error": {"message": "try again"},
                                     "usage": {"cost": 0.001}})

        def sibling_exhausts(*_args, **_kwargs):
            # Constructing the sibling's terminal error trips this operation's
            # shared stop latch while this worker is in retry backoff.
            ProviderSpendLimitError("sibling cap", http_status=402)

        with task_context("run-1"), \
             mock.patch("urllib.request.urlopen", side_effect=[transient]) as post, \
             mock.patch.object(HttpTransport, "_retry_wait",
                               side_effect=sibling_exhausts):
            with self.assertRaises(ProviderSpendLimitError) as ctx:
                HttpTransport().post(
                    "https://openrouter.ai/api/v1/chat/completions", "k",
                    {"model": "m"})
        self.assertEqual(post.call_count, 1)
        self.assertAlmostEqual(ctx.exception.known_cost, 0.001)

    def test_spend_limit_preserves_typesafe_input_token_cost(self):
        capped = _http_error(402, {
            "error": {"message": "Payment required"},
            "usage": {"input_tokens": 100},
        })
        with mock.patch("urllib.request.urlopen", side_effect=[capped]):
            with self.assertRaises(ProviderSpendLimitError) as ctx:
                HttpTransport().post(
                    "https://api.typesafe.ai/v1/systemone", "k", {"model": "m"})
        self.assertAlmostEqual(ctx.exception.known_cost, 100 * 0.042 / 1_000_000)

    def test_transient_error_cost_merges_into_success(self):
        billed = {"choices": [{"message": {"content": "ok"},
                               "finish_reason": "stop"}],
                  "usage": {"cost": 0.002}}
        with mock.patch("urllib.request.urlopen",
                         side_effect=[_http_error(429, {"usage": {"cost": 0.001},
                                                       "error": {"message": "slow"}}),
                                      _ok(billed)]), \
             mock.patch("time.sleep"):
            status, resp = HttpTransport().post("https://openrouter.ai/api/v1/chat/completions", "k", {"model": "m"})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(resp["usage"]["cost"], 0.003)
        self.assertAlmostEqual(resp["usage"]["retry_cost"], 0.001)

    def test_terminal_error_carries_dropped_cost(self):
        with mock.patch("urllib.request.urlopen",
                         side_effect=[_http_error(429, {"usage": {"cost": 0.001}}),
                                      _http_error(429, {"usage": {"cost": 0.002}}),
                                      _http_error(500, {"error": {"message": "down"}})]), \
             mock.patch("time.sleep"), \
             mock.patch.object(HttpTransport, "MAX_RETRIES", 2):
            status, resp = HttpTransport().post("https://openrouter.ai/api/v1/chat/completions", "k", {"model": "m"})
        self.assertEqual(status, 500)
        self.assertAlmostEqual(resp["usage"]["cost"], 0.003)
        self.assertAlmostEqual(resp["usage"]["retry_cost"], 0.003)

    def test_clean_success_untouched(self):
        body = {"choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"cost": 0.0}}
        with mock.patch("urllib.request.urlopen", return_value=_ok(body)):
            status, resp = HttpTransport().post("https://openrouter.ai/api/v1/chat/completions", "k", {"model": "m"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["usage"]["cost"], 0.0)
        self.assertNotIn("retry_cost", resp["usage"])

    def test_successful_model_text_mentioning_spend_limit_is_not_classified(self):
        body = {"choices": [{"message": {
                    "content": "A spend limit can be changed under billing."},
                    "finish_reason": "stop"}],
                "usage": {"cost": 0.0}}
        with mock.patch("urllib.request.urlopen", return_value=_ok(body)):
            status, resp = HttpTransport().post(
                "https://openrouter.ai/api/v1/chat/completions", "k", {"model": "m"})
        self.assertEqual(status, 200)
        self.assertIn("spend limit", resp["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
