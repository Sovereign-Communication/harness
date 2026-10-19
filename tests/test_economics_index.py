"""Hermetic tests for EV-1 (cost-per-capability index) and #196 (per-lane cost metering)."""
import os
import tempfile
import unittest

from harness.benchmark_ingest import BenchmarkRow
from harness.capability import (
    CapabilityProfile,
    MAX_BENCHMARK_PRIOR_MARGIN,
    benchmark_prior,
    cost_per_capability,
)
from harness.config import DISCOUNT_IS_MULTIPLIER, ECONOMICS_SCHEMA_VERSION
from harness.discount_gate import record_discount_semantics
from harness.endpoint_pricing import (
    EndpointPrice,
    ModelEndpoints,
    effective_price,
)
from harness.errors import HarnessError
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor


class EffectivePriceTests(unittest.TestCase):
    def test_never_mixes_cheapest_in_and_out_across_providers(self):
        # Provider A: cheap input ($0.01/M), expensive output ($2.00/M)
        # Provider B: moderate input ($0.10/M), cheap output ($0.20/M)
        # For apply (4000 in, 1500 out):
        #   Provider A = 4000 * 1e-8 + 1500 * 2e-6 = 0.00004 + 0.00300 = $0.00304
        #   Provider B = 4000 * 1e-7 + 1500 * 2e-7 = 0.00040 + 0.00030 = $0.00070
        #   Mixed (illegal) = 4000 * 1e-8 + 1500 * 2e-7 = $0.00034
        ep_a = EndpointPrice("ProvA", prompt=1e-8, completion=2e-6, tag="a")
        ep_b = EndpointPrice("ProvB", prompt=1e-7, completion=2e-7, tag="b")
        me = ModelEndpoints("deepseek/deepseek-v4.1-flash", (ep_a, ep_b))

        res = effective_price(me, lane="apply")
        self.assertEqual(res.endpoint.provider_name, "ProvB")
        self.assertAlmostEqual(res.usd, 0.00070, places=8)
        self.assertNotAlmostEqual(res.usd, 0.00034, places=6)
        self.assertEqual(res["provider"], "ProvB")
        self.assertEqual(res.get("lane"), "apply")

    def test_honors_long_context_overrides_and_cache_eligible(self):
        ep = EndpointPrice(
            "ProvA",
            prompt=1e-6,
            completion=2e-6,
            input_cache_read=1e-7,
            overrides=({"min_prompt_tokens": 10000, "prompt": 3e-6, "completion": 4e-6},),
        )
        me = ModelEndpoints("openai/gpt-5-mini", (ep,))

        base = effective_price(me, input_tokens=1000, output_tokens=500)
        self.assertAlmostEqual(base.usd, 1000 * 1e-6 + 500 * 2e-6, places=9)

        cached = effective_price(
            me, input_tokens=1000, output_tokens=500, cache_eligible=True)
        self.assertAlmostEqual(cached.usd, 1000 * 1e-7 + 500 * 2e-6, places=9)

        stepped = effective_price(me, input_tokens=12000, output_tokens=500)
        self.assertAlmostEqual(stepped.usd, 12000 * 3e-6 + 500 * 4e-6, places=9)

    def test_uptime_gate_filters_unhealthy_endpoints(self):
        ep_down = EndpointPrice("DownProv", prompt=1e-8, completion=1e-8, uptime_1d=0.50)
        ep_up = EndpointPrice("UpProv", prompt=1e-6, completion=1e-6, uptime_1d=0.99)
        me = ModelEndpoints("z-ai/glm-5.3-flash", (ep_down, ep_up))

        res = effective_price(me, min_uptime=0.90)
        self.assertEqual(res.endpoint.provider_name, "UpProv")

        with self.assertRaises(HarnessError):
            effective_price(
                ModelEndpoints("z-ai/glm-5.3-flash", (ep_down,)),
                min_uptime=0.90,
            )

    def test_discounted_endpoint_requires_committed_verdict(self):
        ep = EndpointPrice("PromoProv", prompt=1e-6, completion=2e-6, discount=0.5)
        me = ModelEndpoints("z-ai/glm-5.3-flash", (ep,))
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = os.path.join(tmp, "missing.json")
            with self.assertRaises(HarnessError):
                effective_price(me, path=missing_path)

            verdict_path = os.path.join(tmp, "verdict.json")
            record_discount_semantics({
                "schema": ECONOMICS_SCHEMA_VERSION,
                "model": "z-ai/glm-5.3-flash",
                "semantics": DISCOUNT_IS_MULTIPLIER,
                "fingerprint": {"model": "z-ai/glm-5.3-flash", "max_discount": 0.5},
            }, path=verdict_path)
            res = effective_price(me, input_tokens=1000, output_tokens=500, path=verdict_path)
            self.assertAlmostEqual(res.usd, 0.5 * (1000 * 1e-6 + 500 * 2e-6), places=9)


