"""Regressions for backlog items #15, #16, and #20 (property tests).

All hermetic: no network, no key.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.config import load_settings, DEFAULT_MAX_TOKENS
from harness.capability import (
    CAPABILITIES_SCHEMA_VERSION, load_profiles, save_profiles,
    build_profiles_from_models,
)
from harness.core import SpendGovernor, estimate_prompt_tokens
from harness.errors import HarnessError


class ConfigRangeValidationTests(unittest.TestCase):
    """#15: nonsense numeric config must fail closed, not silently apply."""

    def test_out_of_range_max_tokens_raises(self):
        with mock.patch.dict(os.environ, {"HARNESS_MAX_TOKENS": "10"}):
            with self.assertRaises(HarnessError):
                load_settings()

    def test_out_of_range_max_cost_raises(self):
        with mock.patch.dict(os.environ, {"HARNESS_MAX_COST": "-1"}):
            with self.assertRaises(HarnessError):
                load_settings()

    def test_out_of_range_panelists_raises(self):
        with mock.patch.dict(os.environ, {"HARNESS_MAX_PANELISTS": "99"}):
            with self.assertRaises(HarnessError):
                load_settings()

    def test_valid_ranges_pass(self):
        with mock.patch.dict(os.environ, {"HARNESS_MAX_TOKENS": str(DEFAULT_MAX_TOKENS)}):
            s = load_settings()
        self.assertEqual(s.max_tokens, DEFAULT_MAX_TOKENS)

    def test_unknown_config_key_warns(self):
        """#15: an unknown key in config.json must warn on stderr, not vanish."""
        import io
        import harness.config as cfg
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"max_cost": 0.5, "not_a_real_key": 1}, f)
            with mock.patch.object(cfg, "CONFIG_DIR", d), \
                 mock.patch.object(cfg.sys, "stderr", new=io.StringIO()) as err:
                s = cfg.load_settings()
            self.assertIn("not_a_real_key", err.getvalue())
            self.assertEqual(s.max_cost, 0.5)


class CapabilitiesSchemaVersionTests(unittest.TestCase):
    """#15: a foreign/older capabilities.json is treated as stale, not truth."""

    def _models(self):
        return [{"id": "test/free-model",
                 "pricing": {"prompt": "0", "completion": "0"},
                 "context_length": 4096,
                 "architecture": {"modality": "text->text"}}]

    def test_save_stamps_schema_version(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "capabilities.json")
            profiles = build_profiles_from_models(self._models())
            save_profiles(path, profiles, fetched_at=123.0)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["schema_version"], CAPABILITIES_SCHEMA_VERSION)
            loaded, fetched = load_profiles(path)
            self.assertIn("test/free-model", loaded)
            self.assertEqual(fetched, 123.0)

    def test_wrong_schema_version_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "capabilities.json")
            profiles = build_profiles_from_models(self._models())
            save_profiles(path, profiles, fetched_at=123.0)
            with open(path, "r+", encoding="utf-8") as f:
                data = json.load(f)
                data["schema_version"] = 999
                f.seek(0)
                json.dump(data, f)
                f.truncate()
            loaded, fetched = load_profiles(path)
            self.assertEqual(loaded, {})
            self.assertIsNone(fetched)


class CostByModelTests(unittest.TestCase):
    """#16: the governor tracks per-model spend and never hides it."""

    def _gov(self, **kw):
        transport = mock.Mock()
        with mock.patch.object(SpendGovernor, "verify_key", return_value=None):
            return SpendGovernor(transport, "key", **kw)

    def test_costs_aggregate_per_model(self):
        gov = self._gov(max_cost=1.0)
        gov.record_actual(0.01, "panelist-a")
        gov.record_actual(0.02, "panelist-a")
        gov.record_actual(0.005, "judge")
        self.assertEqual(gov.cost_by_model(), {"panelist-a": 0.03, "judge": 0.005})
        self.assertAlmostEqual(gov.spent, 0.035)

    def test_ceiling_still_enforced(self):
        gov = self._gov(max_cost=0.01)
        gov.record_actual(0.009, "a")
        with self.assertRaises(HarnessError):
            gov.record_actual(0.01, "b")


class TokenEstimatePropertyTests(unittest.TestCase):
    """#7/#20: the estimator must be within sane bounds and never undercount
    below chars/4 (the industry-standard char/token ratio)."""

    def test_never_below_chars_over_4(self):
        samples = [
            "hello world",
            "a" * 1000,
            "def foo(bar):\n    return bar * 2\n",
            json.dumps({"claims": [{"id": f"c{i}", "real": False} for i in range(50)]}),
            "",
        ]
        for s in samples:
            est = estimate_prompt_tokens(s)
            self.assertGreaterEqual(est, len(s) // 4, msg=f"undercount for {s[:40]!r}")

    def test_scales_monotonically(self):
        short = estimate_prompt_tokens("x" * 100)
        long = estimate_prompt_tokens("x" * 10000)
        self.assertGreater(long, short)


if __name__ == "__main__":
    unittest.main()
