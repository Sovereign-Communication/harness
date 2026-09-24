"""harness.orchestrator: the completion judge and the relevance first pass.

Hermetic: every model seam is the injected ``chat_fn`` (the same contract as
``dag.decompose_via_llm``). Pins the strict-JSON verdict schema, loud
degradation to None, hallucination-proof triage (picks validated against the
real listing), and the keyword fallback."""
import unittest

from harness import orchestrator as orch
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult


class ResultProbabilityTests(unittest.TestCase):
    def test_non_dict_answers_is_none(self):
        result = JevEvaluationResult("pass", 0.0, 1.0, None, [])
        self.assertIsNone(orch._result_probability(result, "goal_achieved"))

    def test_noul_wrapped_and_bare_values(self):
        wrapped = JevEvaluationResult(
            "pass", 0.0, 1.0, {"goal_achieved": {"noul": 0.75}}, [])
        self.assertAlmostEqual(
            orch._result_probability(wrapped, "goal_achieved"), 0.75)
        bare = JevEvaluationResult(
            "pass", 0.0, 1.0, {"goal_achieved": 0.4}, [])
        self.assertAlmostEqual(
            orch._result_probability(bare, "goal_achieved"), 0.4)

    def test_non_numeric_or_bool_value_is_none(self):
        boolean = JevEvaluationResult(
            "pass", 0.0, 1.0, {"goal_achieved": True}, [])
        self.assertIsNone(orch._result_probability(boolean, "goal_achieved"))
        stringy = JevEvaluationResult(
            "pass", 0.0, 1.0, {"goal_achieved": "yes"}, [])
        self.assertIsNone(orch._result_probability(stringy, "goal_achieved"))

    def test_value_is_clamped_to_unit_range(self):
        over = JevEvaluationResult(
            "pass", 0.0, 1.0, {"goal_achieved": 1.5}, [])
        self.assertEqual(orch._result_probability(over, "goal_achieved"), 1.0)


class AssessCompletionTests(unittest.TestCase):
    def test_complete_verdict_parsed(self):
        def chat_fn(prompt):
            self.assertIn("completion judge", prompt)
            return 'noise {"complete": true, "remaining": "", "reason": "gates green"} trailing'
        v = orch.assess_completion("the goal", "- subtask n1 status=ok", chat_fn)
        self.assertTrue(v["complete"])
        self.assertEqual(v["reason"], "gates green")

    def test_incomplete_verdict_carries_remaining_scope(self):
        def chat_fn(prompt):
            return '{"complete": false, "remaining": "the second half", "reason": "partial"}'
        v = orch.assess_completion("the goal", "state", chat_fn)
        self.assertFalse(v["complete"])
        self.assertEqual(v["remaining"], "the second half")

    def test_unusable_response_is_none_not_an_invention(self):
        for bad in (None, "", "no json here", "[1, 2]", '{"complete": "yes"}'):
            self.assertIsNone(orch.assess_completion("g", "s", lambda p: bad))

    def test_judge_model_failure_propagates_as_harness_error(self):
        def chat_fn(prompt):
            raise HarnessError("HTTP 429")
        with self.assertRaises(HarnessError):
            orch.assess_completion("g", "s", chat_fn)


