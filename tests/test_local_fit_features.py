"""C3 tests: shared feature builder, extractor/dispatch parity pin, vector pin.

The parity pin is the train/serve skew guard: it proves the extractor's
feature dict and the dispatch-side builder's feature dict are produced by one
implementation and are identical for equivalent seats. If either path changes
shape without the other, these tests fail before a skewed model can ship.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class _FakeProfile:
    """Duck-typed CapabilityProfile surface for the dispatch builder."""

    def __init__(self, context_length=128000, free=True,
                 supports_structured_json=True, supports_json_schema=False,
                 supports_response_format=False, supports_reasoning=True):
        self.model_id = "fake/model"
        self.context_length = context_length
        self.free = free
        self.supports_structured_json = supports_structured_json
        self.supports_json_schema = supports_json_schema
        self.supports_response_format = supports_response_format
        self.supports_reasoning = supports_reasoning


OBSERVED = {
    "acme/model-a": {
        "usable_rate": 0.8, "truncation_rate": 0.1, "unusable_rate": 0.1,
        "mean_resp_chars": 500.0, "median_resp_chars": 450.0,
        "max_resp_chars": 900.0, "n": 10,
        "context_length": 100000, "declared_structured_json": True,
        "declared_reasoning": False, "free_tier": True,
    }
}

OVERRIDES = {
    "seat_role": "panel",
    "task_type": "structured_claims",
    "structured_output_required": True,
    "max_tokens_requested": 4096,
    "reasoning_effort": "low",
    "prompt_chars": 4000,
    "source_window_attached": True,
    "claims_count": 5,
    "convergence_expected": True,
    "is_iterative": False,
    "declared_context_length": 100000,
    "declared_structured_json": True,
    "declared_reasoning": False,
}


class TestExtractorDispatchParityPin(unittest.TestCase):
    def test_extract_and_dispatch_build_identical_dicts(self):
        """THE PIN: extractor-side and dispatch-side dicts must be identical."""
        from harness.local_fit import extract, features

        feats_extract = extract._build_features(
            task_type="structured_claims", seat_role="panel",
            model="acme/model-a", observed=OBSERVED,
            prompt_chars=4000, max_tokens_requested=4096,
            reasoning_effort="low", source_window_attached=True,
            claims_count=5, convergence_expected=True, is_iterative=False,
            # The caller supplies the label-consistent value: recomputing
            # it from task_type alone diverges on convergence runs
            # without a claims payload.
            structured_required=True,
        )
        feats_dispatch = features.build_dispatch_features(
            "acme/model-a", task="structured", free_tier=True,
            call_lane="panel", observed={
                "usable_rate": 0.8, "truncation_rate": 0.1,
                "unusable_rate": 0.1, "mean_resp_chars": 500.0,
                "median_resp_chars": 450.0, "max_resp_chars": 900.0,
                "n": 10},
            _extract_overrides=dict(OVERRIDES),
        )
        self.assertEqual(feats_extract, feats_dispatch,
                         "extractor and dispatch feature builders drifted")

    def test_feature_dict_keys_exactly_match_schema(self):
        from harness.local_fit import extract
        from harness.local_fit.schema import FEATURE_ORDER

        feats = extract._build_features(
            task_type="structured_claims", seat_role="panel",
            model="acme/model-a", observed=OBSERVED,
            prompt_chars=4000, max_tokens_requested=4096,
            reasoning_effort="low", source_window_attached=True,
            claims_count=5, convergence_expected=True, is_iterative=False,
        )
        self.assertEqual(set(feats.keys()), set(FEATURE_ORDER))

    def test_hash_matches_legacy_extractor_hash(self):
        """The shared hash must equal the extractor's historical 31-multi hash."""
        from harness.local_fit.features import hash_model_id

        h = 0
        for ch in "acme/model-a":
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        self.assertEqual(hash_model_id("acme/model-a"), h)


