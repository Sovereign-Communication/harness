"""Hermetic unit tests for apply._prepare() instruction-override on resume.

Covers apply.py lines 197-199:
    if resumed and kwargs.get("instruction"):
        continuation = dict(continuation)
        continuation["remaining_scope"] = kwargs["instruction"]

These lines are NOT reached by CliResumeE2eTests because that test class
patches _prepare() via mock.patch.object, so the real implementation never
runs.  We exercise _prepare() directly with a minimal engine stub.
"""
import os
import tempfile
import unittest

from harness.apply import ApplyEngine
from harness.ledger import AutonomyLedger
from harness.router import Router
from tests._fake import FakeTransport, m


from harness.spend import SpendGovernor


class PrepareInstructionOverrideTests(unittest.TestCase):
    """_prepare() must overwrite continuation.remaining_scope when an
    explicit instruction kwarg accompanies a resume (DF-APPLY-1)."""

    def _engine(self, root, td):
        transport = FakeTransport(models=[m("deepseek/deepseek-chat")])
        gov = SpendGovernor(transport, "sk-test", max_cost=0.10)
        ledger = AutonomyLedger(os.path.join(td, "ledger.jsonl"))
        router = Router(["deepseek/deepseek-chat"], "deepseek/deepseek-chat",
                        "deepseek/deepseek-chat")
        engine = ApplyEngine(
            transport, "sk-test", gov, ledger, router,
            default_require_consent=False,
            default_renew_consent=False,
            allowed_roots=[root],
        )
        return engine

    def _make_continuation(self, file_path, instruction="original scope"):
        return {
            "file_path": file_path,
            "task_id": "orig-task",
            "verify_cmd": None,
            "verify_gate_id": None,
            "verify_only": False,
            "verification_required": False,
            "remaining_scope": instruction,
            "rounds": [],
        }

    def test_instruction_override_replaces_remaining_scope(self):
        """When resumed=True and instruction kwarg is given, continuation
        remaining_scope must be overwritten by kwargs['instruction']."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "sample.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x = 1\n")

            engine = self._engine(d, d)
            continuation = self._make_continuation(target, "original scope")
            kwargs = {
                "file_path": target,
                "instruction": "new override instruction",
                "continuation": continuation,
            }
            req = engine._prepare(kwargs)
            # The override must have taken effect: instruction field
            # is the overriding value (not the saved remaining_scope).
            self.assertEqual(req.instruction, "new override instruction")

    def test_instruction_override_does_not_mutate_original_continuation(self):
        """The original continuation dict must not be mutated (shallow copy)."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "sample.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x = 1\n")

            engine = self._engine(d, d)
            continuation = self._make_continuation(target, "original scope")
            kwargs = {
                "file_path": target,
                "instruction": "new override instruction",
                "continuation": continuation,
            }
            engine._prepare(kwargs)
            # Original dict must not be mutated
            self.assertEqual(continuation["remaining_scope"], "original scope")

    def test_no_instruction_kwarg_keeps_saved_scope(self):
        """Without an instruction kwarg, remaining_scope from continuation
        is used unchanged (regression guard)."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "sample.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x = 1\n")

            engine = self._engine(d, d)
            continuation = self._make_continuation(target, "saved scope")
            kwargs = {
                "file_path": target,
                "continuation": continuation,
            }
            req = engine._prepare(kwargs)
            self.assertEqual(req.instruction, "saved scope")

    def test_empty_instruction_kwarg_keeps_saved_scope(self):
        """An empty-string instruction kwarg should not override (falsy guard)."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "sample.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x = 1\n")

            engine = self._engine(d, d)
            continuation = self._make_continuation(target, "saved scope")
            kwargs = {
                "file_path": target,
                "instruction": "",  # falsy → should not override
                "continuation": continuation,
            }
            req = engine._prepare(kwargs)
            self.assertEqual(req.instruction, "saved scope")


if __name__ == "__main__":
    unittest.main()
