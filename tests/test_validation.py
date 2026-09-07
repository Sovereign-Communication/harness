import hashlib
import json
import os
import tempfile
import unittest

from harness.continuation import gate_id, validate_continuation
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


if __name__ == "__main__":
    unittest.main()
