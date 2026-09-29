"""Production context-intake gate coverage for every composed plan lane."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.token_budget import TokenBudget
from harness.waist import compose_arguments, compose_plan


class ContextIntakeCompositionTests(unittest.TestCase):
    @staticmethod
    def _policy(*, fallback=False, signals=None):
        policy = MagicMock()
        policy.settings = SimpleNamespace(min_confidence=0.7)
        result = SimpleNamespace(is_fallback=fallback, cost=0.001)
        policy.evaluate_hourglass_stage.return_value = (
            result,
            {"native": not fallback, "result_state": "judged",
             **(signals or {
                 "context_relevant": 0.95,
                 "context_coverage_sufficient": 0.95,
                 "context_conflict_present": 0.02,
             })})
        return policy

    def _compose(self, root, policy):
        target = root / "source.py"
        target.write_text("value = 1\n", encoding="utf-8")
        settings = SimpleNamespace(hourglass_stages=["context"],
                                   token_budget_input=12000,
                                   token_budget_output=1200)
        budget = TokenBudget("run", max_input_tokens=12000,
                             max_output_tokens=1200)
        arguments = compose_arguments(
            settings, goal="update source.py", files=["source.py"],
            root=root, token_budget=budget)
        runtime = {}
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="update source.py", candidate_files=["source.py"],
            use_free=True, execute=False, root=str(root),
            jev_policy=policy, composition_runtime=runtime,
            require_context_intake=True, **arguments)
        return result, runtime, policy

    def test_native_complete_context_judgment_allows_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runtime, policy = self._compose(
                Path(tmp), self._policy())

        self.assertEqual(result["status"], "planned")
        evidence = result["context_intake"]
        self.assertTrue(evidence["native"])
        self.assertTrue(evidence["approved"])
        self.assertEqual(runtime["context_intake"], evidence)
        policy.evaluate_hourglass_stage.assert_called_once()
        call = policy.evaluate_hourglass_stage.call_args
        self.assertEqual(call.args[0], "context_intake")
        self.assertEqual(call.kwargs["site"], "hourglass-context-intake")
        self.assertIsInstance(call.kwargs["token_stage_budget"], TokenBudget)

    def test_missing_fallback_or_conflicting_context_refuses_before_planning(self):
        bad_cases = (
            self._policy(fallback=True),
            self._policy(signals={
                "context_relevant": 0.95,
                "context_coverage_sufficient": None,
                "context_conflict_present": 0.02,
            }),
            self._policy(signals={
                "context_relevant": 0.95,
                "context_coverage_sufficient": 0.95,
                "context_conflict_present": 0.8,
            }),
        )
        for policy in bad_cases:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as tmp:
                with patch("harness.waist.plan_task") as planner:
                    result, runtime, _ = self._compose(Path(tmp), policy)

            self.assertEqual(result["status"], "refused")
            self.assertFalse(result["context_intake"]["approved"])
            self.assertEqual(result["reason"], result["context_intake"]["reason"])
            self.assertFalse(planner.called)
            self.assertFalse(runtime["context_intake"]["approved"])

    def test_policy_exception_refuses_before_planning(self):
        policy = self._policy()
        policy.evaluate_hourglass_stage.side_effect = RuntimeError(
            "adapter details are not exposed")
        with tempfile.TemporaryDirectory() as tmp:
            with patch("harness.waist.plan_task") as planner:
                result, _, _ = self._compose(Path(tmp), policy)

        self.assertEqual(result["status"], "refused")
        self.assertFalse(result["context_intake"]["approved"])
        self.assertEqual(result["context_intake"]["result_state"],
                         "unavailable")
        self.assertEqual(result["context_intake"]["reason"],
                         "context intake evaluation failed: RuntimeError")
        planner.assert_not_called()

    def test_intake_gate_runs_even_when_fresh_supplied_brief_bypasses_context_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runtime, policy = self._compose(Path(tmp), self._policy())

        self.assertTrue(result["context_intake"]["approved"])
        self.assertEqual(result["context_intake"]["token_budget"]["label"],
                         "context")
        self.assertEqual(
            result["context_intake"]["token_budget"]["max_input_tokens"],
            12000)
        self.assertEqual(policy.evaluate_hourglass_stage.call_count, 1)
        self.assertIn("context", runtime["composition"]["bypassed"])


if __name__ == "__main__":
    unittest.main()
