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


class JevChoiceRecoverabilityTests(unittest.TestCase):
    """DF-JEV-3: a choice subset is recoverable; an out-of-vocabulary one is not.

    Measured 2026-09-25 over 7,279 keyed calls: the old exact-key-set reject
    discarded 4.6% of answers overall and 94.1% (48 of 51) on a single axis of
    seven criteria -- the stricter the declared vocabulary, the more reliably
    the tool paid for answers it threw away.
    """

    CRITERIA = {"type": "choice", "instructions": "bucket it",
                "criteria": {"a": "first", "b": "second", "c": "third",
                             "d": "fourth", "e": "fifth", "f": "sixth",
                             "g": "seventh"}}

    def _answer(self, probabilities, choice):
        return {"model": "jev-1.13.0", "usage": {"input_tokens": 1000,
                                                "output_tokens": 0},
                "answers": {"bucket": {"type": "choice", "choice": choice,
                                       "probabilities": probabilities,
                                       "confidence": 0.8}}}

    def test_subset_recovers_and_records_unmatched_options_as_none(self):
        result = JevEvaluator(api_key="key", transport=FakeTransport(
            response=self._answer({"d": 0.55, "e": 0.45}, "d"))).evaluate(
                {"x": 1}, {"bucket": self.CRITERIA})
        self.assertFalse(result.is_fallback)
        self.assertFalse(result.discarded)
        self.assertEqual(result.answers["bucket"]["choice"], "d")
        self.assertEqual(
            result.answers["bucket"]["unmatched_options"],
            ["a", "b", "c", "f", "g"])
        probs = result.answers["bucket"]["probabilities"]
        self.assertEqual(probs["d"], 0.55)
        # The omitted options are recorded, never invented.
        for option in ("a", "b", "c", "f", "g"):
            self.assertIsNone(probs[option])

    def test_exact_criteria_set_reports_no_unmatched_options(self):
        result = JevEvaluator(api_key="key", transport=FakeTransport(
            response=self._answer(
                {"a": 0.1, "b": 0.1, "c": 0.1, "d": 0.1, "e": 0.1, "f": 0.1,
                 "g": 0.4}, "g"))).evaluate({"x": 1}, {"bucket": self.CRITERIA})
        self.assertEqual(result.answers["bucket"]["unmatched_options"], [])

    def test_undeclared_option_is_still_fatal(self):
        result = JevEvaluator(api_key="key", transport=FakeTransport(
            response=self._answer({"d": 0.5, "e": 0.3, "zzz": 0.2}, "d"))).evaluate(
                {"x": 1}, {"bucket": self.CRITERIA})
        # A shape refusal is a hard fail that never becomes a local fallback.
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.is_fallback)
        self.assertTrue(result.discarded)
        self.assertIn("undeclared options", result.reasons[0])

    def test_subset_that_does_not_normalize_is_still_rejected(self):
        result = JevEvaluator(api_key="key", transport=FakeTransport(
            response=self._answer({"d": 0.55, "e": 0.45}, "d"))).evaluate(
                {"x": 1}, {"bucket": self.CRITERIA})
        # Sanity: the recoverable path above really does sum to one.
        probs = result.answers["bucket"]["probabilities"]
        self.assertAlmostEqual(sum(v for v in probs.values() if v is not None), 1.0)
        bad = JevEvaluator(api_key="key", transport=FakeTransport(
            response=self._answer({"d": 0.55, "e": 0.2}, "d"))).evaluate(
                {"x": 1}, {"bucket": self.CRITERIA})
        self.assertEqual(bad.verdict, "fail")
        self.assertFalse(bad.is_fallback)
        self.assertIn("must sum to 1", bad.reasons[0])

    def test_billed_but_unusable_response_is_marked_discarded_and_still_settled(self):
        response = self._answer({"d": 0.55, "e": 0.45}, "d")
        response["answers"]["bucket"]["probabilities"] = {"d": 0.5, "e": 0.9}
        result = JevEvaluator(api_key="key", transport=FakeTransport(
            response=response)).evaluate({"x": 1}, {"bucket": self.CRITERIA})
        self.assertEqual(result.verdict, "fail")
        # The provider billed it, so the spend is settled honestly...
        self.assertEqual(result.input_tokens, 1000)
        self.assertAlmostEqual(result.cost, 1000 * 0.0042 / 1_000_000)
        # ...and the loss is named instead of degrading silently.
        self.assertTrue(result.discarded)

    def test_unbilled_failure_is_not_reported_as_discarded(self):
        result = JevEvaluator(api_key="key", transport=FakeTransport(
            status=500, response={})).evaluate({"x": 1}, {"bucket": self.CRITERIA})
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.discarded)
        self.assertEqual(result.cost, 0.0)


if __name__ == "__main__":
    unittest.main()
