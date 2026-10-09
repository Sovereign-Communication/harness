"""Hermetic Jev core hardening: cache, fallback ledger bounds, reasons,
settlement, confidence-aware routing, parallel fan-out and the site breaker.

Every transport here is a counting stub; no test touches the network, a real
key, the operator ledger or HOME.
"""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from harness._http import HttpTransport
from harness.config import load_settings
from harness.errors import HarnessError, ToolCancelled
from harness.events import (add_sink, current_cancel_check, emit, remove_sink,
                            task_context)
from harness.jev import (CircuitBreakers, JevCache, JevEvaluator,
                         PROCESS_CACHE, jev_cost)
from harness.jev_policy import (BREAKER_FAILURE_THRESHOLD,
                                FALLBACK_DISTINCT_ROW_CAP,
                                UNATTRIBUTED_FALLBACK, policy_for)
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
PACK = {"id": "ladder-v1", "rungs": [
    {"rung_id": "r0", "tier": "T0", "model": "m-t0", "cost_class": "free"},
    {"rung_id": "r1", "tier": "T1", "model": "m-t1", "cost_class": "cheap"},
    {"rung_id": "r2", "tier": "T2", "model": "m-t2", "cost_class": "moderate"},
]}
LOW_PACK = {"id": "low-v1", "rungs": [
    {"rung_id": "only", "tier": "T0", "model": "m-t0", "cost_class": "free"}]}


def noul_resp(tokens=100, noul=0.95):
    return {"model": "jev-test", "usage": {"input_tokens": tokens,
                                           "output_tokens": 3},
            "answers": {"instruction_matches": {"type": "noul", "noul": noul}}}


def choice_resp(choice, confidence, criteria, tokens=100):
    probs = {k: (1.0 if k == choice else 0.0) for k in criteria}
    return {"model": "jev-test", "usage": {"input_tokens": tokens,
                                           "output_tokens": 3},
            "answers": {"rung": {"type": "choice", "choice": choice,
                                 "probabilities": probs,
                                 "confidence": confidence}}}


def answers_for(questions, *, choice=None, confidence=0.9):
    """A well-formed answer for any pack the policy might send."""
    out = {}
    for name, question in questions.items():
        kind = question["type"]
        if kind == "noul":
            out[name] = {"type": "noul", "noul": 0.9}
        elif kind == "choice":
            criteria = list(question["criteria"])
            pick = choice if choice in criteria else criteria[0]
            out[name] = {"type": "choice", "choice": pick,
                         "probabilities": {k: float(k == pick) for k in criteria},
                         "confidence": confidence}
        else:
            levels = question["criteria"]
            out[name] = {"type": "score", "score": 0.0,
                         "legend": {str(i): lv for i, lv in enumerate(levels)},
                         "probabilities": {str(i): float(i == 0)
                                           for i in range(len(levels))},
                         "confidence": confidence}
    return out


class CountingTransport:
    """Counts requests; ``plan`` is a list of results, then ``default``."""

    def __init__(self, default=None, plan=(), delay=None):
        self.default = default
        self.plan = list(plan)
        self.calls = []
        self.delay = delay or {}
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def post(self, url, key, payload, timeout=45):
        with self._lock:
            self.calls.append(payload)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            item = self.plan.pop(0) if self.plan else self.default
        try:
            wait = self.delay.get(tuple(sorted(payload["questions"])), 0.0)
            if wait:
                time.sleep(wait)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = item(payload)
            if isinstance(item, tuple):
                return item
            return 200, item
        finally:
            with self._lock:
                self.in_flight -= 1


