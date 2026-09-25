"""Hermetic JEV-P0 contract tests."""
import unittest
from unittest import mock

from harness.config import load_settings
from harness.jev import JevEvaluator, jev_cost


class FakeTransport:
    def __init__(self, status=200, response=None):
        self.status, self.response, self.calls = status, response or {}, []

    def post(self, url, key, payload):
        self.calls.append((url, key, payload))
        return self.status, self.response


def live_response(answers=None, input_tokens=100, output_tokens=20):
    return {"model": "jev-1.13.0", "answers": answers or {
        "supported": {"type": "noul", "noul": 0.94},
        "confidence": {"type": "score", "score": 2.7,
                        "legend": {"0": "low", "1": "medium", "2": "high"},
                        "probabilities": {"0": 0.01, "1": 0.04, "2": 0.95},
                        "confidence": 0.91},
    }, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


class JevP0Tests(unittest.TestCase):
    def test_cost_is_input_only_and_exposed(self):
        self.assertEqual(jev_cost(1_000_000), 0.0042)
        self.assertEqual(jev_cost(0), 0.0)
        transport = FakeTransport(response=live_response(input_tokens=250))
        result = JevEvaluator(api_key="key", transport=transport).evaluate(
            {"x": 1}, {"supported": {"type": "noul", "instructions": "Is x valid?"},
                       "confidence": {"type": "score", "instructions": "Rate x", "criteria": ["low", "high"]}})
        self.assertEqual(result.input_tokens, 250)
        self.assertEqual(result.output_tokens, 20)
        self.assertAlmostEqual(result.cost, 250 * 0.0042 / 1_000_000)

    def test_official_shapes_parse_and_noul_is_not_confidence(self):
        result = JevEvaluator()._parse_jev_response(live_response(), {
            "supported": {"type": "noul", "instructions": "supported?"},
            "confidence": {"type": "score", "instructions": "rate", "criteria": ["low", "high"]},
        })
        self.assertEqual(result.supported, 0.94)
        self.assertEqual(result.confidence, 0.91)
        self.assertNotIn("confidence", result.answers["supported"])
        self.assertIn("probabilities", result.answers["confidence"])

    def test_missing_fields_never_default_pass(self):
        evaluator = JevEvaluator(api_key="key", transport=FakeTransport(response={
            "answers": {"supported": {"type": "noul", "noul": 0.99}},
            "usage": {"input_tokens": 10, "output_tokens": 2}}))
        result = evaluator.evaluate({}, {"supported": {"type": "noul", "instructions": "valid?"},
                                         "syntax_clean": {"type": "noul", "instructions": "parses?"}})
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.is_fallback)
        self.assertIn("missing answer", result.reasons[0])

    def test_non_official_answer_shapes_are_rejected(self):
        with self.assertRaises(ValueError):
            JevEvaluator()._parse_jev_response({
                "answers": {"q": True}, "usage": {"input_tokens": 1, "output_tokens": 1}},
                {"q": {"type": "noul", "instructions": "valid?"}})

    def test_empty_question_map_is_not_replaced_by_a_default_pack(self):
        result = JevEvaluator().evaluate({"code": "x = 1"}, questions={})
        self.assertEqual(result.verdict, "fail")
        self.assertIn("non-empty", result.reasons[0])

    def test_empty_diff_boundary_fails_without_raising(self):
        result = JevEvaluator().verify_diff_mechanics(None, None, None)
        self.assertEqual(result.verdict, "fail")
        self.assertTrue(result.is_fallback)

    def test_non_finite_answer_values_are_rejected(self):
        with self.assertRaises(ValueError):
            JevEvaluator()._parse_jev_response({
                "answers": {"q": {"type": "noul", "noul": float("nan")}},
                "usage": {"input_tokens": 1, "output_tokens": 1}},
                {"q": {"type": "noul", "instructions": "valid?"}})

    def test_live_threshold_uses_settings(self):
        settings = load_settings({"jev_api_key": "key", "min_confidence": 0.95})
        transport = FakeTransport(response=live_response())
        result = JevEvaluator(settings=settings, transport=transport).evaluate(
            {}, {"supported": {"type": "noul", "instructions": "supported?"},
                 "confidence": {"type": "score", "instructions": "rate", "criteria": ["low", "high"]}})
        self.assertEqual(result.supported, 0.94)
        self.assertEqual(result.confidence, 0.91)
        self.assertEqual(result.verdict, "fail")

    def test_unkeyed_local_fallback_and_keyed_rejection(self):
        local = JevEvaluator().evaluate({"code": "x = 1\n"})
        self.assertTrue(local.is_fallback)
        for status in (401, 422):
            rejected = JevEvaluator(api_key="key", transport=FakeTransport(status=status, response={})).evaluate({"code": "x = 1"})
            self.assertFalse(rejected.is_fallback)
            self.assertEqual(rejected.verdict, "fail")

    def test_local_diff_rejects_noop_and_empty_hunks(self):
        evaluator = JevEvaluator()
        for diff in (
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n context\n",
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n",
        ):
            result = evaluator.verify_diff_mechanics(diff, "change it", "foo.py")
            self.assertEqual(result.verdict, "fail")
            self.assertTrue(any("diff change" in reason or "hunk shape" in reason
                                for reason in result.reasons))

    def test_malformed_live_answer_keeps_valid_usage_cost(self):
        response = {"answers": {"supported": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 250, "output_tokens": 7}}
        result = JevEvaluator(api_key="key", transport=FakeTransport(response=response)).evaluate(
            {}, {"supported": {"type": "noul", "instructions": "supported?"},
                 "other": {"type": "noul", "instructions": "other?"}})
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.input_tokens, 250)
        self.assertEqual(result.output_tokens, 7)
        self.assertAlmostEqual(result.cost, 250 * 0.0042 / 1_000_000)

    def test_candidate_ast_fact_is_computed_at_agent_boundary(self):
        evaluator = JevEvaluator()
        diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old()\n+new()\n"
        self.assertEqual(evaluator.verify_diff_mechanics(
            diff, "change it", "foo.py", candidate="def broken(:\n").verdict, "fail")
        self.assertEqual(evaluator.verify_diff_mechanics(
            diff, "change it", "foo.py", candidate="def ok():\n    return 1\n").verdict, "pass")

    def test_keyed_diff_entry_point_calls_only_answerable_semantics(self):
        diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old()\n+new()\n"
        transport = FakeTransport(response={
            "model": "jev-1.13.0",
            "answers": {"instruction_matches": {"type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 5},
        })
        result = JevEvaluator(api_key="key", transport=transport).verify_diff_mechanics(
            diff, "rename old to new", "foo.py")
        self.assertEqual(result.verdict, "pass")
        self.assertFalse(result.is_fallback)
        self.assertEqual(set(transport.calls[0][2]["questions"]), {"instruction_matches"})

    def test_keyed_diff_mechanical_failure_does_not_spend_or_call(self):
        transport = FakeTransport(response={})
        result = JevEvaluator(api_key="key", transport=transport).verify_diff_mechanics(
            "", "rename old to new", "foo.py")
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(transport.calls, [])

    def test_model_setting_and_endpoint(self):
        settings = load_settings({"jev_api_key": "key", "jev_model": "jev-1.13.0",
                                  "jev_endpoint": "https://custom/v1"})
        transport = FakeTransport(response=live_response())
        JevEvaluator(settings=settings, transport=transport).evaluate({"x": 1}, {"supported": {"type": "noul", "instructions": "x?"}, "confidence": {"type": "score", "instructions": "rate", "criteria": ["a", "b"]}})
        self.assertEqual(transport.calls[0][0], "https://custom/v1")
        self.assertEqual(transport.calls[0][2]["model"], "jev-1.13.0")

    def test_resolve_jev_key_from_env_file(self):
        import os
        import tempfile
        from harness.config import resolve_jev_key
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "jev.env"), "w", encoding="utf-8") as handle:
                handle.write("HARNESS_JEV_KEY=test-key\n")
            with mock.patch("harness.config.CONFIG_DIR", td), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_jev_key(), "test-key")


if __name__ == "__main__":
    unittest.main()
