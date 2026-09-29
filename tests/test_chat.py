"""Response extraction: content/cost pulls and the reasoning-only fallback."""
import unittest

from harness.chat import (_extract_json, _reported_cost, chat, extract_content_and_cost,
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

    def test_null_reported_cost_is_unknown_not_zero(self):
        fake = FakeTransport(models=[m("paid/x", "0.000001", "0.000002")],
                             posts=[_resp(cost=None, prompt_tokens=10,
                                          completion_tokens=5)])
        gov = self._gov(fake)
        with self.assertRaisesRegex(HarnessError, "invalid billed cost"):
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}],
                 64, governor=gov)
        self.assertTrue(gov._settlement_unknown)
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_reported_non_finite_cost_is_unknown(self):
        self.assertIsNone(_reported_cost({"usage": {"cost": float("inf")}}))

    def test_boolean_reported_cost_is_unknown(self):
        self.assertIsNone(_reported_cost({"usage": {"cost": True}}))
        self.assertIsNone(_reported_cost({"usage": {"cost": False}}))

    def test_error_object_cost_is_reported_when_usage_cost_is_missing(self):
        self.assertEqual(
            _reported_cost({"error": {"message": "metered retry", "cost": 0.002}}),
            0.002)

    def test_non_mapping_response_fails_closed_and_closes_dispatch(self):
        fake = FakeTransport(models=[m("paid/x")], posts=[(200, None)])
        gov = self._gov(fake)
        with self.assertRaisesRegex(HarnessError, "no usage envelope"):
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}],
                 64, governor=gov)
        self.assertTrue(gov._settlement_unknown)
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_non_200_explicit_cost_is_preserved_for_settlement(self):
        for cost in (0.0, 0.004):
            with self.subTest(cost=cost):
                fake = FakeTransport(
                    models=[m("paid/x")],
                    posts=[(500, {"error": {"message": "provider error"},
                                  "usage": {"cost": cost}})])
                gov = self._gov(fake)
                status, resp = chat(
                    fake, "k", "paid/x", [{"role": "user", "content": "hi"}],
                    64, governor=gov)
                self.assertEqual(status, 500)
                self.assertEqual(_reported_cost(resp), cost)
                gov.record_actual(_reported_cost(resp), "paid/x")
                self.assertAlmostEqual(gov.spent, cost)
                self.assertEqual(gov._dispatches_in_progress, 0)
                self.assertFalse(gov._settlement_unknown)

    def test_non_200_error_cost_is_preserved_for_settlement(self):
        fake = FakeTransport(
            models=[m("paid/x")],
            posts=[(429, {"error": {"message": "metered throttle", "cost": 0.002}})])
        gov = self._gov(fake)
        status, resp = chat(
            fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64,
            governor=gov)
        self.assertEqual(status, 429)
        self.assertEqual(resp["usage"]["cost"], 0.002)
        gov.record_actual(_reported_cost(resp), "paid/x")
        self.assertEqual(gov.spent, 0.002)
        self.assertFalse(gov._settlement_unknown)
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_malformed_byok_marker_fails_closed(self):
        fake = FakeTransport(
            models=[m("paid/x")],
            posts=[_resp(cost="omit", prompt_tokens=10, is_byok="false")])
        gov = self._gov(fake)
        with self.assertRaisesRegex(HarnessError, "invalid usage.is_byok"):
            chat(fake, "k", "paid/x", [{"role": "user", "content": "hi"}], 64,
                 governor=gov)
        self.assertTrue(gov._settlement_unknown)
        self.assertEqual(gov._dispatches_in_progress, 0)
        with self.assertRaisesRegex(HarnessError, "unaccounted cost"):
            gov.preflight("hi", [("next", "paid/x", 64, 0)])

    def test_extract_rejects_malformed_byok_marker(self):
        with self.assertRaisesRegex(HarnessError, "invalid usage.is_byok"):
            extract_content_and_cost({"usage": {"is_byok": "false"}})

    def test_non_200_missing_or_invalid_cost_fails_closed(self):
        invalid_cases = (
            ("missing", {}),
            ("none", {"cost": None}),
            ("boolean", {"cost": False}),
            ("empty", {"cost": ""}),
            ("malformed", {"cost": "not-a-cost"}),
            ("non-finite", {"cost": float("inf")}),
        )
        for name, usage in invalid_cases:
            with self.subTest(name=name):
                fake = FakeTransport(
                    models=[m("paid/x")],
                    posts=[(500, {"error": {"message": "provider error"},
                                  "usage": usage})])
                gov = self._gov(fake)
                with self.assertRaises(HarnessError):
                    chat(fake, "k", "paid/x",
                         [{"role": "user", "content": "hi"}], 64,
                         governor=gov)
                self.assertEqual(gov.spent, 0.0)
                self.assertTrue(gov._settlement_unknown)
                self.assertEqual(gov._dispatches_in_progress, 0)


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

    def test_reasoning_retry_is_included_in_governed_text_preflight(self):
        model = "z-ai/glm-5.3-flash"
        fake = FakeTransport(
            models=[m(model)],
            posts=[(400, {"error": {"message": "Reasoning is mandatory"},
                          "usage": {"cost": 0.0002}}),
                   comp("plan", cost=0.0001)])
        gov = SpendGovernor(fake, "sk-test")
        planned = []
        original_preflight = gov.preflight

        def record_preflight(prompt, calls):
            planned.extend(calls)
            return original_preflight(prompt, calls)

        gov.preflight = record_preflight
        content, cost = governed_text(
            fake, "k", gov, model, "p", 64, reasoning_effort="auto")
        self.assertEqual(content, "plan")
        self.assertAlmostEqual(cost, 0.0003)
        self.assertEqual(len(planned), 2)

    def test_requires_governor(self):
        with self.assertRaises(HarnessError):
            governed_text(self.fake, "k", None, "cheap/x", "p", 64)

    def test_http_failure_fails_closed(self):
        self.fake.posts = [(500, {"error": {"message": "boom"},
                                 "usage": {"cost": 0.0}})]
        with self.assertRaises(HarnessError) as ctx:
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertFalse(self.gov._settlement_unknown)
        self.assertEqual(self.gov._dispatches_in_progress, 0)

    def test_http_failure_settles_reported_cost_before_error(self):
        self.fake.posts = [(500, {"error": {"message": "boom"},
                                 "usage": {"cost": 0.002}})]
        with self.assertRaisesRegex(HarnessError, "HTTP 500"):
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertAlmostEqual(self.gov.spent, 0.002)
        self.assertEqual(self.gov._dispatches_in_progress, 0)

    def test_http_failure_byok_is_learned_without_local_charge(self):
        for cost in ("omit", 0.002):
            with self.subTest(cost=cost):
                self.gov = SpendGovernor(self.fake, "sk-test")
                error = {"error": {"message": "boom"},
                         "usage": {"is_byok": True}}
                if cost != "omit":
                    error["usage"]["cost"] = cost
                self.fake.posts = [(500, error)]
                with self.assertRaisesRegex(HarnessError, "HTTP 500"):
                    governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
                self.assertEqual(self.gov.spent, 0.0)
                self.assertTrue(self.gov.learned_blocked("cheap/x"))
                self.assertEqual(self.gov._dispatches_in_progress, 0)

    def test_byok_response_refused_and_recorded(self):
        body = comp("hello")
        body["usage"]["is_byok"] = True
        self.fake.posts = [body]
        with self.assertRaises(HarnessError) as ctx:
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertIn("BYOK", str(ctx.exception))

    def test_byok_response_without_cost_is_refused_and_recorded(self):
        self.fake.posts = [_resp(is_byok=True)]
        with self.assertRaisesRegex(HarnessError, "BYOK"):
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)
        self.assertEqual(self.gov.spent, 0.0)
        self.assertTrue(self.gov.learned_blocked("cheap/x"))
        self.assertEqual(self.gov._dispatches_in_progress, 0)

    def test_empty_body_refused(self):
        self.fake.posts = [comp("  ")]
        with self.assertRaises(HarnessError):
            governed_text(self.fake, "k", self.gov, "cheap/x", "p", 64)


if __name__ == "__main__":
    unittest.main()
