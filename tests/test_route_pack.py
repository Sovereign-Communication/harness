"""Hermetic SITE-2 tests: the route marketplace pack and policy method.

Pins the 0-hallucination contract end to end: choice ⊆ declared rungs,
fallback honesty (unkeyed / out-of-ladder / transport fail), combo fields
bound to pack data only, and ONE ledger jev_eval per call. The hermetic
keyed evaluator follows the same doubles pattern as test_jev_issue_sort.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger
from harness.route_pack import (
    choose_rung_for_tier,
    cost_class_from_prices,
    fallback_route,
    normalize_tier,
    route_combo,
    route_question_pack,
    tier_floor_for_goal,
    tier_rank,
    validate_route_pack,
)


def sample_pack():
    return {
        "id": "route-ladder-v1",
        "rungs": [
            {"rung_id": "r0", "tier": "T0", "model": "test/free-scout:free",
             "cost_class": "free", "observed_success": 0.9, "samples": 10,
             "guidance": ["typo", "rename"],
             "notes": "mechanical edits"},
            {"rung_id": "r1", "tier": "T1", "model": "test/paid-flash",
             "cost_class": "cheap", "observed_success": 0.95, "samples": 40,
             "guidance": ["implement", "fix"],
             "notes": "structured logic"},
            {"rung_id": "r2", "tier": "T2", "model": "test/paid-reasoner",
             "cost_class": "moderate", "observed_success": 0.97, "samples": 20,
             "guidance": ["concurrency", "invariant"],
             "notes": "hard local reasoning"},
            {"rung_id": "r3", "tier": "T3", "model": "test/frontier-max",
             "cost_class": "premium",
             "guidance": ["architecture", "cross-module-protocol"],
             "notes": "frontier planning at the waist"},
        ],
    }


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


class _CountingGovernor:
    def __init__(self):
        self.reserved = []
        self.reconciled = []
        self.max_cost = 1.0
        self.spent = 0.0

    def reserve(self, worst, label):
        self.reserved.append((worst, label))
        return {"label": label, "worst": worst}

    def reconcile(self, reservation, cost):
        self.reconciled.append((reservation, cost))
        self.spent += float(cost or 0.0)

    def record_actual(self, cost, model):
        self.spent += float(cost or 0.0)


class _CountingEvaluator:
    """Hermetic keyed evaluator: returns canned answers, counts calls."""

    api_key = "jev-key"
    model = "jev-test"

    def __init__(self, answers=None, fail=False):
        self.calls = 0
        self.answers = answers or {}
        self.fail = fail

    def evaluate(self, state, questions=None):
        self.calls += 1
        if self.fail:
            raise HarnessError("transport down")
        return JevEvaluationResult(
            "pass", 0.72, 1.0, dict(self.answers), ["hermetic"],
            is_fallback=False, model=self.model)


def _unkeyed_settings():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


def _tmp_ledger():
    return AutonomyLedger(
        os.path.join(tempfile.mkdtemp(prefix="route_pack_test_"), "led.jsonl"))


def _keyed_policy(answers=None, fail=False, ledger=None):
    # Keyed calls preflight through a governor (documented contract: keyed
    # calls without one fall back honestly), so tests supply a counting one.
    settings = load_settings()
    settings.jev_api_key = "test-key"
    evaluator = _CountingEvaluator(answers=answers, fail=fail)
    policy = JevPolicy(settings, ledger=ledger or _tmp_ledger(),
                       evaluator=evaluator, governor=_CountingGovernor())
    return policy, evaluator


class PackSchemaTests(unittest.TestCase):
    def test_valid_pack_passes(self):
        self.assertEqual(validate_route_pack(sample_pack())["id"],
                         "route-ladder-v1")

    def test_rejects_defects(self):
        with self.assertRaises(ValueError):
            validate_route_pack(["nope"])
        with self.assertRaises(ValueError):
            validate_route_pack({"id": "x", "rungs": []})
        bad = sample_pack()
        bad["rungs"][1]["rung_id"] = "r0"  # duplicate
        with self.assertRaises(ValueError):
            validate_route_pack(bad)
        bad = sample_pack()
        bad["rungs"][0]["tier"] = "T9"
        with self.assertRaises(ValueError):
            validate_route_pack(bad)
        bad = sample_pack()
        del bad["rungs"][0]["cost_class"]
        with self.assertRaises(ValueError):
            validate_route_pack(bad)
        bad = sample_pack()
        bad["rungs"][0]["observed_success"] = 1.5
        with self.assertRaises(ValueError):
            validate_route_pack(bad)
        bad = sample_pack()
        bad["rungs"][0]["guidance"] = ["ok", ""]
        with self.assertRaises(ValueError):
            validate_route_pack(bad)
        bad = sample_pack()
        del bad["id"]
        with self.assertRaises(ValueError):
            validate_route_pack(bad)


class VocabularyTests(unittest.TestCase):
    def test_normalize_tier(self):
        self.assertEqual(normalize_tier("t0"), "T0")
        self.assertEqual(normalize_tier("FRONTIER"), "T3")
        self.assertEqual(normalize_tier("2"), "T2")
        self.assertIsNone(normalize_tier("warp"))

    def test_tier_rank_orders(self):
        self.assertLess(tier_rank("T0"), tier_rank("T1"))
        self.assertLess(tier_rank("T2"), tier_rank("T3"))
        self.assertGreater(tier_rank(None), tier_rank("T3"))

    def test_cost_class_buckets(self):
        self.assertEqual(cost_class_from_prices(0, 0), "free")
        self.assertEqual(cost_class_from_prices(0.1, 0.4), "cheap")
        self.assertEqual(cost_class_from_prices(1.0, 2.0), "moderate")
        self.assertEqual(cost_class_from_prices(5.0, 5.0), "expensive")
        self.assertEqual(cost_class_from_prices(15.0, 50.0), "premium")
        self.assertEqual(cost_class_from_prices(None, None), "free")

    def test_floor_heuristic_is_deterministic(self):
        self.assertEqual(tier_floor_for_goal("fix a typo in the comment"), "T0")
        self.assertEqual(tier_floor_for_goal("implement a rate limiter"), "T1")
        self.assertEqual(tier_floor_for_goal("fix the race in the token bucket"),
                         "T2")
        self.assertIn(tier_floor_for_goal(
            "design the architecture for a distributed protocol"), ("T2", "T3"))
        self.assertEqual(tier_floor_for_goal(""), "T0")
        self.assertEqual(tier_floor_for_goal(None), "T0")

    def test_choose_rung_maps_floor_onto_declared_ladder(self):
        pack = sample_pack()
        self.assertEqual(choose_rung_for_tier(pack, "T0"), "r0")
        self.assertEqual(choose_rung_for_tier(pack, "T2"), "r2")
        self.assertEqual(choose_rung_for_tier(pack, "T3"), "r3")

    def test_fallback_route_honest_when_unsatisfiable(self):
        small = {"id": "x", "rungs": [sample_pack()["rungs"][0]]}
        rung_id, tier, reasons = fallback_route("fix the race in the token bucket",
                                                small)
        self.assertIsNone(rung_id)
        self.assertEqual(tier, "T2")
        self.assertTrue(any("no declared rung" in r for r in reasons))

    def test_question_pack_criteria_are_declared_rungs_only(self):
        questions = route_question_pack(sample_pack())
        criteria = questions["rung"]["criteria"]
        self.assertEqual(set(criteria), {"r0", "r1", "r2", "r3"})
        self.assertIn("tier T0", criteria["r0"])
        self.assertIn("observed success 0.90", criteria["r0"])
        self.assertIn("do not invent rungs", questions["rung"]["instructions"])


class UnkeyedPolicyTests(unittest.TestCase):
    def test_unkeyed_routes_via_heuristic_and_ledgers_once(self):
        ledger = _tmp_ledger()
        policy = policy_for(_unkeyed_settings(), ledger=ledger)
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "fix the race in the token bucket"}, sample_pack())
        self.assertTrue(result.is_fallback)
        self.assertEqual(combo["rung_id"], "r2")
        self.assertEqual(combo["tier"], "T2")
        self.assertTrue(combo["is_fallback"])
        self.assertEqual(combo["model"], "test/paid-reasoner")
        self.assertEqual(combo["cost_class"], "moderate")
        evals = [e for e in ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0].get("site"), "model_route")

    def test_unkeyed_below_floor_is_honest(self):
        policy = policy_for(_unkeyed_settings(), ledger=_tmp_ledger())
        _, _, combo = policy.evaluate_model_route(
            {"goal": "rename this variable"}, sample_pack())
        self.assertEqual(combo["rung_id"], "r0")
        self.assertEqual(combo["tier"], "T0")

    def test_invalid_pack_fails_closed(self):
        ledger = _tmp_ledger()
        policy = policy_for(_unkeyed_settings(), ledger=ledger)
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "anything"}, {"id": "broken"})
        self.assertTrue(result.is_fallback)
        self.assertIsNone(combo["rung_id"])
        self.assertEqual(structural["site"], "model_route")


class KeyedPolicyTests(unittest.TestCase):
    def test_keyed_in_ladder_choice_wins(self):
        policy, evaluator = _keyed_policy(answers={"rung": {"choice": "r1"}})
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "implement a retry queue"}, sample_pack())
        self.assertFalse(result.is_fallback)
        self.assertEqual(combo["rung_id"], "r1")
        self.assertFalse(combo["is_fallback"])
        self.assertAlmostEqual(combo["confidence"], 0.72, places=6)
        self.assertEqual(combo["model"], "test/paid-flash")
        self.assertEqual(evaluator.calls, 1)

    def test_out_of_ladder_choice_refused_then_heuristic(self):
        policy, evaluator = _keyed_policy(
            answers={"rung": {"choice": "rung-invented"}})
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "fix a typo"}, sample_pack())
        self.assertTrue(combo["is_fallback"])
        self.assertEqual(combo["rung_id"], "r0")
        self.assertTrue(any("out-of-ladder choice refused" in r
                            for r in combo["reasons"]))
        self.assertEqual(evaluator.calls, 1)

    def test_transport_failure_falls_back(self):
        policy, _ = _keyed_policy(fail=True)
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "fix a typo"}, sample_pack())
        self.assertTrue(result.is_fallback)
        self.assertEqual(combo["rung_id"], "r0")

    def test_combo_never_invents_pack_fields(self):
        policy, _ = _keyed_policy(answers={"rung": {"choice": "r3"}})
        _, _, combo = policy.evaluate_model_route(
            {"goal": "plan the architecture"}, sample_pack())
        self.assertEqual(combo["rung_id"], "r3")
        self.assertEqual(combo["model"], "test/frontier-max")
        self.assertEqual(combo["guidance"],
                         ["architecture", "cross-module-protocol"])
        self.assertEqual(combo["pack_id"], "route-ladder-v1")


class ComboBindingTests(unittest.TestCase):
    def test_unknown_rung_id_binds_to_none(self):
        combo = route_combo("ghost", sample_pack(), tier="T1",
                            reasons=[], is_fallback=False)
        self.assertIsNone(combo["rung_id"])
        self.assertIsNone(combo["model"])


if __name__ == "__main__":
    unittest.main()
