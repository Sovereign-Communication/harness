"""JEV-P4 ops/exit hermetic gates.

Proves the product seams the P4 checklist can honestly claim:
- session.jev_for routes through policy_for (no orphan JevEvaluator factory)
- HARNESS_JEV_DISABLE forces unkeyed is_fallback for dogfood A/B
- settings.jev_model pin is honored on live payload + freeze helper
- envelope cost still equals token math; preflight still blocks over-ceiling
- ledger analytics surface jev events + calibration
- agent composition uses the policy owner (not a raw evaluator factory)
"""
import inspect
import os
import tempfile
import unittest
from unittest.mock import patch

from harness.config import freeze_jev_settings, load_settings
from harness.jev import JevEvaluator, jev_cost
from harness.jev_policy import (
    JEV_MAX_INPUT_TOKENS,
    JevPolicy,
    jev_cost_ceiling,
    policy_for,
)
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
import harness.session as session_mod
from tests._fake import FakeTransport, m


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


def _ledger():
    td = tempfile.TemporaryDirectory()
    return td, AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))


class TestSessionJevForUsesPolicy(unittest.TestCase):
    def test_jev_for_returns_policy_not_raw_evaluator(self):
        settings = load_settings({"jev_api_key": None})
        policy = session_mod.jev_for(settings)
        self.assertIsInstance(policy, JevPolicy)
        self.assertIsInstance(policy.evaluator, JevEvaluator)

    def test_session_source_has_no_orphan_jev_evaluator_factory(self):
        src = inspect.getsource(session_mod)
        self.assertIn("policy_for", src)
        # Factory must not construct a raw evaluator outside the policy owner.
        self.assertNotIn("return JevEvaluator(", src)
        self.assertIn("def jev_for(", src)
        self.assertIn("return policy_for(", src)

    def test_agent_composes_via_session_jev_for(self):
        import harness.agent as agent_mod
        src = inspect.getsource(agent_mod)
        self.assertIn("jev_for(", src)
        # Agent must not pass a raw evaluator=jev_for(...) factory call.
        self.assertNotIn("evaluator=jev_for(", src)


class TestDogfoodDisableSwitch(unittest.TestCase):
    def test_harness_jev_disable_forces_unkeyed_settings(self):
        with patch.dict(os.environ, {"HARNESS_JEV_DISABLE": "1"}, clear=False):
            settings = load_settings({"jev_api_key": "should-be-ignored"})
            self.assertIsNone(settings.jev_api_key)
            policy = policy_for(settings)
            self.assertFalse(policy.keyed)

    def test_unkeyed_policy_marks_is_fallback_and_zero_cost(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings)
        result, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.cost, 0.0)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(structural["cost"], 0.0)

    def test_disable_switch_absent_key_still_resolves_normally(self):
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_JEV_DISABLE"}
        with patch.dict(os.environ, env, clear=True):
            # No key file guarantee on CI; just prove the switch is not sticky.
            settings = load_settings({"jev_api_key": "key-from-overrides"})
            self.assertEqual(settings.jev_api_key, "key-from-overrides")


