"""Optional live JEV-P0 smoke: run with HARNESS_JEV_LIVE_SMOKE=1."""
import json
import os
import unittest

from harness.config import load_settings
from harness.jev import JevEvaluator


@unittest.skipUnless(os.environ.get("HARNESS_JEV_LIVE_SMOKE") == "1", "operator-gated live Jev smoke")
class LiveJevSmokeTests(unittest.TestCase):
    def test_live_contract_and_cost(self):
        settings = load_settings()
        self.assertTrue(settings.jev_api_key, "resolve_jev_key() did not find a key")
        evaluator = JevEvaluator(settings=settings)
        result = evaluator.evaluate(
            {"candidate": "A small bounded change", "facts": {"syntax": "ok"}},
            {"supported": {"type": "noul", "instructions": "Is the candidate supported by the supplied facts?"},
             "bounded": {"type": "noul", "instructions": "Is the candidate bounded and concrete?"}},
        )
        diff_result = evaluator.verify_diff_mechanics(
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old()\n+new()\n",
            "Rename old to new", "foo.py")
        for checked in (result, diff_result):
            self.assertFalse(checked.is_fallback)
            self.assertGreater(checked.input_tokens, 0)
            self.assertGreater(checked.cost, 0)
            self.assertAlmostEqual(checked.cost, checked.input_tokens * 42 / 1_000_000)
            self.assertNotIn(checked.verdict, ("transport_error",))
        print(json.dumps({
            "generic": {"model": result.model, "verdict": result.verdict,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens, "cost": result.cost,
                        "is_fallback": result.is_fallback},
            "diff": {"model": diff_result.model, "verdict": diff_result.verdict,
                     "input_tokens": diff_result.input_tokens,
                     "output_tokens": diff_result.output_tokens, "cost": diff_result.cost,
                     "is_fallback": diff_result.is_fallback},
        }, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