class TestDispatchFeatureBuilder(unittest.TestCase):
    def test_public_dispatch_call_shape(self):
        from harness.local_fit.features import build_dispatch_features
        from harness.local_fit.schema import FEATURE_ORDER

        feats = build_dispatch_features(
            "acme/model-a", task="structured", free_tier=True,
            profile=_FakeProfile(),
            calibration={"samples": 12, "success_rate": 0.9,
                         "unusable_outputs": 1, "consent_unusable": 0},
            call_lane="apply",
        )
        self.assertEqual(set(feats.keys()), set(FEATURE_ORDER))
        self.assertEqual(feats["task_type"], "structured_claims")
        self.assertEqual(feats["seat_role"], "apply")
        self.assertTrue(feats["structured_output_required"])
        self.assertTrue(feats["declared_structured_json"])
        self.assertTrue(feats["declared_reasoning"])
        self.assertEqual(feats["declared_context_length"], 128000)
        self.assertTrue(feats["free_tier"])
        self.assertAlmostEqual(feats["observed_usable_rate"], 0.9)
        self.assertAlmostEqual(feats["observed_unusable_rate"], 1 / 12)
        self.assertEqual(feats["observed_sample_count"], 12)
        # structured task but declared JSON -> no need-vs-declared flag
        self.assertEqual(feats["structured_need_vs_declared_json"], 0)

    def test_vector_length_from_dispatch_features(self):
        from harness.local_fit.features import (
            build_dispatch_features, build_feature_vector, expected_vector_length)

        feats = build_dispatch_features("m", task="code", free_tier=False)
        vec = build_feature_vector(feats, {})
        self.assertEqual(len(vec), expected_vector_length())
        # 6 task_type + 5 seat_role + 5 reasoning_effort one-hots + 23 numerics
        self.assertEqual(expected_vector_length(), 39)

    def test_calibration_missing_defaults(self):
        from harness.local_fit.features import build_dispatch_features

        feats = build_dispatch_features("m", task="default", free_tier=False,
                                        calibration=None)
        self.assertEqual(feats["observed_usable_rate"], 0.0)
        self.assertEqual(feats["observed_sample_count"], 0)

    def test_lane_and_task_mapping(self):
        from harness.local_fit.features import build_dispatch_features

        self.assertEqual(
            build_dispatch_features("m", task="default", call_lane="panel")["task_type"],
            "verify_panel")
        self.assertEqual(
            build_dispatch_features("m", task="code", call_lane="apply")["task_type"],
            "apply")
        self.assertEqual(
            build_dispatch_features("m", task="default", call_lane="panel")["seat_role"],
            "panel")
        self.assertEqual(
            build_dispatch_features("m", task="default", call_lane="apply")["seat_role"],
            "apply")

    def test_profile_free_overrides_tier_flag(self):
        from harness.local_fit.features import build_dispatch_features

        feats = build_dispatch_features("m", task="code", free_tier=True,
                                        profile=_FakeProfile(free=False))
        self.assertFalse(feats["free_tier"])


class TestSharedMath(unittest.TestCase):
    def test_safe_div(self):
        from harness.local_fit.features import safe_div
        self.assertEqual(safe_div(1.0, 0), 0.0)
        self.assertEqual(safe_div(1.0, 2), 0.5)

    def test_prompt_chars_estimate(self):
        from harness.local_fit.features import prompt_chars_estimate
        self.assertEqual(prompt_chars_estimate(0), 1)
        self.assertEqual(prompt_chars_estimate(400), 100)


class TestNoNetworkFeatures(unittest.TestCase):
    def test_features_module_has_no_network_imports(self):
        import harness.local_fit.features as f
        with open(f.__file__, encoding="utf-8") as f:
            source = f.read().lower()
        banned = ["requests", "openai", "anthropic", "httpx", "aiohttp",
                  "urllib.request", "websocket", "socket"]
        for b in banned:
            self.assertNotIn(b, source, f"features.py should not reference {b}")


if __name__ == "__main__":
    unittest.main()
