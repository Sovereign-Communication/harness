"""Hermetic SITE-3 tests: the Proof Bench metrics owner.

Pins the honesty rules: gated-only headlines, failures kept in the data,
modeled numbers degrade to None-with-reason instead of being fabricated,
and the influence cap actually caps.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.site_aggregate import (
    aggregate_bundles,
    build_snapshot,
    cohort_splits,
    cost_per_gated_task,
    escalation_escape_rate,
    frontier_warrant_rate,
    gated_pass_rate,
    hourglass_savings,
    jev_leverage,
    run_depth_distribution,
    _downweight_by_cap,
)


def _run(**over):
    run = {
        "run_id": "r", "task_ref": "t", "lane": "task",
        "ts": "2026-09-01T00:00:00Z", "primary_model": "m-free",
        "tier": "T0", "entry_tier": "T0", "rounds": 1,
        "tokens_in": None, "tokens_out": None, "cost": 0.001,
        "outcome": "pass", "gated": True, "deepest_tier_reached": "T0",
        "jev_evals": {"count": 1, "cost": 0.00001, "fallback": 0},
    }
    run.update(over)
    return run


def _pricing():
    # Input-only prices keep the blend math human-checkable (blend = input).
    return {
        "m-free": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
        "m-cheap": {"input_per_mtok": 0.10, "output_per_mtok": 0.0},
        "m-frontier": {"input_per_mtok": 10.0, "output_per_mtok": 0.0},
    }


class CostPerGatedTaskTests(unittest.TestCase):
    def test_median_per_tier_gated_only(self):
        runs = [
            _run(tier="T0", cost=0.001, gated=True, outcome="pass"),
            _run(tier="T0", cost=0.003, gated=True, outcome="pass"),
            _run(tier="T0", cost=9.0, gated=False, outcome="pass"),  # ignored
            _run(tier="T2", cost=0.05, gated=True, outcome="pass"),
        ]
        out = cost_per_gated_task(runs)
        self.assertEqual(out["T0"]["median_cost"], 0.002)
        self.assertEqual(out["T0"]["samples"], 2)
        self.assertEqual(out["T2"]["median_cost"], 0.05)
        # Ungated self-reports never make a proof row.
        self.assertNotIn(9.0, [out[t]["median_cost"] for t in out])

    def test_empty_is_empty(self):
        self.assertEqual(cost_per_gated_task([]), {})


class PassRateTests(unittest.TestCase):
    def test_failures_count(self):
        runs = [_run(outcome="pass"), _run(outcome="fail"),
                _run(outcome="aborted"), _run(outcome="pass")]
        out = gated_pass_rate(runs)
        self.assertAlmostEqual(out["overall"]["pass_rate"], 0.5, places=4)
        self.assertEqual(out["overall"]["samples"], 4)

    def test_no_gated_runs_is_none_not_zero(self):
        out = gated_pass_rate([_run(gated=False)])
        self.assertIsNone(out["overall"]["pass_rate"])


class EscapeRateTests(unittest.TestCase):
    def test_base_layer_low_frontier_escape_high(self):
        runs = [_run(entry_tier="T0", deepest_tier_reached="T0")] * 9 + [
            _run(entry_tier="T0", deepest_tier_reached="T2"),
            _run(entry_tier="T2", deepest_tier_reached="T3"),
        ]
        out = escalation_escape_rate(runs)
        self.assertAlmostEqual(out["T0"]["escape_rate"], 0.1, places=4)
        self.assertAlmostEqual(out["T2"]["escape_rate"], 1.0, places=4)
        self.assertEqual(out["T0"]["samples"], 10)


class RunDepthTests(unittest.TestCase):
    def test_histogram_shares(self):
        runs = [_run(deepest_tier_reached="T0")] * 8 + [
            _run(deepest_tier_reached="T2"),
            _run(deepest_tier_reached="T3"),
        ]
        out = run_depth_distribution(runs)
        self.assertEqual(out["T0"]["share"], 0.8)
        self.assertEqual(out["T3"]["runs"], 1)
        # The hourglass story in one line: base carries the volume.
        self.assertGreater(out["T0"]["share"], out["T3"]["share"] * 5)


class WarrantTests(unittest.TestCase):
    def test_warrant_rate_and_flagging(self):
        runs = [
            _run(deepest_tier_reached="T3",
                 escalation={"warrant": {"lower_rung_verify_failures": 2,
                                         "lower_rung_rounds": 3}}),
            _run(deepest_tier_reached="T3",
                 escalation={"warrant": {"lower_rung_verify_failures": 0,
                                         "lower_rung_rounds": 1}}),
            _run(deepest_tier_reached="T3"),  # no escalation block at all
        ]
        out = frontier_warrant_rate(runs)
        self.assertEqual(out["frontier_runs"], 3)
        self.assertEqual(out["warranted"], 1)
        self.assertAlmostEqual(out["warrant_rate"], 1 / 3, places=4)
        self.assertEqual(out["unwarranted_flagged"], 2)

    def test_no_frontier_runs_is_clean_zero(self):
        out = frontier_warrant_rate([_run()])
        self.assertEqual(out["frontier_runs"], 0)
        self.assertIsNone(out["warrant_rate"])


class SavingsTests(unittest.TestCase):
    def test_modeled_savings_with_pricing(self):
        runs = [_run(primary_model="m-cheap", cost=0.001,
                     jev_evals={"count": 0, "cost": 0.0, "fallback": 0})]
        out = hourglass_savings(runs, _pricing())
        self.assertEqual(out["frontier_model"], "m-frontier")
        # 10.0/0.10 = 100x modeled frontier cost for the same run.
        self.assertAlmostEqual(out["modeled_frontier_cost"], 0.1, places=9)
        self.assertAlmostEqual(out["savings"], 0.099, places=9)
        self.assertTrue(out["modeled"])
        self.assertIn("pricing_snapshot", out["basis"])

    def test_no_pricing_degrades_to_none_with_reason(self):
        out = hourglass_savings([_run()], None)
        self.assertIsNone(out["modeled_frontier_cost"])
        self.assertIsNone(out["savings"])
        self.assertIn("unavailable", out["basis"])


class JevLeverageTests(unittest.TestCase):
    def test_floor_modeled_not_fabricated(self):
        runs = [_run(jev_evals={"count": 2, "cost": 0.00002, "fallback": 0})]
        out = jev_leverage(runs, _pricing())
        self.assertEqual(out["jev_evals"], 2)
        # Floor: 2 evals x 1000 input tokens x 0.10/Mtok = 0.0002.
        self.assertAlmostEqual(out["modeled_generative_floor"], 0.0002, places=9)
        self.assertAlmostEqual(out["ratio"], 10.0, places=2)
        self.assertIn("floor", out["basis"])

    def test_degrades_without_pricing_or_evals(self):
        out = jev_leverage([_run()], None)
        self.assertIsNone(out["ratio"])
        self.assertIn("unavailable", out["basis"])
        out = jev_leverage([_run(jev_evals={"count": 0, "cost": 0.0,
                                            "fallback": 0})], _pricing())
        self.assertIsNone(out["ratio"])


class CohortTests(unittest.TestCase):
    def test_month_and_version_split(self):
        runs = [_run(ts="2026-08-15T00:00:00Z"),
                _run(ts="2026-09-02T00:00:00Z")]
        out = cohort_splits(runs)
        self.assertEqual(set(out), {"2026-08|unknown", "2026-09|unknown"})


class InfluenceCapTests(unittest.TestCase):
    def test_cap_bounds_dominant_session(self):
        # Absolute bound: a session's weight never exceeds cap * pool volume,
        # so a 950-vs-50 dominant submitter is limited to 2x the small one,
        # not 19x. (With two sessions a 10% *share* is arithmetically
        # impossible; the disclosed absolute bound is the honest rule.)
        weights = _downweight_by_cap([950, 50], cap=0.10)
        self.assertEqual(weights[0], 100.0)
        self.assertEqual(weights[1], 50.0)
        self.assertLessEqual(weights[0], 0.10 * 1000 + 1e-9)

    def test_small_pool_flattens_proportionally(self):
        # In a tiny pool every session may exceed the cap as a share; the
        # bound still applies absolutely and equal sizes stay equal.
        weights = _downweight_by_cap([5, 5], cap=0.10)
        self.assertEqual(weights[0], weights[1])
        self.assertLessEqual(max(weights), 0.10 * 10 + 1e-9)


class AggregateTests(unittest.TestCase):
    def test_aggregate_reports_contributors_and_flags(self):
        bundles = [
            {"bundle_id": "b1", "runs": [_run() for _ in range(4)]},
            {"bundle_id": "b2", "runs": [_run()]},
            {"bundle_id": "b3", "runs": [_run(deepest_tier_reached="T3")] * 3
             + [_run(deepest_tier_reached="T3")]},
        ]
        out = aggregate_bundles(bundles)
        self.assertEqual(out["contributors"], 3)
        self.assertEqual(len(out["metrics"]), 3)
        kinds = {a["kind"] for a in out["anomalies"]}
        self.assertIn("low_samples", kinds)
        self.assertIn("frontier_heavy", kinds)

    def test_empty_aggregate_is_honest(self):
        out = aggregate_bundles([])
        self.assertEqual(out["contributors"], 0)
        self.assertIsNone(out["metrics"])

    def test_snapshot_shape(self):
        snap = build_snapshot([{"bundle_id": "b1", "runs": [_run()]}],
                              harness_version="0.3.3")
        self.assertEqual(snap["schema"], "site-snapshot-v1")
        self.assertEqual(snap["contributors"], 1)
        self.assertIn("b1", snap["generated_from_bundles"])


class SessionTracesTests(unittest.TestCase):
    """Public trace cards: Jev-directed provenance reaches the GUI surfaces
    (site traces page + local UI panes) through the shared builder."""

    def _run(self, run_id, *, escalation=None, outcome="pass", gated=True):
        run = {"run_id": run_id, "lane": "task", "outcome": outcome,
               "gated": gated, "entry_tier": "T0", "deepest_tier_reached": "T2",
               "rounds": 2, "cost": 0.004,
               "jev_evals": {"count": 1, "cost": 0.00004, "fallback": 0}}
        if escalation is not None:
            run["escalation"] = escalation
        return run

    def test_trace_carries_directed_provenance_not_context(self):
        import json as _json
        from harness.site_aggregate import run_trace
        trace = run_trace(self._run("r1", escalation={
            "directed_by": "jev", "jev_confidence": 0.05,
            "target_rung": 1, "condensed_context_chars": 128,
            "rungs": ["m/top"],
            "condensed_context": "SECRET-ISH failure text"}))
        self.assertEqual(trace["escalation"]["directed_by"], "jev")
        self.assertEqual(trace["escalation"]["jev_confidence"], 0.05)
        self.assertEqual(trace["escalation"]["condensed_context_chars"], 128)
        # The context itself must never reach a trace card.
        self.assertNotIn('"condensed_context":', _json.dumps(trace))

    def test_session_traces_sample_escalating_runs_newest_last(self):
        from harness.site_aggregate import session_traces
        runs = [self._run(f"r{i}") for i in range(5)] + [
            self._run(f"e{i}", escalation={"directed_by": "jev",
                                            "jev_confidence": 0.3})
            for i in range(10)]
        traces = session_traces(runs, limit=4)
        self.assertEqual(len(traces), 4)
        self.assertEqual([t["run_id"] for t in traces],
                         ["e6", "e7", "e8", "e9"],
                         "newest escalations last, cap respected")

    def test_snapshot_sessions_carry_traces(self):
        from harness.site_aggregate import build_snapshot
        runs = [self._run("e0", escalation={"directed_by": "verify_lane"})]
        snapshot = build_snapshot([{"bundle_id": "b", "runs": runs}])
        self.assertEqual(len(snapshot["sessions"][0]["traces"]), 1)
        self.assertEqual(snapshot["sessions"][0]["traces"][0]
                         ["escalation"]["directed_by"], "verify_lane")


if __name__ == "__main__":
    unittest.main()
