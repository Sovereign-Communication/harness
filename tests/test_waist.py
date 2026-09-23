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
from unittest.mock import patch

from harness.dag import (DAGNode, TaskDAG, build_waist_prompt,
                         decompose_via_llm, parse_waist_verdict, plan_task)
from harness.errors import HarnessError
from harness.ledger import AutonomyLedger
from harness.sliding_scale import resolve_frontier_model
from harness.spend import SpendGovernor
from harness.capability import source_budget_for
from harness.validation import MAX_INSTRUCTION_CHARS
from harness.waist import (chunk_oversized_nodes, compose_plan, confirm_plan,
                           node_apply_kwargs, plan_task_id, read_window,
                           resolve_waist_ladder)

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


class ChunkOversizedNodesTests(unittest.TestCase):
    """The chunking policy (ONE owner: waist.chunk_oversized_nodes).

    A node that cannot fit one model pass is split into ordered chunks that
    each fit; everything else is untouched.
    """

    def test_oversized_instruction_becomes_ordered_in_budget_chunks(self):
        text = "Refactor the scheduling core so the queue drains in order. " * 60
        self.assertGreater(len(text), MAX_INSTRUCTION_CHARS)
        dag = TaskDAG(nodes={"task_1": DAGNode(
            node_id="task_1", instruction=text, target_files=("missing.py",))})
        fitted = chunk_oversized_nodes(dag)
        self.assertGreater(len(fitted.nodes), 1)
        ids = list(fitted.nodes)
        # ordered and acyclic: pass i waits for pass i-1
        self.assertEqual([fitted.nodes[i].dependencies for i in ids[1:]],
                         [(previous,) for previous in ids[:-1]])
        self.assertEqual(fitted.nodes[ids[0]].dependencies, ())
        for node in fitted.nodes.values():
            self.assertLessEqual(len(node.instruction), MAX_INSTRUCTION_CHARS)
            self.assertIn("pass ", node.instruction)
        # nothing lost: the instruction text survives the split verbatim
        bodies = " ".join(n.instruction.split("\n\n[pass")[0]
                          for n in fitted.nodes.values())
        self.assertEqual(" ".join(bodies.split()), " ".join(text.split()))

    def test_normal_instruction_is_unchanged(self):
        dag = TaskDAG(nodes={"task_1": DAGNode(
            node_id="task_1", instruction="Add a docstring to tokens.py",
            target_files=("does/not/exist.py",))})
        # Same object: ordinary plans never detour through the chunk policy.
        self.assertIs(chunk_oversized_nodes(dag), dag)

    @staticmethod
    def _large_file_repo(tmp):
        root = os.path.join(tmp, "repo")
        os.makedirs(root)
        with open(os.path.join(root, "big.py"), "w", encoding="utf-8") as f:
            f.write("".join(f"line_{i} = {i}\n" for i in range(1300)))
        with open(os.path.join(root, "small.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        return root

    def test_large_target_is_one_node_with_the_diff_hint(self):
        """Past the rewrite cap is NOT a reason to split: the engine's own
        large-file path is bounded hunks, so a small edit stays one call."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._large_file_repo(tmp)
            dag = TaskDAG(nodes={
                "task_1": DAGNode(node_id="task_1",
                                  instruction="Rename the helper",
                                  target_files=("big.py",)),
                "task_2": DAGNode(node_id="task_2", instruction="Add tests",
                                  target_files=("small.py",),
                                  dependencies=("task_1",)),
            })
            fitted = chunk_oversized_nodes(dag, root=root)
        self.assertEqual(list(fitted.nodes), ["task_1", "task_2"])
        self.assertEqual(fitted.nodes["task_1"].backend, "diff")
        self.assertEqual(fitted.nodes["task_1"].instruction, "Rename the helper")
        # no split, so dependents keep referring to the original node
        self.assertEqual(fitted.nodes["task_2"].dependencies, ("task_1",))
        self.assertEqual(fitted.nodes["task_2"].backend, None)

    def test_target_splits_by_range_only_when_one_pass_cannot_read_it(self):
        """The file axis fires on a MEASURED read budget (the rung's declared
        context), not on the file merely being big."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._large_file_repo(tmp)
            dag = TaskDAG(nodes={
                "task_1": DAGNode(node_id="task_1",
                                  instruction="Rename the helper",
                                  target_files=("big.py",)),
                "task_2": DAGNode(node_id="task_2", instruction="Add tests",
                                  target_files=("small.py",),
                                  dependencies=("task_1",)),
            })
            # A rung whose declared context cannot hold the file in one pass.
            fitted = chunk_oversized_nodes(
                dag, root=root, source_tokens={"task_1": 1000, "task_2": 0})
            # A real rung (256k-token context): the same file fits one pass.
            roomy = chunk_oversized_nodes(
                dag, root=root,
                source_tokens={"task_1": source_budget_for(262144)})
        chunks = [nid for nid in fitted.nodes if nid.startswith("task_1.")]
        self.assertGreater(len(chunks), 1)
        for node_id in chunks:
            node = fitted.nodes[node_id]
            self.assertEqual(node.backend, "diff")
            self.assertIn("apply ONLY big.py lines", node.instruction)
        # the dependent now waits for the LAST chunk, not the original id
        self.assertEqual(fitted.nodes["task_2"].dependencies, (chunks[-1],))
        self.assertNotIn("task_1", fitted.nodes)
        self.assertEqual(list(roomy.nodes), ["task_1", "task_2"])

    def test_plan_lane_hints_large_targets_and_chunks_long_goals(self):
        """compose_plan is the single owner's call site: a big target comes
        back as one diff-hinted node, while a goal too long to send at all
        comes back chunked (and marked)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._large_file_repo(tmp)
            small_edit = compose_plan(
                transport=None, api_key=None, governor=None, ledger=None,
                opts_goal="Add a docstring", candidate_files=["big.py"],
                root=root)
            long_goal = compose_plan(
                transport=None, api_key=None, governor=None, ledger=None,
                opts_goal="Refactor the scheduler. " * 90,
                candidate_files=["small.py"], root=root)
        self.assertEqual(small_edit["total_nodes"], 1)
        self.assertNotIn("chunking", small_edit)
        self.assertEqual(small_edit["nodes"][0]["backend"], "diff")
        # the route detail carries the hint into execution kwargs
        self.assertEqual(
            node_apply_kwargs(small_edit["nodes"][0]).get("backend"), "diff")
        self.assertTrue(long_goal["chunking"]["required"])
        self.assertGreater(long_goal["total_nodes"], 1)
        for detail in long_goal["nodes"]:
            self.assertLessEqual(len(detail["instruction"]),
                                 MAX_INSTRUCTION_CHARS)


class ComposePlanTests(_WaistFixture):
    def test_waist_gate_resolves_its_own_frontier_rung(self):
        """Two truths the default-ON hourglass depends on: (1) an unset
        --frontier-model resolves through the same owner the Router binds
        its frontier from instead of killing every default-settings plan
        run; (2) the decomposition seam is not the gate's seam -- the cheap
        decomposer never answers (or is credited with) the frontier
        verdict."""
        seen = {}

        def fake_governed(transport, api_key, governor, model, prompt, tokens,
                          label=None):
            seen["model"] = model
            seen["label"] = label
            return '{"verdict": "approve"}', 0.0

        with patch("harness.waist.governed_text", side_effect=fake_governed):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Split the work", candidate_files=["harness/sync.py"],
                decompose_llm=True, confirm=True, execute=True,
                chat_fn=lambda p: (DECOMP_JSON, 0.005))
        expected = resolve_frontier_model(None, use_free=True)
        self.assertEqual(seen["model"], expected)
        self.assertEqual(seen["label"], "waist")
        self.assertEqual(plan["confirmation"]["model"], expected)
        self.assertEqual(plan["confirmation"]["verdict"], "approved")

    def test_plan_only_llm_failure_fails_closed_by_default(self):
        """DF-HG-3b (Board ruling on PR #68): plan-only preview must fail
        closed by default when LLM decomposition fails -- never silently
        hand back the heuristic plan (restores the original fail-loud
        contract PR #68 regressed; see git log -p -S 'fails_lo' -- tests)."""
        def broken(prompt):
            raise HarnessError("HTTP 429: rate limited")

        with self.assertRaises(HarnessError):
            compose_plan(transport=None, api_key="k", governor=self.gov,
                         ledger=self.ledger, opts_goal="Split the work",
                         decompose_llm=True, execute=False, chat_fn=broken)

    def test_plan_only_heuristic_preview_optin_degrades_loudly(self):
        """DF-HG-3b: allow_heuristic_preview=True (CLI:
        --allow-heuristic-preview) opts a plan-only preview into the same
        loud heuristic fallback --execute always used, but the plan is
        never reported as waist-confirmed for a decomposition the LLM
        never actually produced."""
        def broken(prompt):
            raise HarnessError("HTTP 429: rate limited")

        plan = compose_plan(transport=None, api_key="k", governor=self.gov,
                            ledger=self.ledger, opts_goal="Split the work",
                            decompose_llm=True, execute=False, confirm=True,
                            chat_fn=broken, allow_heuristic_preview=True)
        self.assertEqual(plan["decomposition"], "heuristic")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["confirmation"]["verdict"], "skipped")

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

    def test_resolve_waist_ladder_ordering(self):
        ladder_free = resolve_waist_ladder(use_free=True, allow_escalation=False)
        self.assertIn(":free", ladder_free[0])
        self.assertTrue(all(m.endswith(":free") or "free" in m for m in ladder_free))

        ladder_esc = resolve_waist_ladder(use_free=True, allow_escalation=True)
        self.assertIn(":free", ladder_esc[0])
        self.assertTrue(any(not m.endswith(":free") for m in ladder_esc))
        self.assertIn("qwen/qwen3.8-max-0902", ladder_esc)

        ladder_paid = resolve_waist_ladder(use_free=False, custom_frontier="claude-3.5-sonnet")
        self.assertEqual(ladder_paid[0], "anthropic/claude-3.5-sonnet")

    def test_compose_plan_rotates_on_429_to_next_free_model(self):
        attempts = []
        ladder = resolve_waist_ladder(use_free=True, allow_escalation=False)
        first_model = ladder[0]
        second_model = ladder[1]

        def fake_gov(transport, api_key, governor, model, prompt, tokens, label=None):
            attempts.append(model)
            if model == first_model:
                raise HarnessError("HTTP 429: Provider returned error")
            return '{"verdict": "approve"}', 0.0

        with patch("harness.waist.governed_text", side_effect=fake_gov):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Split the work", candidate_files=["harness/sync.py"],
                confirm=True, execute=True)
        self.assertEqual(attempts[0], first_model)
        self.assertEqual(attempts[1], second_model)
        self.assertEqual(plan["confirmation"]["model"], second_model)
        self.assertEqual(plan["confirmation"]["verdict"], "approved")

    def test_compose_plan_escalates_on_429_to_paid_model(self):
        attempts = []
        ladder = resolve_waist_ladder(use_free=True, allow_escalation=True)
        paid_model = "qwen/qwen3.8-max-0902"
        self.assertIn(paid_model, ladder)

        def fake_gov(transport, api_key, governor, model, prompt, tokens, label=None):
            attempts.append(model)
            if model.endswith(":free"):
                raise HarnessError("HTTP 429: Provider returned error")
            return '{"verdict": "approve"}', 0.001

        with patch("harness.waist.governed_text", side_effect=fake_gov):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Split the work", candidate_files=["harness/sync.py"],
                confirm=True, execute=True, allow_escalation=True)
        self.assertIn(paid_model, attempts)
        self.assertEqual(plan["confirmation"]["model"], paid_model)
        self.assertEqual(plan["confirmation"]["verdict"], "approved")

    def test_compose_plan_exhausted_ladder_degrades_to_local_gate(self):
        """HG: confirm-armed waist unreachable across the full ladder DEGRADES
        to local-gate execution (operator no-interruptions ruling): an
        unreachable seat is an availability failure, not a policy refusal.
        The plan proceeds with explicit unavailable-verdict provenance."""
        def broken(transport, api_key, governor, model, prompt, tokens, label=None):
            raise HarnessError("HTTP 429: Provider returned error")

        with patch("harness.waist.governed_text", side_effect=broken):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Split the work", candidate_files=["harness/sync.py"],
                confirm=True, execute=True)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["confirmation"]["verdict"], "unavailable")
        self.assertIn("unreachable", plan["confirmation"]["reason"])
        self.assertIn("429", plan["confirmation"]["evidence"])
        self.assertEqual(plan["confirmation"]["cost"], 0.0)
        self.assertGreaterEqual(plan["total_nodes"], 1)
        self.assertGreaterEqual(plan["total_cost_ceiling"], 0.0)

    def test_compose_plan_exhausted_ladder_plan_only_fails_closed(self):
        def broken(transport, api_key, governor, model, prompt, tokens, label=None):
            raise HarnessError("HTTP 429: Provider returned error")

        with patch("harness.waist.governed_text", side_effect=broken):
            with self.assertRaises(HarnessError) as ctx:
                compose_plan(
                    transport=None, api_key="k", governor=self.gov, ledger=None,
                    opts_goal="Split the work", candidate_files=["harness/sync.py"],
                    confirm=True, execute=False)
        self.assertIn("waist confirmation could not run on", str(ctx.exception))
        self.assertIn("plan NOT executed", str(ctx.exception))

    def test_compose_plan_refusal_fails_closed(self):
        def refuse_gov(transport, api_key, governor, model, prompt, tokens, label=None):
            return json.dumps({
                "verdict": "refuse",
                "reason": "goal is out of scope",
                "evidence": "sync.py does not need refactoring"
            }), 0.0

        with patch("harness.waist.governed_text", side_effect=refuse_gov):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Split the work", candidate_files=["harness/sync.py"],
                confirm=True, execute=True)
        self.assertEqual(plan["status"], "refused")
        self.assertEqual(plan["confirmation"]["verdict"], "refused")

    def test_compose_plan_refusal_critique_replan_succeeds(self):
        call_count = {"decompose": 0, "waist": 0}

        def mock_chat(prompt_text):
            call_count["decompose"] += 1
            if call_count["decompose"] == 1:
                # First decomposition: missing the loop
                return json.dumps({
                    "nodes": [{"node_id": "n1", "instruction": "Do step 1",
                               "target_files": ["harness/sync.py"], "dependencies": []}]
                }), 0.0
            else:
                # Re-planned decomposition incorporating critique
                return json.dumps({
                    "nodes": [
                        {"node_id": "n1", "instruction": "Iterate step 1",
                         "target_files": ["harness/sync.py"], "dependencies": []},
                        {"node_id": "n2", "instruction": "Check convergence condition",
                         "target_files": ["harness/sync.py"], "dependencies": ["n1"]}
                    ]
                }), 0.0

        def mock_gov(transport, api_key, governor, model, prompt, tokens, label=None):
            call_count["waist"] += 1
            if call_count["waist"] == 1:
                # First waist check: refuse due to missing iteration loop
                return json.dumps({
                    "verdict": "refuse",
                    "reason": "DAG lacks iteration loop",
                    "evidence": "only 1 node without convergence"
                }), 0.0
            else:
                # Second waist check on re-planned DAG: approve
                return json.dumps({"verdict": "approve"}), 0.0

        with patch("harness.waist.governed_text", side_effect=mock_gov):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Run convergence loop", candidate_files=["harness/sync.py"],
                decompose_llm=True, confirm=True, execute=True,
                chat_fn=mock_chat)

        self.assertEqual(plan["confirmation"]["verdict"], "approved")
        self.assertEqual(len(plan["nodes"]), 2)
        self.assertIn(":critique_replan", plan["decomposition"])
        self.assertGreaterEqual(call_count["decompose"], 2)

    def test_compose_plan_refusal_critique_replan_error_falls_back(self):
        call_count = {"decompose": 0}

        def mock_chat(prompt_text):
            call_count["decompose"] += 1
            if call_count["decompose"] == 1:
                return json.dumps({
                    "nodes": [{"node_id": "n1", "instruction": "Do step 1",
                               "target_files": ["harness/sync.py"], "dependencies": []}]
                }), 0.0
            else:
                raise HarnessError("decomposition service unavailable")

        def mock_gov(transport, api_key, governor, model, prompt, tokens, label=None):
            return json.dumps({
                "verdict": "refuse",
                "reason": "missing loop",
                "evidence": "no loop in DAG"
            }), 0.0

        with patch("harness.waist.governed_text", side_effect=mock_gov):
            plan = compose_plan(
                transport=None, api_key="k", governor=self.gov, ledger=None,
                opts_goal="Run loop", candidate_files=["harness/sync.py"],
                decompose_llm=True, confirm=True, execute=True,
                chat_fn=mock_chat)

        self.assertEqual(plan["status"], "refused")
        self.assertEqual(plan["confirmation"]["verdict"], "refused")


if __name__ == "__main__":
    unittest.main()
