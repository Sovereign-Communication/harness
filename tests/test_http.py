"""Transport retries: transient-attempt spend must survive to the bill."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from harness._http import HttpTransport


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


if __name__ == "__main__":
    unittest.main()
