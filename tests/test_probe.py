"""Known-answer capability probe: ceilings and error accounting."""
import unittest

from harness.capability import probe_json_reliability
from harness.errors import HarnessError
from tests._fake import FakeTransport, m


class ProbeCeilingTests(unittest.TestCase):
    def test_probe_preflights_against_ceiling(self):
        """#4: the capability probe loop must not spend through the ceiling."""
        captured = []

        class Gov:
            max_cost = 0.02
            spent = 0.015  # only $0.005 remains

            def check_byok(self, m): pass

            def preflight(self, prompt_text, calls):
                captured.append((prompt_text, calls))
                if self.spent + 0.006 > self.max_cost:
                    raise HarnessError("worst-case estimate exceeds ceiling. Refusing.")

        _fake = FakeTransport(models=[m("m1")])
        res = probe_json_reliability("t", "k", Gov(), ["m1"], max_tokens=16)
        self.assertEqual(len(captured), 5, "each question is preflighted")
        self.assertEqual(res["m1"]["errors"], 5, "blocked questions count as errors")
        self.assertEqual(res["m1"]["calls"], 5)
        self.assertEqual(res["m1"]["json_ok_rate"], 0.0)


class ProbeByokTests(unittest.TestCase):
    def _gov(self, **kw):
        from harness.spend import SpendGovernor
        fake = FakeTransport(models=[m("m/byok")], posts=kw.pop("posts", []))
        gov = SpendGovernor(fake, "sk-test", **kw)
        return fake, gov

    def _byok_resp(self):
        return {"choices": [{"message": {"content": '{"answer": 4}'},
                             "finish_reason": "stop"}],
                "usage": {"cost": 0.001, "is_byok": True}}

    def test_paid_byok_probe_skips_without_spend_leak(self):
        """A BYOK-routed probe bills invisibly: learn the prefix once and
        stop burning questions, instead of scoring $0 answers."""
        from harness.ledger import AutonomyLedger
        import os
        import tempfile
        posts = [self._byok_resp() for _ in range(5)]
        fake, gov = self._gov(posts=posts)
        with tempfile.TemporaryDirectory() as td:
            ledger = AutonomyLedger(os.path.join(td, "l.jsonl"))
            res = probe_json_reliability(fake, "k", gov, ["m/byok"],
                                         ledger=ledger)
        # priced model (default $0.01/M fixture) + BYOK => paid route.
        self.assertEqual(res["m/byok"]["calls"], 5)
        self.assertEqual(res["m/byok"]["errors"], 5)
        self.assertEqual(res["m/byok"]["json_ok_rate"], 0.0)
        # one network call happened (first question), the rest skipped.
        self.assertEqual(len(fake.chat_posts()), 1)
        self.assertTrue(gov.learned_blocked("m/byok"))
        self.assertAlmostEqual(gov.spent, 0.0, places=9)

    def test_reasoning_models_reserve_two_slots_per_question(self):
        """A reasoning-probed model may cost two POSTs per question; the
        preflight must reserve both."""
        captured = []

        class Gov:
            def check_byok(self, m): pass

            def preflight(self, prompt_text, calls):
                captured.append(list(calls))
                return 0.0, []

            def record_actual(self, amount, label): pass

        from harness.capability import CapabilityProfile
        profiles = {"m1": CapabilityProfile("m1", context_length=32768,
                                            supports_reasoning=True)}
        _fake = FakeTransport(models=[m("m1")])
        # No canned posts: every chat raises inside the loop and counts as
        # an error; the preflight capture is what matters here.
        probe_json_reliability("t", "k", Gov(), ["m1"], max_tokens=16,
                               profiles=profiles)
        self.assertEqual(len(captured), 5, "each question is preflighted")
        for calls in captured:
            self.assertEqual(len(calls), 2,
                             "reasoning probe reserves the fallback retry")


if __name__ == "__main__":
    unittest.main()
