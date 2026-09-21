"""HG-plan-consensus: cheap soundness check runs BEFORE the waist."""
import json
import unittest

from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from harness.waist import compose_plan, parse_plan_consensus

from tests._fake import FakeTransport, m
import tempfile
import os


class ParseConsensusTests(unittest.TestCase):
    def test_parse_sound_and_reasons(self):
        verdict = parse_plan_consensus('{"sound": true, "reasons": []}')
        self.assertTrue(verdict["sound"])
        self.assertEqual(verdict["reasons"], [])

    def test_parse_unsound(self):
        verdict = parse_plan_consensus(
            '{"sound": false, "reasons": ["missing write step"]}')
        self.assertFalse(verdict["sound"])
        self.assertEqual(verdict["reasons"], ["missing write step"])

    def test_missing_sound_fails_closed(self):
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            parse_plan_consensus('{"reasons": []}')


class PlanConsensusOrderTests(unittest.TestCase):
    def test_consensus_runs_before_waist_and_cost_is_accounted(self):
        order = []
        ledger_dir = tempfile.mkdtemp()
        ledger = AutonomyLedger(os.path.join(ledger_dir, "l.jsonl"))
        fake = FakeTransport(models=[
            m("m/cheap", "0.000001", "0.000002"),
            m("m/front", "0.000001", "0.000002"),
            m("google/gemma-4-31b-it:free", "0", "0"),
        ])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)

        def chat_fn(prompt):
            order.append("decompose")
            return json.dumps({"nodes": [
                {"node_id": "n1", "instruction": "Do the work",
                 "target_files": ["harness/sync.py"], "dependencies": []}]}), 0.0

        def governed(transport, api_key, governor, model, prompt, tokens, label=None):
            order.append(label or model)
            if label == "plan_consensus" or "soundness checker" in prompt:
                order.append("consensus")
                return '{"sound": true, "reasons": []}', 0.001
            order.append("waist")
            return '{"verdict": "approve"}', 0.001

        from unittest.mock import patch
        with patch("harness.waist.governed_text", side_effect=governed):
            plan = compose_plan(
                transport=fake, api_key="k", governor=gov, ledger=ledger,
                opts_goal="Update sync", candidate_files=["harness/sync.py"],
                decompose_llm=True, confirm=True, execute=False,
                plan_consensus=True, chat_fn=chat_fn, use_free=False,
                frontier_model="m/front")

        self.assertIn("consensus", order)
        self.assertIn("waist", order)
        self.assertLess(order.index("consensus"), order.index("waist"))
        self.assertIn("consensus", plan)
        self.assertTrue(plan["consensus"]["sound"])
        self.assertIn("composed_worst_case", plan)
        events = []
        with open(ledger.path, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        kinds = [e.get("event") for e in events]
        self.assertIn("plan_consensus", kinds)
        self.assertIn("plan_verdict", kinds)
        self.assertLess(kinds.index("plan_consensus"), kinds.index("plan_verdict"))

    def test_unsound_consensus_forces_waist_amend_path(self):
        waist_prompts = []

        def chat_fn(prompt):
            return json.dumps({"nodes": [
                {"node_id": "n1", "instruction": "Do the work",
                 "target_files": ["harness/sync.py"], "dependencies": []}]}), 0.0

        def governed(transport, api_key, governor, model, prompt, tokens, label=None):
            if "soundness checker" in prompt:
                return '{"sound": false, "reasons": ["DAG omits the write step"]}', 0.0
            waist_prompts.append(prompt)
            # Waist still says approve -- the prompt must have forced amend.
            return '{"verdict": "approve"}', 0.0

        from unittest.mock import patch
        fake = FakeTransport(models=[m("m/front", "0.000001", "0.000002")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
        with patch("harness.waist.governed_text", side_effect=governed):
            plan = compose_plan(
                transport=fake, api_key="k", governor=gov, ledger=None,
                opts_goal="Update sync", candidate_files=["harness/sync.py"],
                decompose_llm=True, confirm=True, execute=False,
                plan_consensus=True, chat_fn=chat_fn, use_free=False,
                frontier_model="m/front")
        self.assertFalse(plan["consensus"]["sound"])
        self.assertTrue(waist_prompts)
        self.assertIn("UNSound", waist_prompts[0].replace("unsound", "UNSound"))
        self.assertIn("MUST return 'amend'", waist_prompts[0])
        self.assertIn("omits the write step", waist_prompts[0])


if __name__ == "__main__":
    unittest.main()
