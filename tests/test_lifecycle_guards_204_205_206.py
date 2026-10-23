"""Lifecycle guards for issues #204 (429 breaker), #205 (refusal-loop guard
+ phase memo), #206 (honest terminal renderer).

Hermetic: no network, no model spend. Time is injected (fake clock), the
waist ladder's confirm seam is patched, and the agent refusal surface is
driven with a stubbed conversation lane.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent import AutonomousAgent
from harness.config import DEFAULT_MAX_COST, DEFAULT_TASK_MAX_COST, load_settings
from harness.errors import HarnessError, ToolCancelled
from harness.lifecycle_guards import (
    AUTH_FAILED,
    BUDGET_REFUSED,
    MODEL_UNAVAILABLE,
    RATE_LIMITED,
    SPEND_EXHAUSTED,
    UNKNOWN_FAILURE,
    PhaseMemo,
    RateCircuit,
    RefusalLoopGuard,
    classify_harness_error,
    classify_provider_failure,
    dedupe_notices,
    has_write_surface,
    is_missing_tool_refusal,
    is_terminal_failure,
    render_gate_guard,
    render_refused_plan,
)


def _fake_clock(start=1000.0):
    now = [float(start)]

    def clock():
        return now[0]

    def advance(seconds):
        now[0] += seconds

    return clock, advance


def _agent(tmp):
    settings = load_settings()
    settings.max_cost = DEFAULT_MAX_COST
    settings.task_max_cost = DEFAULT_TASK_MAX_COST
    settings.jev_api_key = None
    return AutonomousAgent(settings=settings, root_dir=Path(tmp),
                           history_dir=Path(tmp))


# ---------------------------------------------------------------------------
# #204: classification
# ---------------------------------------------------------------------------

class ClassifyProviderFailureTests(unittest.TestCase):
    def test_plain_429_is_rate_limited(self):
        self.assertEqual(
            classify_provider_failure(429, {"error": {"message": "Rate limit exceeded"}}),
            RATE_LIMITED)

    def test_429_carrying_quota_text_is_spend_exhausted(self):
        # Providers report quota exhaustion as 429; it must stop terminally.
        self.assertEqual(
            classify_provider_failure(429, {"error": {"message": "insufficient quota, top up"}}),
            SPEND_EXHAUSTED)

    def test_402_is_spend_exhausted(self):
        self.assertEqual(classify_provider_failure(402, {"error": {"message": "credit exhausted"}}),
                         SPEND_EXHAUSTED)

    def test_401_is_auth_failed(self):
        self.assertEqual(classify_provider_failure(401, {"error": {"message": "invalid api key"}}),
                         AUTH_FAILED)

    def test_404_is_model_unavailable(self):
        self.assertEqual(classify_provider_failure(404, {"error": {"message": "model not found"}}),
                         MODEL_UNAVAILABLE)

    def test_5xx_is_model_unavailable(self):
        self.assertEqual(classify_provider_failure(503, {"error": {"message": "overloaded"}}),
                         MODEL_UNAVAILABLE)

    def test_harness_budget_shapes_are_budget_refused(self):
        self.assertEqual(classify_harness_error("jev:x worst-case $1 would exceed phase ceiling $2"),
                         BUDGET_REFUSED)
        self.assertEqual(classify_harness_error("worst-case $1 would eat terminal_reserve $2"),
                         BUDGET_REFUSED)

    def test_http_prefix_in_harness_error_classifies(self):
        self.assertEqual(classify_harness_error("HTTP 429: Rate limit exceeded"),
                         RATE_LIMITED)
        self.assertEqual(classify_harness_error("HTTP 402: credit exhausted"),
                         SPEND_EXHAUSTED)

    def test_spend_and_budget_are_terminal_but_429_is_not(self):
        self.assertTrue(is_terminal_failure(SPEND_EXHAUSTED))
        self.assertTrue(is_terminal_failure(BUDGET_REFUSED))
        self.assertFalse(is_terminal_failure(RATE_LIMITED))
        self.assertFalse(is_terminal_failure(AUTH_FAILED))
        self.assertFalse(is_terminal_failure(MODEL_UNAVAILABLE))
        self.assertFalse(is_terminal_failure(UNKNOWN_FAILURE))

    def test_rate_limit_exceeded_is_never_misread_as_budget(self):
        # DF-JEV-4 narrowed the budget matcher for exactly this reason.
        self.assertEqual(classify_harness_error("rate limit exceeded, retry later"),
                         RATE_LIMITED)


# ---------------------------------------------------------------------------
# #204: circuit breaker
# ---------------------------------------------------------------------------

class RateCircuitTests(unittest.TestCase):
    def test_limited_model_is_skipped_inside_cooldown(self):
        clock, _ = _fake_clock()
        circuit = RateCircuit(clock=clock)
        circuit.note_rate_limited("m1")
        self.assertTrue(circuit.should_skip("m1"))
        self.assertFalse(circuit.should_skip("m2"))

    def test_cooldown_expires(self):
        clock, advance = _fake_clock()
        circuit = RateCircuit(cooldown_s=60.0, clock=clock)
        circuit.note_rate_limited("m1")
        advance(61.0)
        self.assertFalse(circuit.should_skip("m1"))

    def test_available_filters_the_ladder_in_order(self):
        clock, _ = _fake_clock()
        circuit = RateCircuit(clock=clock)
        circuit.note_rate_limited("m1")
        circuit.note_rate_limited("m3")
        self.assertEqual(circuit.available(["m1", "m2", "m3"]), ["m2"])

    def test_wait_ceiling_stops_the_run_honestly(self):
        clock, _ = _fake_clock()
        circuit = RateCircuit(max_wait_s=30.0, clock=clock)
        circuit.note_rate_limited("m1", wait_s=29.0)
        self.assertFalse(circuit.wait_exhausted())
        circuit.note_rate_limited("m2", wait_s=2.0)
        self.assertTrue(circuit.wait_exhausted())
        with self.assertRaisesRegex(HarnessError, "retry-wait budget exhausted"):
            circuit.check_wait_budget()

    def test_429_storm_skips_dead_models_and_stays_in_budget(self):
        # Later attempts skip dead models and total waiting stays capped.
        clock, advance = _fake_clock()
        circuit = RateCircuit(cooldown_s=60.0, max_wait_s=30.0, clock=clock)
        ladder = ["m1", "m2", "m3"]
        waited = 0.0
        for attempt in range(3):
            runnable = circuit.available(ladder)
            self.assertTrue(runnable, "the run must always have a live model or stop")
            if attempt < 2:
                dead = runnable[0]
                advance(5.0)
                waited += 5.0
                circuit.note_rate_limited(dead, wait_s=5.0)
        self.assertEqual(circuit.available(ladder), ["m3"])
        self.assertLessEqual(waited, 30.0)
        circuit.check_wait_budget()  # still inside the ceiling: no raise


# ---------------------------------------------------------------------------
# #205: refusal-loop guard + phase memo
# ---------------------------------------------------------------------------

class RefusalLoopGuardTests(unittest.TestCase):
    def test_missing_tool_kind_stops_immediately(self):
        guard = RefusalLoopGuard()
        self.assertEqual(guard.check("missing_tool", "web capability is detached"), "stop")
        self.assertEqual(guard.attempts, 0)

    def test_missing_tool_reason_text_stops_immediately(self):
        guard = RefusalLoopGuard()
        self.assertEqual(guard.check("environmental", "missing capability: no web tool"), "stop")

    def test_missing_tool_detector_matches_capability_absences(self):
        self.assertTrue(is_missing_tool_refusal("web capability is detached"))
        self.assertTrue(is_missing_tool_refusal("missing tool: browser"))
        self.assertFalse(is_missing_tool_refusal("scope too broad for one pass"))
        self.assertFalse(is_missing_tool_refusal(""))

    def test_same_refusal_twice_terminates(self):
        guard = RefusalLoopGuard()
        self.assertEqual(guard.check("environmental", "scope too broad for one pass"), "continue")
        self.assertEqual(guard.check("environmental", "scope too broad for one pass"), "stop")

    def test_different_refusal_is_useful_refinement(self):
        guard = RefusalLoopGuard()
        self.assertEqual(guard.check("environmental", "scope too broad for one pass"), "continue")
        self.assertEqual(guard.check("policy", "needs an explicit file target"), "continue")

class PhaseMemoTests(unittest.TestCase):
    def test_unchanged_inputs_hit_but_changed_inputs_miss(self):
        memo = PhaseMemo()
        self.assertIsNone(memo.get("scope", {"files": ["a.py"]}))
        memo.put("scope", {"files": ["a.py"]}, ["a.py"])
        self.assertEqual(memo.get("scope", {"files": ["a.py"]}), ["a.py"])
        self.assertIsNone(memo.get("scope", {"files": ["a.py", "b.py"]}))

    def test_invalidate_clears_on_evidence_change(self):
        memo = PhaseMemo()
        memo.put("brief", {"q": "x"}, "cached")
        memo.invalidate()
        self.assertIsNone(memo.get("brief", {"q": "x"}))
        self.assertEqual(len(memo), 0)

    def test_key_is_order_stable(self):
        self.assertEqual(PhaseMemo.stable_key({"b": 1, "a": 2}),
                         PhaseMemo.stable_key({"a": 2, "b": 1}))


# ---------------------------------------------------------------------------
# #206: honest terminal rendering
# ---------------------------------------------------------------------------

class HonestRendererTests(unittest.TestCase):
    def test_refused_plan_names_the_reason_and_never_claims_verified(self):
        text = render_refused_plan("Update util.py", "scope too broad", stages=["context"])
        self.assertIn("refused", text.lower())
        self.assertIn("scope too broad", text)
        self.assertNotIn("verified", text.lower())
        self.assertNotIn("Analysis & Proposed Plan", text)

    def test_empty_plan_says_no_stages(self):
        text = render_refused_plan("Update util.py", "waist refused")
        self.assertIn("No executable stages were produced", text)
        self.assertNotIn("verified", text.lower())

    def test_listed_stages_are_labeled_unconfirmed(self):
        text = render_refused_plan("Update util.py", "waist refused",
                                   nodes=[{"name": "n1", "summary": "do the chunk"}])
        self.assertIn("n1", text)
        self.assertIn("unconfirmed", text.lower())

    def test_gate_guard_absent_without_a_write_surface(self):
        self.assertEqual(render_gate_guard("waist refused", False), "")
        self.assertFalse(has_write_surface([], {"nodes": []}))
        self.assertFalse(has_write_surface([], None))

    def test_gate_guard_present_with_a_write_surface(self):
        self.assertIn("Gate Guard", render_gate_guard("waist refused", True))
        self.assertTrue(has_write_surface(["util.py"], None))
        self.assertTrue(has_write_surface([], {"nodes": [{"target_files": ["a.py"]}]}))

    def test_identical_blocks_deduplicated_to_one(self):
        block = render_gate_guard("waist refused", True)
        self.assertEqual(dedupe_notices([block, block, block]), [block])
        self.assertEqual(dedupe_notices([block, "", None, block]), [block])


class RefusedEditIntegrationTests(unittest.TestCase):
    def _refused(self, **extra):
        plan = {"status": "refused", "confirmation": {"reason": "scope too broad"},
                "dag": {"nodes": []}, "nodes": []}
        plan.update(extra)
        return plan

    def test_read_only_refusal_has_no_file_write_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch.object(agent, "_handle_conversation",
                              side_effect=RuntimeError("lane down")):
                res = agent._refused_edit(self._refused(), "is example.com up?", [], "s1")
        self.assertEqual(res["status"], "refused")
        self.assertIn("refused", res["response"].lower())
        self.assertIn("scope too broad", res["response"])
        self.assertNotIn("verified", res["response"].lower())
        self.assertNotIn("Gate Guard", res["response"])

    def test_write_surface_refusal_carries_exactly_one_guard_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch.object(agent, "_handle_conversation",
                              side_effect=RuntimeError("lane down")):
                res = agent._refused_edit(self._refused(), "Update util.py",
                                          ["util.py"], "s2")
        self.assertEqual(res["status"], "refused")
        self.assertEqual(res["response"].count("Autonomous Waist Gate Guard"), 1)

    def test_conversational_answer_still_wins_when_real(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch.object(agent, "_handle_conversation",
                              return_value={"response": "  util.py sets x.  "}):
                res = agent._refused_edit(self._refused(), "what is x?", ["util.py"], "s3")
        self.assertTrue(res["response"].startswith("util.py sets x."))
        self.assertIn("scope too broad", res["response"])


# ---------------------------------------------------------------------------
# Waist wiring: the ladder honors the circuit; missing tools skip re-plan
# ---------------------------------------------------------------------------

class WaistBreakerWiringTests(unittest.TestCase):
    def _compose(self, **overrides):
        from tests._fake import FakeTransport, _gov
        from harness.waist import compose_plan
        gov = _gov(FakeTransport(), max_cost=0.05)
        kwargs = dict(transport=FakeTransport(), api_key="k", governor=gov, ledger=None,
                      opts_goal="Update the helper", candidate_files=["pkg/mod.py"],
                      decompose_llm=False, confirm=True, execute=False,
                      use_free=False, frontier_model="m/front")
        kwargs.update(overrides)
        return compose_plan(**kwargs)

    def test_spend_exhaustion_stops_with_no_rotation(self):
        calls = []

        def confirm(**kwargs):
            calls.append(kwargs.get("model"))
            raise HarnessError("HTTP 402: credit exhausted, top up")

        with patch("harness.waist.resolve_waist_ladder", return_value=["m1", "m2"]), \
             patch("harness.waist.confirm_plan", side_effect=confirm):
            with self.assertRaisesRegex(HarnessError, "credit exhausted"):
                self._compose()
        self.assertEqual(calls, ["m1"])

    def test_ordinary_429_rotates_to_the_next_rung(self):
        calls = []

        def confirm(**kwargs):
            calls.append(kwargs.get("model"))
            if kwargs.get("model") == "m1":
                raise HarnessError("HTTP 429: rate limit exceeded")
            return {"status": "ok", "dag": {"nodes": []},
                    "confirmation": {"verdict": "approved"}}

        with patch("harness.waist.resolve_waist_ladder", return_value=["m1", "m2"]), \
             patch("harness.waist.confirm_plan", side_effect=confirm):
            plan = self._compose()
        self.assertEqual(calls, ["m1", "m2"])
        self.assertEqual(plan["status"], "ok")

    def test_replan_skips_the_rate_limited_rung(self):
        from harness.dag import TaskDAG
        calls = []

        def confirm(**kwargs):
            calls.append(kwargs.get("model"))
            if kwargs.get("model") == "m1":
                raise HarnessError("HTTP 429: rate limit exceeded")
            return {"status": "refused", "dag": {"nodes": []},
                    "confirmation": {"verdict": "refused", "reason": "plan too coarse",
                                     "evidence": "", "cost": 0.0}}

        dag = TaskDAG(nodes={})
        with patch("harness.waist.resolve_waist_ladder", return_value=["m1", "m2"]), \
             patch("harness.waist.confirm_plan", side_effect=confirm), \
             patch("harness.waist.decompose_via_llm", return_value=dag), \
             patch("harness.waist._decompose_repo_context", return_value="ctx"):
            plan = self._compose(decompose_llm=True, execute=True,
                                 chat_fn=lambda prompt: ("{}", 0.0))
        # First pass m1 (429) -> m2 (refused); re-plan pass skips m1, retries m2.
        self.assertEqual(calls, ["m1", "m2", "m2"])
        self.assertEqual(plan["status"], "refused")

    def test_replan_pass_propagates_spend_exhaustion(self):
        from harness.dag import TaskDAG
        calls = []
        refused = {"status": "refused", "dag": {"nodes": []},
                   "confirmation": {"verdict": "refused",
                                    "reason": "plan too coarse",
                                    "evidence": "", "cost": 0.0}}

        def confirm(**kwargs):
            calls.append(kwargs.get("model"))
            if len(calls) <= 2:
                return dict(refused)
            raise HarnessError("HTTP 402: credit exhausted, top up")

        with patch("harness.waist.resolve_waist_ladder", return_value=["m1", "m2"]), \
             patch("harness.waist.confirm_plan", side_effect=confirm), \
             patch("harness.waist.decompose_via_llm",
                   return_value=TaskDAG(nodes={})), \
             patch("harness.waist._decompose_repo_context", return_value="ctx"):
            with self.assertRaisesRegex(HarnessError, "credit exhausted"):
                self._compose(decompose_llm=True, execute=True,
                              chat_fn=lambda prompt: ("{}", 0.0))
        # First pass m1 (refused) enters re-plan; the re-plan retries m1
        # (refused), then m2's spend failure propagates instead of rotating
        # or being laundered into the retained coarse-plan refusal.
        self.assertEqual(calls, ["m1", "m1", "m2"])

    def test_missing_tool_refusal_never_replans(self):
        from harness.dag import TaskDAG
        decomposes = []

        def decompose(*args, **kwargs):
            decomposes.append(1)
            return TaskDAG(nodes={})

        refused = {"status": "refused", "dag": {"nodes": []},
                   "confirmation": {"verdict": "refused",
                                    "reason": "missing tool: web capability is detached",
                                    "evidence": "", "cost": 0.0}}

        with patch("harness.waist.resolve_waist_ladder", return_value=["m1"]), \
             patch("harness.waist.confirm_plan", return_value=dict(refused)), \
             patch("harness.waist.decompose_via_llm", side_effect=decompose), \
             patch("harness.waist._decompose_repo_context", return_value="ctx"):
            from tests._fake import FakeTransport, _gov
            from harness.waist import compose_plan
            plan = compose_plan(
                transport=FakeTransport(), api_key="k",
                governor=_gov(FakeTransport(), max_cost=0.05), ledger=None,
                opts_goal="Update the helper", candidate_files=["pkg/mod.py"],
                decompose_llm=True, confirm=True, execute=True,
                use_free=False, frontier_model="m/front",
                chat_fn=lambda prompt: ("{}", 0.0))
        self.assertEqual(plan["status"], "refused")
        self.assertEqual(len(decomposes), 1,
                         "the missing capability must not trigger a re-plan")


class CoverageGapRegressionTests(unittest.TestCase):
    """Pin the guard behaviors D12 flagged as never-executed (CI SD 8.33).

    Each test drives a real branch through the public API -- no mocks
    except the classifier-failure case, where a patched classifier
    proves error classification degrades instead of raising.
    """

    def test_none_body_is_empty_text(self):
        self.assertEqual(
            classify_provider_failure(500), MODEL_UNAVAILABLE)

    def test_unserializable_dict_body_falls_back_to_str(self):
        self.assertEqual(
            classify_provider_failure(None, {"k": object()}),
            UNKNOWN_FAILURE)

    def test_non_mapping_body_falls_back_to_str(self):
        self.assertEqual(
            classify_provider_failure(None, 12345), UNKNOWN_FAILURE)

    def test_budget_hint_is_terminal_before_any_status(self):
        self.assertEqual(
            classify_provider_failure(None, "would eat terminal_reserve"),
            BUDGET_REFUSED)

    def test_429_with_spend_hint_stops_terminally(self):
        self.assertEqual(
            classify_provider_failure(429, "insufficient credit, top up"),
            SPEND_EXHAUSTED)

    def test_throttled_403_is_rate_limited(self):
        self.assertEqual(
            classify_provider_failure(403, "request throttled, slow down"),
            RATE_LIMITED)

    def test_plain_403_is_auth_failed(self):
        self.assertEqual(
            classify_provider_failure(403, "forbidden"), AUTH_FAILED)

    def test_auth_hint_body_is_auth_failed(self):
        self.assertEqual(
            classify_provider_failure(418, "Unauthorized"), AUTH_FAILED)

    def test_broken_classifier_degrades_instead_of_raising(self):
        with patch("harness.lifecycle_guards.classify_provider_failure",
                   side_effect=ValueError("boom")):
            self.assertEqual(
                classify_harness_error("HTTP 500: boom"), UNKNOWN_FAILURE)

    def test_garbage_wait_seconds_are_ignored(self):
        circuit = RateCircuit()
        circuit.note_rate_limited("m", wait_s="not-a-number")
        self.assertEqual(circuit.total_wait_s, 0.0)

    def test_circular_payload_still_renders_a_key(self):
        payload = {}
        payload["me"] = payload
        self.assertIsInstance(PhaseMemo.stable_key(payload), str)

    def test_stray_non_dict_node_is_skipped(self):
        self.assertFalse(
            has_write_surface([], {"nodes": ["oops", {"name": "x"}]}))

    def test_string_node_renders_unconfirmed(self):
        out = render_refused_plan("Do it", "nope", nodes=["do the thing"])
        self.assertIn("1. do the thing (unconfirmed)", out)


# ---------------------------------------------------------------------------
# Entry seams: cancellation, target extraction, probe verdicts, hourglass route
# ---------------------------------------------------------------------------

def _resolve_public(*ips):
    def _resolve(host, port):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    return _resolve


class SimpleActionEntrySeamTests(unittest.TestCase):
    def test_cancel_check_at_entry_raises_before_probe(self):
        probed = []
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with self.assertRaises(ToolCancelled):
                agent.run_simple_action(
                    "Is https://example.com up?",
                    probe_fn=lambda host, path, timeout: probed.append(1),
                    cancel_check=lambda: True)
        self.assertEqual(probed, [],
                         "entry cancellation must pre-empt the probe")

    def test_prompt_without_target_defers_with_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            res = agent.run_simple_action("tell me a joke")
        self.assertEqual(res["status"], "deferred")
        self.assertEqual(res["defer_reason"], "no site target in prompt")
        self.assertIn("could not find a site", res["response"])
        self.assertIn("rephrase with a URL", res["next_step"])

    def test_redirect_answers_yes_with_redirect(self):
        def probe(host, path, timeout):
            return (301, "https://example.com/new-path", host)
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            res = agent.run_simple_action("Is https://example.com up?",
                                          probe_fn=probe,
                                          resolve_fn=_resolve_public("93.184.216.34"))
        self.assertEqual(res["status"], "ok")
        self.assertIn("redirect", res["response"])

    def test_ledger_failure_on_probe_path_still_returns_ok(self):
        def probe(host, path, timeout):
            return (200, "", host)
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch("harness.agent.ledger_for",
                       side_effect=RuntimeError("ledger down")):
                res = agent.run_simple_action("Is https://example.com up?",
                                              probe_fn=probe,
                                              resolve_fn=_resolve_public("93.184.216.34"))
        self.assertEqual(res["status"], "ok")

    def test_preflight_probe_failure_with_ledger_down_still_defers(self):
        def boom(host, path, timeout):
            raise HarnessError("denied")
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch("harness.agent.ledger_for",
                       side_effect=RuntimeError("ledger down")):
                res = agent.run_simple_action("Is https://example.com up?",
                                              probe_fn=boom,
                                              resolve_fn=_resolve_public("93.184.216.34"))
        self.assertEqual(res["status"], "deferred")
        self.assertIn("can't check that site", res["response"])

    def test_hourglass_routes_simple_action_without_model(self):
        sentinel = {"status": "ok", "intent": "simple-action"}
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            with patch.object(agent, "run_simple_action",
                              return_value=sentinel) as run:
                res = agent.run_hourglass_request("Is https://example.com up?")
        self.assertIs(res, sentinel)
        run.assert_called_once()


class ProviderFailureTailTests(unittest.TestCase):
    def test_plain_402_is_spend_exhausted(self):
        self.assertEqual(classify_provider_failure(402, ""), SPEND_EXHAUSTED)

    def test_unmatched_status_and_body_is_unknown(self):
        self.assertEqual(classify_provider_failure(None, "wobbly"),
                         UNKNOWN_FAILURE)

    def test_model_hint_without_status_is_model_unavailable(self):
        self.assertEqual(
            classify_provider_failure(None, "model not found: m/x"),
            MODEL_UNAVAILABLE)


class WaistCooldownSkipTests(unittest.TestCase):
    def _compose(self, **overrides):
        from tests._fake import FakeTransport, _gov
        from harness.waist import compose_plan
        gov = _gov(FakeTransport(), max_cost=0.05)
        kwargs = dict(transport=FakeTransport(), api_key="k", governor=gov, ledger=None,
                      opts_goal="Update the helper", candidate_files=["pkg/mod.py"],
                      decompose_llm=False, confirm=True, execute=False,
                      use_free=False, frontier_model="m/front")
        kwargs.update(overrides)
        return compose_plan(**kwargs)

    def _all_429(self, calls):
        def confirm(**kwargs):
            calls.append(kwargs.get("model"))
            raise HarnessError("HTTP 429: rate limit exceeded")
        return confirm

    def test_repeated_rung_skips_cooldown_without_reconfirm(self):
        calls = []
        with patch("harness.waist.resolve_waist_ladder",
                   return_value=["m1", "m1"]), \
             patch("harness.waist.confirm_plan",
                   side_effect=self._all_429(calls)):
            with self.assertRaisesRegex(HarnessError,
                                        "waist confirmation could not run"):
                self._compose()
        # The second rung sees m1 on cooldown and skips it without
        # another confirm call; with no rung left the ladder raises.
        self.assertEqual(calls, ["m1"])

    def test_replan_notes_rate_limited_rung_then_returns_refusal(self):
        from harness.dag import TaskDAG
        calls = []
        refused = {"status": "refused", "dag": {"nodes": []},
                   "confirmation": {"verdict": "refused",
                                    "reason": "plan too coarse",
                                    "evidence": "", "cost": 0.0}}

        def confirm(**kwargs):
            calls.append(kwargs.get("model"))
            if len(calls) <= 2:
                if kwargs.get("model") == "m1":
                    raise HarnessError("HTTP 429: rate limit exceeded")
                return dict(refused)
            raise HarnessError("HTTP 429: rate limit exceeded")

        with patch("harness.waist.resolve_waist_ladder",
                   return_value=["m1", "m2"]), \
             patch("harness.waist.confirm_plan", side_effect=confirm), \
             patch("harness.waist.decompose_via_llm",
                   return_value=TaskDAG(nodes={})), \
             patch("harness.waist._decompose_repo_context", return_value="ctx"):
            plan = self._compose(decompose_llm=True, execute=True,
                                 chat_fn=lambda prompt: ("{}", 0.0))
        # First pass m1 (429) -> m2 (refused); the re-plan pass retries
        # m2, hits 429 (noted, not fatal), and the retained coarse-plan
        # refusal is returned instead of laundering the rate limit.
        self.assertEqual(calls, ["m1", "m2", "m2"])
        self.assertEqual(plan["status"], "refused")


if __name__ == "__main__":
    unittest.main()
