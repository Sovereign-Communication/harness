"""Waist plan-confirmation + LLM decomposition lanes (hermetic).

Covers the M1/M2 seams: dag.decompose_via_llm (chat_fn injected),
plan_task(decomposed_dag=...), dag.build_waist_prompt/parse_waist_verdict
(strict verdict contract), waist.read_window (cwd containment + honest
in-band failures), waist.confirm_plan (bounded window rounds, ledgered
verdicts, fail-closed refusal), and waist.compose_plan (the ONE owner the
CLI and MCP call). No network: chat_fn/reader seams + canned responses.
"""
import json
import os
import tempfile
import unittest

from harness.dag import (build_waist_prompt, decompose_via_llm,
                         parse_waist_verdict, plan_task)
from harness.errors import HarnessError
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from harness.waist import (compose_plan, confirm_plan,
                           plan_task_id, read_window)

from tests._fake import FakeTransport

DECOMP_JSON = json.dumps({"nodes": [
    {"node_id": "task_1", "instruction": "Rename helper_a to helper_a2",
     "target_files": ["harness/sync.py"], "dependencies": [],
     "local_gate": None, "complexity_tier": 0},
    {"node_id": "task_2", "instruction": "Refactor the concurrency architecture",
     "target_files": ["harness/sync.py"], "dependencies": ["task_1"],
     "local_gate": None, "complexity_tier": 2},
]})


class DecomposeViaLlmTests(unittest.TestCase):
    def test_happy_path_returns_validated_dag(self):
        dag = decompose_via_llm(lambda prompt: f"```json\n{DECOMP_JSON}\n```",
                                "Split the work", candidate_files=["harness/sync.py"])
        self.assertEqual(set(dag.nodes), {"task_1", "task_2"})

    def test_prompt_reaches_chat_fn(self):
        seen = []
        decompose_via_llm(lambda p: (seen.append(p), DECOMP_JSON)[1], "Split the work")
        self.assertIn("Split the work", seen[0])
        self.assertIn("node_id", seen[0])

    def test_empty_node_set_refused(self):
        with self.assertRaises(HarnessError):
            decompose_via_llm(lambda p: json.dumps({"nodes": []}), "g")

    def test_overlong_instruction_refused(self):
        with self.assertRaises(HarnessError):
            decompose_via_llm(lambda p: json.dumps({"nodes": [
                {"node_id": "task_1", "instruction": "x" * 1001}]}), "g")

    def test_malformed_json_refused(self):
        with self.assertRaises(HarnessError):
            decompose_via_llm(lambda p: "```json\n{malformed\n```", "g")

    def test_plan_task_uses_decomposed_dag_and_classifies_tiers(self):
        dag = decompose_via_llm(lambda p: DECOMP_JSON, "Split the work")
        plan = plan_task("Split the work", decomposed_dag=dag)
        by_id = {n["node_id"]: n for n in plan["nodes"]}
        # The LLM's proposal is re-classified by the repo's own scale: the
        # concurrency node may move, but every node carries a real route.
        for node in plan["nodes"]:
            self.assertIn(node["complexity_tier"], (0, 1, 2))
            self.assertTrue(node["route"]["ladder"])
        self.assertGreaterEqual(by_id["task_2"]["complexity_tier"],
                                by_id["task_1"]["complexity_tier"])


