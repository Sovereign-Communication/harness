"""HV-5 acceptance: consent freshness and honest defer handoffs."""
import tempfile
import unittest

from harness.jev_packs import validate_restart_request
from harness.orchestrator import drive
from tests._applyfixture import ApplyFixture, CHANGED, scripted_run
from tests._fake import comp, consent


class ConsentContinuationTests(ApplyFixture):
    def _failed_task(self):
        path = self.make_file()
        _fake, _, _, engine = self.make_env(
            posts=[consent("accept"), comp(CHANGED)],
            run=scripted_run([(1, "verify failed")]),
            default_consent=True, renew=False)
        result = engine.apply_edit(
            task_id="hv5-cost-resume", file_path=path, instruction="change",
            verify_cmd="check", require_consent=True, max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        return path, result["continuation"]

    def test_changed_token_or_cost_limit_requires_fresh_consent(self):
        _path, continuation = self._failed_task()
        fake, _, ledger, engine = self.make_env(
            posts=[consent("accept"), comp(CHANGED + "\n")],
            default_consent=True, renew=False)
        engine.run_verify = scripted_run([(0, "")])

        result = engine.apply_edit(
            continuation=continuation, instruction="change",
            max_tokens=2048, require_consent=True, max_rounds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(fake.chat_posts()), 2)
        stale = next(row for row in ledger.entries()
                     if row["event"] == "consent_stale")
        self.assertEqual(stale["events"], ["changed_token_cost_limits"])


class DeferHandoffTests(unittest.TestCase):
    def test_decline_or_defer_stops_before_completion_and_preserves_results(self):
        with tempfile.TemporaryDirectory() as root:
            completed = {"status": "ok", "content": "evidence"}
            deferred = {"status": "deferred", "reason": "worker needs input"}
            calls = []
            judges = []

            def execute(_plan):
                calls.append("dispatch")
                return {"done": completed, "later": deferred}

            result = drive(
                goal="finish the requested change", target_files=[],
                initial_plan={"dag": {"nodes": [{"node_id": "done"}]},
                              "nodes": [{"node_id": "done"}],
                              "total_nodes": 1, "total_cost_ceiling": 0.1},
                execute_plan=execute, completion_chat=lambda prompt: judges.append(prompt),
                emit=lambda *_a, **_kw: None, root_dir=root,
                plan_round=lambda _goal: self.fail("defer must not re-plan"),
                max_rounds=3)

            self.assertEqual(calls, ["dispatch"])
            self.assertEqual(judges, [])
            self.assertFalse(result["final_all_ok"])
            self.assertIn("Execution stopped", result["remaining_scope"])
            self.assertEqual(result["all_results"]["r1/done"], completed)
            self.assertEqual(result["all_results"]["r1/later"], deferred)

    def test_restart_target_must_be_declared_and_preserve_completed_stages(self):
        decision = validate_restart_request(
            "execution", "planning", completed_stages=["context", "planning"],
            consent_fresh=False)
        self.assertFalse(decision["allowed"])
        self.assertIn("planning", decision["preserved_stages"])

        stale_assignment = validate_restart_request(
            "execution", "planning", completed_stages=["context"],
            consent_fresh=False)
        self.assertFalse(stale_assignment["allowed"])
        self.assertIn("context", stale_assignment["preserved_stages"])
        self.assertTrue(stale_assignment["consent_renewal_required"])
        fresh_assignment = validate_restart_request(
            "execution", "planning", completed_stages=["context"],
            consent_fresh=True)
        self.assertTrue(fresh_assignment["allowed"])


if __name__ == "__main__":
    unittest.main()
