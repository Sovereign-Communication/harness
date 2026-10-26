"""Hermetic tests for EV-3 (Jev-driven pool resolver -- advisory mode)."""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_packs import ECONOMICS_ROUTE_SITE, economics_route_question_pack
from harness.jev_policy import JevPolicy
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor


class FakeJevEvaluator:
    def __init__(self, *, keyed=True, tier="T2", confidence=0.92):
        self.api_key = "jev-test-key" if keyed else None
        self.model = "jev-latest"
        self.explicitly_disabled = not keyed
        self.lazy_reserve = False
        self._tier = tier
        self._confidence = confidence

    def evaluate(self, state, questions):
        return JevEvaluationResult(
            verdict="pass",
            confidence=self._confidence,
            supported=0.90,
            answers={
                "tier": self._tier,
                "reasoning_required": True,
                "context_band": "medium",
            },
            reasons=["multi-file refactor requires T2"],
            cost=0.00002,
            input_tokens=400,
            output_tokens=20,
            is_fallback=False,
            model=self.model,
        )


class EconomicsRoutePolicyTests(unittest.TestCase):
    def test_pack_contains_zero_provider_brands(self):
        pack = economics_route_question_pack()
        self.assertIn("tier", pack)
        self.assertIn("reasoning_required", pack)
        self.assertIn("context_band", pack)
        serialized = repr(pack).lower()
        for brand in ("deepseek", "openai", "google", "anthropic", "qwen", "glm"):
            self.assertNotIn(brand, serialized)

    def test_unkeyed_falls_back_to_deterministic_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(overrides={"ledger_path": os.path.join(tmp, "l.jsonl")})
            ledger = AutonomyLedger(settings.ledger_path)
            ledger.append("offer", task_id="t-unkeyed", model="(test)")
            policy = JevPolicy(
                settings,
                ledger=ledger,
                evaluator=FakeJevEvaluator(keyed=False),
            )
            out = policy.evaluate_economics_route(
                {"prompt": "fix deadlock in mutex lock", "target_files": ["a.py"]},
                task_id="t-unkeyed",
            )
            self.assertEqual(out["site"], ECONOMICS_ROUTE_SITE)
            self.assertEqual(out["mode"], "advisory")
            self.assertTrue(out["is_fallback"])
            self.assertEqual(out["effective_tier"], "T3")
            self.assertEqual(out["advisory_tier"], "T3")

    def test_keyed_records_advisory_judgment_and_keeps_deterministic_effective_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(overrides={"ledger_path": os.path.join(tmp, "l.jsonl")})
            ledger = AutonomyLedger(settings.ledger_path)
            ledger.append("offer", task_id="t-keyed", model="(test)")
            gov = SpendGovernor(transport=None, api_key="k", max_cost=0.50)
            policy = JevPolicy(
                settings,
                governor=gov,
                ledger=ledger,
                evaluator=FakeJevEvaluator(keyed=True, tier="T2", confidence=0.95),
            )
            out = policy.evaluate_economics_route(
                {"prompt": "add small helper check", "target_files": ["a.py"]},
                task_id="t-keyed",
            )
            self.assertFalse(out["is_fallback"])
            self.assertTrue(out["confident"])
            self.assertEqual(out["deterministic_tier"], "T1")
            self.assertEqual(out["advisory_tier"], "T2")
            # Advisory mode (DF-EV-6): effective_tier stays deterministic
            self.assertEqual(out["effective_tier"], "T1")
            self.assertFalse(out["agreement"])


if __name__ == "__main__":
    unittest.main()