class DriveTruthTests(unittest.TestCase):
    def test_failed_node_overrides_complete_judge(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                            "target_files": ["a.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}
        driven = orch.drive(
            goal="edit a.py", target_files=[], initial_plan=plan,
            root_dir=".", plan_round=lambda goal: plan,
            execute_plan=lambda current: {"n1": {"status": "failed"}},
            completion_chat=lambda prompt: '{"complete": true}',
            emit=lambda *args, **kwargs: None)
        self.assertFalse(driven["final_all_ok"])
        self.assertIn("did not complete", driven["remaining_scope"])

    def test_jev_completion_threshold_requires_another_round(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                            "target_files": ["a.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}

        class _FakeJevPolicy:
            def evaluate_completion_nouls(self, goal, state_summary, *,
                                          named_artifacts=None, root_dir=None,
                                          site="completion"):
                result = JevEvaluationResult(
                    "pass", 0.0, 0.5, {"goal_achieved": 0.5}, [],
                    is_fallback=False, model="jev-test")
                return result, {"cannot_complete": False,
                               "missing_artifacts": []}

        events = []
        driven = orch.drive(
            goal="edit a.py", target_files=[], initial_plan=plan,
            root_dir=".", plan_round=lambda goal: plan,
            execute_plan=lambda current: {"n1": {"status": "ok"}},
            completion_chat=lambda prompt: '{"complete": true}',
            emit=lambda *args, **kwargs: events.append((args, kwargs)),
            jev_policy=_FakeJevPolicy(), jev_completion_threshold=0.99,
            max_rounds=1)

        self.assertFalse(driven["final_all_ok"])
        self.assertIn("Jev completion support 0.500", driven["remaining_scope"])
        self.assertIn("below the required 0.990", driven["remaining_scope"])
        self.assertEqual(driven["rounds_history"][-1]["jev"],
                         {"native": True, "supported": 0.5,
                          "cannot_complete": False,
                          "reason": "completion nouls passed"})

    def test_jev_native_support_at_threshold_falls_through_to_judge(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                            "target_files": ["a.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}

        class _FakeJevPolicy:
            def evaluate_completion_nouls(self, goal, state_summary, *,
                                          named_artifacts=None, root_dir=None,
                                          site="completion"):
                result = JevEvaluationResult(
                    "pass", 0.0, 0.995, {"goal_achieved": 0.995}, [],
                    is_fallback=False, model="jev-test")
                return result, {"cannot_complete": False,
                               "missing_artifacts": []}

        driven = orch.drive(
            goal="improve the widget", target_files=[], initial_plan=plan,
            root_dir=".", plan_round=lambda goal: plan,
            execute_plan=lambda current: {"n1": {"status": "ok"}},
            completion_chat=lambda prompt: '{"complete": true}',
            emit=lambda *args, **kwargs: None,
            jev_policy=_FakeJevPolicy(), jev_completion_threshold=0.99,
            max_rounds=1)

        self.assertTrue(driven["final_all_ok"])


class TriageFilesTests(unittest.TestCase):
    _FILES = ["harness/agent.py", "harness/dag.py", "harness/web.py",
              "tests/test_agent.py", "README.md"]

    def test_picks_validated_against_real_listing(self):
        def chat_fn(prompt):
            self.assertIn("harness/dag.py", prompt)
            return ('{"files": ["harness/dag.py", "harness/ghost.py", '
                    '"harness/agent.py", "harness/dag.py"]}')
        picked = orch.triage_files("fix the dag planner", self._FILES, chat_fn)
        # hallucinated ghost.py dropped, duplicate dag.py dropped, order kept
        self.assertEqual(picked, ["harness/dag.py", "harness/agent.py"])

    def test_model_failure_returns_empty_for_caller_fallback(self):
        def chat_fn(prompt):
            raise HarnessError("no model")
        self.assertEqual(orch.triage_files("g", self._FILES, chat_fn), [])

    def test_cap_enforced(self):
        files = [f"f{i}.py" for i in range(40)]
        blob = ", ".join(f'"f{i}.py"' for i in range(40))
        def chat_fn(prompt):
            return '{"files": [' + blob + "]}"
        picked = orch.triage_files("g", files, chat_fn)
        self.assertEqual(len(picked), orch.MAX_TRIAGE_FILES)

    def test_empty_listing_short_circuits(self):
        self.assertEqual(orch.triage_files("g", [], lambda p: '["x"]'), [])


class KeywordFallbackTests(unittest.TestCase):
    def test_keyword_overlap_ranks_and_caps(self):
        files = ["harness/spend.py", "harness/dag.py", "harness/spend_gov.py",
                 "README.md"]
        picked = orch.keyword_fallback("fix the spend governor ceiling",
                                       files, max_files=2)
        self.assertNotIn("harness/dag.py", picked)
        self.assertNotIn("README.md", picked)
        self.assertEqual(len(picked), 2)
        self.assertTrue(all("spend" in p for p in picked))

    def test_no_overlap_returns_empty(self):
        self.assertEqual(orch.keyword_fallback("quantum toaster repair",
                                               ["harness/dag.py"]), [])


class BuildStateSummaryTests(unittest.TestCase):
    def test_renders_node_statuses_and_notes_bounded(self):
        summary = orch.build_state_summary(
            [{"node_id": "n1", "file_path": "a.py",
                      "status": "verify_failed", "error": "boom"}] * 200,
            extra_notes=["round 1"])
        self.assertIn("n1", summary)
        self.assertIn("verify_failed", summary)
        self.assertIn("round 1", summary)
        self.assertLessEqual(len(summary), 8000)


if __name__ == "__main__":
    unittest.main()
