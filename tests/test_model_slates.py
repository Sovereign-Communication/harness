"""Verified 2026-09-13 model slates: shipped ids are current-generation and
the paid pools contain no banned/superseded ids.

The twice-recurred stale-id defect class is machine-checked live by
`harness capabilities --check-shipped` (a live-catalog read; never CI).
This module pins the *policy* hermetically: ruling 3 bans gemini-2.5-pro
(outdated generation); the handoff section 3 lists the dropped V3-era ids.
"""
import unittest

import harness.config as config


class SlatePolicyTests(unittest.TestCase):
    def test_no_banned_or_dropped_ids_in_shipped_defaults(self):
        banned = {
            "google/gemini-2.5-pro",          # ruling 3 (banned)
            "openai/gpt-5",                   # superseded by the 5.6 line
            "deepseek/deepseek-chat",         # V3.1-era, dominated by V4
            "deepseek/deepseek-v3.2",
            "ibm-granite/granite-4.0-h-micro",
            "meta-llama/llama-3.1-8b-instruct",
            "openai/gpt-4o",
        }
        shipped = config.shipped_model_ids()
        offenders = banned & shipped
        self.assertEqual(offenders, set(),
                         "superseded/banned ids must not ship as defaults")

    def test_paid_vote_pool_is_the_verified_slate(self):
        self.assertEqual(config.DEFAULT_PANEL_PAID, [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4.1-flash",
            "inclusionai/ling-3.0-flash",
            "openai/gpt-4o-mini",
            "openai/gpt-5-mini",
            "openai/gpt-5.6-luna",
            "google/gemini-3.8-flash",
        ])

    def test_paid_judge_is_the_value_deep_thinker(self):
        self.assertEqual(config.DEFAULT_JUDGE_PAID, "z-ai/glm-5.3-flash")
        # Mandatory-reasoning route: an explicit disable must draw the
        # documented 400 and retry (proven in test_reasoning_disable).
        from harness.chat import looks_reasoning
        self.assertTrue(looks_reasoning(config.DEFAULT_JUDGE_PAID))

    def test_paid_apply_pool_leads_with_operator_pick(self):
        self.assertEqual(config.DEFAULT_APPLY_MODEL_PAID,
                         "deepseek/deepseek-v4.1-flash")
        self.assertEqual(config.DEFAULT_APPLY_POOL_PAID[0],
                         config.DEFAULT_APPLY_MODEL_PAID)

    def test_escalation_ladder_is_verified_deep_think_tier(self):
        self.assertEqual(config.ESCALATION_POOL_PAID, [
            "z-ai/glm-5.3-flash",
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.8-max-0902",
            "openai/gpt-4.1",
            "openai/gpt-5.6-sol",
        ])
        self.assertEqual(config.DEFAULT_JUDGE_PAID_TOP, "openai/gpt-5.6-sol")
        # Rung caps match the new ladder length.
        self.assertEqual(len(config.ESCALATION_RUNG_CAPS["paid"]),
                         len(config.ESCALATION_POOL_PAID))

    def test_frontier_default_is_the_price_efficient_rung(self):
        # Same listed intelligence tier as the $10/$50 flagships at $2/$6:
        # frontier work defaults to qwen unless the operator pins a model.
        from harness.sliding_scale import resolve_frontier_model
        self.assertEqual(resolve_frontier_model(None, use_free=False),
                         "qwen/qwen3.8-max-0902")
        self.assertEqual(resolve_frontier_model("qwen"),
                         "qwen/qwen3.8-max-0902")

    def test_every_shipped_id_is_enumerated(self):
        shipped = config.shipped_model_ids()
        for mid in config.DEFAULT_APPLY_POOL_PAID:
            self.assertIn(mid, shipped)
        for mid in config.ESCALATION_POOL_PAID:
            self.assertIn(mid, shipped)

    def test_paid_settings_use_new_pools(self):
        s = config.load_settings(overrides={"use_free": False})
        self.assertEqual(s.panel, config.DEFAULT_PANEL_PAID)
        self.assertEqual(s.judge, config.DEFAULT_JUDGE_PAID)
        self.assertIn(config.DEFAULT_APPLY_MODEL_PAID, s.apply_pool)


if __name__ == "__main__":
    unittest.main()
