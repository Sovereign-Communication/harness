"""HG-composed-ceiling: composed pyramid budget refuses before any spend."""
import json
import unittest

from harness.spend import SpendGovernor
from harness.waist import composed_worst_case, compose_plan

from tests._fake import FakeTransport, m

DECOMP_JSON = json.dumps({"nodes": [
    {"node_id": "task_1", "instruction": "Rename helper_a to helper_a2",
     "target_files": ["harness/sync.py"], "dependencies": [],
     "local_gate": None, "complexity_tier": 0},
    {"node_id": "task_2", "instruction": "Refactor the concurrency architecture",
     "target_files": ["harness/sync.py"], "dependencies": ["task_1"],
     "local_gate": None, "complexity_tier": 2},
]})


class ComposedCeilingTests(unittest.TestCase):
    def test_composed_pyramid_ceiling_refuses_before_any_spend(self):
        """When the composed worst-case (nodes + decompose + waist) exceeds
        remaining budget, compose_plan refuses on the execute path and the
        governor records spend == 0 on that refuse path."""
        fake = FakeTransport(models=[
            m("m/cheap", "0.0000001", "0.0000002"),
            m("m/front", "0.000001", "0.000002"),
            m("qwen/qwen3.8-max-0902", "0.000002", "0.000006"),
        ])
        # Tiny ceiling: planned node route ceilings alone cannot fit.
        gov = SpendGovernor(fake, "sk-test", max_cost=0.0000001)

        def chat_fn(prompt):
            # Injected decompose seam: returns text/cost without recording
            # spend on the governor (the refuse path itself must not spend).
            return DECOMP_JSON, 0.0

        # confirm stays False so the waist ladder cannot spend or steal the
        # refuse reason -- this pin is specifically the composed ceiling.
        plan = compose_plan(
            transport=fake, api_key="k", governor=gov, ledger=None,
            opts_goal="Split the work", candidate_files=["harness/sync.py"],
            decompose_llm=True, decompose_model="m/cheap",
            confirm=False, execute=True, chat_fn=chat_fn, use_free=False,
            frontier_model="m/front")

        self.assertEqual(plan["status"], "refused")
        self.assertEqual(gov.spent, 0.0)
        composed = plan["composed_worst_case"]
        self.assertTrue(composed["exceeds_remaining"])
        self.assertGreater(composed["node_ceiling"], 0.0)
        reason = plan["confirmation"]["reason"]
        self.assertIn("composed worst-case", reason)
        self.assertEqual(plan["confirmation"]["model"], "composed-ceiling")

    def test_composed_worst_case_envelope_shape(self):
        plan = {
            "goal": "g",
            "total_cost_ceiling": 0.08,
            "nodes": [
                {"node_id": "a", "route": {"cost_ceiling": 0.04}},
                {"node_id": "b", "route": {"cost_ceiling": 0.04}},
            ],
        }
        composed = composed_worst_case(plan)
        self.assertEqual(composed["node_ceiling"], 0.08)
        self.assertEqual(composed["composed_worst_case"], 0.08)
        self.assertIsNone(composed["exceeds_remaining"])

    def test_plan_envelope_carries_composed_worst_case(self):
        plan = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="Fix typo", candidate_files=["a.py"])
        self.assertIn("composed_worst_case", plan)
        self.assertIn("node_ceiling", plan["composed_worst_case"])
        self.assertGreaterEqual(plan["composed_worst_case"]["composed_worst_case"], 0.0)

    def test_refuses_when_waist_cost_alone_exceeds_remaining(self):
        """Even with free nodes, a priced waist rung can blow a tiny ceiling."""
        fake = FakeTransport(models=[
            m("m/front", "0.001", "0.002"),
        ])
        gov = SpendGovernor(fake, "sk-test", max_cost=0.00001)

        def approve(transport, api_key, governor, model, prompt, tokens, label=None):
            return '{"verdict": "approve"}', 0.0

        from unittest.mock import patch
        with patch("harness.waist.governed_text", side_effect=approve):
            plan = compose_plan(
                transport=fake, api_key="k", governor=gov, ledger=None,
                opts_goal="Fix typo", candidate_files=["a.py"],
                confirm=True, execute=True, use_free=False,
                frontier_model="m/front")
        self.assertEqual(plan["status"], "refused")
        self.assertEqual(gov.spent, 0.0)
        self.assertTrue(plan["composed_worst_case"]["exceeds_remaining"])


if __name__ == "__main__":
    unittest.main()
