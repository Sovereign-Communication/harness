"""Regressions for backlog items #15, #16, and #20 (property tests).

All hermetic: no network, no key.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.apply import READY_MARKER, _apply_unified_diff, _parse_ready
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


class ReadyParserSovereigntyTests(unittest.TestCase):
    """Playtest pass: a defer declared anywhere in the response must be
    honored -- the 5-line window silently applied edits the model tried to
    defer (diff bodies push the marker past the old window)."""

    def test_trailing_defer_after_diff_body_is_honored(self):
        body = "@@ -1,2 +1,2 @@\n-alpha\n+beta\n" + READY_MARKER + " defer cannot match\n"
        decision, reason, rest = _parse_ready(body)
        self.assertEqual(decision, "defer")
        self.assertIn("beta", rest)
        self.assertNotIn("READY", rest)

    def test_hedged_response_defers_conservatively(self):
        body = READY_MARKER + " confident\nbody\n" + READY_MARKER + " defer unsure\n"
        decision, _, _ = _parse_ready(body)
        self.assertEqual(decision, "defer")

    def test_first_line_confident_still_works(self):
        decision, _, rest = _parse_ready(READY_MARKER + " confident\nfile body\n")
        self.assertEqual(decision, "confident")
        self.assertEqual(rest, "file body\n")

    def test_missing_marker_stays_missing(self):
        decision, _, rest = _parse_ready("plain text\n")
        self.assertEqual(decision, "missing")
        self.assertEqual(rest, "plain text\n")


class UnifiedDiffEngineTests(unittest.TestCase):
    """Playtest pass: the #11 strict diff engine, driven exactly as models
    feed it (prose-wrapped diffs, truncations, zero-context hunks)."""

    SRC = "line1\nline2\nline3\nline4\nline5\n"

    def test_happy_path(self):
        diff = ("--- a/f\n+++ b/f\n@@ -2,3 +2,4 @@\n line2\n-line3\n"
                "+line3 patched\n line4\n+line4b\n")
        self.assertEqual(_apply_unified_diff(self.SRC, diff),
                         "line1\nline2\nline3 patched\nline4\nline4b\nline5\n")

    def test_context_mismatch_refused(self):
        diff = "@@ -1,2 +1,2 @@\n-LINE1\n+x\n line2\n"
        with self.assertRaises(Exception):
            _apply_unified_diff(self.SRC, diff)

    def test_truncated_hunk_refused(self):
        with self.assertRaises(Exception):
            _apply_unified_diff(self.SRC, "@@ -1,3 +1,1 @@\n line1\n")

    def test_prose_only_refused(self):
        with self.assertRaises(Exception):
            _apply_unified_diff(self.SRC, "I made some changes, looks great!")

    def test_zero_context_insertion(self):
        out = _apply_unified_diff(self.SRC, "@@ -0,0 +1,2 @@\n+new top\n+new top2\n")
        self.assertTrue(out.startswith("new top\nnew top2\n"))


class ApplyConsentOptionalTests(unittest.TestCase):
    """Playtest pass: apply with require_consent=False crashed with
    UnboundLocalError when the renewal ladder read the never-created consent
    result; renewal must tolerate an absent initial probe."""

    def test_apply_without_consent_does_not_crash_on_renewal(self):
        from tests._fake import FakeTransport, m, comp
        from harness.core import SpendGovernor
        from harness.ledger import AutonomyLedger
        from harness.router import Router
        from harness.apply import ApplyEngine
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "f.txt")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("def a():\n    return 1\n")
            body = ("@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 10\n"
                    "\n" + READY_MARKER + " confident\n")
            fake = FakeTransport(
                models=[m("m/apply"), m("m/judge")],
                posts=[comp('{"decision": "accept", "reason": "ok", '
                            '"redirect_model": null, "scope_suggestion": null}'),
                       comp(body)])
            gov = SpendGovernor(fake, "sk-test")
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            engine = ApplyEngine(fake, "k", gov, ledger, Router(["a"], "m/judge", "m/apply"),
                                 default_require_consent=False, default_renew_consent=True)
            r = engine.apply_edit(task_id="t", file_path=fp, instruction="bump",
                                  backend="diff", require_consent=False)
            self.assertEqual(r["status"], "ok")
            self.assertIn("return 10", open(fp, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
