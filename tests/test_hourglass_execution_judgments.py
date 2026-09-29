"""HV-1 execution judgment is supplemental to package safety gates."""
import unittest
from unittest.mock import MagicMock

from harness.dag import DAGNode
from harness.executor import PlanExecutor
from harness.token_budget import TokenBudget


class FakeHourglassPolicy:
    def __init__(self, signals, native=True):
        self.signals = signals
        self.native = native
        self.calls = []

    def evaluate_hourglass_stage(self, dimension, state, **kwargs):
        self.calls.append((dimension, state, kwargs))
        return None, {"dimension": dimension, "native": self.native,
                      **self.signals}


class ExecutionJudgmentTests(unittest.TestCase):
    def setUp(self):
        self.budget = TokenBudget("execution", max_input_tokens=20_000,
                                  max_output_tokens=8_000)
        self.node = DAGNode(
            "pkg-1", "edit one function", target_files=("src/module.py",),
            local_gate="python -m unittest")

    def _executor(self, policy, engine):
        return PlanExecutor(
            engine, {}, parallel=False, isolate=False,
            token_budget=self.budget, jev_policy=policy,
            base_apply_kwargs={"allow_verify": True},
            require_diff_authorization=True)

    def test_unsuitable_package_is_deferred_before_apply(self):
        policy = FakeHourglassPolicy({
            "execution_suitable": 0.1, "checkpoint_required": 0.0})
        engine = MagicMock()
        result = self._executor(policy, engine).run_node(self.node)

        self.assertEqual(result["status"], "deferred")
        self.assertIn("recommends deferring", result["reason"])
        engine.apply_edit.assert_not_called()
        self.assertEqual(policy.calls[0][0], "execution")
        self.assertEqual(policy.calls[0][1]["node_id"], "pkg-1")

    def test_checkpoint_recommendation_stops_before_write(self):
        policy = FakeHourglassPolicy({
            "execution_suitable": 0.9, "checkpoint_required": 0.8})
        engine = MagicMock()
        result = self._executor(policy, engine).run_node(self.node)

        self.assertEqual(result["status"], "deferred")
        self.assertIn("checkpoint", result["reason"])
        engine.apply_edit.assert_not_called()

    def test_positive_judgment_never_replaces_consent_or_local_gates(self):
        policy = FakeHourglassPolicy({
            "execution_suitable": 0.99, "checkpoint_required": 0.01})
        engine = MagicMock()
        engine.apply_edit.return_value = {"status": "ok"}
        result = self._executor(policy, engine).run_node(self.node)

        self.assertEqual(result["status"], "ok")
        kwargs = engine.apply_edit.call_args.kwargs
        self.assertIs(kwargs["require_consent"], True)
        self.assertIs(kwargs["renew_consent"], True)
        self.assertIs(kwargs["require_diff_authorization"], True)
        self.assertIs(kwargs["allow_verify"], True)
        self.assertEqual(kwargs["verify_cmd"], "python -m unittest")
        self.assertEqual(result["hourglass_execution"]["native"], True)

    def test_unavailable_answer_is_not_exposed_as_positive_approval(self):
        policy = FakeHourglassPolicy({
            "execution_suitable": None, "checkpoint_required": None},
            native=False)
        engine = MagicMock()
        engine.apply_edit.return_value = {"status": "ok"}
        result = self._executor(policy, engine).run_node(self.node)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["hourglass_execution"]["native"])
        self.assertIsNone(result["hourglass_execution"]["execution_suitable"])
        kwargs = engine.apply_edit.call_args.kwargs
        self.assertTrue(kwargs["require_consent"])
        self.assertTrue(kwargs["require_diff_authorization"])
        self.assertEqual(kwargs["verify_cmd"], "python -m unittest")


if __name__ == "__main__":
    unittest.main()
