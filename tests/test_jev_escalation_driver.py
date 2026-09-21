"""Hermetic gate tests for JEV-P2-dead-code: Jev-directed escalation.

The previously dead decision functions (``decide_probe_verify_escalate`` /
``should_abstain``) are wired onto the production apply path: the shared Jev
policy answers a typed noul pack over code-owned failure context, the code
maps the calibrated confidence onto a ladder rung, and the escalation driver
consumes the parked directive as a one-shot pending state (the generative
verify lane keeps priority). Every test here is hermetic: fake transports,
scripted evaluators, and no live calls. Confidence is confidence in ANOTHER
ATTEMPT AT THE CURRENT TIER (low => escalate).
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.apply_state import RunState
from harness.errors import HarnessError
from harness.escalation import (EscalationDriver, confidence_to_start_rung,
                                jev_escalation_directive)
from harness.jev import JevEvaluationResult
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.sliding_scale import (TIER_1_DISTILLER, TIER_2_FRONTIER,
                                   decide_probe_verify_escalate)
from harness.config import load_settings
from tests._fake import FakeTransport, comp, m

MODEL = "test/model"
JUDGE = "judge/model"
ORIGINAL = "x = 1\n"
CHANGED = "x = 2\n"


def jev_result(confidence, *, budget_noul=None, is_fallback=False,
               verdict="pass"):
    """A live (non-fallback) decision result from the noul pack.

    ``answers`` mirrors exactly what ``JevEvaluator._parse_jev_response``
    produces for a noul pack: each answer is {"type": "noul", "noul": p}.
    """
    answers = {"escalation_decision": {"type": "noul", "noul": confidence}}
    if budget_noul is not None:
        answers["capability_budget"] = {"type": "noul", "noul": budget_noul}
    return JevEvaluationResult(
        verdict, confidence, confidence, answers=answers,
        is_fallback=is_fallback, model="jev-1.13.0")


class MapperTests(unittest.TestCase):
    """confidence_to_start_rung: pure arithmetic, clamped, no failed-rung."""

    def test_low_confidence_climbs_to_frontier(self):
        rung, bucket = confidence_to_start_rung(0.10, 4)
        self.assertEqual((rung, bucket), (2, "escalate"))

    def test_very_low_confidence_takes_top_rung(self):
        rung, bucket = confidence_to_start_rung(0.0, 4)
        self.assertEqual((rung, bucket), (3, "escalate"))

    def test_marginal_confidence_skips_failed_rung(self):
        rung, bucket = confidence_to_start_rung(0.60, 4, current_rung=0)
        self.assertEqual((rung, bucket), (1, "retry"))

    def test_high_confidence_retries_from_zero(self):
        rung, bucket = confidence_to_start_rung(0.90, 4, current_rung=1)
        self.assertEqual((rung, bucket), (0, "retry-shaped"))

    def test_boundary_matches_decision_function(self):
        # 0.70 IS decide_probe_verify_escalate's min_confidence: the bucket
        # boundary and the tier decision can never disagree.
        rung, bucket = confidence_to_start_rung(0.70, 3)
        self.assertEqual(bucket, "retry-shaped")
        self.assertEqual(rung, 0)
        tier = decide_probe_verify_escalate(
            "t", confidence=0.70, current_tier=TIER_1_DISTILLER)
        self.assertEqual(tier, TIER_1_DISTILLER)

    def test_clamped_to_ladder(self):
        self.assertEqual(confidence_to_start_rung(0.0, 1), (0, "escalate"))
        self.assertEqual(confidence_to_start_rung(0.0, 0), (0, "escalate"))
        self.assertEqual(confidence_to_start_rung(1.0, 2), (0, "retry-shaped"))


class DirectiveTests(unittest.TestCase):
    """jev_escalation_directive: the REAL decision functions, fed by Jev."""

    def test_dead_function_wired_low_confidence_escalates(self):
        # THE JEV-P2-dead-code proof: the previously dead tier decision
        # decides the directive's tier because the Jev noul is low.
        tier = decide_probe_verify_escalate(
            "t", confidence=0.20, current_tier=TIER_1_DISTILLER)
        self.assertEqual(tier, TIER_2_FRONTIER)
        directive = jev_escalation_directive(
            jev_result(0.20), ladder_size=3, current_rung=0)
        self.assertEqual(directive["kind"], "escalate")
        self.assertEqual(directive["tier"], TIER_2_FRONTIER)
        self.assertEqual(directive["decision"], "escalate")
        # 0.075 <= conf < 0.5 climbs to the SECOND-from-top rung; only a
        # near-zero signal (conf < 0.075) takes the top rung.
        self.assertEqual(directive["start_rung"], 1)

    def test_marginal_signal_de_esclates_one_rung(self):
        directive = jev_escalation_directive(
            jev_result(0.60), ladder_size=4, current_rung=1)
        self.assertEqual(directive["decision"], "retry")
        self.assertEqual(directive["start_rung"], 2)

    def test_budget_noul_abstains_via_should_abstain(self):
        # THE should_abstain wiring: the budget noul below the abstention
        # threshold retires the walk instead of spending a rung.
        directive = jev_escalation_directive(
            jev_result(0.90, budget_noul=0.10), ladder_size=3)
        self.assertEqual(directive["kind"], "abstain")
        self.assertEqual(directive["decision"], "abstain")

    def test_high_budget_noul_does_not_abstain(self):
        directive = jev_escalation_directive(
            jev_result(0.90, budget_noul=0.80), ladder_size=3)
        self.assertEqual(directive["kind"], "escalate")

    def test_fallback_result_is_no_signal(self):
        self.assertIsNone(jev_escalation_directive(
            jev_result(0.10, is_fallback=True), ladder_size=3))

    def test_none_and_malformed_results_are_no_signal(self):
        self.assertIsNone(jev_escalation_directive(None, ladder_size=3))
        malformed = jev_result(0.10)
        malformed.answers["escalation_decision"] = {"type": "noul"}
        self.assertIsNone(jev_escalation_directive(malformed, ladder_size=3))
        out_of_domain = jev_result(0.10)
        out_of_domain.answers["escalation_decision"] = {
            "type": "noul", "noul": 7.0}
        self.assertIsNone(jev_escalation_directive(
            out_of_domain, ladder_size=3))


class DriverDirectiveTests(unittest.TestCase):
    """The driver consumes the parked directive as a one-shot pending state."""

    def _walk(self, *, pool, state, pending=None, finish_result=None,
              start_assert=None):
        router = SimpleNamespace(
            allow_escalation=True, escalation_pool=list(pool),
            de_escalate_to_rung=lambda r: True)

        def _escalation(override=True):
            del override  # the fake router serves the state's resume rung
            rung = state.de_escalation_target_rung
            if not (rung and 0 <= rung < len(pool)):
                rung = 0
            return {"model": pool[rung]}

        router.escalation = _escalation
        driver = EscalationDriver(router, transport=None, api_key="k",
                                  governor=_FakeGov(), ledger=None,
                                  task_id="t")
        state.pending_jev_directive = pending
        if start_assert is not None:
            start_assert(state)
        req = SimpleNamespace(allow_escalation=True, model="primary/model")
        calls = []

        def fake_chat(transport, api_key, model, messages, max_tokens,
                      effort, budget, governor):
            calls.append(model)
            return 200, {
                "choices": [{"message": {"content": f"content-from-{model}"},
                             "finish_reason": "stop"}],
                "usage": {"cost": 0.0},
            }

        with mock.patch("harness.escalation.chat", side_effect=fake_chat):
            result = driver.run_with_escalation(
                req, state, lambda st, ctx: "prompt",
                finish_result or (lambda model, content, cost: (
                    {"status": "ok", "model": model, "escalated": True})))
        return result, calls, driver

    def test_jev_directive_starts_walk_at_deeper_rung(self):
        state = RunState(rounds=[], history=[], current_content="x")
        # Built through the ONE construction path (the extractor), exactly
        # as apply_policy parks it.
        pending = jev_escalation_directive(
            jev_result(0.10), ladder_size=3, current_rung=0,
            condensed_context="verify output tail")
        # conf 0.10 (>= 0.075) climbs to the SECOND-from-top rung on a
        # 3-rung ladder; the failed rung 0 is never re-bought.
        self.assertEqual(pending["start_rung"], 1)
        result, calls, _ = self._walk(
            pool=["rung0/a", "rung1/b", "rung2/c"], state=state,
            pending=pending)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, ["rung1/b"])
        self.assertTrue(pending.get("applied"))
        self.assertEqual(state.de_escalation_target_rung, 1)
        self.assertIn("JEV-DIRECTED ESCALATION",
                      state.escalation_condensed_context)

    def test_jev_directive_is_one_shot(self):
        state = RunState(rounds=[], history=[], current_content="x")
        pending = jev_escalation_directive(
            jev_result(0.10), ladder_size=2, current_rung=0,
            condensed_context="verify output tail")
        self._walk(pool=["rung0/a", "rung1/b"], state=state, pending=pending)
        self.assertIsNone(state.pending_jev_directive)

    def test_lane_directive_keeps_priority(self):
        # The generative verify lane already picked rung 1 with condensed
        # context; the Jev directive must NOT override it.
        state = RunState(rounds=[], history=[], current_content="x",
                         escalation_condensed_context="lane context",
                         de_escalation_target_rung=1)
        pending = {"kind": "escalate", "confidence": 0.10,
                   "decision": "escalate", "start_rung": 2}
        _, calls, _ = self._walk(
            pool=["rung0/a", "rung1/b", "rung2/c"], state=state,
            pending=pending)
        self.assertEqual(calls[0], "rung1/b")
        self.assertEqual(state.de_escalation_target_rung, 1)
        self.assertEqual(state.escalation_condensed_context, "lane context")
        self.assertFalse(pending.get("applied", False))

    def test_abstain_directive_retires_the_walk(self):
        state = RunState(rounds=[], history=[], current_content="x")
        pending = {"kind": "abstain", "confidence": 0.90,
                   "budget_noul": 0.10, "decision": "abstain"}
        result, calls, _ = self._walk(
            pool=["rung0/a", "rung1/b"], state=state, pending=pending)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertIsNone(state.pending_jev_directive)

    def test_lane_directive_beats_abstain(self):
        # Lane priority holds even against an abstain directive.
        state = RunState(rounds=[], history=[], current_content="x",
                         escalation_condensed_context="lane context",
                         de_escalation_target_rung=0)
        pending = {"kind": "abstain", "confidence": 0.90,
                   "budget_noul": 0.10, "decision": "abstain"}
        result, calls, _ = self._walk(
            pool=["rung0/a", "rung1/b"], state=state, pending=pending)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, ["rung0/a"])


class _FakeGov:
    """Minimal governor: no reservations, unlimited budget."""

    def __init__(self):
        self.spent = 0.0
        self.preflight_calls = []

    def preflight(self, prompt, calls):
        self.preflight_calls.append((prompt, calls))

    def record_actual(self, amount, model):
        self.spent += float(amount or 0.0)

    def record_byok(self, model):
        pass

    def is_free(self, model):
        return str(model).endswith(":free")


class _ScriptedJevEvaluator:
    """Scripted evaluator double: records calls, returns a canned result."""

    api_key = "jev-key"
    model = "jev-test"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def evaluate(self, state, questions=None):
        self.calls.append((state, questions))
        if not self.api_key:
            # Mirror the real JevEvaluator unkeyed contract: honest local
            # fallback, never a live-shaped answer.
            return JevEvaluationResult(
                "fail", 0.0, 0.0, {}, ["unkeyed: no live evaluation"],
                is_fallback=True, model=self.model)
        return self.result


class _CountingGovernor:
    """Reservation-capable governor double (mirrors the policy doubles)."""

    def __init__(self):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.max_cost = 1.0
        self.spent = 0.0

    def reserve(self, worst, label):
        self.reserved.append((worst, label))
        return {"label": label, "worst": worst}

    def reconcile(self, reservation, cost):
        self.reconciled.append((reservation, cost))
        self.spent += float(cost or 0.0)

    def record_actual(self, cost, model):
        self.actual.append((cost, model))
        self.spent += float(cost or 0.0)


class PolicyEscalationDecisionTests(unittest.TestCase):
    """JevPolicy.evaluate_escalation_decision: the owner of the signal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(
            os.path.join(self.tmp.name, "ledger.jsonl"))

    def _policy(self, result, keyed=True):
        settings = load_settings()
        settings.jev_api_key = "jev-key" if keyed else None
        evaluator = _ScriptedJevEvaluator(result)
        if not keyed:
            evaluator.api_key = None
        return policy_for(settings, transport=object(),
                          governor=_CountingGovernor(), ledger=self.ledger,
                          evaluator=evaluator), evaluator

    def _jev_evals(self):
        return [e for e in self.ledger.entries() if e["event"] == "jev_eval"]

    def test_keyed_live_call_shape_and_one_jev_eval(self):
        policy, evaluator = self._policy(jev_result(0.30, budget_noul=0.10))
        result, structural = policy.evaluate_escalation_decision(
            "instruction: fix\nrounds tried: 2\nlast verify output: boom",
            task_id="t1")
        self.assertFalse(result.is_fallback)
        self.assertEqual(structural["site"], "escalation-decision")
        # ONE state key: the code-owned failure context (never model output).
        state, questions = evaluator.calls[0]
        self.assertIn("last verify output: boom", state["context"])
        self.assertEqual(set(questions),
                         {"escalation_decision", "capability_budget"})
        self.assertEqual(questions["escalation_decision"]["type"], "noul")
        events = self._jev_evals()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], "escalation-decision")
        self.assertFalse(events[0]["is_fallback"])

    def test_unkeyed_returns_honest_fallback_signal(self):
        policy, evaluator = self._policy(jev_result(0.30), keyed=False)
        result, structural = policy.evaluate_escalation_decision("ctx")
        self.assertTrue(result.is_fallback)
        # One evaluator dispatch, zero network: the evaluator's own unkeyed
        # contract resolves to the honest local fallback (mirrors the real
        # JevEvaluator; the scripted canned result is never used).
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(structural["verdict"], "fail")
        self.assertEqual(structural["confidence"], 0.0)
        self.assertTrue(structural["is_fallback"])
        events = self._jev_evals()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_fallback"])

    def test_transport_refusal_refunds_and_records_honestly(self):
        class _ExplodingEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions=None):
                raise HarnessError("TypeSafe request rejected (HTTP 429)")

        settings = load_settings()
        settings.jev_api_key = "jev-key"
        gov = _CountingGovernor()
        policy = policy_for(settings, transport=object(), governor=gov,
                            ledger=self.ledger, evaluator=_ExplodingEvaluator())
        result, structural = policy.evaluate_escalation_decision("ctx")
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(len(gov.reconciled), 1)
        self.assertEqual(gov.reconciled[0][1], 0.0)  # reservation refunded
        refusals = [e for e in self.ledger.entries()
                    if e["event"] == "jev_refusal"]
        self.assertEqual(len(refusals), 1)
        # No signal -> no directive (apply_policy keeps the status-quo walk).
        self.assertIsNone(jev_escalation_directive(result, ladder_size=3))

    def test_end_to_end_policy_to_directive_chain(self):
        # The full JEV-P2-dead-code chain in one test: live noul result ->
        # extractor -> the REAL decision functions -> driver-visible
        # directive with code-owned condensed context.
        policy, _ = self._policy(jev_result(0.05, budget_noul=0.90))
        result, _ = policy.evaluate_escalation_decision(
            "instruction: fix\nlast verify output: boom")
        directive = jev_escalation_directive(
            result, ladder_size=3, current_rung=0,
            condensed_context="verify tail")
        self.assertEqual(directive["kind"], "escalate")
        self.assertEqual(directive["tier"], TIER_2_FRONTIER)
        # conf 0.05 < 0.075: the ladder's top rung.
        self.assertEqual(directive["start_rung"], 2)
        self.assertIn("JEV-DIRECTED ESCALATION",
                      directive["condensed_context"])
        self.assertIn("verify tail", directive["condensed_context"])
        state = RunState(rounds=[], history=[], current_content="x")
        state.pending_jev_directive = directive
        self.assertEqual(state.pending_jev_directive["decision"], "escalate")


