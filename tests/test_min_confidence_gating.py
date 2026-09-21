import json
import unittest

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
