"""Spend-governor policy: per-token math, ceilings, key trust, payload guards."""
import unittest

from harness.errors import HarnessError
from harness.panel import panel_judge
from harness.tokens import estimate_prompt_tokens
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class CostMathTests(unittest.TestCase):
    def test_pricing_is_per_token_not_per_million(self):
        """Regression: OpenRouter pricing fields are per-token dollars. An
        earlier SCMessenger version divided by 1e6 a second time and
        undercounted worst-case cost ~1,000,000x. Costs here must land in the
        ~1e-5..1e-4 range, not ~1e-10."""
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)])
        gov = _gov(fake)
        prompt = " ".join(["word"] * 200)  # ~350 estimated tokens
        calls = [(P1, P1, 300, 0), (P2, P2, 300, 0), ("judge", JUDGE, 350, 700)]
        total, breakdown = gov.preflight(prompt, calls)
        pt = estimate_prompt_tokens(prompt)
        expected = (pt * 1e-8 + 300 * 2e-8) * 2 + (pt + 700) * 1e-8 + 350 * 2e-8
        self.assertAlmostEqual(total, expected, places=12)
        for _, model, cost in breakdown:
            self.assertGreater(cost, 1e-9, "cost is mis-scaled by orders of magnitude")
        self.assertEqual(gov.spent, 0.0, "preflight must not spend anything")

    def test_preflight_refuses_when_over_ceiling(self):
        fake = FakeTransport(models=[m(P1, "0.0001", "0.0002")])
        gov = _gov(fake, max_cost=0.01)
        with self.assertRaises(HarnessError):
            gov.preflight(" ".join(["word"] * 5000), [(P1, P1, 300, 0)])

    def test_unknown_model_refused(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            gov.preflight("hi", [("x", "nope/model", 10, 0)])

    def test_key_must_have_finite_limit(self):
        fake = FakeTransport(key={"label": "sk-test", "limit": None})
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            gov.verify_key()

    def test_expect_key_label_mismatch(self):
        fake = FakeTransport(key={"label": "sk-or-v1-aaaa", "limit": 1.0})
        gov = _gov(fake, expect_key_label="bbbb")
        with self.assertRaises(HarnessError):
            gov.verify_key()

    def test_expect_key_label_match(self):
        """Audit #9b: label expectation is an EXACT match now (substring let a
        similarly-named key through), and the error never echoes labels."""
        fake = FakeTransport(key={"label": "sk-or-v1-aaaa", "limit": 1.0,
                                  "limit_remaining": 0.5})
        gov = _gov(fake, expect_key_label="sk-or-v1-aaaa")
        info = gov.verify_key()
        self.assertEqual(info["label"], "sk-or-v1-aaaa")
        wrong = _gov(FakeTransport(key={"label": "sk-or-v1-bbbb", "limit": 1.0,
                                        "limit_remaining": 0.5}),
                     expect_key_label="sk-or-v1-aaaa")
        with self.assertRaises(HarnessError) as ctx:
            wrong.verify_key()
        self.assertNotIn("bbbb", str(ctx.exception))
        self.assertNotIn("aaaa", str(ctx.exception))


class GuardTests(unittest.TestCase):
    def test_no_tools_key_ever(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a"), comp("b"), comp("verdict")])
        gov = _gov(fake)
        panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                    panel=[P1, P2], judge=JUDGE)
        for payload in fake.payloads():
            self.assertNotIn("tools", payload)

    def test_byok_denied_before_any_post(self):
        fake = FakeTransport(models=[m(P1), m("anthropic/claude-3.5-sonnet")])
        gov = _gov(fake)
        with self.assertRaises(HarnessError) as ctx:
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, "anthropic/claude-3.5-sonnet"], judge=JUDGE)
        self.assertIn("BYOK", str(ctx.exception))
        self.assertEqual(fake.chat_posts(), [], "no chat call may go out")

    def test_mid_batch_fail_closed(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a", cost=0.0009), comp("b", cost=0.0009)])
        gov = _gov(fake, max_cost=0.001)
        with self.assertRaises(HarnessError):
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, P2], judge=JUDGE)
        # judge must never have been called
        self.assertEqual(len(fake.chat_posts()), 2)


if __name__ == "__main__":
    unittest.main()
