"""harness.orchestrator: the completion judge and the relevance first pass.

Hermetic: every model seam is the injected ``chat_fn`` (the same contract as
``dag.decompose_via_llm``). Pins the strict-JSON verdict schema, loud
degradation to None, hallucination-proof triage (picks validated against the
real listing), and the keyword fallback."""
import unittest

from harness import orchestrator as orch
from harness.errors import HarnessError


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
