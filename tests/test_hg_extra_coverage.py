"""Additional HG coverage pins for compose/executor/CLI seams."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.chat import chat_ladder
from harness.cli_parser import build_parser
from harness.config import DEFAULT_FRONTIER_PAID, resolve_hourglass
from harness.dag import DAGNode, TaskDAG
from harness.executor import PlanExecutor, partition_by_target_overlap
from harness.sliding_scale import resolve_frontier_model
from harness.spend import SpendGovernor
from harness.waist import (
    _decompose_repo_context, composed_worst_case, compose_plan,
    parse_plan_consensus, plan_consensus_check)

from tests._fake import FakeTransport, m


class ExtraCoverageTests(unittest.TestCase):
    def test_parser_hourglass_and_resume_flags(self):
        parser = build_parser()
        opts = parser.parse_args([
            "plan", "--goal", "g", "--decompose-llm", "--plan-consensus",
            "--final-gate", "python -m unittest tests.test_tokens",
            "--resume", "state.json", "--no-final-gate"])
        self.assertTrue(opts.decompose_llm)
        self.assertTrue(opts.plan_consensus)
        # last final-gate flag wins
        self.assertIs(opts.final_gate, False)
        self.assertEqual(opts.resume, "state.json")

    def test_resolve_frontier_paid_constant(self):
        self.assertEqual(resolve_frontier_model(None, use_free=False),
                         DEFAULT_FRONTIER_PAID)

    def test_chat_ladder_settings_judge_head(self):
        ladder = chat_ladder(SimpleNamespace(
            tier1_model="t/one", judge="j/two", panel_pool=["p/x"],
            allow_escalation=True, escalation_pool=["e/y"]))
        self.assertEqual(ladder[0], "t/one")
        self.assertIn("e/y", ladder)

    def test_composed_worst_case_with_priced_waist_rungs(self):
        fake = FakeTransport(models=[
            m("m/front", "0.001", "0.002"),
            m("qwen/qwen3.8-max-0902", "0.001", "0.002"),
        ])
        gov = SpendGovernor(fake, "sk", max_cost=1.0)
        plan = {"total_cost_ceiling": 0.01, "nodes": [
            {"node_id": "a", "route": {"cost_ceiling": 0.01}}]}
        composed = composed_worst_case(
            plan, governor=gov, decompose_llm=True, confirm=True,
            plan_consensus=True, use_free=False, frontier_model="m/front",
            allow_escalation=True)
        self.assertGreater(composed["waist"], 0.0)
        self.assertGreaterEqual(composed["composed_worst_case"],
                                composed["node_ceiling"])

    def test_decompose_repo_context_none_without_files(self):
        self.assertIsNone(_decompose_repo_context("g", None))
        self.assertIsNone(_decompose_repo_context("g", ["missing/nope.py"]))

    def test_parse_plan_consensus_reasons_coerced(self):
        verdict = parse_plan_consensus('{"sound": false, "reasons": "one"}')
        self.assertEqual(verdict["reasons"], ["one"])

    def test_plan_consensus_requires_model(self):
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            plan_consensus_check(
                transport=None, api_key=None, governor=None, ledger=None,
                plan_result={}, model=None)

    def test_partition_empty_targets_serialize(self):
        nodes = [
            DAGNode(node_id="a", instruction="x", target_files=()),
            DAGNode(node_id="b", instruction="y", target_files=("z.py",)),
        ]
        iso, shared = partition_by_target_overlap(nodes)
        self.assertEqual([n.node_id for n in iso], ["b"])
        self.assertEqual([n.node_id for n in shared], ["a"])

    def test_plan_executor_final_gate_runner_typeerror_fallback(self):
        engine = MagicMock()
        dag = TaskDAG(nodes={
            "t": DAGNode(node_id="t", instruction="x",
                         target_files=("a.py",), local_gate="echo gate")})

        def picky(command):
            # Signature without timeout/cwd -> TypeError path.
            return 0, "ok"

        results = PlanExecutor(
            engine, {}, parallel=False, isolate=False,
            apply=lambda t, n, k, r: {"status": "ok", "cost": 0.0},
            final_gate_runner=picky).execute(dag)
        self.assertEqual(results["final_gate"]["status"], "ok")

    def test_compose_plan_plan_consensus_unsound_then_waist(self):
        fake = FakeTransport(models=[
            m("m/front", "0.000001", "0.000002"),
            m("m/cheap", "0.000001", "0.000002"),
        ])
        gov = SpendGovernor(fake, "k", max_cost=1.0)
        calls = []

        def chat_fn(prompt):
            calls.append("decompose")
            return json.dumps({"nodes": [
                {"node_id": "n1", "instruction": "Do work",
                 "target_files": ["harness/sync.py"], "dependencies": []}]}), 0.0

        def governed(transport, api_key, governor, model, prompt, tokens, label=None):
            if "soundness checker" in prompt:
                calls.append("consensus")
                return '{"sound": false, "reasons": ["missing write"]}', 0.0
            calls.append("waist")
            return '{"verdict": "amend", "nodes": [{"node_id": "n1", "instruction": "Do the real write work now", "target_files": ["harness/sync.py"], "dependencies": []}]}', 0.0

        with patch("harness.waist.governed_text", side_effect=governed):
            plan = compose_plan(
                transport=fake, api_key="k", governor=gov, ledger=None,
                opts_goal="Update sync", candidate_files=["harness/sync.py"],
                decompose_llm=True, confirm=True, execute=False,
                plan_consensus=True, chat_fn=chat_fn, use_free=False,
                frontier_model="m/front")
        self.assertFalse(plan["consensus"]["sound"])
        self.assertLess(calls.index("consensus"), calls.index("waist"))
        self.assertEqual(plan["confirmation"]["verdict"], "amended")

    def test_cli_plan_compose_resolves_hourglass_decompose(self):
        from harness.cli import _plan_compose
        settings = SimpleNamespace(
            use_free=False, frontier_model=None,
            hourglass_confirm=False, hourglass_parallel=True,
            hourglass_decompose=None, allow_escalation=False)
        opts = SimpleNamespace(
            goal="g", decompose_llm=None, plan_consensus=None,
            allow_escalation=False, max_tokens=None)
        captured = {}

        def fake_compose(**kwargs):
            captured.update(kwargs)
            return {"status": "planned", "goal": "g",
                    "dag": {"nodes": []}, "nodes": []}

        with patch("harness.cli._compose_plan", side_effect=fake_compose), \
             patch("harness.cli._resolve_hourglass",
                   return_value={"confirm": False, "parallel": True,
                                 "isolate": True,
                                 "require_diff_authorization": True,
                                 "decompose": True}):
            _plan_compose(settings, opts, None, None, None,
                          candidate_files=None, frontier_model=None,
                          execute=False)
        self.assertTrue(captured["decompose_llm"])
        self.assertIs(captured["confirm"], False)

    def test_hourglass_settings_decompose_explicit_off(self):
        settings = SimpleNamespace(
            hourglass_confirm=True, hourglass_parallel=True,
            hourglass_isolate=True, hourglass_require_attestation=True,
            hourglass_decompose=False)
        self.assertFalse(resolve_hourglass(settings)["decompose"])


if __name__ == "__main__":
    unittest.main()
