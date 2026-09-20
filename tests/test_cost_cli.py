"""Tests for harness cost CLI and cost analytics."""
import json
import os
import tempfile
import unittest
from harness.cli import main
from harness.ledger import AutonomyLedger


class CostCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        self.ledger = AutonomyLedger(self.ledger_path)

        # Seed realistic entries across tiers
        # T0: Free
        self.ledger.append("model_result", model="deepseek/deepseek-chat:free", cost=0.0)
        # T1: Ultra-cheap
        self.ledger.append("model_result", model="deepseek/deepseek-chat", cost=0.0002)
        # T2: Mid-tier
        self.ledger.append("model_result", model="google/gemini-2.5-flash", cost=0.0015)
        # T3: Frontier
        self.ledger.append("model_result", model="qwen/qwen3.8-max-0902", cost=0.012)


    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_report_structure(self):
        report = self.ledger.cost_report()
        self.assertIn("total_cost", report)
        self.assertIn("by_tier", report)
        self.assertIn("by_model", report)
        self.assertIn("savings", report)

        self.assertAlmostEqual(report["total_cost"], 0.0137, places=4)
        self.assertEqual(report["by_tier"]["T0"]["calls"], 1)
        self.assertEqual(report["by_tier"]["T1"]["calls"], 1)
        self.assertEqual(report["by_tier"]["T2"]["calls"], 1)
        self.assertEqual(report["by_tier"]["T3"]["calls"], 1)

        savings = report["savings"]
        self.assertGreater(savings["net_savings"], 0.0)
        self.assertGreater(savings["savings_percent"], 50.0)

    def test_cost_report_filtered_flags(self):
        rep_tier = self.ledger.cost_report(by_tier=True)
        self.assertIn("by_tier", rep_tier)
        self.assertNotIn("by_model", rep_tier)
        self.assertNotIn("savings", rep_tier)

        rep_model = self.ledger.cost_report(by_model=True)
        self.assertIn("by_model", rep_model)
        self.assertNotIn("by_tier", rep_model)

        rep_savings = self.ledger.cost_report(savings=True)
        self.assertIn("savings", rep_savings)
        self.assertNotIn("by_model", rep_savings)

    def test_cost_report_window_count(self):
        rep_last_2 = self.ledger.cost_report(window="2")
        self.assertEqual(rep_last_2["events_count"], 2)

    def test_cost_report_invalid_window(self):
        rep_invalid = self.ledger.cost_report(window="not-a-number")
        self.assertGreaterEqual(rep_invalid["events_count"], 4)


    def test_cost_report_time_windows(self):
        # Test all window suffixes (h, d, m, s)
        rep_h = self.ledger.cost_report(window="24h")
        self.assertEqual(rep_h["events_count"], 4)
        rep_d = self.ledger.cost_report(window="7d")
        self.assertEqual(rep_d["events_count"], 4)
        rep_m = self.ledger.cost_report(window="60m")
        self.assertEqual(rep_m["events_count"], 4)
        rep_s = self.ledger.cost_report(window="3600s")
        self.assertEqual(rep_s["events_count"], 4)

    def test_cli_cost_command_json_out(self):
        out_file = os.path.join(self.tmp.name, "cost.json")
        env_backup = dict(os.environ)
        os.environ["HARNESS_LEDGER_PATH"] = self.ledger_path
        try:
            main(["cost", "--out", out_file, "--json"])
            self.assertTrue(os.path.exists(out_file))
            with open(out_file, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("total_cost", data)
            self.assertIn("by_tier", data)
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_cli_cost_command_pretty(self):
        out_file = os.path.join(self.tmp.name, "cost_pretty.json")
        env_backup = dict(os.environ)
        os.environ["HARNESS_LEDGER_PATH"] = self.ledger_path
        try:
            main(["cost", "--out", out_file, "--by-tier", "--by-model", "--savings"])
            self.assertTrue(os.path.exists(out_file))
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_cost_report_edge_cases(self):
        # Edge cases for coverage: bad cost, no ts, naive ts, bad ts
        self.ledger._tail.append({"event": "model_result", "model": "edge/no-ts"})
        self.ledger._tail.append({"event": "model_result", "model": "edge/naive-ts", "ts": "2026-09-20T00:00:00"})
        self.ledger._tail.append({"event": "model_result", "model": "edge/bad-ts", "ts": "not-iso-format"})
        self.ledger._tail.append({"event": "model_result", "model": "edge/bad-cost", "cost": "unparseable"})
        rep = self.ledger.cost_report(window="24h")
        self.assertGreaterEqual(rep["events_count"], 1)




if __name__ == "__main__":
    unittest.main()