class ParseWaistVerdictTests(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(parse_waist_verdict('{"verdict": "approve"}'),
                         {"verdict": "approve"})

    def test_amend_returns_validated_dag(self):
        verdict = parse_waist_verdict(json.dumps({
            "verdict": "amend", "nodes": [
                {"node_id": "task_1", "instruction": "do x",
                 "dependencies": []}]}))
        self.assertEqual(verdict["verdict"], "amend")
        self.assertEqual(set(verdict["dag"].nodes), {"task_1"})

    def test_amend_rejects_cycles(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "amend", "nodes": [
                    {"node_id": "a", "instruction": "x", "dependencies": ["b"]},
                    {"node_id": "b", "instruction": "y", "dependencies": ["a"]}]}))

    def test_amend_requires_nodes(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "amend"}')

    def test_split_returns_validated_dag(self):
        verdict = parse_waist_verdict(json.dumps({
            "verdict": "split", "nodes": [
                {"node_id": "task_1a", "instruction": "part one",
                 "dependencies": []},
                {"node_id": "task_1b", "instruction": "part two",
                 "dependencies": ["task_1a"]}]}))
        self.assertEqual(verdict["verdict"], "split")
        self.assertEqual(set(verdict["dag"].nodes), {"task_1a", "task_1b"})

    def test_split_requires_nodes(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "split"}')

    def test_split_rejects_cycles(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "split", "nodes": [
                    {"node_id": "a", "instruction": "x", "dependencies": ["b"]},
                    {"node_id": "b", "instruction": "y", "dependencies": ["a"]}]}))

    def test_refuse_requires_evidence(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "refuse", "reason": "bad plan"}')
        verdict = parse_waist_verdict(json.dumps({
            "verdict": "refuse", "reason": "tier mismatch",
            "evidence": "PLANNED DAG: task_2 is tier 1 but concurrency"}))
        self.assertEqual(verdict["verdict"], "refuse")
        self.assertEqual(verdict["evidence"], "PLANNED DAG: task_2 is tier 1 but concurrency")

    def test_request_windows(self):
        verdict = parse_waist_verdict(json.dumps({
            "verdict": "request_windows",
            "file_window_requests": [{"path": "harness/sync.py",
                                      "start_line": 10, "end_line": 40}]}))
        self.assertEqual(verdict["file_window_requests"][0]["path"], "harness/sync.py")

    def test_unknown_verdict_refused(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "vibes"}')

    def test_non_object_json_refused(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict("[1, 2, 3]")

    def test_amend_rejects_overlong_instruction(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "amend", "nodes": [
                    {"node_id": "task_1", "instruction": "x" * 1001}]}))

    def test_window_request_rejects_non_object_entries(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "request_windows", "file_window_requests": ["x.py"]}))

    def test_window_request_requires_path(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "request_windows", "file_window_requests": [{}]}))

    def test_window_request_requires_integer_lines(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "request_windows", "file_window_requests": [
                    {"path": "a.py", "start_line": "ten"}]}))

    def test_window_request_rejects_nonpositive_lines(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "request_windows", "file_window_requests": [
                    {"path": "a.py", "start_line": 0}]}))

    def test_window_request_rejects_inverted_range(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(json.dumps({
                "verdict": "request_windows", "file_window_requests": [
                    {"path": "a.py", "start_line": 40, "end_line": 10}]}))

    def test_window_request_requires_list(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict(
                '{"verdict": "request_windows", "file_window_requests": []}')

    def test_malformed_json_refused(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict("no json at all")

    def test_prompt_carries_verdict_contract(self):
        prompt = build_waist_prompt({"goal": "g", "nodes": []}, "BRIEF")
        self.assertIn("VERDICT CONTRACT", prompt)
        self.assertIn("BRIEF", prompt)
        self.assertIn('"verdict": "approve"', prompt)


class ReadWindowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.dir.name)
        with open("mod.py", "w", encoding="utf-8") as f:
            f.write("".join(f"line {i}\n" for i in range(1, 51)))

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.dir.cleanup()

    def test_bounded_window(self):
        text = read_window("mod.py", 10, 20)
        self.assertIn("lines 10-20 of 50", text)
        self.assertIn("line 10\n", text)
        self.assertNotIn("line 9\n", text)

    def test_defaults_and_caps(self):
        text = read_window("mod.py")
        self.assertIn("lines 1-", text)

    def test_missing_file_honest(self):
        self.assertIn("FILE NOT FOUND", read_window("nope.py"))

    def test_out_of_start_honest(self):
        self.assertIn("WINDOW EMPTY", read_window("mod.py", 99, 120))

    def test_traversal_refused(self):
        self.assertIn("WINDOW REFUSED", read_window("../outside.py"))
        self.assertIn("WINDOW REFUSED", read_window("sub/../../x.py"))

    def test_absolute_refused(self):
        self.assertIn("WINDOW REFUSED", read_window(os.path.abspath("mod.py")))


class _WaistFixture(unittest.TestCase):
    """Governor + ledger + canned chat_fn seams for confirm_plan."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = AutonomyLedger(os.path.join(self.dir.name, "ledger.jsonl"))
        self.gov = SpendGovernor(FakeTransport(models=[
            {"id": "m/front", "pricing": {"prompt": "0.000005",
                                          "completion": "0.00001"}}]),
            "sk-test", max_cost=1.0)

    def tearDown(self):
        self.dir.cleanup()

    def plan(self):
        return plan_task("Split the work", decomposed_dag=decompose_via_llm(
            lambda p: DECOMP_JSON, "Split the work"))

    def events(self):
        with open(self.ledger.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class ConfirmPlanTests(_WaistFixture):
    def test_approve_flow_ledgered(self):
        plan = self.plan()
        confirmed = confirm_plan(
            transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
            plan_result=plan, model="m/front", chat_fn=lambda p: ('{"verdict": "approve"}', 0.01))
        self.assertEqual(confirmed["confirmation"]["verdict"], "approved")
        self.assertEqual(confirmed["confirmation"]["rounds"], 1)
        self.assertEqual([e["event"] for e in self.events()], ["plan_verdict"])

    def test_refuse_stops_execution_fail_closed(self):
        refused = confirm_plan(
            transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
            plan_result=self.plan(), model="m/front",
            chat_fn=lambda p: (json.dumps({
                "verdict": "refuse", "reason": "tier mismatch",
                "evidence": "brief: task_2 is concurrency"}), 0.01))
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["confirmation"]["evidence"], "brief: task_2 is concurrency")

    def test_amend_retiers_through_plan_task(self):
        amended = confirm_plan(
            transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
            plan_result=self.plan(), model="m/front",
            chat_fn=lambda p: (json.dumps({
                "verdict": "amend", "nodes": [
                    {"node_id": "solo", "instruction": "Refactor the concurrency architecture",
                     "target_files": ["harness/sync.py"], "dependencies": []}]}), 0.01))
        self.assertEqual(amended["confirmation"]["verdict"], "amended")
        self.assertEqual([n["node_id"] for n in amended["nodes"]], ["solo"])
        self.assertTrue(amended["nodes"][0]["route"]["ladder"])

    def test_split_ledgered_as_its_own_kind(self):
        split = confirm_plan(
            transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
            plan_result=self.plan(), model="m/front",
            chat_fn=lambda p: (json.dumps({
                "verdict": "split", "nodes": [
                    {"node_id": "part_a", "instruction": "Refactor concurrency architecture",
                     "target_files": ["harness/sync.py"], "dependencies": []},
                    {"node_id": "part_b", "instruction": "Add regression tests",
                     "target_files": ["tests/test_sync.py"], "dependencies": ["part_a"]}]}), 0.01))
        self.assertEqual(split["confirmation"]["verdict"], "split")
        self.assertEqual([n["node_id"] for n in split["nodes"]], ["part_a", "part_b"])
        self.assertEqual(self.events()[-1]["verdict"], "split")

    def test_window_round_trip_then_approve(self):
        prompts = []

        def chat_fn(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                return json.dumps({
                    "verdict": "request_windows",
                    "file_window_requests": [{"path": "harness/sync.py",
                                              "start_line": 1, "end_line": 40}]}), 0.01
            return '{"verdict": "approve"}', 0.01

        confirmed = confirm_plan(
            transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
            plan_result=self.plan(), model="m/front", chat_fn=chat_fn,
            reader=lambda path, s, e: f"--- WINDOW: {path} ---")
        self.assertEqual(confirmed["confirmation"]["rounds"], 2)
        self.assertIn("WINDOW: harness/sync.py", prompts[1])

    def test_round_budget_fail_closed(self):
        def chat_fn(prompt):
            return json.dumps({
                "verdict": "request_windows",
                "file_window_requests": [{"path": "x.py"}]}), 0.01

        with self.assertRaises(HarnessError) as ctx:
            confirm_plan(
                transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
                plan_result=self.plan(), model="m/front", chat_fn=chat_fn)
        self.assertIn("will not execute", str(ctx.exception))

    def test_unparseable_verdict_fails_closed_and_ledgers(self):
        with self.assertRaises(HarnessError):
            confirm_plan(
                transport=None, api_key="k", governor=self.gov, ledger=self.ledger,
                plan_result=self.plan(), model="m/front",
                chat_fn=lambda p: ("reasoning trace with no json", 0.01))
        self.assertEqual(self.events()[-1]["verdict"], "unparseable")

    def test_requires_frontier_model(self):
        with self.assertRaises(HarnessError):
            confirm_plan(transport=None, api_key="k", governor=self.gov,
                         ledger=self.ledger, plan_result=self.plan(), model=None)


class ComposePlanTests(_WaistFixture):
    def test_plan_only_llm_failure_fails_loudly(self):
        def broken(prompt):
            raise HarnessError("HTTP 429: rate limited")

        with self.assertRaises(HarnessError):
            compose_plan(transport=None, api_key="k", governor=self.gov,
                         ledger=self.ledger, opts_goal="Split the work",
                         decompose_llm=True, execute=False, chat_fn=broken)

    def test_execute_falls_back_to_heuristic_with_note(self):
        def broken(prompt):
            raise HarnessError("HTTP 429: rate limited")

        plan = compose_plan(transport=None, api_key="k", governor=self.gov,
                            ledger=self.ledger, opts_goal="Split the work",
                            decompose_llm=True, execute=True, chat_fn=broken)
        self.assertEqual(plan["decomposition"], "heuristic")
        self.assertEqual(plan["status"], "planned")

    def test_llm_decomposition_recorded(self):
        plan = compose_plan(transport=None, api_key="k", governor=self.gov,
                            ledger=self.ledger, opts_goal="Split the work",
                            decompose_llm=True,
                            decompose_model="m/cheap",
                            chat_fn=lambda p: (DECOMP_JSON, 0.005))
        self.assertEqual(plan["decomposition"], "llm:m/cheap")

    def test_heuristic_default_untouched(self):
        plan = compose_plan(transport=None, api_key=None, governor=None,
                            ledger=None, opts_goal="Fix typo",
                            candidate_files=["a.py"])
        self.assertEqual(plan["decomposition"], "heuristic")
        self.assertEqual(plan["status"], "planned")

    def test_llm_features_require_governor(self):
        with self.assertRaises(HarnessError):
            compose_plan(transport=None, api_key="k", governor=None,
                         ledger=None, opts_goal="g", decompose_llm=True)

    def test_plan_task_id_deterministic(self):
        plan = {"goal": "same goal"}
        self.assertEqual(plan_task_id(plan), plan_task_id(dict(plan)))
        self.assertTrue(plan_task_id(plan).startswith("plan_"))


if __name__ == "__main__":
    unittest.main()
