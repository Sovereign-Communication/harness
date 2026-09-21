import unittest

from harness.config import load_settings
from harness.jev_policy import policy_for


class JevTriageTests(unittest.TestCase):
    def test_unkeyed_triage_uses_heuristic_without_claiming_live(self):
        settings = load_settings()
        settings.jev_api_key = None
        result, envelope = policy_for(settings).evaluate_triage(
            "refactor the algorithm loop", ["a.py"], site="triage")
        self.assertEqual(result.answers["route"], "frontier")
        self.assertTrue(result.is_fallback)
        self.assertTrue(envelope["is_fallback"])
        self.assertEqual(envelope["site"], "triage")


if __name__ == "__main__":
    unittest.main()
