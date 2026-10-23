"""D9: stop paying for the same doomed request twice.

The dogfood logged 18 reasoning-parameter rejections in one session. The
retry was always correct -- the provider said "unknown parameter", we dropped
the key and re-asked -- but nothing remembered the outcome, so every later
call to that model paid for the doomed attempt again.

What is pinned here:

* the first call still discovers the rejection by asking, because the
  rejection is learned from the provider's own text and never from a provider
  brand list;
* the second call to that model does not;
* the memory is keyed on the canonical model, so a variant suffix does not
  buy a free pass;
* forgetting is possible, and a 200 that mentions nothing is never learned as
  a rejection.
"""
import unittest

from harness import chat as chatmod


class ScriptedTransport:
    """Rejects the reasoning parameter, then answers."""

    def __init__(self, reject_first=True, message="Unknown parameter: reasoning"):
        self.reject_first = reject_first
        self.message = message
        self.calls = []

    def post(self, url, api_key, payload):
        self.calls.append(payload)
        if self.reject_first and "reasoning" in payload:
            return 400, {"error": {"message": self.message}}
        return 200, {"choices": [{"message": {"content": "ok"}}],
                     "usage": {"cost": 0.0}}


class ReasoningParamMemoryTests(unittest.TestCase):
    def setUp(self):
        chatmod.reset_reasoning_param_memory()
        self.addCleanup(chatmod.reset_reasoning_param_memory)

    def _chat(self, transport, model="deepseek/deepseek-v4.1-flash",
              effort="auto"):
        return chatmod.chat(transport, "k", model,
                            [{"role": "user", "content": "hi"}], 64,
                            reasoning_effort=effort)

    def test_the_first_call_asks_with_reasoning_and_retries(self):
        transport = ScriptedTransport()
        status, _resp = self._chat(transport)
        self.assertEqual(status, 200)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("reasoning", transport.calls[0])
        self.assertNotIn("reasoning", transport.calls[1])

    def test_the_second_call_skips_the_doomed_attempt(self):
        first = ScriptedTransport()
        self._chat(first)
        self.assertTrue(chatmod.reasoning_param_rejected(
            "deepseek/deepseek-v4.1-flash"))

        second = ScriptedTransport()
        self._chat(second)
        self.assertEqual(len(second.calls), 1,
                         "a known rejection must not be re-discovered per call")
        self.assertNotIn("reasoning", second.calls[0])

    def test_the_memory_is_keyed_on_the_canonical_model(self):
        self._chat(ScriptedTransport(), model="deepseek/deepseek-v4.1-flash")
        # The same model under a different suffix is still the same model.
        self.assertTrue(chatmod.reasoning_param_rejected(
            "deepseek/deepseek-v4.1-flash:free"))
        # A different model has learned nothing.
        self.assertFalse(chatmod.reasoning_param_rejected("z-ai/glm-5.3-flash"))

    def test_only_a_rejection_is_learned(self):
        ok = ScriptedTransport(reject_first=False)
        self._chat(ok)
        self.assertFalse(chatmod.reasoning_param_rejected(
            "deepseek/deepseek-v4.1-flash"))
        self.assertEqual(len(ok.calls), 1)
        self.assertIn("reasoning", ok.calls[0])

    def test_a_non_reasoning_failure_is_not_learned(self):
        # A 500 is a transport failure, not a statement about the parameter.
        class Down:
            def __init__(self):
                self.calls = []

            def post(self, url, api_key, payload):
                self.calls.append(payload)
                return 500, {"error": {"message": "upstream exploded"}}

        down = Down()
        self._chat(down)
        self.assertFalse(chatmod.reasoning_param_rejected(
            "deepseek/deepseek-v4.1-flash"))
        self.assertEqual(len(down.calls), 1)

    def test_the_memory_can_be_forgotten(self):
        self._chat(ScriptedTransport())
        self.assertTrue(chatmod.reasoning_param_rejected(
            "deepseek/deepseek-v4.1-flash"))
        chatmod.reset_reasoning_param_memory()
        self.assertFalse(chatmod.reasoning_param_rejected(
            "deepseek/deepseek-v4.1-flash"))

    def test_a_non_reasoning_model_still_never_sends_the_key(self):
        transport = ScriptedTransport()
        self._chat(transport, model="meta-llama/llama-3.1-8b-instruct")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("reasoning", transport.calls[0])


if __name__ == "__main__":
    unittest.main()
