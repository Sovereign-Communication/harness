"""Response extraction: content/cost pulls and the reasoning-only fallback."""
import unittest

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