class DefensiveBranchTests(unittest.TestCase):
    """Every honest defensive branch is suite-executed, not hidden."""

    def test_mapper_rejects_non_numeric_ladder_size(self):
        # Pure defensive: garbage ladder size falls to the retry shape.
        rung, bucket = confidence_to_start_rung(0.30, "three")
        self.assertEqual((rung, bucket), (0, "escalate"))

    def test_extractor_rejects_non_dict_answers(self):
        result = JevEvaluationResult(
            "pass", 0.3, 0.3, answers="not-a-map", is_fallback=False)
        self.assertIsNone(jev_escalation_directive(result, ladder_size=3))

    def test_driver_out_of_ladder_escalate_directive_is_ignored(self):
        # A directive whose rung is outside the CURRENT ladder (pool changed
        # between decision and walk) degrades to the status-quo walk from 0.
        state = RunState(rounds=[], history=[], current_content="x")
        pending = {"kind": "escalate", "confidence": 0.10,
                   "decision": "escalate", "start_rung": 7,
                   "condensed_context": "ctx"}
        result, calls, _ = DriverDirectiveTests()._walk(
            pool=["rung0/a", "rung1/b"], state=state, pending=pending)
        self.assertEqual(calls[0], "rung0/a")
        self.assertFalse(pending.get("applied", False))

    def test_engine_parks_no_directive_when_jev_refuses(self):
        """The engine wiring's except branch: a Jev transport refusal must
        leave pending_jev_directive None and the escalation walk intact."""
        from tests.test_jev_lane_parity import hermetic_settings
        from harness.apply import ApplyEngine
        from harness.router import Router
        from harness.spend import SpendGovernor

        class _ExplodingJevPolicy:
            """Duck-typed policy: candidate nouls pass, decision calls refuse."""

            def __init__(self, settings):
                self.settings = settings
                self.evaluator = SimpleNamespace(model="jev-test")

            def evaluate_escalation_decision(self, context, **kwargs):
                raise HarnessError("TypeSafe request rejected (HTTP 429)")

            def evaluate_candidate(self, *args, **kwargs):
                result = JevEvaluationResult(
                    "pass", 0.95, 0.95, {}, [], is_fallback=False,
                    model="jev-test")
                structural = {
                    "verdict": "pass", "confidence": 0.95, "supported": 0.95,
                    "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
                    "is_fallback": False, "model": "jev-test", "site": "apply"}
                return result, structural

        settings = hermetic_settings()
        settings.jev_api_key = None
        transport = FakeTransport(
            models=[m(MODEL), m(JUDGE)], posts=[comp(CHANGED), comp(CHANGED),
                                                comp(CHANGED)])
        governor = SpendGovernor(transport, "sk-test")
        engine = ApplyEngine(
            transport, "sk-test", governor, self._ledger(),
            Router([MODEL], JUDGE, MODEL, escalation_pool=["paid/b"],
                   allow_escalation=True),
            default_require_consent=False, default_renew_consent=False,
            jev_policy=_ExplodingJevPolicy(settings), min_confidence=0.70)
        engine.run_verify = scripted_run_two_fail_then_pass()
        result = engine.apply_edit(
            task_id="jev-refuse", file_path=self._target(),
            instruction="change x", verify_cmd="python -c \"pass\"",
            require_consent=False, edit_snippet=CHANGED)
        # Whatever the terminal, the engine must not crash and the Jev
        # refusal must never produce a directive.
        self.assertIsInstance(result, dict)
        self.assertIsNone(result.get("pending_jev_directive"))

    def _ledger(self):
        import tempfile as _tf
        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutonomyLedger(os.path.join(tmp.name, "ledger.jsonl"))

    def _target(self):
        import tempfile as _tf
        tmp = _tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "x.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(ORIGINAL)
        return target


def scripted_run_two_fail_then_pass():
    """Gate runner: two failing rounds, then a passing one."""
    seq = [(1, "fail one"), (1, "fail two"), (0, "ok")]
    state = {"n": 0}

    def runner(cmd, timeout=None, cwd=None):
        rc, out = seq[min(state["n"], len(seq) - 1)]
        state["n"] += 1
        return rc, out

    return runner


if __name__ == "__main__":
    unittest.main()
