import unittest

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_policy import JevPolicy, policy_for
from harness.waist import compose_plan


class _FallbackEvaluator:
    api_key = None
    model = "jev-latest"

    def evaluate(self, state, questions):
        return JevEvaluationResult("pass", 0.0, 1.0, {}, ["fallback"] ,
                                   is_fallback=True, model=self.model)

    def evaluate_plan_requirements(self, prompt, target_files):
        return JevEvaluationResult(
            "pass", 0.0, 1.0, {"requires_iteration": True}, ["fallback"],
            is_fallback=True, model=self.model)


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

    def test_plan_fallback_adds_iterative_guideline_and_structural_envelope(self):
        settings = load_settings()
        policy = JevPolicy(settings, evaluator=_FallbackEvaluator())
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="design an algorithm loop", candidate_files=[],
            jev_policy=policy)
        self.assertIn("STRUCTURAL GUIDELINE", result["goal"])
        self.assertTrue(result["triage"]["requires_iteration"])
        self.assertTrue(result["structural"]["is_fallback"])


if __name__ == "__main__":
    unittest.main()
