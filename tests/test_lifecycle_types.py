"""Behavioral checks for the discriminated request lifecycle contract."""
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from harness.errors import HarnessError
from harness.lifecycle import LifecycleCoordinator, adapter_for
from harness.lifecycle_types import validate_lifecycle_state


class LifecycleTypesTests(unittest.TestCase):
    def state(self, lane="chat"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        coordinator = LifecycleCoordinator(history_dir=Path(tmp.name))
        return adapter_for(coordinator, lane).begin(
            f"session-{lane}", "continue the task")

    def assert_invalid(self, state):
        with self.assertRaises(HarnessError):
            validate_lifecycle_state(state)

    def test_each_lane_uses_a_valid_discriminated_stage(self):
        for lane in ("chat", "audit", "auto_edit", "driver", "provisioning"):
            with self.subTest(lane=lane):
                state = self.state(lane)
                self.assertIs(validate_lifecycle_state(state), state)

        audit = self.state("audit")
        audit["stage"] = "answer_review"
        self.assert_invalid(audit)

    def test_status_and_stage_must_form_a_valid_state(self):
        state = self.state()
        invalid = deepcopy(state)
        invalid["status"], invalid["stage"] = "complete", "answer_review"
        self.assert_invalid(invalid)
        invalid = deepcopy(state)
        invalid["status"], invalid["stage"] = "interrupted", "terminal"
        self.assert_invalid(invalid)

    def test_followup_and_review_history_are_bounded_and_consistent(self):
        state = self.state()
        invalid = deepcopy(state)
        invalid["followups"] = ["resume"] * 17
        self.assert_invalid(invalid)
        invalid = deepcopy(state)
        invalid["review"]["rounds"] = 1
        self.assert_invalid(invalid)

        invalid = deepcopy(state)
        invalid["review"].update({
            "rounds": 1, "decision": "pass", "confidence": 0.9,
            "history": [{
                "review_id": "review-1", "decision": "pass", "native": True,
                "confidence": 0.9, "supported": 0.8,
                "iteration_required": False, "plan_required": False, "cost": 0.0,
            }],
        })
        self.assertIs(validate_lifecycle_state(invalid), invalid)

        for field, value in (("confidence", 1.1), ("supported", -0.1),
                             ("cost", float("nan"))):
            bad = deepcopy(invalid)
            bad["review"]["history"][0][field] = value
            self.assert_invalid(bad)

    def test_provider_usage_and_effect_receipts_must_reconcile(self):
        state = self.state()
        invalid = deepcopy(state)
        invalid["usage"]["known_cost"] = True
        self.assert_invalid(invalid)

        invalid = deepcopy(state)
        invalid["usage"]["provider_calls"]["call-1"] = {
            "status": "unknown", "reserved_cost": -0.01,
            "actual_cost": 0.0, "result": None,
        }
        self.assert_invalid(invalid)

        invalid = deepcopy(state)
        invalid["status"], invalid["stage"] = "complete", "terminal"
        invalid["usage"]["provider_calls"]["call-1"] = {
            "status": "dispatched", "reserved_cost": 0.01,
            "actual_cost": 0.0, "result": None,
        }
        self.assert_invalid(invalid)

        invalid = deepcopy(state)
        invalid["execution"]["effects"]["effect-1"] = {
            "effect_id": "effect-1", "operation": "provision.create",
            "status": "unknown", "approval_state": {}, "result": None,
        }
        self.assert_invalid(invalid)

        valid = deepcopy(invalid)
        valid["execution"]["requires_reconciliation"] = ["effect-1"]
        self.assertIs(validate_lifecycle_state(valid), valid)
        valid["status"], valid["stage"] = "complete", "terminal"
        self.assert_invalid(valid)


if __name__ == "__main__":
    unittest.main()