class CostPerCapabilityTests(unittest.TestCase):
    def test_benchmark_prior_is_bounded_and_shrunk_by_ledger_evidence(self):
        row_hi = BenchmarkRow(
            "deepseek/deepseek-v4.1-flash",
            "artificial_analysis",
            terminal_bench_hard=0.95,
            swe_bench_verified=0.90,
            livecodebench=0.92,
            intelligence_index=85.0,
        )
        margin = benchmark_prior(row_hi, lane="apply")
        self.assertLessEqual(margin, MAX_BENCHMARK_PRIOR_MARGIN)
        self.assertGreater(margin, 0.0)

        profile = CapabilityProfile(
            "deepseek/deepseek-v4.1-flash",
            context_length=131072,
            free=False,
            prompt_price=1e-7,
            completion_price=4e-7,
            supports_reasoning=True,
            supports_structured_json=True,
        )
        ep = EndpointPrice("DeepSeek", prompt=1e-7, completion=4e-7)
        me = ModelEndpoints("deepseek/deepseek-v4.1-flash", (ep,))

        cold = cost_per_capability(profile, me, benchmark_rows=[row_hi], lane="apply")
        self.assertGreater(cold["effective_capability"], cold["reliability"])

        # With heavy observed failure evidence in report, ledger dominates benchmark prior
        report = {
            "calibration": {
                "deepseek/deepseek-v4.1-flash": {
                    "confidence_precision": 0.1,
                    "success_rate": 0.1,
                    "samples": 50,
                }
            }
        }
        warm = cost_per_capability(
            profile, me, benchmark_rows=[row_hi], report=report, lane="apply")
        self.assertLess(warm["effective_capability"], cold["effective_capability"])
        self.assertEqual(warm["samples"], 50)


class PerLaneMeteringTests(unittest.TestCase):
    def test_cost_report_by_lane_tokens_rolling_and_price_table_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            ledger.append(
                "model_result",
                task_id="t1",
                model="deepseek/deepseek-v4.1-flash",
                lane="apply",
                input_tokens=2000,
                output_tokens=1000,
                cost=0.0,
            )
            ledger.append(
                "model_result",
                task_id="t1",
                model="z-ai/glm-5.3-flash",
                lane="judge",
                input_tokens=1000,
                output_tokens=200,
                cost=0.0005,
            )
            ledger.append("complete", task_id="t1", model="deepseek/deepseek-v4.1-flash")

            price_table = {
                "deepseek/deepseek-v4.1-flash": {
                    "prompt_per_token": 1e-7,
                    "completion_per_token": 4e-7,
                }
            }
            rep = ledger.cost_report(price_table=price_table)
            self.assertIn("by_lane", rep)
            self.assertIn("apply", rep["by_lane"])
            self.assertIn("judge", rep["by_lane"])
            self.assertEqual(rep["cost_estimated_calls"], 1)
            self.assertAlmostEqual(rep["by_lane"]["apply"]["cost"], 0.0006, places=6)
            self.assertEqual(rep["tokens"]["input_tokens"], 3000)
            self.assertEqual(rep["tokens"]["output_tokens"], 1200)
            self.assertEqual(rep["rolling"]["verified_lanes"], 1)
            self.assertAlmostEqual(rep["rolling"]["cost_per_verified_lane"], 0.0011, places=6)

    def test_governor_tracks_cost_by_lane(self):
        gov = SpendGovernor(transport=None, api_key="k", max_cost=1.0)
        gov.record_actual(0.01, "deepseek/deepseek-v4.1-flash", lane="apply")
        gov.record_actual(0.005, "z-ai/glm-5.3-flash", lane="judge")
        by_lane = gov.cost_by_lane()
        self.assertEqual(by_lane["apply"], 0.01)
        self.assertEqual(by_lane["judge"], 0.005)


if __name__ == "__main__":
    unittest.main()
