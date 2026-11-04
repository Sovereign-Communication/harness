"""#430: one terminal state for validated restart/consent refusals."""
import tempfile
import unittest
from pathlib import Path

from harness.execution_stages import ExecutionStageMixin
from harness.restart_policy import (
    BLOCKED,
    REAPPROVAL_REQUIRED,
    RESUMED,
    RestartStateStore,
    assignment_hash,
)


class _Host(ExecutionStageMixin):
    """No Jev: exercises the production restart path without a model."""

    def _ask_stage_dimension(self, dimension, state, jev_policy, *, task_id=None):
        return {"signals": {}, "native": False}


def restart(store, **overrides):
    values = dict(
        request_id="req-1",
        assignment={"goal": "edit calc.py"},
        current_stage="execution",
        requested_target="execution",
        completed_stages=[],
        consent_fresh=None,
        consent_id="consent-1",
        checkpoint={"id": "ckpt-1"},
        prior_results={"planning": {"ok": True}},
    )
    values.update(overrides)
    return store.decide_restart(**values)


class RestartRefusalTests(unittest.TestCase):
    def test_same_stage_refusal_is_terminal_and_deduplicated(self):
        events = []
        store = RestartStateStore(event=lambda *a, **k: events.append((a, k)))
        first = restart(store)
        self.assertEqual(first["status"], BLOCKED)
        self.assertTrue(first["terminal"])
        self.assertFalse(first["deduplicated"])
        self.assertTrue(first["event_emitted"])
        self.assertEqual(events[0][0], ("restart_terminal",))

        second = restart(store)
        self.assertTrue(second["deduplicated"])
        self.assertFalse(second["event_emitted"])
        self.assertEqual(len(events), 1)
        self.assertEqual(len(store._states), 1)
        self.assertIn("not earlier", " ".join(second["reasons"]))

    def test_stale_consent_requires_exact_reapproval(self):
        store = RestartStateStore()
        result = restart(store, requested_target="planning",
                         consent_fresh=False)
        self.assertEqual(result["status"], REAPPROVAL_REQUIRED)
        self.assertTrue(result["terminal"])
        self.assertIn(result["assignment_hash"], result["next_action"])
        self.assertIn("renew consent", result["next_action"])
        self.assertEqual(
            len(store._states), 1,
            "a refusal may not dispatch, write, or spend on repetition")

    def test_valid_earlier_restart_preserves_completed_evidence(self):
        store = RestartStateStore()
        result = restart(store, requested_target="planning",
                         consent_fresh=True)
        self.assertEqual(result["status"], "allowed")
        self.assertFalse(result["terminal"])
        self.assertEqual(result["preserved_stages"], [])
        self.assertEqual(result["prior_results"], {"planning": {"ok": True}})

    def test_completed_target_is_blocked_and_preserved(self):
        store = RestartStateStore()
        result = restart(store, requested_target="planning",
                         completed_stages=["planning"])
        self.assertEqual(result["status"], BLOCKED)
        self.assertEqual(result["preserved_stages"], ["planning"])
        self.assertIn("already recorded complete", " ".join(result["reasons"]))

    def test_checkpoint_resume_is_distinct_and_bound(self):
        store = RestartStateStore()
        checkpoint = {"request_id": "req-1", "plan_hash": "plan-1",
                      "stage": "execution"}
        consent = {"fresh": True, "assignment_hash": "plan-1", "id": "c-1"}
        result = store.decide_resume(
            request_id="req-1", plan_hash="plan-1",
            checkpoint=checkpoint, consent=consent)
        self.assertEqual(result["status"], RESUMED)
        self.assertEqual(result["operation"], "resume")

        mismatched = store.decide_resume(
            request_id="req-1", plan_hash="plan-1",
            checkpoint={"request_id": "other", "plan_hash": "plan-1"},
            consent=consent)
        self.assertEqual(mismatched["status"], BLOCKED)
        self.assertTrue(mismatched["terminal"])

    def test_resume_never_reuses_expired_consent(self):
        store = RestartStateStore()
        result = store.decide_resume(
            request_id="req-1", plan_hash="plan-1",
            checkpoint={"request_id": "req-1", "plan_hash": "plan-1"},
            consent={"fresh": False, "assignment_hash": "plan-1"})
        self.assertEqual(result["status"], REAPPROVAL_REQUIRED)
        self.assertTrue(result["terminal"])
        self.assertIn("renew consent", result["next_action"])

    def test_durable_store_reloads_and_dedupes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "restart.json"
            first = restart(RestartStateStore(path=str(path)))
            self.assertFalse(first["deduplicated"])
            reloaded = RestartStateStore(path=str(path))
            self.assertEqual(reloaded.last("req-1")["status"], BLOCKED)
            again = restart(reloaded)
            self.assertTrue(again["deduplicated"])
            self.assertEqual(len(reloaded._states), 1)

    def test_secret_never_lands_in_the_state(self):
        secret = "sk-or-v1-" + "z" * 24
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "restart.json"
            restart(RestartStateStore(path=str(path)),
                    assignment={"goal": secret},
                    checkpoint={"id": "c", "secret": secret},
                    consent_id=secret)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, text)
            self.assertIn("[REDACTED]", text)

    def test_production_restart_decision_carries_terminal_state(self):
        host = _Host()
        judgment = {"signals": {}}
        first = host._restart_decision("goal", None, judgment)
        self.assertIn("terminal_state", first)
        self.assertTrue(first["terminal_state"]["terminal"])
        second = host._restart_decision("goal", None, {"signals": {}})
        self.assertTrue(second["terminal_state"]["deduplicated"])

    def test_assignment_identity_is_stable_and_content_free(self):
        secret = "sk-or-v1-" + "q" * 24
        one = assignment_hash({"goal": secret})
        two = assignment_hash({"goal": secret})
        self.assertEqual(one, two)
        self.assertNotIn(secret, one)
        self.assertNotEqual(one, assignment_hash({"goal": "other"}))


if __name__ == "__main__":
    unittest.main()
