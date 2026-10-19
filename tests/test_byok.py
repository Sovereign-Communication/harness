import json
import os
import tempfile
import unittest

from harness.spend import SpendGovernor
from harness.panel import panel_judge
from tests._fake import FakeTransport, m, comp

JUDGE = "inclusionai/ling-2.6-flash"
PAID = "vendor/paid-pro"
FREE = "vendor-free/gemma-free:free"
P2 = "other/model-b"


def _byok_body(cost):
    return {"choices": [{"message": {"content": "byok reply"}, "finish_reason": "stop"}],
            "usage": {"cost": cost, "is_byok": True}}


class ByokLearnTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.prefix_path = os.path.join(self.dir.name, "byok_prefixes.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_paid_byok_records_prefix_and_rotates(self):
        fake = FakeTransport(
            models=[m(PAID, "0.000001", "0.000002"), m(P2), m(JUDGE)],
            posts=[_byok_body(0.0002), comp("take two"), comp("verdict")])
        gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=self.prefix_path)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[PAID, P2], judge=JUDGE)
        models = [r["model"] for r in result["panel_results"]]
        self.assertEqual(models, [P2], "paid-BYOK member must rotate out")
        self.assertIn("vendor/", gov._learned_byok)
        self.assertTrue(os.path.exists(self.prefix_path))

    def test_free_byok_is_accepted_not_recorded(self):
        fake = FakeTransport(
            models=[m(FREE, "0", "0"), m(P2), m(JUDGE)],
            posts=[_byok_body(0.0), comp("take two"), comp("verdict")])
        gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=self.prefix_path)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[FREE, P2], judge=JUDGE)
        models = [r["model"] for r in result["panel_results"]]
        self.assertIn(FREE, models, "free BYOK-routed model costs $0 and should be used")
        self.assertFalse(gov._learned_byok)

    def test_learned_prefix_skipped_on_next_run(self):
        # First run learns the paid prefix.
        fake = FakeTransport(
            models=[m(PAID, "0.000001", "0.000002"), m(P2), m(JUDGE)],
            posts=[_byok_body(0.0002), comp("take two"), comp("verdict")])
        gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=self.prefix_path)
        panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?", panel=[PAID, P2],
                    judge=JUDGE)
        self.assertTrue(os.path.exists(self.prefix_path))

        # Next run: PAID must be filtered before any call (no posts consumed by it).
        fake2 = FakeTransport(
            models=[m(PAID, "0.000001", "0.000002"), m(P2), m(JUDGE)],
            posts=[comp("take two"), comp("verdict")])
        gov2 = SpendGovernor(fake2, "sk-test", byok_prefixes_path=self.prefix_path)
        result = panel_judge(transport=fake2, api_key="k", governor=gov2, prompt="Q?",
                             panel=[PAID, P2], judge=JUDGE)
        self.assertEqual([r["model"] for r in result["panel_results"]], [P2])

    def test_learned_paid_prefix_does_not_block_free_model_from_same_org(self):
        free = "vendor/gemma-free:free"
        with open(self.prefix_path, "w", encoding="utf-8") as f:
            json.dump(["vendor/"], f)
        fake = FakeTransport(
            models=[m(free, "0", "0"), m(JUDGE)],
            posts=[comp("free vote"), comp("verdict")])
        gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=self.prefix_path)
        cached_free = "vendor/zero-price-model"
        gov._pricing_cache[cached_free] = (0.0, 0.0)
        self.assertFalse(gov.learned_blocked(cached_free))

        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[free], judge=JUDGE)

        self.assertEqual([r["model"] for r in result["panel_results"]], [free])


if __name__ == "__main__":
    unittest.main()
