"""Hermetic unit tests for the Dynamic Resource Allocation engine."""
import unittest

from harness.config import HARD_MAX_COST
from harness.dynamic_allocation import (
    DEFAULT_MODEL_SPEC,
    ModelContextSpec,
    compute_adaptive_waist_rounds,
    compute_adaptive_window_lines,
    compute_dynamic_confidence_threshold,
    compute_dynamic_evidence_budget,
    compute_dynamic_terminal_reserve,
    compute_dynamic_tier_ceiling,
    dynamic_run_token_budget,
    rank_models_dynamically,
    resolve_model_spec,
)
from harness.sliding_scale import (
    TIER_0_SCOUT,
    TIER_1_DISTILLER,
    TIER_2_FRONTIER,
    tier_cost_ceiling,
)
from harness.token_budget import TokenBudget


class DynamicAllocationTests(unittest.TestCase):
    def test_resolve_model_spec_exact_and_family(self):
        gemini_spec = resolve_model_spec("google/gemini-3.8-flash")
        self.assertGreaterEqual(gemini_spec.context_window, 1_000_000)

        deepseek_spec = resolve_model_spec("deepseek/deepseek-v4.1-flash")
        self.assertEqual(deepseek_spec.context_window, 131_072)

        unknown_spec = resolve_model_spec("unknown-provider/foo-bar")
        self.assertEqual(unknown_spec, DEFAULT_MODEL_SPEC)
        self.assertIsInstance(unknown_spec, ModelContextSpec)

    def test_dynamic_run_token_budget(self):
        in_gem, out_gem = dynamic_run_token_budget("google/gemini-3.8-flash", safe_ratio=0.75)
        self.assertGreater(in_gem, 500_000)
        self.assertGreater(out_gem, 10_000)

        in_ds, out_ds = dynamic_run_token_budget("deepseek/deepseek-v4.1-flash", safe_ratio=0.75)
        self.assertEqual(in_ds, int(131_072 * 0.75))
        self.assertLessEqual(out_ds, 8_192)

    def test_compute_adaptive_window_lines(self):
        lines_plenty = compute_adaptive_window_lines(
            total_lines=150, remaining_budget_tokens=50_000, max_lines=500
        )
        self.assertEqual(lines_plenty, 150)

        lines_tight = compute_adaptive_window_lines(
            total_lines=1000, remaining_budget_tokens=500, min_lines=50, max_lines=500
        )
        self.assertLess(lines_tight, 100)
        self.assertGreaterEqual(lines_tight, 50)

        # Boundary checks
        self.assertEqual(compute_adaptive_window_lines(0, 1000), 50)
        self.assertEqual(compute_adaptive_window_lines(100, 0), 50)

    def test_compute_adaptive_waist_rounds(self):
        self.assertEqual(compute_adaptive_waist_rounds(1, remaining_tokens=10_000), 2)
        scaled = compute_adaptive_waist_rounds(12, remaining_tokens=10_000)
        self.assertGreater(scaled, 2)
        self.assertLessEqual(scaled, 6)

    def test_compute_dynamic_terminal_reserve(self):
        res_hermetic = compute_dynamic_terminal_reserve("python -m unittest tests/test_foo.py")
        self.assertEqual(res_hermetic, 0.0)

        pytest_hermetic = compute_dynamic_terminal_reserve("pytest tests/")
        self.assertEqual(pytest_hermetic, 0.0)

        res_judged = compute_dynamic_terminal_reserve(
            "harness verify --judge z-ai/glm-5.3-flash", judge_model="z-ai/glm-5.3-flash"
        )
        self.assertGreater(res_judged, 0.0)
        self.assertLess(res_judged, 0.05)

        self.assertEqual(compute_dynamic_terminal_reserve(""), 0.0)
        self.assertEqual(compute_dynamic_terminal_reserve(None), 0.0)

    def test_compute_dynamic_tier_ceiling(self):
        c0 = compute_dynamic_tier_ceiling(0, model_id="deepseek/deepseek-v4.1-flash")
        c1 = compute_dynamic_tier_ceiling(1, model_id="deepseek/deepseek-v4.1-flash")
        c2 = compute_dynamic_tier_ceiling(2, model_id="qwen/qwen3.8-max-0902")

        self.assertLessEqual(c0, c1)
        self.assertLessEqual(c1, c2)
        self.assertLessEqual(c2, HARD_MAX_COST)

    def test_compute_dynamic_evidence_budget(self):
        q_simple, chars_simple = compute_dynamic_evidence_budget(
            instruction="Fix typo in comment", target_files=["foo.py"]
        )
        self.assertEqual(q_simple, 2)

        q_complex, chars_complex = compute_dynamic_evidence_budget(
            instruction="Major refactor of ledger concurrency, transaction lifecycle, and verify hashing across modules",
            target_files=["a.py", "b.py", "c.py", "d.py", "e.py"],
            remaining_tokens=4000,
        )
        self.assertGreater(q_complex, q_simple)
        self.assertGreaterEqual(chars_complex, chars_simple)

    def test_compute_dynamic_confidence_threshold(self):
        t_ro = compute_dynamic_confidence_threshold(
            "file_triage", is_destructive=False, has_verification_gate=True
        )
        self.assertEqual(t_ro, 0.65)

        t_dest = compute_dynamic_confidence_threshold(
            "apply_edit", is_destructive=True, has_verification_gate=False
        )
        self.assertGreaterEqual(t_dest, 0.85)

        t_gated_dest = compute_dynamic_confidence_threshold(
            "apply_edit", is_destructive=True, has_verification_gate=True
        )
        self.assertEqual(t_gated_dest, 0.78)

    def test_rank_models_dynamically(self):
        self.assertEqual(rank_models_dynamically([]), [])
        models = [
            "deepseek/deepseek-v4.1-flash",
            "inclusionai/ling-3.0-flash",
            "z-ai/glm-5.3-flash",
        ]
        health = {
            "inclusionai/ling-3.0-flash": {"errors": 5, "latency_ms": 2500},
            "deepseek/deepseek-v4.1-flash": {"errors": 0, "latency_ms": 300},
        }
        ranked = rank_models_dynamically(models, health_status=health, prefer_paid=True)
        self.assertEqual(ranked[0], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(ranked[-1], "inclusionai/ling-3.0-flash")

    def test_token_budget_for_model_and_proportional_stage(self):
        tb = TokenBudget.for_model("deepseek/deepseek-v4.1-flash", label="dynamic_run")
        self.assertGreaterEqual(tb.max_input_tokens, 90_000)

        child = tb.proportional_stage("waist_inspection", ratio=0.4)
        self.assertLessEqual(child.max_input_tokens, tb.max_input_tokens)
        self.assertGreater(child.max_input_tokens, 1000)

        # Settle with reasoning and cached tokens to verify multi-kind tracking
        al = child.allowance(500, max_output_tokens=200, label="call_1")
        usage = child.settle(al, input_tokens=400, output_tokens=150, reasoning_tokens=50, cached_tokens=100)
        self.assertEqual(usage.reasoning_tokens, 50)
        self.assertEqual(usage.cached_tokens, 100)
        self.assertEqual(child.used_reasoning(), 50)
        self.assertEqual(child.used_cached(), 100)
        self.assertEqual(tb.used_reasoning(), 50)
        self.assertEqual(tb.used_cached(), 100)

    def test_sliding_scale_tier_ceiling_dynamic(self):
        c0 = tier_cost_ceiling(TIER_0_SCOUT, use_free=False)
        c1 = tier_cost_ceiling(TIER_1_DISTILLER, use_free=False)
        c2 = tier_cost_ceiling(TIER_2_FRONTIER, use_free=False, model_id="qwen/qwen3.8-max-0902")
        self.assertGreater(c0, 0.0)
        self.assertGreater(c1, c0)
        self.assertGreater(c2, c1)
        self.assertLessEqual(c2, HARD_MAX_COST)


if __name__ == "__main__":
    unittest.main()
