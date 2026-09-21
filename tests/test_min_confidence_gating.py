import json
import unittest

from harness.apply_state import ApplyRequest
from harness.consent import probe_consent
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, comp, m


class MinimumConfidenceGatingTests(unittest.TestCase):
    def test_threshold_is_configured_and_strict(self):
        body = {"decision": "accept", "confidence": 0.69,
                "reason": "borderline", "redirect_model": None,
                "scope_suggestion": None}
        transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
        governor = SpendGovernor(transport, "sk-test")
        result = probe_consent(
            transport=transport, api_key="k", governor=governor,
            task_id="t", task="write", model="judge", min_confidence=0.70)
        self.assertEqual(result["decision"], "defer")
        self.assertFalse(result["dispatched"])

    def test_threshold_rejects_out_of_range_confidence(self):
        body = {"decision": "accept", "confidence": 1.5,
                "reason": "invalid", "redirect_model": None,
                "scope_suggestion": None}
        transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
        result = probe_consent(
            transport=transport, api_key="k", governor=SpendGovernor(transport, "sk-test"),
            task_id="t", task="write", model="judge", min_confidence=0.70)
        self.assertEqual(result["decision"], "accept")
        self.assertIsNone(result["confidence"])

    def test_apply_request_carries_threshold(self):
        fields = dict(task_id="t", file_path="x.py", instruction="edit",
                      edit_snippet=None, verify_cmd=None, backend="harness",
                      verify_only=False, max_lines=10, max_rounds=1,
                      max_tokens=100, task_max_cost=0.1, max_rot=0,
                      reasoning="auto", renew=False, allow_escalation=None,
                      model="m", ordered=None, profiles=None, want_consent=False,
                      original="", task_start_spent=0.0, continuation=None,
                      continuation_gate=None, task_runner=None, cancel_check=None,
                      min_confidence=0.83)
        self.assertEqual(ApplyRequest(**fields).min_confidence, 0.83)

    def test_threshold_allows_confident_accept(self):
        body = {"decision": "accept", "confidence": 0.70,
                "reason": "ready", "redirect_model": None,
                "scope_suggestion": None}
        transport = FakeTransport(models=[m("judge")], posts=[comp(json.dumps(body))])
        governor = SpendGovernor(transport, "sk-test")
        result = probe_consent(
            transport=transport, api_key="k", governor=governor,
            task_id="t", task="write", model="judge", min_confidence=0.70)
        self.assertEqual(result["decision"], "accept")
        self.assertTrue(result["dispatched"])


if __name__ == "__main__":
    unittest.main()
