"""2026-09-13 model-selection handoff: explicit reasoning disable + budgets.

Hermetic proof of the handoff's core contract (rulings 1, 2, 6):
  * "off"/"none" send an explicit {"effort": "none"} payload -- OMITTING the
    reasoning key means the provider default (reasoning ON) for
    reasoning-native models, the failure that killed the SCMessenger BoD
    runs.
  * No max_tokens cap inside the disable payload.
  * Mandatory-reasoning routes reject the disable with HTTP 400; the
    existing param-rejection retry covers it, with the rejected attempt's
    billable cost merged into the retry response.
  * Reservation slots: a disable is ONE logical call (2 slots) like any
    other reasoning parameter, not 1.
"""
import unittest

from harness.chat import (_build_reasoning_param, _chat_reservation_slots,
                          _effort_to_send, chat, looks_reasoning)
from tests._fake import FakeTransport, comp


class ReasoningDisableTests(unittest.TestCase):
    def test_effort_to_send_disable(self):
        self.assertEqual(_effort_to_send("off", "x-r1"), "none")
        self.assertEqual(_effort_to_send("none", "x-r1"), "none")
        self.assertEqual(_effort_to_send("off", "cohere/x"), "none")

    def test_build_param_disable_has_no_cap(self):
        rp = _build_reasoning_param("deepseek/deepseek-v4.1-flash", "off",
                                    4096, 0.4)
        self.assertEqual(rp, {"effort": "none"})
        self.assertNotIn("max_tokens", rp)

    def test_chat_sends_explicit_disable_payload(self):
        fake = FakeTransport(models=[], posts=[comp("ok")])
        chat(fake, "k", "deepseek/deepseek-v4.1-flash",
             [{"role": "user", "content": "hi"}], 4096, reasoning_effort="off")
        payload = fake.payloads()[0]
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertNotIn("max_tokens", payload["reasoning"])

    def test_chat_none_equals_off(self):
        fake = FakeTransport(models=[], posts=[comp("ok"), comp("ok")])
        chat(fake, "k", "deepseek/deepseek-v4.1-flash",
             [{"role": "user", "content": "hi"}], 4096, reasoning_effort="none")
        self.assertEqual(fake.payloads()[0]["reasoning"], {"effort": "none"})

    def test_non_reasoning_effort_still_omits_key(self):
        fake = FakeTransport(models=[], posts=[comp("ok")])
        chat(fake, "k", "cohere/north-mini-code:free",
             [{"role": "user", "content": "hi"}], 4096, reasoning_effort="auto")
        self.assertNotIn("reasoning", fake.payloads()[0])

    def test_mandatory_reasoning_400_then_retry_without_reasoning(self):
        """GLM-5.3-flash / gpt-5-mini routes: an explicit disable draws the
        documented mandatory-reasoning 400; the retry runs the provider
        default (reasoning on) -- fail-visible, never fabricated success."""
        fake = FakeTransport(
            models=[],
            posts=[(400, {"error": {"message":
                    "Reasoning is mandatory for this endpoint and cannot be "
                    "disabled"}}),
                   comp("ok")])
        status, resp = chat(fake, "k", "z-ai/glm-5.3-flash",
                            [{"role": "user", "content": "hi"}], 4096,
                            reasoning_effort="off")
        self.assertEqual(status, 200)
        posts = fake.chat_posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["reasoning"], {"effort": "none"})
        self.assertNotIn("reasoning", posts[1][2])

    def test_disable_retry_merges_rejected_attempt_cost(self):
        """The rejected 400 attempt can still be metered; dropping its cost
        would let the governor and the ledger disagree about spend."""
        fake = FakeTransport(
            models=[],
            posts=[(400, {"error": {"message": "Reasoning is mandatory"},
                          "usage": {"cost": 0.0002}}),
                   comp("ok", cost=0.0001)])
        _status, resp = chat(fake, "k", "z-ai/glm-5.3-flash",
                             [{"role": "user", "content": "hi"}], 4096,
                             reasoning_effort="off")
        self.assertAlmostEqual(resp["usage"]["cost"], 0.0003, places=9)
        self.assertAlmostEqual(resp["usage"]["retry_cost"], 0.0002, places=9)

    def test_reservation_slots_disable_is_two(self):
        """A disable is a reasoning PARAMETER and may be rejected: its
        preflight reservation is 2 slots, not 1 (the ceiling must cover the
        mandatory-reasoning retry)."""
        self.assertEqual(_chat_reservation_slots("deepseek/deepseek-v4-flash",
                                                 "off"), 2)
        self.assertEqual(_chat_reservation_slots("z-ai/glm-5.3-flash",
                                                 "none"), 2)
        self.assertEqual(_chat_reservation_slots("deepseek/deepseek-v4-flash",
                                                 "auto"), 2)
        self.assertEqual(_chat_reservation_slots("cohere/north", "off"), 2)
        self.assertEqual(_chat_reservation_slots("cohere/north", "auto"), 1)

    def test_glm_5_3_flash_is_a_reasoning_hint(self):
        self.assertTrue(looks_reasoning("z-ai/glm-5.3-flash"))
        self.assertTrue(looks_reasoning("z-ai/glm-5.3"))

    def test_auto_on_reasoning_model_sends_low_with_cap(self):
        fake = FakeTransport(models=[], posts=[comp("ok")])
        chat(fake, "k", "z-ai/glm-5.3-flash",
             [{"role": "user", "content": "hi"}], 8192, reasoning_effort="auto")
        rp = fake.payloads()[0]["reasoning"]
        self.assertEqual(rp["effort"], "low")
        self.assertEqual(rp["max_tokens"], int(8192 * 0.4))


if __name__ == "__main__":
    unittest.main()