class _Base(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = mock.patch.dict(os.environ, {"HOME": home.name,
                                           "USERPROFILE": home.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HARNESS_JEV_DISABLE", None)
        os.environ.pop("HARNESS_JEV_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))

    def keyed_settings(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        settings.hourglass_confirm = False
        return settings

    def unkeyed_settings(self):
        settings = load_settings()
        settings.jev_api_key = None
        return settings

    def governor(self, max_cost=0.10):
        return SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=max_cost)

    def keyed(self, transport, **kw):
        kw.setdefault("governor", self.governor())
        kw.setdefault("ledger", self.ledger)
        return policy_for(self.keyed_settings(), transport=transport, **kw)

    def rows(self):
        return [e for e in self.ledger.entries() if e["event"] == "jev_eval"]


class CacheTests(_Base):
    def test_identical_call_is_served_without_a_request(self):
        transport = CountingTransport(noul_resp(tokens=100))
        gov = self.governor()
        policy = self.keyed(transport, governor=gov)
        first, _ = policy.evaluate_diff(DIFF, "change x", "x.py", site="apply")
        spent_after_first = gov.spent
        second, structural = policy.evaluate_diff(
            DIFF, "change x", "x.py", site="apply")
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertFalse(second.is_fallback)
        self.assertEqual(second.cost, 0.0)
        self.assertEqual(second.input_tokens, 0)
        self.assertEqual(second.verdict, first.verdict)
        self.assertEqual(gov.spent, spent_after_first)
        self.assertTrue(structural["cache_hit"])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("cache_hit", rows[0])
        self.assertTrue(rows[1]["cache_hit"])
        self.assertEqual(rows[1]["cost"], 0.0)
        self.assertIn("cache hit", rows[1]["note"])

    def test_different_state_or_questions_miss(self):
        transport = CountingTransport(noul_resp())
        policy = self.keyed(transport)
        policy.evaluate_diff(DIFF, "change x", "x.py")
        policy.evaluate_diff(DIFF, "a different instruction", "x.py")
        self.assertEqual(len(transport.calls), 2)

    def test_transport_failure_is_never_cached(self):
        transport = CountingTransport(
            noul_resp(), plan=[OSError("offline")])
        evaluator = JevEvaluator(api_key="k", transport=transport)
        failed = evaluator.evaluate({"instruction": "x"})
        self.assertTrue(failed.is_fallback)
        self.assertEqual(failed.fallback_reason, "transport_failure")
        ok = evaluator.evaluate({"instruction": "x"})
        self.assertFalse(ok.is_fallback)
        self.assertFalse(ok.cache_hit)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(evaluator.cache), 1)

    def test_http_error_and_invalid_response_are_never_cached(self):
        bad = {"model": "j", "usage": {"input_tokens": 50, "output_tokens": 1},
               "answers": {"instruction_matches": {"type": "noul", "noul": 7}}}
        transport = CountingTransport(
            noul_resp(), plan=[(500, {}), (401, {}), bad])
        evaluator = JevEvaluator(api_key="k", transport=transport)
        self.assertTrue(evaluator.evaluate({"c": 1}).is_fallback)
        self.assertEqual(evaluator.evaluate({"c": 1}).verdict, "fail")
        discarded = evaluator.evaluate({"c": 1})
        self.assertTrue(discarded.discarded)
        self.assertEqual(len(evaluator.cache), 0)
        self.assertFalse(evaluator.evaluate({"c": 1}).cache_hit)
        self.assertEqual(len(transport.calls), 4)

    def test_cache_is_bounded_lru_with_ttl(self):
        clock = [0.0]
        cache = JevCache(max_entries=2, ttl=10.0, clock=lambda: clock[0])
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(cache.get("a"), 1)      # a is now most recent
        cache.put("c", 3)                         # evicts b
        self.assertIsNone(cache.get("b"))
        self.assertEqual(len(cache), 2)
        clock[0] = 11.0
        self.assertIsNone(cache.get("a"))         # expired
        disabled = JevCache(max_entries=0)
        disabled.put("a", 1)
        self.assertEqual(len(disabled), 0)
        cache.clear()
        self.assertEqual((len(cache), cache.hits, cache.misses), (0, 0, 0))

    def test_process_cache_only_for_the_real_transport(self):
        self.assertIs(JevEvaluator(api_key="k").cache, PROCESS_CACHE)
        self.assertIs(JevEvaluator(api_key="k", transport=HttpTransport()).cache,
                      PROCESS_CACHE)
        stub = JevEvaluator(api_key="k", transport=CountingTransport())
        self.assertIsNot(stub.cache, PROCESS_CACHE)
        self.assertIsNot(
            stub.cache, JevEvaluator(api_key="k", transport=CountingTransport()).cache)

    def test_key_and_model_partition_the_cache(self):
        transport = CountingTransport(noul_resp())
        cache = JevCache()
        one = JevEvaluator(api_key="k1", transport=transport, cache=cache)
        two = JevEvaluator(api_key="k2", transport=transport, cache=cache)
        one.evaluate({"c": 1})
        two.evaluate({"c": 1})
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(one.evaluate({"c": 1}).cache_hit)

    def test_unkeyed_never_reads_the_cache(self):
        cache = JevCache()
        transport = CountingTransport(noul_resp())
        JevEvaluator(api_key="k", transport=transport, cache=cache).evaluate({"c": 1})
        unkeyed = JevEvaluator(api_key=None, transport=transport, cache=cache)
        result = unkeyed.evaluate({"c": 1})
        self.assertTrue(result.is_fallback)
        self.assertFalse(result.cache_hit)

    def test_plan_requirements_carry_cache_hit_through(self):
        def plan_resp(payload):
            return {"model": "j", "usage": {"input_tokens": 10, "output_tokens": 1},
                    "answers": answers_for(payload["questions"])}
        transport = CountingTransport(plan_resp)
        policy = self.keyed(transport)
        policy.evaluate_plan("write a loop", ["a.py"])
        result, structural = policy.evaluate_plan("write a loop", ["a.py"])
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(result.cache_hit)
        self.assertTrue(structural["cache_hit"])


class FallbackLedgerTests(_Base):
    def test_identical_unkeyed_loop_cannot_flood_the_ledger(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for _ in range(1000):
            policy.evaluate_diff(DIFF, "change x", "x.py", site="apply")
        rows = self.rows()
        self.assertLessEqual(len(rows), 12)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(r["fallback_reason"] == "missing_key" for r in rows))
        self.assertEqual(rows[0].get("repeat_count"), None)
        self.assertGreater(rows[-1]["repeat_count"], 1)

    def test_distinct_unkeyed_states_are_capped_too(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for i in range(1000):
            policy.evaluate_diff(DIFF, "change {}".format(i), "x.py", site="apply",
                                 candidate=None)
        # instruction is part of the state, so every call is distinct
        self.assertLess(len(self.rows()), FALLBACK_DISTINCT_ROW_CAP + 40)

    def test_distinct_sites_and_reasons_are_tracked_separately(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for _ in range(3):
            policy.evaluate_diff(DIFF, "change x", "x.py", site="apply")
            policy.evaluate_diff(DIFF, "change x", "x.py", site="other")
        self.assertEqual(sorted({r["site"] for r in self.rows()}),
                         ["apply", "other"])

    def test_billed_fallbacks_are_never_deduped(self):
        from harness.jev import JevEvaluationResult
        policy = self.keyed(CountingTransport(noul_resp()))
        billed = JevEvaluationResult(
            "pass", 0.0, 1.0, {}, ["x"], cost=0.001, input_tokens=10,
            is_fallback=True, model="m", fallback_reason="route_out_of_vocabulary")
        for _ in range(5):
            policy._account(billed, site="route")
        self.assertEqual(len(self.rows()), 5)

    def test_missing_reason_is_stamped_never_null(self):
        from harness.jev import JevEvaluationResult
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        bare = JevEvaluationResult("pass", 0.0, 1.0, {}, ["x"], is_fallback=True)
        structural = policy._account(bare, site="orphan")
        self.assertEqual(structural["fallback_reason"], UNATTRIBUTED_FALLBACK)
        self.assertEqual(self.rows()[0]["fallback_reason"], UNATTRIBUTED_FALLBACK)


class FallbackTrackingBoundTests(_Base):
    def test_tracked_state_keys_are_bounded(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        with mock.patch("harness.jev_policy.FALLBACK_TRACKED_KEYS", 3):
            for i in range(10):
                policy.evaluate_diff(DIFF, "change {}".format(i), "x.py",
                                     site="apply")
        self.assertLessEqual(len(policy._fallback_counts), 3)


class ReasonPropagationTests(_Base):
    def assert_named_reasons(self):
        rows = [r for r in self.rows() if r["is_fallback"]]
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["fallback_reason"], row)
            self.assertNotEqual(row["fallback_reason"], UNATTRIBUTED_FALLBACK, row)

    def test_every_unkeyed_path_names_its_reason(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        policy.evaluate_diff(DIFF, "i", "x.py", site="apply")
        _, st = policy.evaluate_triage("fix a loop", ["a.py", "b.py"], site="triage")
        self.assertEqual(st["fallback_reason"], "missing_key")
        _, st = policy.evaluate_route("fix a loop", ["a.py"], site="route")
        self.assertEqual(st["fallback_reason"], "missing_key")
        _, st = policy.evaluate_plan("fix a loop", ["a.py"], site="waist")
        self.assertEqual(st["fallback_reason"], "missing_key")
        _, st = policy.evaluate_file_triage(
            "fix parser", ["a.py", "parser.py"], ["a.py", "parser.py"])
        self.assertEqual(st["fallback_reason"], "missing_key")
        _, st, combo = policy.evaluate_model_route(
            {"goal": "rename a thing"}, PACK)
        self.assertEqual(st["fallback_reason"], "missing_key")
        self.assertTrue(combo["is_fallback"])
        _, st, _ = policy.evaluate_model_route({"goal": "x"}, {"id": "bad"})
        self.assertEqual(st["fallback_reason"], "invalid_pack")
        self.assert_named_reasons()

    def test_disabled_policy_reports_explicit_disable(self):
        with mock.patch.dict(os.environ, {"HARNESS_JEV_DISABLE": "1"}):
            settings = load_settings({"jev_api_key": "configured"})
        policy = policy_for(settings, ledger=self.ledger)
        _, _, combo = policy.evaluate_model_route({"goal": "x"}, PACK)
        self.assertEqual(combo["structural"]["fallback_reason"], "explicit_disable")

    def test_transport_failure_reason_reaches_every_keyed_site(self):
        transport = CountingTransport(OSError("offline"))
        policy = self.keyed(transport, breaker_threshold=99)
        for call in (
            lambda: policy.evaluate_triage("fix a loop", ["a.py"]),
            lambda: policy.evaluate_route("fix a loop", ["a.py"]),
            lambda: policy.evaluate_file_triage("g", ["a.py"], ["a.py"]),
            lambda: policy.evaluate_model_route({"goal": "x"}, PACK)[:2],
        ):
            structural = call()[1]
            self.assertEqual(structural["fallback_reason"], "transport_failure")
        self.assert_named_reasons()

    def test_preflight_refusal_fallbacks_carry_a_reason(self):
        policy = self.keyed(CountingTransport(noul_resp()),
                            governor=self.governor(max_cost=0.0000001))
        route, st = policy.evaluate_route("fix a loop", ["a.py"])
        self.assertEqual(st["fallback_reason"], "preflight_refused")
        _, st = policy.evaluate_triage("fix a loop", ["a.py"])
        self.assertEqual(st["fallback_reason"], "preflight_refused")
        _, st = policy.evaluate_file_triage("g", ["a.py"], ["a.py"])
        self.assertEqual(st["fallback_reason"], "preflight_refused")
        _, st, combo = policy.evaluate_model_route({"goal": "x"}, PACK)
        self.assertEqual(st["fallback_reason"], "preflight_refused")
        self.assertTrue(route.is_fallback)
        self.assertEqual(policy.evaluator.min_confidence, 0.70)

    def test_log_item_preflight_refusal_names_its_reason(self):
        from tests.test_jev_log_judgment import sample_log_pack
        policy = self.keyed(CountingTransport(noul_resp()),
                            governor=self.governor(max_cost=0.0000001))
        result, structural, _ = policy.evaluate_log_item(
            {"item": "dial failed on swarm"}, sample_log_pack())
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["fallback_reason"], "preflight_refused")

    def test_billed_discarded_answer_settles_real_cost(self):
        unusable = {"model": "j", "usage": {"input_tokens": 250,
                                            "output_tokens": 2},
                    "answers": {"route": {"type": "choice", "choice": "nope",
                                          "probabilities": {"nope": 1.0},
                                          "confidence": 0.9}}}
        transport = CountingTransport(unusable)
        gov = self.governor()
        policy = self.keyed(transport, governor=gov)
        result, structural = policy.evaluate_route("fix a loop", ["a.py"])
        expected = jev_cost(250)
        self.assertTrue(result.is_fallback)
        self.assertTrue(result.discarded)
        self.assertAlmostEqual(result.cost, expected)
        self.assertEqual(structural["fallback_reason"], "response_discarded")
        self.assertAlmostEqual(gov.spent, expected)
        self.assertEqual(gov.outstanding, 0.0)
        row = self.rows()[0]
        self.assertAlmostEqual(row["cost"], expected)
        self.assertEqual(row["fallback_reason"], "response_discarded")

    def test_discarded_model_route_settles_and_keeps_live_confidence(self):
        unusable = {"model": "j", "usage": {"input_tokens": 400,
                                            "output_tokens": 2},
                    "answers": {"rung": {"type": "choice", "choice": "ghost",
                                         "probabilities": {"ghost": 1.0},
                                         "confidence": 0.9}}}
        gov = self.governor()
        policy = self.keyed(CountingTransport(unusable), governor=gov)
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "fix a parse bug"}, PACK)
        self.assertTrue(combo["is_fallback"])
        self.assertAlmostEqual(gov.spent, jev_cost(400))
        self.assertAlmostEqual(result.cost, jev_cost(400))
        self.assertTrue(structural["discarded"])
        self.assertEqual(structural["fallback_reason"], "response_discarded")

    def test_governor_without_reservations_still_settles_billed_fallback(self):
        class Plain:
            max_cost = 1.0
            spent = 0.0
            outstanding = 0.0
            recorded = []

            def record_actual(self, cost, label):
                self.recorded.append(cost)

        from harness.jev import JevEvaluationResult
        gov = Plain()
        policy = policy_for(self.keyed_settings(), governor=gov,
                            transport=CountingTransport(noul_resp()))
        billed = JevEvaluationResult(
            "pass", 0.0, 1.0, {}, [], cost=0.002, input_tokens=5,
            is_fallback=True, fallback_reason="x")
        free = JevEvaluationResult("pass", 0.0, 1.0, {}, [], is_fallback=True,
                                   fallback_reason="x")
        policy._account(billed, site="s")
        policy._account(free, site="s")
        self.assertEqual(gov.recorded, [0.002])


class ConfidenceRouteTests(_Base):
    def route(self, goal, choice, confidence, pack=PACK, tokens=100):
        criteria = [r["rung_id"] for r in pack["rungs"]]
        transport = CountingTransport(
            choice_resp(choice, confidence, criteria, tokens=tokens))
        policy = self.keyed(transport)
        return policy, policy.evaluate_model_route({"goal": goal}, pack)

    def test_low_confidence_in_ladder_choice_is_honored(self):
        policy, (result, structural, combo) = self.route(
            "rename a variable", "r1", 0.31)
        self.assertFalse(combo["is_fallback"])
        self.assertFalse(result.is_fallback)
        self.assertEqual(combo["rung_id"], "r1")
        self.assertEqual(combo["model"], "m-t1")
        self.assertAlmostEqual(combo["confidence"], 0.31)
        self.assertTrue(any("0.31" in r for r in combo["reasons"]))
        self.assertIsNone(structural["fallback_reason"])
        self.assertEqual(self.rows()[0]["is_fallback"], False)
        self.assertEqual(policy.evaluator.min_confidence, 0.70)

    def test_low_confidence_cheaper_than_floor_is_raised_to_floor(self):
        _, (_, _, combo) = self.route("fix a parse bug", "r0", 0.31)
        self.assertFalse(combo["is_fallback"])
        self.assertEqual(combo["rung_id"], "r1")        # floor T1
        self.assertTrue(any("raised" in r for r in combo["reasons"]))

    def test_low_confidence_never_cheaper_than_floor_when_unsatisfiable(self):
        _, (result, structural, combo) = self.route(
            "design the architecture", "only", 0.31, pack=LOW_PACK)
        self.assertTrue(combo["is_fallback"])
        self.assertIsNone(combo["rung_id"])
        self.assertAlmostEqual(combo["confidence"], 0.31)
        self.assertTrue(structural["fallback_reason"])
        self.assertNotEqual(structural["fallback_reason"], UNATTRIBUTED_FALLBACK)
        self.assertTrue(result.is_fallback)

    def test_high_confidence_behaviour_is_unchanged(self):
        _, (_, _, combo) = self.route("fix a parse bug", "r0", 0.95)
        self.assertFalse(combo["is_fallback"])
        self.assertEqual(combo["rung_id"], "r0")        # not raised
        self.assertAlmostEqual(combo["confidence"], 0.95)

    def test_other_sites_keep_the_global_threshold(self):
        # A route-lane choice at 0.31 is still a below-threshold verdict.
        transport = CountingTransport(
            lambda payload: {"model": "j", "usage": {"input_tokens": 9,
                             "output_tokens": 1},
                             "answers": answers_for(payload["questions"],
                                                    confidence=0.31)})
        policy = self.keyed(transport)
        result, _ = policy.evaluate_route("fix a loop", ["a.py"])
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(policy.evaluator.min_confidence, 0.70)


class FanOutTests(_Base):
    def make_policy(self, transport):
        return self.keyed(transport)

    def test_results_are_order_stable_and_actually_concurrent(self):
        def respond(payload):
            return {"model": "j", "usage": {"input_tokens": 10,
                                            "output_tokens": 1},
                    "answers": answers_for(payload["questions"])}
        transport = CountingTransport(respond, delay={
            ("requires_iteration", "route"): 0.25,
            ("requirement_complexity", "requires_iteration"): 0.05,
        })
        policy = self.make_policy(transport)
        order = []

        def call(name, fn):
            def run():
                value = fn()
                order.append(name)
                return value
            return run

        started = time.monotonic()
        out = policy.fan_out([
            ("route", call("route", lambda: policy.evaluate_route(
                "fix a loop", ["a.py"], site="route"))),
            ("waist", call("waist", lambda: policy.evaluate_plan(
                "fix a loop", ["a.py"], site="waist"))),
        ])
        elapsed = time.monotonic() - started
        self.assertEqual(order, ["waist", "route"])      # finished reversed
        self.assertEqual(out[0][1]["site"], "route")      # ...returned in order
        self.assertEqual(out[1][1]["site"], "waist")
        self.assertGreaterEqual(transport.max_in_flight, 2)
        self.assertLess(elapsed, 0.25 + 0.05 + 0.2)
        self.assertEqual(len(self.rows()), 2)

    def test_exception_fails_closed_per_question(self):
        policy = self.make_policy(CountingTransport(noul_resp()))

        def boom():
            raise RuntimeError("kaboom")

        out = policy.fan_out([
            ("a", lambda: ("ok-a", {"site": "a"})),
            ("b", boom),
            ("c", lambda: ("ok-c", {"site": "c"})),
        ])
        self.assertEqual(out[0][0], "ok-a")
        self.assertEqual(out[2][0], "ok-c")
        result, structural = out[1]
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["fallback_reason"], "fanout_exception")
        self.assertEqual(structural["site"], "b")
        rows = self.rows()
        self.assertEqual([r["fallback_reason"] for r in rows],
                         ["fanout_exception"])

    def test_failure_accounting_error_still_fails_closed(self):
        policy = self.make_policy(CountingTransport(noul_resp()))
        policy._account = mock.Mock(side_effect=RuntimeError("ledger busy"))

        def boom():
            raise ValueError("x")

        with mock.patch("harness.jev_policy.eprint"):
            out = policy.fan_out([("s", boom), ("t", lambda: ("ok", {}))])
        self.assertEqual(out[0][1]["fallback_reason"], "fanout_exception")
        self.assertEqual(out[1][0], "ok")

    def test_on_failure_reshapes_the_pair(self):
        policy = self.make_policy(CountingTransport(noul_resp()))

        def boom():
            raise ValueError("x")

        out = policy.fan_out([("s", boom, lambda r, st: (r, st, "combo")),
                              ("t", lambda: ("ok", {}))])
        self.assertEqual(out[0][2], "combo")

    def test_unkeyed_and_single_job_run_inline(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        threads = []
        out = policy.fan_out([
            ("a", lambda: threads.append(threading.current_thread()) or 1),
            ("b", lambda: threads.append(threading.current_thread()) or 2),
        ])
        self.assertEqual(out, [1, 2])
        self.assertTrue(all(t is threading.current_thread() for t in threads))
        keyed = self.make_policy(CountingTransport(noul_resp()))
        self.assertEqual(keyed.fan_out([("a", lambda: 7)]), [7])
        self.assertEqual(keyed.fan_out([]), [])

    def test_waist_fans_route_plan_and_issue_sort_out(self):
        from harness.waist import compose_plan
        pack = {"id": "ops", "buckets": {"auth": {
            "label": "Auth", "kind": "trouble_area", "path_id": "p/auth",
            "keywords": ["auth"], "suggested_next_action": "look",
            "attention": "high"}}}

        def respond(payload):
            return {"model": "j", "usage": {"input_tokens": 10,
                                            "output_tokens": 1},
                    "answers": answers_for(payload["questions"])}
        transport = CountingTransport(respond)
        policy = self.make_policy(transport)
        calls = []
        original = policy.fan_out

        def spy(jobs, **kw):
            calls.append([job[0] for job in jobs])
            return original(jobs, **kw)

        policy.fan_out = spy
        envelope = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="fix auth token handling", candidate_files=[],
            jev_policy=policy, issue_sort_pack=pack)
        self.assertEqual(calls, [["route", "waist", "issue_sort"]])
        self.assertIn("issue_sort", {r["site"] for r in self.rows()})
        self.assertIsNotNone(envelope)

    def test_waist_issue_sort_exception_degrades_to_empty_combo(self):
        from harness.waist import compose_plan
        pack = {"id": "ops", "buckets": {"auth": {
            "label": "Auth", "kind": "trouble_area", "path_id": "p/auth",
            "keywords": ["auth"], "suggested_next_action": "look",
            "attention": "high"}}}
        policy = self.make_policy(CountingTransport(noul_resp()))
        policy.evaluate_issue_sort = mock.Mock(side_effect=RuntimeError("x"))
        envelope = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="fix auth token handling", candidate_files=[],
            jev_policy=policy, issue_sort_pack=pack)
        self.assertIsNotNone(envelope)


class BreakerTests(_Base):
    def make(self, transport, clock, **kw):
        return self.keyed(transport, clock=clock, breaker_cooldown=30.0, **kw)

    def test_opens_after_consecutive_transport_failures_and_recloses(self):
        now = [100.0]
        transport = CountingTransport(OSError("down"))
        policy = self.make(transport, lambda: now[0])
        self.assertTrue(policy.available("route"))
        for _ in range(BREAKER_FAILURE_THRESHOLD):
            policy.evaluate_route("fix a loop {}".format(len(transport.calls)), ["a.py"])
        self.assertEqual(len(transport.calls), BREAKER_FAILURE_THRESHOLD)
        self.assertFalse(policy.available("route"))
        self.assertTrue(policy.available("other-site"))

        # Open breaker: refused locally, no request, named reason, no spend.
        before = len(transport.calls)
        result, structural = policy.evaluate_route("fix a loop open", ["a.py"])
        self.assertEqual(len(transport.calls), before)
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["fallback_reason"], "circuit_open")

        # Cooldown elapsed: one probe goes through; success closes it.
        now[0] += 31.0
        self.assertTrue(policy.available("route"))
        transport.default = noul_resp()
        transport.plan = [lambda payload: {
            "model": "j", "usage": {"input_tokens": 5, "output_tokens": 1},
            "answers": answers_for(payload["questions"])}]
        ok, _ = policy.evaluate_route("fix a loop probe", ["a.py"])
        self.assertFalse(ok.is_fallback)
        self.assertEqual(len(transport.calls), before + 1)
        self.assertTrue(policy.available("route"))
        for i in range(BREAKER_FAILURE_THRESHOLD - 1):
            transport.plan = [OSError("again")]
            policy.evaluate_route("fix a loop again {}".format(i), ["a.py"])
        self.assertTrue(policy.available("route"))   # counter was reset

    def test_failed_probe_reopens_the_breaker(self):
        now = [0.0]
        transport = CountingTransport(OSError("down"))
        policy = self.make(transport, lambda: now[0])
        for i in range(BREAKER_FAILURE_THRESHOLD):
            policy.evaluate_route("a loop {}".format(i), ["a.py"])
        now[0] += 31.0
        policy.evaluate_route("probe", ["a.py"])          # probe fails
        self.assertFalse(policy.available("route"))
        now[0] += 5.0
        calls = len(transport.calls)
        policy.evaluate_route("blocked", ["a.py"])
        self.assertEqual(len(transport.calls), calls)

    def test_availability_survives_a_broken_governor_probe(self):
        policy = self.keyed(CountingTransport(noul_resp()))
        policy.governor.remaining = mock.Mock(side_effect=HarnessError("x"))
        self.assertTrue(policy.available("route"))

    def test_unkeyed_or_unaffordable_is_unavailable(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        self.assertFalse(policy.available("route"))
        poor = self.keyed(CountingTransport(noul_resp()),
                          governor=self.governor(max_cost=0.0000001))
        self.assertFalse(poor.available("route"))
        rich = self.keyed(CountingTransport(noul_resp()))
        self.assertTrue(rich.available("route"))

    def test_healthy_keyed_policy_is_unchanged(self):
        transport = CountingTransport(noul_resp(tokens=100))
        gov = self.governor()
        policy = self.keyed(transport, governor=gov)
        result, _ = policy.evaluate_diff(DIFF, "change x", "x.py", site="apply")
        self.assertFalse(result.is_fallback)
        self.assertAlmostEqual(gov.spent, jev_cost(100))
        self.assertTrue(policy.available("apply"))

    def test_http_fallback_also_counts(self):
        now = [0.0]
        transport = CountingTransport(noul_resp(), plan=[(503, {})] * 5)
        policy = self.make(transport, lambda: now[0])
        for i in range(BREAKER_FAILURE_THRESHOLD):
            policy.evaluate_diff(DIFF, "i{}".format(i), "x.py", site="apply")
        self.assertFalse(policy.available("apply"))


def echo(tokens=100):
    def respond(payload):
        return {"model": "jev-test",
                "usage": {"input_tokens": tokens, "output_tokens": 3},
                "answers": answers_for(payload["questions"])}
    return respond


SITES = {
    "diff": lambda p: p.evaluate_diff(DIFF, "set x to 2", "x.py"),
    "triage": lambda p: p.evaluate_triage("fix loop", ["a.py"]),
    "route": lambda p: p.evaluate_route("fix loop", ["a.py"]),
    "plan": lambda p: p.evaluate_plan("fix loop", ["a.py"]),
    "file_triage": lambda p: p.evaluate_file_triage("fix", ["a.py", "b.py"]),
    "claims": lambda p: p.evaluate_claim_support(
        ["the sky is blue"], "ctx", enabled=True),
    "decision": lambda p: p.evaluate_decision("act", "end", "ctx"),
    "answer": lambda p: p.evaluate_answer("q", "a", "c"),
    "escalation": lambda p: p.evaluate_escalation_decision("fail ctx"),
    "completion": lambda p: p.evaluate_completion_nouls("goal", "state"),
    "scope": lambda p: p.evaluate_scope(
        {"goal": "g", "in_scope": ["a"], "state": "s"}),
    "model_route": lambda p: p.evaluate_model_route("do thing", PACK),
}
SITE_NAMES = {"diff": "apply", "triage": "triage", "route": "route",
              "plan": "waist", "file_triage": "triage-files",
              "claims": "claims", "scope": "hul_scope", "answer": "answer",
              "model_route": "model_route"}


def first(out):
    return out[0]


class EverySiteCacheTests(_Base):
    def test_second_identical_call_is_free_at_every_site(self):
        for name, call in SITES.items():
            with self.subTest(site=name):
                self.setUp()
                transport = CountingTransport(echo(100))
                gov = self.governor(max_cost=0.5)
                policy = self.keyed(transport, governor=gov)
                call(policy)
                calls, spent = len(transport.calls), gov.spent
                call(policy)
                self.assertEqual(len(transport.calls), calls)
                self.assertEqual(gov.spent, spent)
                self.assertEqual(gov.outstanding, 0.0)
                hits = [r for r in self.rows() if r.get("cache_hit")]
                self.assertTrue(hits, name)
                self.assertTrue(all(r["cost"] == 0.0 for r in hits))

    def test_free_hit_is_not_refused_when_the_budget_is_gone(self):
        transport = CountingTransport(echo(100))
        gov = self.governor(max_cost=0.5)
        policy = self.keyed(transport, governor=gov)
        policy.evaluate_route("fix loop", ["a.py"])
        gov.max_cost = gov.spent          # nothing left to reserve
        result, structural = policy.evaluate_route("fix loop", ["a.py"])
        self.assertFalse(result.is_fallback)
        self.assertTrue(result.cache_hit)
        self.assertEqual(len(transport.calls), 1)
        self.assertIsNone(structural["fallback_reason"])


class EverySiteBilledTests(_Base):
    def run_mode(self, call, transport):
        gov = self.governor(max_cost=0.5)
        policy = self.keyed(transport, governor=gov, breaker_threshold=10 ** 6)
        for _ in range(3):
            call(policy)
        return policy, gov

    def test_billed_discarded_answers_settle_exactly_at_every_site(self):
        bad = {"model": "jev-test",
               "usage": {"input_tokens": 100, "output_tokens": 3},
               "answers": {"zzz": {"type": "noul", "noul": 0.5}}}
        for name, call in SITES.items():
            with self.subTest(site=name):
                self.setUp()
                transport = CountingTransport(bad)
                _, gov = self.run_mode(call, transport)
                billed = sum(r["cost"] for r in self.rows())
                self.assertAlmostEqual(
                    gov.spent, len(transport.calls) * jev_cost(100))
                self.assertAlmostEqual(billed, gov.spent)
                self.assertEqual(gov.outstanding, 0.0)

    def test_failed_transports_are_free_and_named_at_every_site(self):
        for mode, reason in (("raise", "transport_failure"),
                             (500, "http_fallback")):
            for name, call in SITES.items():
                with self.subTest(site=name, mode=mode):
                    self.setUp()
                    item = OSError("boom") if mode == "raise" else (500, {})
                    transport = CountingTransport(item)
                    _, gov = self.run_mode(call, transport)
                    self.assertEqual(gov.spent, 0.0)
                    self.assertEqual(gov.outstanding, 0.0)
                    for row in self.rows():
                        if row["is_fallback"]:
                            self.assertEqual(row["fallback_reason"], reason)
                            self.assertEqual(row["cost"], 0.0)


class BreakerDegradationTests(_Base):
    def build(self, transport, clock=None):
        return self.keyed(transport, clock=clock or (lambda: 1000.0))

    def test_open_breaker_never_hardens_any_site(self):
        for name in SITE_NAMES:
            with self.subTest(site=name):
                self.setUp()
                transport = CountingTransport(OSError("down"))
                policy = self.build(transport)
                shapes = []
                for _ in range(6):
                    res = first(SITES[name](policy))
                    shapes.append((res.verdict, res.is_fallback,
                                   res.is_passing()))
                self.assertEqual(len(set(shapes)), 1, shapes)
                self.assertEqual(len(transport.calls), BREAKER_FAILURE_THRESHOLD)
                self.assertFalse(policy.available(SITE_NAMES[name]))
                reasons = [r["fallback_reason"] for r in self.rows()
                           if r["is_fallback"]]
                self.assertIn("circuit_open", reasons)

    def test_apply_gate_keeps_its_graceful_fallback_while_open(self):
        transport = CountingTransport(OSError("down"))
        policy = self.build(transport)
        for _ in range(BREAKER_FAILURE_THRESHOLD):
            policy.evaluate_diff(DIFF, "set x to 2", "x.py")
        result, structural = policy.evaluate_diff(DIFF, "set x to 2", "x.py")
        self.assertEqual(result.verdict, "pass")
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["fallback_reason"], "circuit_open")

    def test_open_breaker_leaves_deduped_ledger_evidence(self):
        transport = CountingTransport(OSError("down"))
        policy = self.build(transport)
        for _ in range(50):
            policy.evaluate_triage("same", ["a.py"])
        rows = [r for r in self.rows() if r.get("fallback_reason") == "circuit_open"]
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 8)
        self.assertGreater(sum(r.get("repeat_count", 1) for r in rows), 1)

    def test_only_a_parsed_round_trip_closes_it_and_neutrals_do_not_reset(self):
        seq = [OSError("x"), OSError("x"), (401, {}), OSError("x"),
               OSError("x"), OSError("x")]
        transport = CountingTransport(plan=seq)
        policy = self.build(transport)
        reasons = []
        for i in range(6):
            _, structural = policy.evaluate_route("g{}".format(i), ["a.py"])
            reasons.append(structural["fallback_reason"])
        # 401 is neither success nor failure: the 3rd failure still opens it.
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(reasons[-2:], ["circuit_open", "circuit_open"])

    def test_local_mechanics_rejection_counts_as_neither(self):
        transport = CountingTransport(OSError("down"))
        policy = self.build(transport)
        for _ in range(BREAKER_FAILURE_THRESHOLD - 1):
            policy.evaluate_diff(DIFF, "set x", "x.py")
        policy.evaluate_diff("garbage not a diff", "set x", "x.py")
        board = policy.evaluator.breakers
        self.assertEqual(board.snapshot("apply")["failures"],
                         BREAKER_FAILURE_THRESHOLD - 1)
        policy.evaluate_diff(DIFF, "set y", "x.py")
        self.assertFalse(policy.available("apply"))

    def test_invalid_response_does_not_close_an_open_breaker(self):
        now = [0.0]
        transport = CountingTransport(OSError("down"))
        policy = self.build(transport, clock=lambda: now[0])
        for i in range(BREAKER_FAILURE_THRESHOLD):
            policy.evaluate_route("g{}".format(i), ["a.py"])
        now[0] += 31.0
        transport.default = {"model": "j", "usage": {"input_tokens": 5,
                                                      "output_tokens": 1},
                             "answers": {"nope": {"type": "noul", "noul": 1}}}
        policy.evaluate_route("probe", ["a.py"])      # unparseable: neutral
        snap = policy.evaluator.breakers.snapshot("route")
        self.assertIsNotNone(snap["opened_at"])
        self.assertFalse(snap["probing"])

    def test_half_open_lets_exactly_one_probe_through_concurrently(self):
        now = [0.0]
        transport = CountingTransport(OSError("down"))
        policy = self.build(transport, clock=lambda: now[0])
        for i in range(BREAKER_FAILURE_THRESHOLD):
            policy.evaluate_route("g{}".format(i), ["a.py"])
        now[0] += 31.0
        transport.default = echo(5)
        transport.delay = {("requires_iteration", "route"): 0.2}
        base = len(transport.calls)
        results = []

        def go(i):
            results.append(policy.evaluate_route(
                "conc {}".format(i), ["a.py"])[1]["fallback_reason"])
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(transport.calls) - base, 1)
        self.assertEqual(results.count("circuit_open"), 7)
        self.assertTrue(policy.available("route"))

    def test_breaker_is_shared_across_per_request_policies(self):
        from harness import jev as jev_mod
        key = "shared-key-{}".format(time.time_ns())
        calls = []

        def down(self_, url, api_key, payload, timeout=45):
            calls.append(1)
            raise OSError("down")

        def fresh_policy():
            settings = load_settings({"jev_api_key": key})
            settings.hourglass_confirm = False
            return policy_for(settings, governor=self.governor(),
                              ledger=self.ledger)
        with mock.patch.object(HttpTransport, "post", down):
            fresh_policy().evaluate_route("one", ["a.py"])
            fresh_policy().evaluate_route("two", ["a.py"])
            fresh_policy().evaluate_route("three", ["a.py"])
            _, structural = fresh_policy().evaluate_route("four", ["a.py"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(structural["fallback_reason"], "circuit_open")
        jev_mod._SHARED_BREAKERS.clear()


class CacheHardeningTests(_Base):
    Q = {"a": {"type": "noul", "instructions": "is it ok?"}}

    def stub(self, noul=0.9, delay=0.0):
        class T:
            calls = 0
            lock = threading.Lock()

            def post(s, url, key, payload, timeout=45):
                with s.lock:
                    s.calls += 1
                time.sleep(delay)
                return 200, {"model": "jev-test",
                             "usage": {"input_tokens": 10, "output_tokens": 1},
                             "answers": {"a": {"type": "noul", "noul": noul}}}
        return T()

    def test_partitioning_by_key_endpoint_model_state_and_question(self):
        t, cache = self.stub(), JevCache()

        def ev(**kw):
            kw.setdefault("api_key", "k1")
            kw.setdefault("endpoint", "https://e1")
            return JevEvaluator(transport=t, cache=cache, **kw)
        ev().evaluate({"x": 1}, self.Q)
        for build, state, qs in (
                (lambda: ev(api_key="k2"), {"x": 1}, self.Q),
                (lambda: ev(endpoint="https://e2"), {"x": 1}, self.Q),
                (lambda: ev(), {"x": 2}, self.Q),
                (lambda: ev(), {"x": 1}, {"a": {"type": "noul",
                                                "instructions": "other?"}}),
                (lambda: ev(), {"x": True}, self.Q),
                (lambda: ev(), {"x": 1.0}, self.Q),
                (lambda: ev(), {"x": "1"}, self.Q)):
            before = t.calls
            build().evaluate(state, qs)
            self.assertEqual(t.calls, before + 1, (state, qs))
        other = ev()
        other.model = "other-model"
        before = t.calls
        other.evaluate({"x": 1}, self.Q)
        self.assertEqual(t.calls, before + 1)

    def test_digest_is_total_and_unhashable_state_is_not_cached(self):
        t, cache = self.stub(), JevCache()
        keyed = JevEvaluator(api_key="k", transport=t, cache=cache)
        unkeyed = JevEvaluator(api_key=None)
        circular = {}
        circular["self"] = circular
        weird = {1: "a", "b": 2}
        for state in (weird, circular):
            self.assertIn(unkeyed.evaluate(state, self.Q).verdict, ("pass", "fail"))
            keyed.evaluate(state, self.Q)
        self.assertEqual(len(cache), 0)
        self.assertEqual(t.calls, 2)         # sent, never cached

    def test_hits_are_defensive_copies(self):
        t, cache = self.stub(), JevCache()
        ev = JevEvaluator(api_key="k", transport=t, cache=cache)
        first_result = ev.evaluate({"m": 1}, self.Q)
        first_result.answers["a"]["noul"] = 0.0
        first_result.reasons.append("poison")
        second = ev.evaluate({"m": 1}, self.Q)
        self.assertEqual(second.answers["a"]["noul"], 0.9)
        self.assertNotIn("poison", second.reasons)
        second.answers["a"]["noul"] = 0.123
        self.assertEqual(ev.evaluate({"m": 1}, self.Q).answers["a"]["noul"], 0.9)

    def test_concurrent_identical_misses_share_one_request(self):
        t, cache = self.stub(delay=0.15), JevCache()
        ev = JevEvaluator(api_key="k", transport=t, cache=cache)
        out = []
        threads = [threading.Thread(
            target=lambda: out.append(ev.evaluate({"q": 1}, self.Q)))
            for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(t.calls, 1)
        self.assertEqual(sum(1 for r in out if r.cache_hit), 7)
        self.assertEqual(sum(r.cost > 0 for r in out), 1)

    def test_failed_leader_lets_followers_retry(self):
        class Flaky:
            calls = 0
            lock = threading.Lock()

            def post(s, *a, **k):
                with s.lock:
                    s.calls += 1
                    n = s.calls
                time.sleep(0.1)
                if n == 1:
                    raise OSError("first fails")
                return 200, {"model": "j",
                             "usage": {"input_tokens": 1, "output_tokens": 1},
                             "answers": {"a": {"type": "noul", "noul": 0.9}}}
        f, cache = Flaky(), JevCache()
        ev = JevEvaluator(api_key="k", transport=f, cache=cache)
        out = []
        threads = [threading.Thread(
            target=lambda: out.append(ev.evaluate({"q": 1}, self.Q)))
            for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertGreaterEqual(sum(1 for r in out if not r.is_fallback), 2)

    def test_real_transport_shares_the_process_cache_across_evaluators(self):
        PROCESS_CACHE.clear()
        resp = (200, {"model": "jev-test",
                      "usage": {"input_tokens": 10, "output_tokens": 1},
                      "answers": {"a": {"type": "noul", "noul": 0.9}}})
        with mock.patch.object(HttpTransport, "post", return_value=resp) as post:
            JevEvaluator(api_key="pk").evaluate({"z": 1}, self.Q)
            JevEvaluator(api_key="pk").evaluate({"z": 1}, self.Q)
            JevEvaluator(api_key="other").evaluate({"z": 1}, self.Q)
        self.assertEqual(post.call_count, 2)
        PROCESS_CACHE.clear()


class DedupeAccountingTests(_Base):
    def test_billed_rows_never_dedupe_and_free_rows_carry_deltas(self):
        from harness.jev import JevEvaluationResult
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for _ in range(9):
            policy.evaluate_triage("same", ["a.py"])
        rows = self.rows()
        # calls 1, 2, 4, 8 write rows standing for 1, 1, 2, 4 (call 9 pending)
        self.assertEqual([r.get("repeat_count", 1) for r in rows], [1, 1, 2, 4])
        self.assertEqual(policy.flush_fallbacks(), 1)
        self.assertEqual(sum(r.get("repeat_count", 1) for r in self.rows()), 9)
        self.assertEqual(policy.flush_fallbacks(), 0)
        billed = JevEvaluationResult(
            "pass", 0.0, 1.0, {}, [], cost=0.001, input_tokens=10,
            is_fallback=True, fallback_reason="x", discarded=True)
        for _ in range(3):
            policy._account(billed, site="s")
        mine = [r for r in self.rows() if r["site"] == "s"]
        self.assertEqual(len(mine), 3)
        self.assertTrue(all(r["discarded"] for r in mine))

    def test_refusals_are_deduped_and_coded(self):
        policy = self.keyed(CountingTransport(noul_resp()),
                            governor=self.governor(max_cost=0.0000001))
        for _ in range(40):
            policy.evaluate_diff(DIFF, "x", "x.py")
        refusals = [e for e in self.ledger.entries()
                    if e["event"] == "jev_refusal"]
        self.assertTrue(refusals)
        self.assertLessEqual(len(refusals), 8)
        self.assertEqual({r["reason_code"] for r in refusals}, {"preflight_refused"})

    def test_hard_refusal_paths_leave_a_ledger_row(self):
        policy = self.keyed(CountingTransport(noul_resp()),
                            governor=self.governor(max_cost=0.0000001))
        policy.evaluate_triage("fix", ["a.py"])
        policy.evaluate_route("fix", ["a.py"])
        policy.evaluate_file_triage("g", ["a.py"], ["a.py"])
        policy.evaluate_claim_support(["a claim"], "ctx", enabled=True)
        policy.evaluate_scope({"goal": "g", "in_scope": ["a"], "state": "s"})
        sites = {r["site"] for r in self.rows()
                 if r["fallback_reason"] == "preflight_refused"}
        self.assertTrue({"triage", "route", "triage-files"} <= sites, sites)


class FanOutHardeningTests(_Base):
    def test_unkeyed_inline_path_propagates_bugs(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)

        def boom():
            raise RuntimeError("bug")
        with self.assertRaises(RuntimeError):
            policy.fan_out([("route", boom)])

    def test_reservation_is_released_when_a_call_dies_after_preflight(self):
        for keyed_pool in (True, False):
            self.setUp()
            gov = self.governor()
            policy = self.keyed(CountingTransport(noul_resp()), governor=gov)

            def raising(*a, **k):
                raise RuntimeError("evaluator bug")
            policy.evaluator.evaluate = raising
            jobs = [("route", lambda: policy.evaluate_route("x", ["a.py"])),
                    ("waist", lambda: policy.evaluate_plan("x", ["a.py"]))]
            if keyed_pool:
                out = policy.fan_out(jobs)
                self.assertEqual(
                    [o[1]["fallback_reason"] for o in out],
                    ["fanout_exception", "fanout_exception"])
            else:
                with self.assertRaises(RuntimeError):
                    policy.fan_out(jobs[:1])
            self.assertEqual(gov.outstanding, 0.0)
            self.assertEqual(gov.remaining(), gov.max_cost)

    def test_ledger_failure_is_reported_not_swallowed(self):
        policy = self.keyed(CountingTransport(noul_resp()))
        policy._account = mock.Mock(side_effect=OSError("disk full"))

        def boom():
            raise ValueError("x")
        with mock.patch("harness.jev_policy.eprint") as eprint:
            out = policy.fan_out([("s", boom), ("t", lambda: ("ok", {}))])
        self.assertEqual(out[0][1]["fallback_reason"], "fanout_exception")
        self.assertIn("disk full", eprint.call_args[0][0])

    def test_workers_are_bounded_and_not_leaked(self):
        policy = self.keyed(CountingTransport(noul_resp()))
        cur = [0, 0]
        lock = threading.Lock()

        def job(i):
            def run():
                with lock:
                    cur[0] += 1
                    cur[1] = max(cur[1], cur[0])
                time.sleep(0.05)
                with lock:
                    cur[0] -= 1
                return i
            return run
        out = policy.fan_out([("s{}".format(i), job(i)) for i in range(10)],
                             max_workers=100)
        self.assertEqual(out, list(range(10)))
        self.assertLessEqual(cur[1], 8)
        time.sleep(0.1)
        self.assertFalse([t for t in threading.enumerate()
                          if t.name.startswith("jev-fanout")])

    def test_post_dispatch_overrun_is_not_labelled_a_preflight_refusal(self):
        big = {"model": "jev-test",
               "usage": {"input_tokens": 5000, "output_tokens": 3}}

        def respond(payload):
            return dict(big, answers=answers_for(payload["questions"]))
        gov = self.governor(max_cost=jev_cost(1024) * 1.5)
        policy = self.keyed(CountingTransport(respond), governor=gov)
        result, structural = policy.evaluate_route("x", ["a.py"])
        self.assertFalse(result.is_fallback)          # the paid answer stands
        self.assertTrue(structural["settlement_overrun"])
        self.assertNotEqual(structural["fallback_reason"], "preflight_refused")


class LowConfidenceFloorTests(_Base):
    def test_near_zero_confidence_choice_is_discarded_and_billed(self):
        criteria = [r["rung_id"] for r in PACK["rungs"]]
        transport = CountingTransport(choice_resp("r1", 0.02, criteria, tokens=200))
        gov = self.governor()
        policy = self.keyed(transport, governor=gov)
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "rename a variable"}, PACK)
        self.assertTrue(combo["is_fallback"])
        self.assertEqual(structural["fallback_reason"], "low_confidence")
        self.assertTrue(structural["discarded"])
        self.assertAlmostEqual(gov.spent, jev_cost(200))


class AnalyticsTests(_Base):
    def test_calibration_honors_repeat_count_and_excludes_cache_hits(self):
        ledger = self.ledger
        ledger.append("jev_eval", site="route", is_fallback=True, cost=0.0,
                      input_tokens=0, repeat_count=4, fallback_reason="x",
                      task_id="t1", verdict="pass", confidence=0.0, supported=1.0)
        ledger.append("jev_eval", site="route", is_fallback=False, cost=0.001,
                      input_tokens=10, task_id="t1", verdict="pass",
                      confidence=0.9, supported=0.9)
        ledger.append("jev_eval", site="route", is_fallback=False, cost=0.0,
                      input_tokens=0, cache_hit=True, task_id="t1",
                      verdict="pass", confidence=0.9, supported=0.9)
        ledger.append("verify_round", task_id="t1", passed=True)
        report = ledger.jev_calibration_report()
        self.assertEqual(report["jev_evals"], 6)
        self.assertEqual(report["jev_fallback_evals"], 4)
        self.assertEqual(report["jev_cache_hit_evals"], 1)
        self.assertEqual(report["jev_keyed_evals"], 1)
        self.assertEqual(report["by_site"], {"route": 6})
        high = report["confidence_buckets"]["high_supported_ge_0.8"]
        self.assertEqual(high["evals"], 1)

    def test_a_fallback_row_claims_no_credit_even_when_it_settled(self):
        # The Jev credit measures keyed Jev usage. A fallback is the money Jev
        # did NOT save, so it claims nothing -- including when the governor
        # settled real money for it. Crediting a fallback would let the system
        # bank savings it never made.
        ledger = self.ledger
        ledger.append("jev_eval", model="jev-test", input_tokens=1000,
                      is_fallback=True, cost=jev_cost(1000), discarded=True)
        ledger.append("jev_eval", model="jev-test", input_tokens=500,
                      is_fallback=True, cost=0.0)
        report = ledger.cost_report()
        self.assertEqual(report["jev"]["calls"], 0)
        self.assertEqual(report["jev"]["cost"], 0.0)
        self.assertEqual(report["jev"]["used_percent"], 0.0)


class CoverageGapTests(_Base):
    def test_digest_survives_objects_that_cannot_describe_themselves(self):
        from harness.jev import _digest

        class Mute:
            def __str__(self):
                raise RuntimeError("no str")

            __repr__ = __str__
        self.assertEqual(len(_digest({"x": Mute()})), 64)
        self.assertEqual(
            len(JevEvaluator(api_key=None).evaluate({"x": Mute()}).state_hash), 64)

    def test_unusable_usage_fields_are_zeroed_when_a_response_is_discarded(self):
        bad = {"model": "j", "usage": {"input_tokens": -5, "output_tokens": "x"},
               "answers": {"zzz": {"type": "noul", "noul": 0.5}}}
        ev = JevEvaluator(api_key="k", transport=CountingTransport(bad))
        result = ev.evaluate({"c": 1})
        self.assertFalse(result.discarded)
        self.assertEqual((result.input_tokens, result.output_tokens), (0, 0))

    def test_flush_writes_the_capped_site_tail(self):
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for i in range(FALLBACK_DISTINCT_ROW_CAP + 6):
            policy.evaluate_triage("goal {}".format(i), ["a.py"])
        written = sum(r.get("repeat_count", 1) for r in self.rows())
        self.assertGreater(policy.flush_fallbacks(), 0)
        total = sum(r.get("repeat_count", 1) for r in self.rows())
        self.assertEqual(total, FALLBACK_DISTINCT_ROW_CAP + 6)
        self.assertGreaterEqual(total, written)

    def test_release_tolerates_an_already_settled_token(self):
        policy = self.keyed(CountingTransport(noul_resp()))
        policy.governor = mock.Mock()
        policy.governor.reconcile.side_effect = HarnessError("unknown token")
        policy._tl.token = ("jev:x", 0.1)
        policy._release_reservation()
        self.assertIsNone(policy._tl.token)
        policy._release_reservation()        # nothing left: no second call
        self.assertEqual(policy.governor.reconcile.call_count, 1)

    def test_a_broken_cache_probe_never_blocks_a_call(self):
        transport = CountingTransport(noul_resp())
        policy = self.keyed(transport)
        policy.evaluator.peek = mock.Mock(side_effect=RuntimeError("x"))
        result, _ = policy.evaluate_diff(DIFF, "set x", "x.py")
        self.assertFalse(result.is_fallback)
        self.assertEqual(len(transport.calls), 1)

    def test_analytics_tolerates_garbage_ledger_fields(self):
        self.ledger.append("jev_eval", site="s", is_fallback=True, cost=0.0,
                           repeat_count="many", task_id="t")
        self.ledger.append("jev_eval", model="jev-test", input_tokens=10,
                           is_fallback=True, cost="oops")
        self.assertEqual(self.ledger.jev_calibration_report()["jev_evals"], 2)
        self.assertEqual(self.ledger.cost_report()["jev"]["calls"], 0)


class ReservedTransport(CountingTransport):
    """Fails the test if a request is ever sent without a live reservation."""

    def __init__(self, gov, default):
        super().__init__(default)
        self.gov = gov
        self.unreserved = 0

    def post(self, url, key, payload, timeout=45):
        if self.gov.outstanding <= 0.0:
            self.unreserved += 1
        return super().post(url, key, payload, timeout)


class LazyReservationTests(_Base):
    def test_no_request_is_ever_sent_unreserved_at_any_site(self):
        for name, call in SITES.items():
            with self.subTest(site=name):
                self.setUp()
                gov = self.governor(max_cost=0.5)
                transport = ReservedTransport(gov, echo(100))
                policy = self.keyed(transport, governor=gov)
                call(policy)
                self.assertEqual(transport.unreserved, 0)
                self.assertGreaterEqual(len(transport.calls), 1)
                self.assertEqual(gov.outstanding, 0.0)

    def test_cache_miss_after_a_hit_was_possible_reserves_or_refuses(self):
        gov = self.governor(max_cost=0.5)
        transport = ReservedTransport(gov, noul_resp(tokens=100))
        policy = self.keyed(transport, governor=gov)
        policy.evaluate_diff(DIFF, "change x", "x.py")
        gov.max_cost = gov.spent + 1e-9              # nothing affordable
        policy.evaluator.cache.clear()               # entry evicted/expired
        result, structural = policy.evaluate_diff(DIFF, "change x", "x.py")
        self.assertEqual(len(transport.calls), 1)    # never dispatched
        self.assertEqual(transport.unreserved, 0)
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.is_fallback)         # a budget hard stop
        refusals = [e for e in self.ledger.entries() if e["event"] == "jev_refusal"]
        self.assertTrue(refusals)

    def test_cache_hit_still_works_with_no_budget(self):
        gov = self.governor(max_cost=0.5)
        policy = self.keyed(CountingTransport(noul_resp()), governor=gov)
        policy.evaluate_diff(DIFF, "change x", "x.py")
        gov.max_cost = gov.spent
        result, _ = policy.evaluate_diff(DIFF, "change x", "x.py")
        self.assertTrue(result.cache_hit)

    def test_dispatch_state_is_reset_after_every_call(self):
        from harness.jev import ACTIVE_RESERVER, ACTIVE_SITE
        policy = self.keyed(CountingTransport(echo(10)))
        policy.evaluate_route("x", ["a.py"])
        self.assertEqual(ACTIVE_SITE.get(), "")
        self.assertIsNone(ACTIVE_RESERVER.get())
        policy.evaluate_route("x", ["a.py"])          # cache hit
        self.assertEqual(ACTIVE_SITE.get(), "")
        self.assertIsNone(ACTIVE_RESERVER.get())


class SettlementOverrunTests(_Base):
    def run_site(self, call):
        def respond(payload):
            return {"model": "jev-test",
                    "usage": {"input_tokens": 5000, "output_tokens": 1},
                    "answers": answers_for(payload["questions"])}
        gov = self.governor(max_cost=jev_cost(1024) * 1.5)
        policy = self.keyed(CountingTransport(respond), governor=gov)
        return policy, gov, call(policy)

    def test_real_spend_is_booked_and_ledgered_once_never_erased(self):
        for name in ("diff", "route", "triage"):
            with self.subTest(site=name):
                self.setUp()
                _, gov, _ = self.run_site(SITES[name])
                self.assertAlmostEqual(gov.spent, jev_cost(5000))
                self.assertEqual(gov.overruns, 1)
                self.assertEqual(gov.outstanding, 0.0)
                rows = self.rows()
                self.assertEqual(len(rows), 1)             # the billed row only
                self.assertAlmostEqual(rows[0]["cost"], jev_cost(5000))
                self.assertEqual(rows[0]["input_tokens"], 5000)
                self.assertTrue(rows[0]["settlement_overrun"])
                self.assertFalse(rows[0]["is_fallback"])

    def test_a_paid_answer_is_honored_after_an_overrun(self):
        for noul, verdict in ((0.01, "fail"), (0.99, "pass")):
            with self.subTest(verdict=verdict):
                self.setUp()

                def respond(payload, noul=noul):
                    return {"model": "jev-test",
                            "usage": {"input_tokens": 5000, "output_tokens": 1},
                            "answers": {"instruction_matches": {
                                "type": "noul", "noul": noul}}}
                gov = self.governor(max_cost=jev_cost(1024) * 1.5)
                policy = self.keyed(CountingTransport(respond), governor=gov)
                result, structural = policy.evaluate_diff(DIFF, "set x", "x.py")
                self.assertEqual(result.verdict, verdict)
                self.assertFalse(result.is_fallback)
                self.assertTrue(structural["settlement_overrun"])
                self.assertTrue(any("settlement overrun" in r
                                    for r in result.reasons))
                self.assertEqual(gov.overruns, 1)

    def test_next_call_sees_the_true_spend_and_is_refused(self):
        policy, gov, _ = self.run_site(SITES["route"])
        result, structural = policy.evaluate_route("another goal", ["b.py"])
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["fallback_reason"], "preflight_refused")
        self.assertEqual(gov.overruns, 1)               # the refusal is no overrun

    def test_governor_booking_failure_is_reported_not_raised(self):
        from harness.jev import JevEvaluationResult
        policy = self.keyed(CountingTransport(noul_resp()))
        policy.governor = mock.Mock()
        policy.governor.record_overrun.side_effect = RuntimeError("no")
        with mock.patch("harness.jev_policy.eprint") as eprint:
            policy._book_overrun(0.5, JevEvaluationResult(
                "pass", 0.0, 1.0, {}, [], model="m"))
        self.assertIn("could not be booked", eprint.call_args[0][0])