class TestModelPinAndThresholdFreeze(unittest.TestCase):
    def test_settings_model_pin_is_sent_on_live_payload(self):
        class _Settings:
            jev_api_key = "jev-key"
            jev_endpoint = "https://api.typesafe.ai/v1/systemone"
            jev_model = "jev-1.13.0"
            min_confidence = 0.80

        transport = _JevTransport({
            "model": "jev-1.13.0",
            "answers": {"instruction_matches": {
                "type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 10, "output_tokens": 1},
        })
        evaluator = JevEvaluator(settings=_Settings(), transport=transport)
        self.assertEqual(evaluator.model, "jev-1.13.0")
        self.assertEqual(evaluator.min_confidence, 0.80)
        evaluator.evaluate({"code": "x = 1\n"}, {
            "instruction_matches": {"type": "noul", "instructions": "ok?"}})
        self.assertEqual(transport.calls[0]["model"], "jev-1.13.0")

    def test_freeze_jev_settings_pins_model_and_threshold(self):
        settings = load_settings()
        frozen = freeze_jev_settings(
            settings, jev_model="jev-1.13.0", min_confidence=0.85)
        self.assertEqual(settings.jev_model, "jev-1.13.0")
        self.assertEqual(settings.min_confidence, 0.85)
        self.assertEqual(frozen["jev_model"], "jev-1.13.0")
        self.assertEqual(frozen["min_confidence"], 0.85)
        self.assertTrue(frozen["jev_model_is_pinned"])

    def test_freeze_keeps_alias_unpinned_when_omitted(self):
        settings = load_settings()
        settings.jev_model = "jev-latest"
        frozen = freeze_jev_settings(settings, min_confidence=0.70)
        self.assertEqual(frozen["jev_model"], "jev-latest")
        self.assertFalse(frozen["jev_model_is_pinned"])


class TestEnvelopeCostAndPreflightStillHold(unittest.TestCase):
    """P1 cost contract still proven after P4 ops wiring (JEV-P4 box 3)."""

    def test_live_cost_equals_token_math_and_ledger_jev_eval(self):
        transport = _JevTransport({
            "model": "jev-test",
            "answers": {"instruction_matches": {
                "type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 3},
        })
        models = [m("jev-test", prompt="0", completion="0")]
        governor = SpendGovernor(
            FakeTransport(models=models), "sk-test", max_cost=0.10)
        td, ledger = _ledger()
        self.addCleanup(td.cleanup)
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=governor,
                            ledger=ledger)
        result, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply", task_id="t-p4")
        expected = jev_cost(100)
        self.assertAlmostEqual(result.cost, expected)
        self.assertAlmostEqual(structural["cost"], expected)
        self.assertEqual(structural["input_tokens"], 100)
        self.assertAlmostEqual(governor.spent, expected)
        events = [e for e in ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["cost"], expected)

    def test_preflight_blocks_over_ceiling_before_transport(self):
        transport = _JevTransport({
            "model": "jev-test",
            "answers": {"instruction_matches": {
                "type": "noul", "noul": 0.95}},
            "usage": {"input_tokens": 100, "output_tokens": 3},
        })
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.001)
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=governor)
        result, structural = policy.evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(transport.calls, [])
        self.assertEqual(jev_cost_ceiling(),
                         JEV_MAX_INPUT_TOKENS * 42 / 1_000_000)


class TestLedgerAnalyticsJevSurface(unittest.TestCase):
    def test_participation_report_embeds_jev_calibration(self):
        td, ledger = _ledger()
        self.addCleanup(td.cleanup)
        ledger.append("jev_eval", task_id="t1", site="apply", verdict="pass",
                      confidence=0.9, supported=0.9, cost=0.0004,
                      input_tokens=10, is_fallback=False)
        ledger.append("verify_round", task_id="t1", passed=True)
        report = ledger.participation_report()
        self.assertIn("jev_calibration", report)
        cal = report["jev_calibration"]
        self.assertEqual(cal["jev_evals"], 1)
        self.assertEqual(cal["jev_keyed_evals"], 1)
        self.assertEqual(cal["tasks_with_jev"], 1)
        self.assertEqual(cal["tasks_joined_with_verify"], 1)
        self.assertIn("apply", cal["by_site"])


class TestConsentConfidenceGatingStillWired(unittest.TestCase):
    """M2–M3 product still present for the P4 consent checkbox evidence."""

    def test_low_confidence_accept_defers_before_dispatch(self):
        import json
        from harness.consent import probe_consent
        from tests._fake import comp
        body = {"decision": "accept", "confidence": 0.40,
                "reason": "uncertain", "redirect_model": None,
                "scope_suggestion": None}
        td, ledger = _ledger()
        self.addCleanup(td.cleanup)
        transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
        governor = SpendGovernor(transport, "sk-test")
        result = probe_consent(
            transport=transport, api_key="k", governor=governor,
            task_id="t-p4", task="work", model="judge", ledger=ledger,
            min_confidence=0.70)
        self.assertEqual(result["decision"], "defer")
        self.assertFalse(result["dispatched"])
        self.assertIn("below", result["reason"])


if __name__ == "__main__":
    unittest.main()
