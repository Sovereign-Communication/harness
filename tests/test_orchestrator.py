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
    def test_consent_block_stops_without_judge_or_replan(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                            "target_files": ["a.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}

        def unexpected(*args, **kwargs):
            raise AssertionError("consent handoff must stop further dispatch")

        driven = orch.drive(
            goal="edit a.py", target_files=[], initial_plan=plan,
            root_dir=".", plan_round=unexpected,
            execute_plan=lambda current: {
                "n1": {"status": "consent_blocked", "reason": "review needed"}},
            completion_chat=unexpected, emit=lambda *args, **kwargs: None,
            max_rounds=3)

        self.assertFalse(driven["final_all_ok"])
        self.assertIn("consent_blocked", driven["remaining_scope"])
        self.assertIn("review needed", driven["remaining_scope"])
        self.assertEqual(len(driven["rounds_history"]), 1)

    def test_explicit_defer_stops_without_judge_or_replan(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "edit",
                            "target_files": ["a.py"]}],
                "dag": {"nodes": [{"node_id": "n1"}]}}

        def unexpected(*args, **kwargs):
            raise AssertionError("explicit defer must stop further dispatch")

        driven = orch.drive(
            goal="edit a.py", target_files=[], initial_plan=plan,
            root_dir=".", plan_round=unexpected,
            execute_plan=lambda current: {
                "n1": {"status": "deferred", "remaining_scope": "operator handoff"}},
            completion_chat=unexpected, emit=lambda *args, **kwargs: None,
            max_rounds=3)

        self.assertFalse(driven["final_all_ok"])
        self.assertIn("deferred", driven["remaining_scope"])
        self.assertIn("operator handoff", driven["remaining_scope"])
        self.assertEqual(len(driven["rounds_history"]), 1)

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

    def test_final_alignment_preserves_full_brief_and_exact_request(self):
        brief = {"text": "retained " + ("context " * 3000),
                 "sources": [{"uri": "doc://source", "quote": "verbatim"}]}
        captured = {}

        class Policy:
            def evaluate_answer(self, request, answer, context, **kwargs):
                captured.update(request=request, answer=answer, context=context,
                                kwargs=kwargs)
                result = JevEvaluationResult(
                    "pass", 1.0, 1.0,
                    {"answer_sufficient": 1.0, "iteration_required": False,
                     "plan_required": False}, [], is_fallback=False)
                return result, {"native": True}

        original = "  keep bytes exactly\n"
        result, alignment = orch.assess_final_alignment(
            goal=original, candidate={"facts": ["all"]},
            retained_brief=brief, jev_policy=Policy())
        self.assertEqual(captured["request"], original)
        self.assertIn("doc://source", captured["context"])
        self.assertIn("retained " + ("context " * 3000), captured["context"])
        self.assertEqual(captured["kwargs"]["max_context_chars"],
                         len(captured["context"]))
        self.assertGreater(captured["kwargs"]["max_input_tokens"], 512)
        self.assertTrue(alignment["aligned"])

    def test_alignment_unavailable_and_below_threshold_block_completion(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "work"}],
                "dag": {"nodes": [{"node_id": "n1"}]}}

        class Policy:
            def __init__(self, fallback, score):
                self.fallback, self.score = fallback, score
            def evaluate_completion_nouls(self, *args, **kwargs):
                result = JevEvaluationResult(
                    "pass", 1.0, 1.0, {"goal_achieved": 1.0}, [],
                    is_fallback=False)
                return result, {"cannot_complete": False}
            def evaluate_answer(self, *args, **kwargs):
                result = JevEvaluationResult(
                    "pass", 1.0, self.score,
                    {"answer_sufficient": self.score,
                     "iteration_required": False, "plan_required": False}, [],
                    is_fallback=self.fallback)
                return result, {"native": not self.fallback}

        for policy in (Policy(True, 1.0), Policy(False, 0.8)):
            driven = orch.drive(
                goal="g", target_files=[], initial_plan=plan, root_dir=".",
                plan_round=lambda goal: plan,
                execute_plan=lambda current: {"n1": {"status": "ok"}},
                completion_chat=lambda prompt: '{"complete": true}',
                emit=lambda *args, **kwargs: None, jev_policy=policy,
                retained_brief={"brief": "full"}, max_rounds=1)
            self.assertFalse(driven["final_all_ok"])

    def test_validated_alignment_restart_preserves_prior_results(self):
        first_plan = {"total_nodes": 1, "total_cost_ceiling": 0.01,
                      "nodes": [{"node_id": "n1",
                                 "instruction": "implement base",
                                 "target_files": ["a.py"],
                                 "cost_ceiling": 0.01,
                                 "route": {"cost_ceiling": 0.01}}],
                      "dag": {"nodes": [{"node_id": "n1",
                                          "instruction": "implement base",
                                          "target_files": ["a.py"]}]}}
        amendment_plan = {
            "total_nodes": 2, "total_cost_ceiling": 0.02,
            "nodes": [
                {"node_id": "n1", "instruction": "implement base",
                 "target_files": ["a.py"], "cost_ceiling": 0.01,
                 "route": {"cost_ceiling": 0.01}},
                {"node_id": "n2", "instruction": "close alignment gap",
                 "target_files": ["a.py"], "dependencies": ["n1"],
                 "cost_ceiling": 0.01,
                 "route": {"cost_ceiling": 0.01}},
            ],
            "dag": {"nodes": [
                {"node_id": "n1", "instruction": "implement base",
                 "target_files": ["a.py"]},
                {"node_id": "n2", "instruction": "close alignment gap",
                 "target_files": ["a.py"], "dependencies": ["n1"]},
            ]},
        }
        calls = {"executed": [], "amendment": None,
                 "restart_dimension": None}

        class Policy:
            def __init__(self):
                self.alignment_calls = 0
            def evaluate_completion_nouls(self, *args, **kwargs):
                return JevEvaluationResult("pass", 1, 1,
                    {"goal_achieved": 1}, [], is_fallback=False), {
                        "cannot_complete": False}
            def evaluate_answer(self, *args, **kwargs):
                self.alignment_calls += 1
                support = 0.5 if self.alignment_calls == 1 else 1.0
                return JevEvaluationResult("pass", support, support,
                    {"answer_sufficient": support,
                     "iteration_required": self.alignment_calls == 1,
                     "plan_required": False}, [], is_fallback=False), {
                         "native": True}
            def evaluate_hourglass_stage(self, dimension, state, **kwargs):
                calls["restart_dimension"] = dimension
                return JevEvaluationResult("pass", 1, 1,
                    {"restart_target": {"target": "context"}}, [],
                    is_fallback=False), {"native": True}

        def plan_round(_goal):
            self.fail("alignment retries must use the amendment handler")
        def plan_amendment(amendment_goal, request):
            calls["amendment"] = (amendment_goal, request)
            return amendment_plan
        def execute(current):
            node_ids = [node["node_id"] for node in current["dag"]["nodes"]]
            calls["executed"].append(node_ids)
            return {node_id: {"status": "ok", "evidence": node_id}
                    for node_id in node_ids}

        driven = orch.drive(
            goal="original intent", target_files=[], initial_plan=first_plan,
            root_dir=".", plan_round=plan_round, execute_plan=execute,
            completion_chat=lambda prompt: '{"complete": true}',
            emit=lambda *args, **kwargs: None, jev_policy=Policy(),
            retained_brief={"brief": "complete"}, max_rounds=2,
            completed_stages=["context", "planning"],
            plan_amendment=plan_amendment)
        self.assertEqual(set(driven["all_results"]), {"r1/n1", "r2/n2"})
        self.assertTrue(driven["final_all_ok"])
        self.assertEqual(driven["rounds_history"][0]["restart"]["target"],
                         "context")
        self.assertEqual(calls["executed"], [["n1"], ["n2"]])
        self.assertIn("original intent", calls["amendment"][0])
        self.assertIn("bounded delta plan", calls["amendment"][0])
        self.assertEqual(calls["amendment"][1]["target"], "context")
        self.assertEqual(calls["restart_dimension"], "restart_target")
        self.assertEqual(
            driven["rounds_history"][0]["restart"]["preserved_stages"],
            ["context", "planning", "execution"])
        self.assertTrue(driven["rounds_history"][0]["restart"][
            "consent_renewal_required"])

    def test_alignment_restart_without_amendment_handler_fails_closed(self):
        plan = {"total_nodes": 1, "total_cost_ceiling": 0.0,
                "nodes": [{"node_id": "n1", "instruction": "work"}],
                "dag": {"nodes": [{"node_id": "n1",
                                    "instruction": "work"}]}}

        class Policy:
            def evaluate_completion_nouls(self, *args, **kwargs):
                return JevEvaluationResult("pass", 1, 1,
                    {"goal_achieved": 1}, [], is_fallback=False), {
                        "cannot_complete": False}
            def evaluate_answer(self, *args, **kwargs):
                return JevEvaluationResult("fail", 0.5, 0.5,
                    {"answer_sufficient": 0.5, "iteration_required": True,
                     "plan_required": False}, [], is_fallback=False), {
                         "native": True}
            def evaluate_hourglass_stage(self, dimension, state, **kwargs):
                return JevEvaluationResult("pass", 1, 1,
                    {"restart_target": {"target": "context"}}, [],
                    is_fallback=False), {"native": True}

        driven = orch.drive(
            goal="intent", target_files=[], initial_plan=plan, root_dir=".",
            plan_round=lambda _goal: self.fail("must not generic-replan"),
            execute_plan=lambda _plan: {"n1": {"status": "ok"}},
            completion_chat=lambda _prompt: self.fail("must not complete"),
            emit=lambda *args, **kwargs: None, jev_policy=Policy(),
            retained_brief={"brief": "complete"}, max_rounds=2,
            completed_stages=["context", "planning"])
        self.assertFalse(driven["final_all_ok"])
        self.assertIn("handler is unavailable",
                      driven["rounds_history"][0]["restart"]["reason"])
        self.assertEqual(len(driven["all_results"]), 1)

    def test_restart_validator_refuses_completed_and_unknown_stages(self):
        from harness.jev_packs import validate_restart_request
        completed = validate_restart_request(
            "execution", "context", completed_stages=["context"])
        unknown = validate_restart_request(
            "execution", "invented", completed_stages=[])
        self.assertFalse(completed["allowed"])
        self.assertFalse(unknown["allowed"])


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