class SingleFlightTests(_Base):
    Q = {"a": {"type": "noul", "instructions": "ok?"}}

    def hammer(self, ev, n=6):
        out = []
        threads = [threading.Thread(
            target=lambda: out.append(ev.evaluate({"q": 1}, self.Q)))
            for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    def test_followers_neither_reserve_nor_send(self):
        gov = self.governor(max_cost=jev_cost(1024) * 1.5)
        transport = CountingTransport(
            noul_resp(tokens=100), delay={("instruction_matches",): 0.3})
        policy = self.keyed(transport, governor=gov)
        out = []
        bar = threading.Barrier(4)

        def worker():
            bar.wait()
            out.append(policy.evaluate_diff(DIFF, "change x", "x.py")[0])
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(all(not r.is_fallback for r in out), out)
        self.assertEqual(sum(1 for r in out if r.cache_hit), 3)
        self.assertEqual(gov.outstanding, 0.0)

    def test_failed_leader_re_elects_exactly_one_new_leader(self):
        class Flaky:
            calls = 0
            lock = threading.Lock()

            def post(s, *a, **k):
                with s.lock:
                    s.calls += 1
                    n = s.calls
                time.sleep(0.15)
                if n == 1:
                    raise OSError("first fails")
                return 200, {"model": "j",
                             "usage": {"input_tokens": 1, "output_tokens": 1},
                             "answers": {"a": {"type": "noul", "noul": 0.9}}}
        f = Flaky()
        ev = JevEvaluator(api_key="k", transport=f, cache=JevCache())
        out = self.hammer(ev, 6)
        self.assertEqual(f.calls, 2)                 # no thundering herd
        self.assertEqual(sum(1 for r in out if r.is_fallback), 1)
        self.assertEqual(sum(1 for r in out if r.cache_hit), 4)

    def test_coalesces_even_when_the_cache_stores_nothing(self):
        t = CacheHardeningTests.stub(self, delay=0.2)
        ev = JevEvaluator(api_key="k", transport=t,
                          cache=JevCache(max_entries=0))
        out = self.hammer(ev, 6)
        self.assertEqual(t.calls, 1)
        self.assertEqual(sum(1 for r in out if r.cache_hit), 5)

    def test_same_thread_reentrancy_does_not_wait_on_itself(self):
        class Reentrant:
            calls = 0
            ev = None

            def post(s, *a, **k):
                s.calls += 1
                if s.calls == 1:
                    s.ev.evaluate({"q": 1}, SingleFlightTests.Q)  # nested
                return 200, {"model": "j",
                             "usage": {"input_tokens": 1, "output_tokens": 1},
                             "answers": {"a": {"type": "noul", "noul": 0.9}}}
        r = Reentrant()
        r.ev = JevEvaluator(api_key="k", transport=r, cache=JevCache())
        started = time.monotonic()
        result = r.ev.evaluate({"q": 1}, self.Q)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(result.is_fallback)

    def test_a_dead_leader_stalls_followers_only_briefly(self):
        t = CacheHardeningTests.stub(self)
        cache = JevCache()
        ev = JevEvaluator(api_key="k", transport=t, cache=cache)
        key = ev._cache_key({"q": 1}, self.Q)
        cache.begin(key)                             # leader that never finishes
        started = time.monotonic()
        with mock.patch("harness.jev.SINGLE_FLIGHT_WAIT_SECONDS", 0.2):
            result = ev.evaluate({"q": 1}, self.Q)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(result.is_fallback)
        self.assertEqual(t.calls, 1)


class _ListLedger:
    """A free in-memory ledger: these tests write thousands of rows."""

    def __init__(self):
        self.rows = []

    def append(self, event, **fields):
        self.rows.append(dict(fields, event=event))

    def entries(self):
        return self.rows


class FallbackEvictionTests(_Base):
    def test_no_calls_are_lost_to_eviction_at_scale(self):
        ledger = _ListLedger()
        policy = policy_for(self.unkeyed_settings(), ledger=ledger)
        total = written = 0
        for site in range(100):
            for state in range(60):
                for _ in range(3):
                    write, repeat = policy._dedupe(
                        "site{}".format(site), "reason", "k{}".format(state))
                    total += 1
                    written += repeat if write else 0
        self.assertGreater(total, 4096 * 3)
        policy.flush_fallbacks()
        written += sum(e["repeat_count"] for e in ledger.entries())
        self.assertEqual(written, total)
        self.assertEqual(policy.flush_fallbacks(), 0)

    def test_invariant_holds_for_random_traffic_and_midstream_flushes(self):
        import random
        for states in (1, 10, 65, 200):
            for flush_every in (0, 37):
                ledger = _ListLedger()
                random.seed(states * 7 + flush_every)
                policy = policy_for(self.unkeyed_settings(), ledger=ledger)
                with mock.patch("harness.jev_policy.FALLBACK_TRACKED_KEYS", 40):
                    total = written = 0
                    for i in range(1500):
                        write, repeat = policy._dedupe(
                            "s", "r", "k{}".format(random.randrange(states)))
                        total += 1
                        written += repeat if write else 0
                        if flush_every and i % flush_every == 0:
                            policy.flush_fallbacks()
                    policy.flush_fallbacks()
                written += sum(e["repeat_count"] for e in ledger.entries())
                self.assertEqual(written, total, (states, flush_every))


class ParseErrorTests(_Base):
    USAGE = {"input_tokens": 500, "output_tokens": 3}

    def bodies(self):
        u = self.USAGE
        return {
            "answers list": {"model": "j", "usage": u, "answers": [1]},
            "item str": {"model": "j", "usage": u,
                         "answers": {"instruction_matches": "x"}},
            "noul str": {"model": "j", "usage": u, "answers": {
                "instruction_matches": {"type": "noul", "noul": "hi"}}},
            "noul nan": {"model": "j", "usage": u, "answers": {
                "instruction_matches": {"type": "noul", "noul": float("nan")}}},
            "noul huge": {"model": "j", "usage": u, "answers": {
                "instruction_matches": {"type": "noul", "noul": 10 ** 400}}},
            "type unhashable": {"model": "j", "usage": u, "answers": {
                "instruction_matches": {"type": ["noul"], "noul": 0.5}}},
        }

    def test_billed_unparseable_responses_are_discarded_not_failures(self):
        for name, body in self.bodies().items():
            with self.subTest(body=name):
                board = CircuitBreakers(2, 30.0)
                ev = JevEvaluator(api_key="k", transport=CountingTransport(body),
                                  breakers=board, cache=JevCache())
                for _ in range(4):
                    result = ev.evaluate({"x": 1})
                    self.assertTrue(result.discarded)
                    self.assertFalse(result.is_fallback)
                    self.assertEqual(result.input_tokens, 500)
                self.assertEqual(board.snapshot("").get("failures", 0), 0)
                self.assertEqual(len(ev.cache), 0)


class BreakerGenerationTests(_Base):
    def test_a_stale_outcome_cannot_close_clear_or_extend(self):
        now = [0.0]
        board = CircuitBreakers(2, 30.0, clock=lambda: now[0])
        _, slow = board.acquire("s")                 # starts before the trip
        for _ in range(2):
            _, gen = board.acquire("s")
            board.record("s", "failure", gen)
        self.assertFalse(board.would_allow("s"))
        board.record("s", "success", slow)           # stale: ignored
        self.assertFalse(board.would_allow("s"))
        opened = board.snapshot("s")["opened_at"]
        now[0] = 10.0
        board.record("s", "failure", slow)           # stale: no extension
        self.assertEqual(board.snapshot("s")["opened_at"], opened)

    def test_probe_in_flight_blocks_others_and_stale_cannot_clear_it(self):
        now = [0.0]
        board = CircuitBreakers(2, 30.0, clock=lambda: now[0])
        _, old = board.acquire("s")
        for _ in range(2):
            board.record("s", "failure", board.acquire("s")[1])
        now[0] = 31.0
        self.assertTrue(board.would_allow("s"))
        refusal, probe = board.acquire("s")
        self.assertIsNone(refusal)
        self.assertFalse(board.would_allow("s"))     # probe in flight
        self.assertIsNotNone(board.acquire("s")[0])
        board.record("s", "neutral", old)            # stale: probe stays
        self.assertTrue(board.snapshot("s")["probing"])
        board.record("s", "success", probe)
        self.assertTrue(board.would_allow("s"))
        self.assertIsNone(board.acquire("s")[0])

    def test_evaluate_once_respects_and_feeds_the_breaker(self):
        from harness.jev import ACTIVE_SITE
        board = CircuitBreakers(2, 30.0)
        transport = CountingTransport(noul_resp())
        transport.post_once = transport.post
        ev = JevEvaluator(api_key="k", transport=transport, breakers=board)
        questions = {"a": {"type": "noul", "instructions": "ok?"}}
        token = ACTIVE_SITE.set("vision")
        try:
            for _ in range(2):
                board.record("vision", "failure", board.acquire("vision")[1])
            result = ev.evaluate_once({"x": 1}, questions)
        finally:
            ACTIVE_SITE.reset(token)
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.fallback_reason, "circuit_open")
        self.assertEqual(transport.calls, [])

    def test_shared_board_registry_is_capped(self):
        from harness import jev as jev_mod
        jev_mod._SHARED_BREAKERS.clear()
        with mock.patch("harness.jev.SHARED_BREAKER_CAP", 3):
            boards = [jev_mod.shared_breakers("e", "k{}".format(i))
                      for i in range(6)]
            self.assertEqual(len(jev_mod._SHARED_BREAKERS), 3)
            self.assertIs(jev_mod.shared_breakers("e", "k5"), boards[5])
        jev_mod._SHARED_BREAKERS.clear()


