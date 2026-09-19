"""Hermetic unit tests for sliding-scale complexity classification and model routing."""
from dataclasses import FrozenInstanceError
import unittest

from harness.config import (
    FREE_JUDGE,
    HARD_MAX_COST,
    load_settings,
)
from harness.sliding_scale import (
    FRONTIER_ALIASES,
    TIER_0_SCOUT,
    TIER_1_DISTILLER,
    TIER_2_FRONTIER,
    SlidingScaleRoute,
    TaskClassification,
    classify_task_tier,
    resolve_frontier_model,
    resolve_sliding_scale_route,
    resolve_tier_recommended_model,
    tier_cost_ceiling,
    tier_model_ladder,
)


class TestSlidingScale(unittest.TestCase):
    def test_tier_constants(self):
        self.assertEqual(TIER_0_SCOUT, 0)
        self.assertEqual(TIER_1_DISTILLER, 1)
        self.assertEqual(TIER_2_FRONTIER, 2)

    def test_frontier_aliases(self):
        self.assertIn("fable-5.1", FRONTIER_ALIASES)
        self.assertEqual(resolve_frontier_model("fable-5.1"), "fable/fable-5.1")
        self.assertEqual(resolve_frontier_model("gpt-6"), "openai/gpt-6")
        self.assertEqual(resolve_frontier_model("claude-opus"), "anthropic/claude-3-opus")
        self.assertEqual(resolve_frontier_model("sol"), "openai/gpt-5.6-sol")
        self.assertEqual(resolve_frontier_model("custom/model-x"), "custom/model-x")
        self.assertEqual(resolve_frontier_model(None, use_free=True), FREE_JUDGE)
        self.assertEqual(resolve_frontier_model(None, use_free=False),
                         "qwen/qwen3.8-max-0902")

    def test_classify_tier_0_scout(self):
        c = classify_task_tier(
            instruction="Fix typo in docstring and format comments",
            target_files=["harness/util.py"],
            diff_size=5,
            use_free=True,
        )
        self.assertEqual(c.tier, TIER_0_SCOUT)
        self.assertLess(c.score, 0.35)
        self.assertEqual(c.estimated_cost_tier, "free")
        self.assertTrue(any("scout/formatting" in r for r in c.reasons))

    def test_classify_tier_1_distiller(self):
        c = classify_task_tier(
            instruction="Implement response serializer and update validation handlers",
            target_files=["harness/handlers.py", "tests/test_handlers.py"],
            diff_size=40,
            use_free=False,
        )
        self.assertEqual(c.tier, TIER_1_DISTILLER)
        self.assertGreaterEqual(c.score, 0.35)
        self.assertLess(c.score, 0.65)
        self.assertEqual(c.estimated_cost_tier, "budget")

    def test_classify_tier_2_frontier(self):
        c = classify_task_tier(
            instruction="Refactor concurrent thread pool architecture with deadlock detection and cryptographic hash chain verification",
            target_files=["harness/executor.py", "harness/ledger.py", "harness/sync.py", "harness/crypto.py"],
            diff_size=200,
            dependency_depth=3,
            is_leaf=False,
            use_free=False,
            custom_frontier="fable-5.1",
        )
        self.assertEqual(c.tier, TIER_2_FRONTIER)
        self.assertGreaterEqual(c.score, 0.65)
        self.assertEqual(c.recommended_model, "fable/fable-5.1")
        self.assertEqual(c.estimated_cost_tier, "frontier")
        self.assertTrue(any("frontier keywords" in r for r in c.reasons))
        self.assertTrue(any("broad target file" in r for r in c.reasons))

    def test_retry_escalation_forces_frontier(self):
        # Even a simple task escalates to frontier after 2 consecutive failures
        c = classify_task_tier(
            instruction="Fix minor typo",
            previous_failures=2,
            use_free=False,
        )
        self.assertEqual(c.tier, TIER_2_FRONTIER)
        self.assertTrue(any("retry escalation" in r for r in c.reasons))

    def test_tier_recommended_models(self):
        m0_free = resolve_tier_recommended_model(TIER_0_SCOUT, use_free=True)
        m1_free = resolve_tier_recommended_model(TIER_1_DISTILLER, use_free=True)
        m2_free = resolve_tier_recommended_model(TIER_2_FRONTIER, use_free=True)
        self.assertIn(":free", m0_free)
        self.assertIn(":free", m1_free)
        self.assertIn(":free", m2_free)

        m0_paid = resolve_tier_recommended_model(TIER_0_SCOUT, use_free=False)
        m1_paid = resolve_tier_recommended_model(TIER_1_DISTILLER, use_free=False)
        m2_paid = resolve_tier_recommended_model(TIER_2_FRONTIER, use_free=False, custom_frontier="gpt-6")
        self.assertNotIn(":free", m0_paid)
        self.assertNotIn(":free", m1_paid)
        self.assertEqual(m2_paid, "openai/gpt-6")

    def test_tier_model_ladders(self):
        ladder_free_0 = tier_model_ladder(TIER_0_SCOUT, use_free=True)
        self.assertGreaterEqual(len(ladder_free_0), 2)

        ladder_paid_2 = tier_model_ladder(TIER_2_FRONTIER, use_free=False, custom_frontier="fable-5.1")
        self.assertEqual(ladder_paid_2[0], "fable/fable-5.1")
        self.assertIn("openai/gpt-5.6-sol", ladder_paid_2)

    def test_tier_cost_ceilings(self):
        self.assertEqual(tier_cost_ceiling(TIER_0_SCOUT, use_free=True), 0.0)
        self.assertEqual(tier_cost_ceiling(TIER_1_DISTILLER, use_free=True), 0.0)
        self.assertEqual(tier_cost_ceiling(TIER_2_FRONTIER, use_free=True), 0.0)

        c0 = tier_cost_ceiling(TIER_0_SCOUT, use_free=False)
        c1 = tier_cost_ceiling(TIER_1_DISTILLER, use_free=False)
        c2 = tier_cost_ceiling(TIER_2_FRONTIER, use_free=False)
        self.assertLessEqual(c0, c1)
        self.assertLessEqual(c1, c2)
        self.assertLessEqual(c2, HARD_MAX_COST)

    def test_resolve_sliding_scale_route(self):
        route = resolve_sliding_scale_route(
            instruction="Refactor concurrency invariants across ledger modules",
            target_files=["harness/ledger.py"],
            use_free=False,
            custom_frontier="gpt-6",
        )
        self.assertIsInstance(route, SlidingScaleRoute)
        self.assertEqual(route.classification.tier, TIER_2_FRONTIER)
        self.assertEqual(route.ladder[0], "openai/gpt-6")
        self.assertGreater(route.cost_ceiling, 0.0)

    def test_dataclasses_are_frozen(self):
        tc = TaskClassification(
            tier=0, score=0.1, reasons=("ok",), recommended_model="m", estimated_cost_tier="free"
        )
        with self.assertRaises(FrozenInstanceError):
            tc.tier = 1  # type: ignore

        route = SlidingScaleRoute(classification=tc, ladder=("m",), cost_ceiling=0.0)
        with self.assertRaises(FrozenInstanceError):
            route.cost_ceiling = 0.5  # type: ignore

    def test_settings_frontier_model_wiring(self):
        s = load_settings({"frontier_model": "fable-5.1"})
        self.assertEqual(s.frontier_model, "fable-5.1")
        d = s.to_dict()
        self.assertIn("frontier_model", d)
        self.assertEqual(d["frontier_model"], "fable-5.1")


if __name__ == "__main__":
    unittest.main()
