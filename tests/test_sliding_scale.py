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
    decide_probe_verify_escalate,
    should_abstain,
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

        ladder_esc_0 = tier_model_ladder(TIER_0_SCOUT, use_free=True, allow_escalation=True)
        self.assertGreaterEqual(len(ladder_esc_0), 4)

        ladder_esc_1 = tier_model_ladder(TIER_1_DISTILLER, use_free=True, allow_escalation=True)
        self.assertGreaterEqual(len(ladder_esc_1), 4)

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

    def test_calibrated_abstention_and_pipeline(self):
        # Abstain if confidence < threshold
        self.assertTrue(should_abstain(0.65, min_confidence=0.70))
        self.assertFalse(should_abstain(0.75, min_confidence=0.70))

        # Pipeline: structural invalid escalates
        t_esc1 = decide_probe_verify_escalate("update", confidence=0.90, structural_valid=False, current_tier=TIER_1_DISTILLER)
        self.assertEqual(t_esc1, TIER_2_FRONTIER)

        # Pipeline: low confidence triggers calibrated abstention
        t_esc2 = decide_probe_verify_escalate("update", confidence=0.50, structural_valid=True, current_tier=TIER_1_DISTILLER, min_confidence=0.70)
        self.assertEqual(t_esc2, TIER_2_FRONTIER)

        # Pipeline: valid structure & confident accepts at current tier
        t_ok = decide_probe_verify_escalate("update", confidence=0.85, structural_valid=True, current_tier=TIER_1_DISTILLER, min_confidence=0.70)
        self.assertEqual(t_ok, TIER_1_DISTILLER)

    def test_tier_2_free_with_escalation(self):
        ladder = tier_model_ladder(TIER_2_FRONTIER, use_free=True, allow_escalation=True, custom_frontier="gpt-6")
        self.assertIn("openai/gpt-6", ladder)
        self.assertIn("deepseek/deepseek-v4-pro", ladder)

    def test_planner_ladder_and_decomposition(self):
        from harness.waist import resolve_planner_ladder, heuristic_decompose_goal
        ladder_free = resolve_planner_ladder(use_free=True)
        self.assertIn(":free", ladder_free[0])

        ladder_paid = resolve_planner_ladder(use_free=False, custom_frontier="claude-3.7")
        self.assertIn("claude", ladder_paid[0])

        ladder_allow_paid = resolve_planner_ladder(use_free=True, allow_paid=True)
        self.assertGreaterEqual(len(ladder_allow_paid), 2)

        dag = heuristic_decompose_goal("Refactor helper functions in utils.py")
        self.assertEqual(dag.nodes["task_1"].target_files, ("utils.py",))

    def test_ling_excluded_from_free_apply_pool_and_scout_default(self):
        from harness.config import FREE_APPLY_POOL, FREE_PANEL_POOL
        # Ensure Ling is never in the default free apply pool (no incapable apply calls)
        self.assertNotIn("inclusionai/ling-3.0-flash-fin:free", FREE_APPLY_POOL)
        for model in FREE_APPLY_POOL:
            self.assertNotIn("ling", model.lower())

        # Scout tier recommended model must be capable (Gemma 4 31b), not Ling
        scout_rec = resolve_tier_recommended_model(TIER_0_SCOUT, use_free=True)
        self.assertEqual(scout_rec, FREE_PANEL_POOL[0])
        self.assertNotIn("ling", scout_rec.lower())

    def test_classify_task_tier_decomposition_markers(self):
        # Tasks with planning / decomposition markers must classify as Tier 1 Distiller
        c = classify_task_tier("decompose goal into subtasks dag", target_files=["plan.json"], use_free=True)
        self.assertEqual(c.tier, TIER_1_DISTILLER)
        self.assertTrue(any("decompose" in r for r in c.reasons))

    def test_router_classify_and_route_with_jev_policy(self):
        from unittest.mock import MagicMock
        from harness.router import Router

        mock_policy = MagicMock()
        mock_policy.keyed = True
        mock_res = MagicMock()
        mock_res.answers = {"route": "frontier"}
        mock_policy.evaluate_route.return_value = (mock_res, {})

        router = Router(
            apply_model="apply/m",
            apply_pool=["apply/m"],
            panel=["judge/m"],
            judge="judge/m",
            jev_policy=mock_policy,
        )
        spec = router.classify_and_route("Refactor module", ["harness/test.py"])
        self.assertEqual(spec["classification"].tier, TIER_2_FRONTIER)
        self.assertEqual(spec["jev_route"], "frontier")

    def test_load_settings_auto_paid_failover_pools(self):
        # When use_free is true and a paid key is present, paid pools are appended as failovers
        s = load_settings({"use_free": True, "openrouter_api_key": "sk-or-paid-test"})
        self.assertTrue(s.use_free)
        # Verify paid rungs are present in the default pools for failover
        self.assertIn("deepseek/deepseek-v4.1-flash", s.apply_pool)
        self.assertIn("deepseek/deepseek-v4.1-flash", s.panel)
        self.assertIn("z-ai/glm-5.3-flash", s.escalation_pool)

    def test_router_classify_and_route_falls_back_when_jev_route_raises(self):
        from unittest.mock import MagicMock
        from harness.router import Router

        mock_policy = MagicMock()
        mock_policy.keyed = True
        mock_policy.evaluate_route.side_effect = RuntimeError("network fail")

        router = Router(
            apply_model="apply/m",
            apply_pool=["apply/m"],
            panel=["judge/m"],
            judge="judge/m",
            jev_policy=mock_policy,
        )
        spec = router.classify_and_route("Fix typo in docstring", ["harness/util.py"])
        self.assertEqual(spec["classification"].tier, TIER_0_SCOUT)

    def test_engine_for_wires_cheap_judge_when_router_cheap_judge_is_none(self):
        from harness.router import Router
        from harness import session

        settings = load_settings({"judge": "custom/judge-seat"})
        router = Router(
            apply_model="apply/m",
            apply_pool=["apply/m"],
            panel=["custom/judge-seat"],
            judge="custom/judge-seat",
        )
        router.cheap_judge = None
        session.engine_for(settings, "sk-key", object(), object(), router)
        self.assertEqual(router.cheap_judge, "custom/judge-seat")

    def test_hermetic_fake_transport_jev_integration(self):
        from tests._fake import FakeTransport, JEV_URL, jev_resp
        from harness.jev import JevEvaluator, diff_question_pack

        fake = FakeTransport()
        evaluator = JevEvaluator(api_key="sk-test", endpoint=JEV_URL, transport=fake)
        res = evaluator.evaluate({"diff": "@@ -1 +1 @@\n-a\n+b\n"}, diff_question_pack())
        self.assertEqual(res.verdict, "pass")
        self.assertEqual(len(fake.jev_calls()), 1)

        # Custom jev_posts
        custom = jev_resp(noul=0.2, confidence=0.3)
        fake_custom = FakeTransport(jev_posts=[(200, custom)])
        evaluator_custom = JevEvaluator(api_key="sk-test", endpoint=JEV_URL, transport=fake_custom)
        res_custom = evaluator_custom.evaluate({"diff": "@@ -1 +1 @@\n-a\n+b\n"}, diff_question_pack())
        self.assertEqual(res_custom.verdict, "fail")
        self.assertEqual(len(fake_custom.jev_calls()), 1)


if __name__ == "__main__":
    unittest.main()