class DigestIdentityTests(_Base):
    def test_unreprable_states_differ_by_object_not_by_type_name(self):
        from harness.jev import _digest

        class Mute:
            def __str__(self):
                raise RuntimeError("no str")

            __repr__ = __str__
        a, b = Mute(), Mute()
        self.assertNotEqual(_digest({"x": a}), _digest({"x": b}))
        circ = {}
        circ["me"] = circ
        self.assertEqual(_digest(circ), _digest(circ))


class OtherSiteCacheTests(_Base):
    def test_pack_sites_serve_repeats_free(self):
        from tests.test_jev_issue_sort import sample_pack
        from tests.test_jev_log_judgment import sample_log_pack
        from tests.test_jev_repo_judgment import STATE, repo_pack
        evidence = {dim: {"score": 10.0, "checks_satisfied": 12,
                          "checks_count": 12, "checks": []}
                    for dim in ("A", "R", "SM", "SD")}
        calls = {
            "issue_sort": lambda p: p.evaluate_issue_sort(
                {"issue": "auth token login broken"}, sample_pack()),
            "log_item": lambda p: p.evaluate_log_item(
                {"item": "dial failed on swarm"}, sample_log_pack()),
            "repo_summary": lambda p: p.evaluate_repo_summary(STATE, repo_pack()),
            "audit_dims": lambda p: p.evaluate_audit_dimensions(evidence),
        }
        for name, call in calls.items():
            with self.subTest(site=name):
                self.setUp()
                transport = CountingTransport(echo(100))
                gov = self.governor(max_cost=0.5)
                policy = self.keyed(transport, governor=gov)
                call(policy)
                n, spent = len(transport.calls), gov.spent
                call(policy)
                self.assertEqual(len(transport.calls), n)
                self.assertEqual(gov.spent, spent)
                self.assertTrue([r for r in self.rows() if r.get("cache_hit")])


