"""Hermetic tests for EV-4 (automatic pool rotation + safety bounds + streak diversity)."""
import unittest

from harness.capability import (
    evaluate_pool_rotation,
    rotate_pool_by_streak,
    seat_share_report,
)
from harness.config import (
    EV_AUTO_APPLY,
    EV_COOLDOWN_DAYS,
    EV_MAX_HEAD_STREAK,
    EV_MAX_MOVES_PER_RUN,
    EV_MIN_OBSERVED_SAMPLES,
    EV_MIN_UPTIME_1D,
    EV_MIN_VALUE_RATIO,
)


class AutoRotationTests(unittest.TestCase):
    def test_ev4_constants_are_defined(self):
        self.assertEqual(EV_MIN_VALUE_RATIO, 1.15)
        self.assertEqual(EV_MAX_MOVES_PER_RUN, 1)
        self.assertEqual(EV_COOLDOWN_DAYS, 14)
        self.assertEqual(EV_MIN_OBSERVED_SAMPLES, 5)
        self.assertEqual(EV_MAX_HEAD_STREAK, 3)
        self.assertEqual(EV_MIN_UPTIME_1D, 0.90)
        self.assertFalse(EV_AUTO_APPLY)

    def test_evaluate_pool_rotation_enforces_all_safety_bounds(self):
        pool = ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash", "openai/gpt-4o-mini"]
        candidates = [
            {
                "model_id": "deepseek/deepseek-v4.1-flash",
                "effective_usd": 0.0010,
                "value_ratio": 800.0,
                "samples": 10,
            },
            # Marginal challenger (< 15% improvement) -> rejected
            {
                "model_id": "deepseek/deepseek-v4-flash",
                "effective_usd": 0.0009,
                "value_ratio": 850.0,
                "samples": 8,
            },
            # Cost-increasing challenger -> rejected by default
            {
                "model_id": "openai/gpt-4o-mini",
                "effective_usd": 0.0020,
                "value_ratio": 1200.0,
                "samples": 8,
            },
            # Unhealthy endpoint challenger -> rejected
            {
                "model_id": "inclusionai/ling-3.0-flash",
                "effective_usd": 0.0005,
                "value_ratio": 1500.0,
                "samples": 8,
                "endpoint": {"uptime_1d": 0.50},
            },
        ]
        res = evaluate_pool_rotation(pool, candidates, lane="apply")
        self.assertEqual(res["moves"], [])
        self.assertEqual(res["proposed_pool"], pool)
        reasons = {r["model_id"]: r["reason"] for r in res["rejected"]}
        self.assertIn("value_ratio_below_threshold", reasons["deepseek/deepseek-v4-flash"])
        self.assertEqual(reasons["openai/gpt-4o-mini"], "cost_increase_disallowed")
        self.assertEqual(reasons["inclusionai/ling-3.0-flash"], "low_uptime")

        # Now allow cost increase and verify max 1 move per run
        res_allow = evaluate_pool_rotation(
            pool, candidates, lane="apply", allow_cost_increase=True)
        self.assertEqual(len(res_allow["moves"]), 1)
        self.assertEqual(res_allow["moves"][0]["to_head"], "openai/gpt-4o-mini")
        self.assertEqual(res_allow["proposed_pool"][0], "openai/gpt-4o-mini")

        # Cooldown prevents re-promoting a model rotated within 14 days
        res_cool = evaluate_pool_rotation(
            pool,
            candidates,
            lane="apply",
            allow_cost_increase=True,
            cooldown_history={"openai/gpt-4o-mini": 1000.0},
            now=1000.0 + 3600.0,
        )
        self.assertEqual(res_cool["moves"], [])

    def test_rotate_pool_by_streak_and_seat_share(self):
        pool = ["m1", "m2", "m3"]
        scores = {"m1": 0.80, "m2": 0.76, "m3": 0.50}
        # Below streak threshold: no rotation
        self.assertEqual(
            rotate_pool_by_streak(pool, scores, head_streak=2, max_streak=3),
            ["m1", "m2", "m3"],
        )
        # At streak threshold: m2 is within 10% of m1 (0.76 >= 0.72), rotates to head
        self.assertEqual(
            rotate_pool_by_streak(pool, scores, head_streak=3, max_streak=3),
            ["m2", "m1", "m3"],
        )

        entries = [
            {"event": "apply_attempt", "model": "m1"},
            {"event": "apply_attempt", "model": "m1"},
            {"event": "apply_attempt", "model": "m2"},
        ]
        rep = seat_share_report(entries, pool=pool, window=3)
        self.assertEqual(rep["counts"]["m1"], 2)
        self.assertEqual(rep["counts"]["m2"], 1)
        self.assertEqual(rep["counts"]["m3"], 0)
        self.assertEqual(rep["starved"], ["m3"])


if __name__ == "__main__":
    unittest.main()
