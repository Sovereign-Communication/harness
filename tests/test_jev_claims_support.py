"""JEV-P3-claims: optional lean claim-support checks (flag-gated)."""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m

_CLAIMS = [
    {"id": "c1", "text": "Apply runs led by model X ended with verify rounds exhausted 2 times."},
    {"id": "c2", "text": "Model Y was paid for 3 recent calls that returned HTTP 200 with no usable content."},
]


class _ClaimsTransport:
    def __init__(self, supported=(True, False)):
        self.supported = list(supported)
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        answers = {}
        for i, _claim in enumerate(payload["state"]["claims"]):
            noul = 0.9 if self.supported[i] else 0.15
            answers[f"claim_{i}_supported"] = {"type": "noul", "noul": noul}
        return 200, {
            "model": "jev-test",
            "answers": answers,
            "usage": {"input_tokens": 25, "output_tokens": 2},
        }


def _unkeyed():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


class ClaimSupportPolicyTests(unittest.TestCase):
    def test_default_off_skips_and_does_not_break_claims_lint(self):
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_claim_support(
            _CLAIMS, "quoted ledger window", enabled=False, site="claims")
        self.assertTrue(structural.get("skipped"))
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["claim_flags"], [])

    def test_enabled_unkeyed_returns_advisory_unknown_flags(self):
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_claim_support(
            _CLAIMS, "quoted source", enabled=True, site="claims")
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural.get("skipped"))
        flags = structural["claim_flags"]
        self.assertEqual(len(flags), 2)
        self.assertTrue(all(f["supported"] is None for f in flags))
        self.assertTrue(all(f["fallback"] for f in flags))

    def test_enabled_keyed_returns_typed_support_flags(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ledger = AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.10)
        transport = _ClaimsTransport(supported=(True, False))
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport,
                            governor=governor, ledger=ledger)
        result, structural = policy.evaluate_claim_support(
            _CLAIMS, "evidence window", enabled=True, site="claims",
            task_id="t-claims")
        self.assertFalse(result.is_fallback)
        flags = structural["claim_flags"]
        self.assertEqual(flags[0]["supported"], True)
        self.assertEqual(flags[1]["supported"], False)
        self.assertEqual(flags[0]["id"], "c1")
        events = [e for e in ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], "claims")

    def test_empty_claims_enabled_still_skips_cleanly(self):
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_claim_support(
            [], "ctx", enabled=True, site="claims")
        self.assertTrue(structural.get("skipped") or result.is_fallback)


class PanelClaimSupportFlagTests(unittest.TestCase):
    def test_panel_judge_flag_defaults_off_so_lint_ownership_untouched(self):
        # Import-only contract: claim_support is optional on the envelope.
        import inspect
        from harness.panel import panel_judge
        sig = inspect.signature(panel_judge)
        self.assertIn("jev_claim_support", sig.parameters)
        self.assertIn("jev_policy", sig.parameters)
        self.assertFalse(sig.parameters["jev_claim_support"].default)
        self.assertIsNone(sig.parameters["jev_policy"].default)
        # claims.lint_claims remains the deterministic owner.
        from harness import claims as claims_mod
        self.assertTrue(callable(claims_mod.lint_claims))


if __name__ == "__main__":
    unittest.main()