class FanOutPropagationTests(_Base):
    def test_cancelled_backoff_books_known_cost_and_releases_once(self):
        cancel = threading.Event()
        transport = HttpTransport(cancel_check=cancel.is_set)
        governor = self.governor()
        policy = self.keyed(transport, governor=governor)

        def transient_then_cancel(*_args):
            cancel.set()
            return 429, '{"usage":{"input_tokens":100}}', None

        with mock.patch.object(
                transport, "_request_once_cancellable",
                side_effect=transient_then_cancel) as request:
            with self.assertRaises(ToolCancelled) as cancelled:
                policy.evaluate_answer("question", "candidate", "context")

        expected = jev_cost(100)
        request.assert_called_once()
        self.assertAlmostEqual(cancelled.exception.known_cost, expected)
        self.assertTrue(cancelled.exception.cost_accounted)
        self.assertAlmostEqual(governor.spent, expected)
        self.assertAlmostEqual(governor.outstanding, 0.0)

    def test_workers_inherit_gui_task_and_cancellation_context(self):
        policy = self.keyed(CountingTransport(noul_resp()))
        cancel = threading.Event()
        observed = []

        def sink(event):
            if event.get("type") == "fanout_context_test":
                observed.append(event)

        def job(label):
            emit("fanout_context_test", label=label)
            return current_cancel_check() is not None

        add_sink(sink)
        try:
            with task_context("gui-jev-fanout", cancel.is_set):
                result = policy.fan_out([("a", lambda: job("a")),
                                         ("b", lambda: job("b"))])
        finally:
            remove_sink(sink)
        self.assertEqual(result, [True, True])
        self.assertEqual({event.get("task_id") for event in observed},
                         {"gui-jev-fanout"})

    def test_tool_cancelled_escapes_fanout_and_releases_reservation(self):
        governor = self.governor()
        policy = self.keyed(CountingTransport(noul_resp()), governor=governor)

        def cancel_after_reserving():
            policy._preflight(site="cancelled", max_input_tokens=20)
            raise ToolCancelled()

        with task_context("gui-jev-cancel", lambda: True):
            with self.assertRaises(ToolCancelled):
                policy.fan_out([("cancelled", cancel_after_reserving),
                                ("sibling", lambda: "finished")])
        self.assertEqual(governor._outstanding, 0.0)
        self.assertEqual(self.rows(), [])

    def test_evaluator_propagates_tool_cancellation(self):
        class CancelledTransport:
            def post(self, *_args, **_kwargs):
                raise ToolCancelled()

        evaluator = JevEvaluator(api_key="key", transport=CancelledTransport(),
                                 cache=JevCache())
        with self.assertRaises(ToolCancelled):
            evaluator.evaluate({"request": "cancelled"})

    def test_harness_error_propagates_after_siblings_finish_in_job_order(self):
        policy = self.keyed(CountingTransport(noul_resp()))
        done = []

        def fine():
            time.sleep(0.1)
            done.append("fine")
            return ("ok", {})

        def abort_a():
            raise HarnessError("abort a")

        def abort_b():
            time.sleep(0.02)
            raise HarnessError("abort b")
        with self.assertRaises(HarnessError) as ctx:
            policy.fan_out([("a", fine), ("b", abort_b), ("c", abort_a)])
        self.assertEqual(str(ctx.exception), "abort b")   # first in job order
        self.assertEqual(done, ["fine"])                  # siblings finished

    def test_plain_exceptions_still_fail_closed(self):
        policy = self.keyed(CountingTransport(noul_resp()))

        def boom():
            raise ValueError("x")
        out = policy.fan_out([("a", boom), ("b", lambda: ("ok", {}))])
        self.assertEqual(out[0][1]["fallback_reason"], "fanout_exception")


