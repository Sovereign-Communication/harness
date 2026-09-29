import hashlib
import os
import tempfile
import unittest

from harness.continuation import gate_id, validate_continuation
from harness.consent import make_consent_binding
from harness.errors import HarnessError
from harness.validation import finite_number, validate_apply_request


class ValidationTests(unittest.TestCase):
    def test_rejects_nonfinite_numbers(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(HarnessError):
                finite_number(value, "cost")

    def test_apply_request_rejects_bad_limits(self):
        with self.assertRaises(HarnessError):
            validate_apply_request(
                max_rounds=0, max_tokens=4096, task_max_cost=0.05,
                max_rotations=3, max_lines=500, instruction="x")
        with self.assertRaises(HarnessError):
            validate_apply_request(
                max_rounds=3, max_tokens=200001, task_max_cost=0.05,
                max_rotations=3, max_lines=500, instruction="x")

    def test_apply_request_normalizes_values(self):
        result = validate_apply_request(
            max_rounds="3", max_tokens="4096", task_max_cost="0.05",
            max_rotations=2, max_lines=500, instruction="change",
            backend="diff")
        self.assertEqual(result["max_rounds"], 3)
        self.assertEqual(result["backend"], "diff")


class ContinuationIntegrityTests(unittest.TestCase):
    def test_changed_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.py")
            with open(target, "wb") as stream:
                stream.write(b"x = 1\n")
            digest = hashlib.sha256(b"x = 1\n").hexdigest()
            state = {
                "schema_version": 1,
                "file_path": target,
                "target_hash": digest,
                "verify_only": True,
                "verification_required": False,
            }
            with open(target, "wb") as stream:
                stream.write(b"x = 2\n")
            with self.assertRaisesRegex(HarnessError, "changed"):
                validate_continuation(state)

    def test_valid_target_and_gate_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.py")
            with open(target, "wb") as stream:
                stream.write(b"x = 1\n")
            command = "python -m py_compile target.py"
            state = {
                "schema_version": 1,
                "file_path": target,
                "target_hash": hashlib.sha256(b"x = 1\n").hexdigest(),
                "verify_only": False,
                "verification_required": True,
                "verify_cmd": command,
                "verify_gate_id": gate_id(command),
            }
            self.assertEqual(validate_continuation(state)["file_path"], target)

    def test_legacy_continuation_without_consent_binding_remains_loadable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.py")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("x = 1\n")
            state = {"schema_version": 1, "file_path": target,
                     "verify_only": True, "verification_required": False}
            self.assertEqual(validate_continuation(state), state)

    def test_corrupt_consent_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.py")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("x = 1\n")
            binding = make_consent_binding(
                file_path=target, source_content="x = 1\n", instruction="edit",
                selected_model="worker-a", max_tokens=1024,
                task_max_cost=0.05)
            binding["instruction"] = "tampered"
            state = {"schema_version": 1, "file_path": target,
                     "verify_only": True, "verification_required": False,
                     "consent_binding": binding}
            with self.assertRaisesRegex(HarnessError, "consent_binding"):
                validate_continuation(state)


if __name__ == "__main__":
    unittest.main()
