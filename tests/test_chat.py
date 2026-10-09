"""Response extraction: content/cost pulls and the reasoning-only fallback."""
import unittest
from unittest import mock

from harness.chat import (_extract_json, chat, extract_content_and_cost,
                          governed_text)
from harness.errors import HarnessError
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


if __name__ == "__main__":
    unittest.main()