class AnalyticsBaselineTests(_Base):
    def test_cost_report_skips_cache_hits_and_weights_deduped_rows(self):
        ledger = self.ledger
        ledger.append("jev_eval", model="jev-test", input_tokens=1000,
                      is_fallback=False, cost=jev_cost(1000))
        ledger.append("jev_eval", model="jev-test", input_tokens=0,
                      is_fallback=False, cost=0.0, cache_hit=True)
        ledger.append("jev_eval", model="jev-test", input_tokens=0,
                      is_fallback=True, cost=0.0, repeat_count=8)
        ledger.append("jev_eval", model="jev-test", input_tokens=0,
                      is_fallback=False, cost=0.002)         # vision-style row
        report = ledger.cost_report()
        # Only the keyed row bills, priced from its tokens. The cache hit is
        # not a call; the deduped fallback is free; the tokenless row has
        # nothing observed to price, so it claims nothing.
        self.assertEqual(report["jev"]["calls"], 1)
        self.assertAlmostEqual(report["jev"]["cost"], jev_cost(1000), places=6)
        # 1 priced + 8 represented free + 1 free-but-counted; cache hit absent
        by_tier = ledger.cost_report(by_tier=True)["by_tier"]
        self.assertEqual(sum(t["calls"] for t in by_tier.values()), 10)

    def test_site_export_weights_fallbacks_and_skips_cache_and_flush_rows(self):
        from harness.site_export import build_runs
        ledger = self.ledger
        ledger.append("jev_eval", task_id="t", cost=0.0, is_fallback=True,
                      repeat_count=5, confidence=0.0)
        ledger.append("jev_eval", task_id="t", cost=0.0, is_fallback=False,
                      cache_hit=True, confidence=0.9)
        ledger.append("jev_eval", task_id="t", cost=0.0, is_fallback=True,
                      flush=True, repeat_count=3, confidence=0.0)
        ledger.append("jev_eval", task_id="t", cost=0.001, is_fallback=False,
                      confidence=0.8)
        ledger.append("verify_round", task_id="t", passed=True, model="m")
        runs = build_runs(ledger.entries())
        evals = runs[0]["jev_evals"]
        self.assertEqual(evals["fallback"], 8)
        self.assertEqual(evals["count"], 2)         # fresh observations only


