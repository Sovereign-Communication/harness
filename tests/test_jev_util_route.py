"""JEV-P3-route: typed route choice via one policy owner."""
import unittest

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_packs import (
    ROUTE_VOCABULARY,
    heuristic_route,
    normalize_route,
    route_question_pack,
    route_tier_hint,
)
from harness.jev_policy import policy_for
from harness.sliding_scale import classify_task_tier, resolve_sliding_scale_route
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


class _RouteTransport:
    def __init__(self, route_choice="diff", confidence=0.9, tokens=40):
        self.route_choice = route_choice
        self.confidence = confidence
        self.tokens = tokens
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        probs = {k: 0.05 for k in ROUTE_VOCABULARY}
        probs[self.route_choice] = 0.90
        return 200, {
            "model": "jev-test",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": self.route_choice,
                    "probabilities": probs,
                    "confidence": self.confidence,
                },
                "requires_iteration": {
                    "type": "noul",
                    "noul": 0.9 if self.route_choice == "frontier" else 0.1,
                },
            },
            "usage": {"input_tokens": self.tokens, "output_tokens": 2},
        }


class _RouteFallbackEvaluator:
    api_key = None
    model = "jev-latest"

    def evaluate(self, state, questions):
        return JevEvaluationResult("pass", 0.0, 1.0, {}, ["fallback"],
                                   is_fallback=True, model=self.model)


def _unkeyed():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


def _keyed(transport=None):
    settings = load_settings({"jev_api_key": "jev-key"})
    return settings, transport


def _gov():
    return SpendGovernor(
        FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
        "sk-test", max_cost=0.50)


class RoutePackVocabularyTests(unittest.TestCase):
    def test_route_pack_uses_vocabulary_not_brands(self):
        pack = route_question_pack()
        self.assertEqual(set(pack["route"]["criteria"]), set(ROUTE_VOCABULARY))
        for value in pack["route"]["criteria"]:
            self.assertNotIn("/", value)
            self.assertNotIn("deepseek", value.lower())
            self.assertNotIn("gpt", value.lower())

    def test_normalize_and_tier_hint(self):
        self.assertEqual(normalize_route("free-distill"), "free-distill")
        self.assertEqual(normalize_route("DIFF"), "diff")
        self.assertIsNone(normalize_route("deepseek/deepseek-v4.1-flash"))
        self.assertEqual(route_tier_hint("frontier"), 2)
        self.assertIsNone(route_tier_hint(None))


class EvaluateRouteTests(unittest.TestCase):
    def test_unkeyed_fallback_uses_existing_heuristic(self):
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_route(
            "refactor the algorithm loop", ["a.py", "b.py"], site="route")
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(result.answers["route"], "frontier")
        self.assertEqual(structural["site"], "route")
        self.assertEqual(result.cost, 0.0)

    def test_unkeyed_multi_file_without_iterative_is_diff(self):
        policy = policy_for(_unkeyed())
        result, _ = policy.evaluate_route(
            "update handlers", ["a.py", "b.py"], site="route")
        self.assertEqual(result.answers["route"], "diff")
        self.assertTrue(result.is_fallback)

    def test_keyed_choice_is_vocabulary_and_not_fallback(self):
        settings, transport = _keyed(_RouteTransport("diff"))
        policy = policy_for(settings, transport=transport, governor=_gov())
        result, structural = policy.evaluate_route(
            "change a handler", ["a.py"], site="route")
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.answers["route"], "diff")
        self.assertFalse(structural["is_fallback"])
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("route", transport.calls[0]["questions"])

    def test_keyed_out_of_vocabulary_falls_back_honestly(self):
        settings, transport = _keyed(_RouteTransport("gpt-6-brand"))
        # Force out-of-vocab by patching answers after a normal response shape.
        def post(url, key, payload, timeout=45):
            return 200, {
                "model": "jev-test",
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "gpt-6-brand",
                        "probabilities": {
                            "free-distill": 0.1, "diff": 0.1,
                            "frontier": 0.1, "gpt-6-brand": 0.7,
                        },
                        "confidence": 0.9,
                    },
                    "requires_iteration": {"type": "noul", "noul": 0.2},
                },
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
        transport.post = post
        policy = policy_for(settings, transport=transport, governor=_gov())
        # Probability map won't match criteria → parse fail → local fallback.
        result, structural = policy.evaluate_route("fix typo", ["a.py"])
        self.assertTrue(result.is_fallback or structural["is_fallback"])
        self.assertIn(result.answers["route"], ROUTE_VOCABULARY)


class RouteFeedSlidingScaleTests(unittest.TestCase):
    def test_keyed_route_can_raise_tier_floor(self):
        base = classify_task_tier("fix the typo", target_files=["a.py"])
        self.assertLessEqual(base.tier, 1)
        raised = classify_task_tier(
            "fix the typo", target_files=["a.py"], jev_route="frontier")
        self.assertEqual(raised.tier, 2)
        self.assertTrue(any("jev route" in r for r in raised.reasons))

    def test_unkeyed_none_keeps_existing_heuristic(self):
        base = classify_task_tier("implement a handler", target_files=["a.py"])
        with_none = classify_task_tier(
            "implement a handler", target_files=["a.py"], jev_route=None)
        self.assertEqual(base.tier, with_none.tier)

    def test_route_floor_never_cheaper_than_heuristic_when_route_is_frontier(self):
        cheap = classify_task_tier(
            "format the docstring", target_files=["a.py"], jev_route="frontier")
        self.assertEqual(cheap.tier, 2)

    def test_resolve_sliding_scale_route_accepts_jev_route(self):
        route = resolve_sliding_scale_route(
            "fix the typo", target_files=["a.py"], use_free=True,
            jev_route="frontier")
        self.assertEqual(route.classification.tier, 2)
        self.assertTrue(route.ladder)

    def test_fallback_evaluator_route_still_heuristic(self):
        settings = load_settings()
        policy = policy_for(settings, evaluator=_RouteFallbackEvaluator())
        result, structural = policy.evaluate_route("loop forever", [])
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])

    def test_heuristic_route_helper_vocab(self):
        self.assertEqual(heuristic_route("loop algorithm", None), "frontier")
        self.assertEqual(heuristic_route("update handlers", ["a.py", "b.py"]), "diff")
        self.assertEqual(heuristic_route("typo", ["a.py"]), "free-distill")


if __name__ == "__main__":
    unittest.main()