class CoverageGapTests2(_Base):
    def test_reentrant_key_still_serves_a_cache_hit(self):
        t = CacheHardeningTests.stub(self)
        ev = JevEvaluator(api_key="k", transport=t, cache=JevCache())
        first = ev.evaluate({"q": 1}, SingleFlightTests.Q)
        self.assertFalse(first.cache_hit)
        key = ev._cache_key({"q": 1}, SingleFlightTests.Q)
        ev._led.__dict__["keys"] = {key}            # this thread is the leader
        second = ev.evaluate({"q": 1}, SingleFlightTests.Q)
        self.assertTrue(second.cache_hit)
        self.assertEqual(t.calls, 1)

    def test_plan_site_refusal_degrades_with_a_reason(self):
        policy = self.keyed(CountingTransport(noul_resp()),
                            governor=self.governor(max_cost=0.0000001))
        result, structural = policy.evaluate_plan("fix loop", ["a.py"])
        self.assertEqual(result.verdict, "fail")
        refusals = [e for e in self.ledger.entries() if e["event"] == "jev_refusal"]
        self.assertEqual(refusals[0]["site"], "waist")

    def test_a_junk_stored_cost_cannot_erase_a_real_keyed_call(self):
        # The stored cost is never read, so a garbage value is inert rather
        # than fatal: the row is priced from its tokens like any other, and a
        # fallback still claims nothing.
        self.ledger.append("jev_eval", model="jev-test", input_tokens=1000,
                           is_fallback=False)
        self.ledger.append("jev_eval", model="jev-test", input_tokens=50,
                           is_fallback=False, cost="oops")
        self.ledger.append("jev_eval", model="jev-test", input_tokens=5,
                           is_fallback=True)
        report = self.ledger.cost_report()
        self.assertEqual(report["jev"]["calls"], 2)
        self.assertAlmostEqual(report["jev"]["cost"],
                               jev_cost(1000) + jev_cost(50), places=6)

    def test_site_export_tolerates_a_garbage_repeat_count(self):
        from harness.site_export import build_runs
        self.ledger.append("jev_eval", task_id="t", cost=0.0, is_fallback=True,
                           repeat_count="many", confidence=0.0)
        self.ledger.append("verify_round", task_id="t", passed=True, model="m")
        self.assertEqual(build_runs(self.ledger.entries())[0]["jev_evals"]["fallback"], 1)


class DispatchStateLabelTests(_Base):
    def test_failure_after_reservation_is_post_dispatch_before_is_not(self):
        from harness.jev import ACTIVE_RESERVER
        gov = self.governor()
        policy = self.keyed(CountingTransport(noul_resp()), governor=gov)

        def reserve_then_fail(*a, **k):
            ACTIVE_RESERVER.get()()
            raise HarnessError("late failure")
        with mock.patch.object(policy.evaluator, "evaluate",
                               side_effect=reserve_then_fail):
            result, structural = policy.evaluate_answer("q", "a", "c")
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["fallback_reason"], "transport_failure")
        self.assertEqual(gov.outstanding, 0.0)
        with mock.patch.object(policy.evaluator, "evaluate",
                               side_effect=HarnessError("early failure")):
            result, structural = policy.evaluate_answer("q2", "a", "c")
        self.assertFalse(result.is_fallback)             # nothing was sent


class SpendInvariantTests(_Base):
    def test_zero_settlements_never_trip_a_breached_ceiling(self):
        gov = self.governor(max_cost=0.1)
        token = gov.reserve(0.01, "jev:x")
        gov.record_overrun(0.2, "x")                  # spent is now past the cap
        gov.reconcile(token, 0.0)                     # releasing is fine
        gov.record_actual(0.0, "x")                   # a free settlement too
        with self.assertRaises(HarnessError):
            gov.record_actual(0.001, "x")             # real spend still refused
        self.assertEqual(gov.outstanding, 0.0)

    def test_cache_hit_after_an_overrun_stays_a_cache_hit(self):
        gov = self.governor(max_cost=jev_cost(1024) * 3)
        transport = CountingTransport(echo(100))
        policy = self.keyed(transport, governor=gov)
        policy.evaluate_route("A", ["a.py"])
        gov.record_overrun(gov.max_cost, "elsewhere")
        before = gov.overruns
        result, structural = policy.evaluate_route("A", ["a.py"])
        self.assertTrue(result.cache_hit)
        self.assertFalse(result.is_fallback)
        self.assertEqual(gov.overruns, before)        # no phantom overrun
        self.assertEqual(len(transport.calls), 1)

    def test_refused_site_after_an_overrun_books_no_phantom_overrun(self):
        gov = self.governor(max_cost=jev_cost(1024) * 1.5)
        policy = self.keyed(CountingTransport(echo(5000)), governor=gov)
        policy.evaluate_route("fix loop", ["a.py"])
        self.assertEqual(gov.overruns, 1)
        policy.evaluate_hourglass_stage("context_intake", {"request": "x"})
        policy.evaluate_route("other", ["b.py"])
        self.assertEqual(gov.overruns, 1)

    def test_overrun_is_booked_only_for_positive_cost(self):
        from harness.jev import JevEvaluationResult
        policy = self.keyed(CountingTransport(noul_resp()))
        policy.governor = mock.Mock()
        result = JevEvaluationResult("pass", 0.0, 1.0, {}, [], model="m")
        policy._book_overrun(0.0, result)
        policy.governor.record_overrun.assert_not_called()
        policy._book_overrun(0.5, result)
        policy.governor.record_overrun.assert_called_once()

    def test_snapshot_and_status_expose_overruns_only_when_present(self):
        gov = self.governor()
        self.assertNotIn("overruns", gov.snapshot())
        gov.key_info = {"label": "x"}
        self.assertNotIn("overruns", gov.key_status())
        gov.record_overrun(0.01, "x")
        self.assertEqual(gov.snapshot()["overruns"], 1)
        self.assertEqual(gov.key_status()["overruns"], 1)


class VisionNothingSentTests(_Base):
    def test_open_breaker_settles_zero_not_the_estimate(self):
        from tests import test_jev_vision_assessment as V

        class Failing(V.RecordingTransport):
            def post_once(self, url, key, payload, timeout=120):
                self.calls.append(payload)
                raise OSError("down")
        gov = self.governor(max_cost=1.0)
        settings = load_settings({"jev_api_key": "k", "jev_model": "jev-test"})
        transport = Failing()
        policy = policy_for(settings, transport=transport, governor=gov,
                            ledger=self.ledger, breaker_threshold=1)
        policy.evaluate_vision_assessment({"assessment": "hourglass"})
        self.assertEqual(len(transport.calls), 1)
        spent = gov.spent
        envelope = policy.evaluate_vision_assessment({"assessment": "hourglass"})
        self.assertEqual(len(transport.calls), 1)       # nothing was sent
        self.assertEqual(gov.spent, spent)              # and nothing charged
        self.assertEqual(gov.outstanding, 0.0)
        self.assertNotEqual(envelope.status, "assessed")


class PayloadBoundTests(_Base):
    def test_oversized_payload_reserves_for_itself_and_is_refused_not_overrun(self):
        gov = self.governor(max_cost=jev_cost(2048))
        transport = CountingTransport(noul_resp())
        policy = self.keyed(transport, governor=gov)
        result, _ = policy.evaluate_diff(DIFF, "x" * 60000, "x.py")
        self.assertEqual(transport.calls, [])            # never dispatched
        self.assertEqual(result.verdict, "fail")
        self.assertFalse(result.is_fallback)             # a budget hard stop
        self.assertEqual(gov.overruns, 0)
        small, _ = policy.evaluate_diff(DIFF, "set x", "x.py")
        self.assertFalse(small.is_fallback)

    def test_implausible_reported_usage_is_settled_at_the_estimate(self):
        gov = self.governor(max_cost=1.0)
        transport = CountingTransport(echo(2000000))
        policy = self.keyed(transport, governor=gov)
        result, structural = policy.evaluate_route("fix loop", ["a.py"])
        self.assertTrue(result.discarded)
        self.assertEqual(structural["fallback_reason"], "usage_implausible")
        self.assertLess(result.cost, jev_cost(200000))
        self.assertAlmostEqual(gov.spent, result.cost)
        self.assertEqual(gov.overruns, 0)
        self.assertEqual(len(policy.evaluator.cache), 0)  # never cached
        self.assertEqual(gov.outstanding, 0.0)


class ReservationGuardTests(_Base):
    Q = {"a": {"type": "noul", "instructions": "ok?"}}

    def test_a_missing_reservation_never_dispatches(self):
        from harness.jev import ACTIVE_GUARD
        transport = CountingTransport(noul_resp())
        ev = JevEvaluator(api_key="k", transport=transport, cache=JevCache())
        token = ACTIVE_GUARD.set(lambda: False)
        try:
            result = ev.evaluate({"x": 1}, self.Q)
        finally:
            ACTIVE_GUARD.reset(token)
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.fallback_reason, "reservation_missing")
        self.assertEqual(transport.calls, [])

    def test_one_reservation_pays_for_exactly_one_dispatch(self):
        gov = self.governor()
        transport = CountingTransport(echo(10))
        policy = self.keyed(transport, governor=gov)
        handle = policy._preflight(site="route", max_input_tokens=1024)
        first = policy.evaluator.evaluate({"x": 1}, self.Q)
        second = policy.evaluator.evaluate({"x": 2}, self.Q)   # same reservation
        self.assertFalse(first.is_fallback)
        self.assertEqual(second.fallback_reason, "reservation_missing")
        self.assertEqual(len(transport.calls), 1)
        policy._account(first, site="route", reservation=handle)
        self.assertEqual(gov.outstanding, 0.0)


class OverrunEverywhereTests(_Base):
    def test_every_site_survives_an_overrun_consistently(self):
        from tests.test_jev_chaos import sites as all_sites
        for name in all_sites(0):
            with self.subTest(site=name):
                self.setUp()
                gov = self.governor(max_cost=jev_cost(1024) * 1.5)
                policy = self.keyed(CountingTransport(echo(5000)), governor=gov)
                all_sites(0)[name](policy)               # must not raise
                self.assertEqual(gov.outstanding, 0.0)
                billed = sum(r["cost"] for r in self.rows()
                             if not r.get("cache_hit") and not r.get("flush"))
                self.assertAlmostEqual(gov.spent, billed, places=9)
                self.assertGreater(gov.spent, 0.0)
                self.assertEqual(sum(1 for r in self.rows() if r["cost"] > 0), 1)


class FlushWiringTests(_Base):
    def test_flush_all_fallbacks_writes_every_live_tail(self):
        from harness.jev_policy import flush_all_fallbacks
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for _ in range(9):
            policy.evaluate_triage("same", ["a.py"])
        self.assertEqual(sum(r.get("repeat_count", 1) for r in self.rows()), 8)
        self.assertGreaterEqual(flush_all_fallbacks(), 1)
        self.assertEqual(sum(r.get("repeat_count", 1) for r in self.rows()), 9)

    def test_flush_never_breaks_shutdown(self):
        from harness.jev_policy import flush_all_fallbacks
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        policy.flush_fallbacks = mock.Mock(side_effect=OSError("disk"))
        flush_all_fallbacks()                            # swallowed

    def test_command_wrapper_flushes_even_when_the_command_fails(self):
        from harness import cli
        with mock.patch("harness.cli.flush_all_fallbacks") as flush:
            @cli._flushes_fallbacks
            def boom():
                raise RuntimeError("x")
            with self.assertRaises(RuntimeError):
                boom()
        flush.assert_called_once()

    def test_run_mission_flushes_when_it_ends(self):
        from harness import mission_driver
        with mock.patch.object(mission_driver, "_run_mission",
                               return_value={"ok": 1}), \
                mock.patch.object(mission_driver, "flush_all_fallbacks") as flush:
            self.assertEqual(mission_driver.run_mission(object()), {"ok": 1})
        flush.assert_called_once()


class DedupeScaleTests(_Base):
    def test_exact_totals_at_scale_across_random_traffic(self):
        import collections
        import random
        for seed in range(3):
            ledger = _ListLedger()
            policy = policy_for(self.unkeyed_settings(), ledger=ledger)
            rnd = random.Random(seed)
            truth = collections.Counter()
            for _ in range(30000):
                site, reason = rnd.choice("abc"), rnd.choice(["r1", "r2"])
                state = str(rnd.randrange(rnd.choice([5, 200, 6000, 20000])))
                write, repeat = policy._dedupe(site, reason, state)
                truth[(site, reason)] += 1
                if write:
                    ledger.append("jev_eval", site=site, fallback_reason=reason,
                                  repeat_count=repeat)
                if rnd.random() < 0.0005:
                    policy.flush_fallbacks()
            policy.flush_fallbacks()
            got = collections.Counter()
            for row in ledger.rows:
                got[(row["site"], row["fallback_reason"])] += row["repeat_count"]
            self.assertEqual(got, truth, seed)


class StrictPathUsageTests(_Base):
    Q = {"a": {"type": "noul", "instructions": "ok?"}}

    def strict(self, reported, state=None):
        resp = {"model": "jev-test",
                "usage": {"input_tokens": reported, "output_tokens": 2},
                "answers": {"a": {"type": "noul", "noul": 0.9}}}
        transport = CountingTransport(resp)
        transport.post_once = transport.post
        ev = JevEvaluator(api_key="k", transport=transport, cache=JevCache())
        return ev, ev.evaluate_once(state if state is not None else {"x": 1},
                                    self.Q)

    def test_absurd_strict_usage_is_bounded_and_flagged(self):
        from harness.jev import _usage_cap
        for reported in (10 ** 9, 10 ** 15):
            ev, result = self.strict(reported)
            estimate = ev._estimate_tokens({"x": 1}, self.Q)
            self.assertEqual(result.fallback_reason, "usage_implausible")
            self.assertTrue(result.discarded)
            self.assertTrue(result.input_tokens_observed)
            self.assertEqual(result.input_tokens, _usage_cap(estimate))
            self.assertAlmostEqual(result.cost, jev_cost(_usage_cap(estimate)))
            self.assertEqual(ev.breakers.snapshot("").get("failures", 0), 0)

    def test_settlement_is_monotonic_across_the_cap(self):
        from harness.jev import _usage_cap
        ev, _ = self.strict(1)
        cap = _usage_cap(ev._estimate_tokens({"x": 1}, self.Q))
        costs = []
        for reported in (cap - 1, cap, cap + 1, cap * 3):
            _, result = self.strict(reported)
            costs.append(result.cost)
        self.assertEqual(costs, sorted(costs))             # never decreases
        self.assertAlmostEqual(costs[-1], jev_cost(cap))   # bounded at the cap
        self.assertAlmostEqual(costs[0], jev_cost(cap - 1))

    def test_suspiciously_low_usage_settles_at_a_quarter_of_the_estimate(self):
        state = {"blob": "word " * 2500}                    # ~4k-token payload
        ev, result = self.strict(10, state)
        estimate = ev._estimate_tokens(state, self.Q)
        self.assertGreater(estimate, 1000)
        self.assertEqual(result.input_tokens, int(estimate * 0.25))
        self.assertAlmostEqual(result.cost, jev_cost(int(estimate * 0.25)))
        self.assertTrue(any("usage_suspiciously_low" in r for r in result.reasons))
        self.assertFalse(result.is_fallback)
        _, honest = self.strict(int(estimate * 0.5), state)  # plausible: untouched
        self.assertAlmostEqual(honest.cost, jev_cost(int(estimate * 0.5)))
        _, small = self.strict(1)                            # tiny payload: exempt
        self.assertAlmostEqual(small.cost, jev_cost(1))

    def test_loose_path_applies_the_same_low_rule_and_clamps_failures(self):
        state = {"blob": "word " * 2500}
        transport = CountingTransport({
            "model": "j", "usage": {"input_tokens": 5, "output_tokens": 1},
            "answers": {"a": {"type": "noul", "noul": 0.9}}})
        ev = JevEvaluator(api_key="k", transport=transport, cache=JevCache())
        result = ev.evaluate(state, self.Q)
        estimate = ev._estimate_tokens(state, self.Q)
        self.assertEqual(result.input_tokens, int(estimate * 0.25))
        bad = {"model": "j", "usage": {"input_tokens": 10 ** 12, "output_tokens": 1},
               "answers": {"zz": 1}}
        ev2 = JevEvaluator(api_key="k", transport=CountingTransport(bad),
                           cache=JevCache())
        failed = ev2.evaluate({"x": 1}, self.Q)
        self.assertTrue(failed.discarded)
        self.assertLessEqual(failed.input_tokens, 10 ** 7)

    def test_vision_never_books_an_absurd_response(self):
        from tests import test_jev_vision_assessment as V
        for tokens in (900, 10 ** 9, 10 ** 15):
            with self.subTest(tokens=tokens):
                self.setUp()
                gov = self.governor(max_cost=1.0)
                resp = V._response()
                resp["usage"] = {"input_tokens": tokens, "output_tokens": 5}
                transport = V.RecordingTransport(response=resp)
                policy = policy_for(
                    load_settings({"jev_api_key": "k", "jev_model": "jev-test"}),
                    transport=transport, governor=gov, ledger=self.ledger)
                policy.evaluate_vision_assessment({"assessment": "hourglass"})
                self.assertLess(gov.spent, jev_cost(10 ** 7) + 1e-9)
                self.assertEqual(gov.overruns, 0)
                self.assertEqual(gov.outstanding, 0.0)
                policy.evaluate_vision_assessment({"assessment": "hourglass"})


class FlushRobustnessTests(_Base):
    def test_snapshot_survives_concurrent_policy_creation(self):
        from harness import jev_policy as JP

        class Dummy:
            def flush_fallbacks(self):
                return 0
        stop = threading.Event()
        keep = []

        def adder():
            while not stop.is_set():
                d = Dummy()
                keep.append(d)
                JP._LIVE_POLICIES.add(d)
                if len(keep) > 200:
                    keep.clear()
        import weakref
        threads = [threading.Thread(target=adder, daemon=True) for _ in range(3)]
        with mock.patch.object(JP, "_LIVE_POLICIES", weakref.WeakSet()):
            for t in threads:
                t.start()
            try:
                for _ in range(800):
                    JP.flush_all_fallbacks()          # must never raise
            finally:
                stop.set()
                for t in threads:
                    t.join()

    def test_snapshot_retries_a_transient_iteration_error(self):
        from harness import jev_policy as JP

        class Flaky:
            calls = 0

            def __iter__(self):
                Flaky.calls += 1
                if Flaky.calls < 3:
                    raise RuntimeError("Set changed size during iteration")
                return iter(())
        with mock.patch.object(JP, "_LIVE_POLICIES", Flaky()):
            self.assertEqual(JP._live_policies(), ())
            self.assertEqual(Flaky.calls, 3)
        with mock.patch.object(JP, "_LIVE_POLICIES", mock.MagicMock(
                __iter__=mock.Mock(side_effect=RuntimeError("always")))):
            self.assertEqual(JP._live_policies(), ())     # gives up, no raise
            self.assertEqual(JP.flush_all_fallbacks(), 0)

    def test_a_deleted_ledger_directory_is_not_recreated_and_tails_survive(self):
        import shutil
        policy = policy_for(self.unkeyed_settings(), ledger=self.ledger)
        for _ in range(3):
            policy.evaluate_triage("same", ["a.py"])
        directory = os.path.dirname(self.ledger.path)
        os.makedirs(directory, exist_ok=True)
        before = sum(r.get("repeat_count", 1) for r in self.rows())
        shutil.rmtree(directory)
        self.assertEqual(policy.flush_fallbacks(), 0)
        self.assertFalse(os.path.exists(directory))      # not resurrected
        os.makedirs(directory)
        self.assertGreaterEqual(policy.flush_fallbacks(), 1)
        self.assertEqual(sum(r.get("repeat_count", 1) for r in self.rows()) - before, 1)

    def test_a_failed_append_restores_the_tail_counters(self):
        ledger = _ListLedger()
        policy = policy_for(self.unkeyed_settings(), ledger=ledger)
        for _ in range(9):
            policy.evaluate_triage("same", ["a.py"])
        for state in range(3):
            for _ in range(3):
                policy.evaluate_triage("other {}".format(state), ["a.py"])
        total = 9 + 9
        written = sum(r.get("repeat_count", 1) for r in ledger.rows)
        real_append = ledger.append
        calls = []

        def flaky(event, **fields):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_append(event, **fields)
        ledger.append = flaky
        with self.assertRaises(OSError):
            policy.flush_fallbacks()
        ledger.append = real_append
        policy.flush_fallbacks()
        self.assertEqual(sum(r.get("repeat_count", 1) for r in ledger.rows), total)
        self.assertGreater(total, written)
        self.assertEqual(policy.flush_fallbacks(), 0)

    def test_exit_flush_is_bounded_and_survives_a_missing_thread(self):
        from harness import jev_policy as JP

        def stuck():
            time.sleep(5)
        started = time.monotonic()
        with mock.patch.object(JP, "flush_all_fallbacks", stuck), \
                mock.patch.object(JP, "ATEXIT_FLUSH_TIMEOUT_SECONDS", 0.2):
            JP._flush_at_exit()
        self.assertLess(time.monotonic() - started, 2.0)
        inline = mock.Mock()
        with mock.patch.object(JP, "flush_all_fallbacks", inline), \
                mock.patch.object(JP.threading, "Thread",
                                  side_effect=RuntimeError("no threads")):
            JP._flush_at_exit()
        inline.assert_called_once()
        with mock.patch.object(JP, "flush_all_fallbacks",
                               side_effect=OSError("x")), \
                mock.patch.object(JP.threading, "Thread",
                                  side_effect=RuntimeError("no threads")):
            JP._flush_at_exit()                          # swallowed


class ReservationAtomicityTests(unittest.TestCase):
    def test_concurrent_acquire_reserves_once_and_consume_needs_a_token(self):
        from harness.jev_policy import _Reservation

        class Pol:
            def __init__(self):
                self.n = 0
                self.lock = threading.Lock()

            def _reserve(self, site, tokens):
                with self.lock:
                    self.n += 1
                time.sleep(0.002)
                return ("t", 1)
        for _ in range(60):
            pol = Pol()
            handle = _Reservation(pol, "s", 1024)
            wins, early = [], []

            def consumer():
                got = handle.consume()
                if got:
                    wins.append(1)
                    if handle.token is None:
                        early.append(1)
            threads = [threading.Thread(target=handle.acquire) for _ in range(4)]
            threads += [threading.Thread(target=consumer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(pol.n, 1)
            self.assertEqual(early, [])
            self.assertLessEqual(len(wins), 1)


if __name__ == "__main__":
    unittest.main()
