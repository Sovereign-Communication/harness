"""JEV-P3-context-pack: distilled decision-relevant state for generative seats."""
import unittest

from harness.jev_packs import build_context_pack
from harness.waist import build_decomposition_prompt, compose_plan
from harness.jev import JevEvaluationResult
from harness.jev_policy import JevPolicy
from harness.config import load_settings


class ContextPackTests(unittest.TestCase):
    def test_pack_filters_to_decision_relevant_fields(self):
        pack = build_context_pack(
            "Add a types module for shipment filters",
            candidate_files=["pkg/types.py", "pkg/other.py"],
            extra={"run_gate": "python -m unittest tests.test_types",
                   "complexity_hint": "single-file"})
        self.assertIn("DECISION CONTEXT", pack)
        self.assertIn("Add a types module", pack)
        self.assertIn("pkg/types.py", pack)
        self.assertIn("run_gate:", pack)
        self.assertIn("python -m unittest", pack)

    def test_pack_preserves_existing_repo_context(self):
        existing = "REPO: already distilled signatures for pkg/types.py"
        pack = build_context_pack("goal", repo_context=existing)
        self.assertEqual(pack, existing)

    def test_pack_truncates_long_context(self):
        long_ctx = "x" * 5000
        pack = build_context_pack("g", repo_context=long_ctx, max_chars=200)
        self.assertEqual(len(pack), 200)

    def test_decomposition_prompt_includes_pack(self):
        pack = build_context_pack("ship it", candidate_files=["a.py"])
        prompt = build_decomposition_prompt(
            "ship it", repo_context=pack, candidate_files=["a.py"])
        self.assertIn("REPOSITORY CONTEXT:", prompt)
        self.assertIn("DECISION CONTEXT", prompt)
        self.assertIn("a.py", prompt)


class _FallbackEvaluator:
    api_key = None
    model = "jev-latest"

    def evaluate(self, state, questions):
        return JevEvaluationResult("pass", 0.0, 1.0, {}, ["fallback"],
                                   is_fallback=True, model=self.model)

    def evaluate_plan_requirements(self, prompt, target_files=None):
        return JevEvaluationResult(
            "pass", 0.0, 1.0, {"requires_iteration": False}, ["fallback"],
            is_fallback=True, model=self.model)


class ComposePlanContextPackWiringTests(unittest.TestCase):
    def test_compose_plan_passes_distilled_repo_context_to_decompose(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = JevPolicy(settings, evaluator=_FallbackEvaluator())
        seen = {}

        def chat_fn(prompt):
            seen["prompt"] = prompt
            return '{"nodes": [{"node_id": "task_1", "instruction": "Create pkg/types.py with a ShipmentsFilter dataclass", "target_files": ["pkg/types.py"], "dependencies": [], "local_gate": null, "complexity_tier": 0}]}'

        result = compose_plan(
            transport=None, api_key=None, governor=object(), ledger=None,
            opts_goal="Create pkg/types.py",
            candidate_files=["pkg/types.py"],
            decompose_llm=True,
            decompose_model="scout",
            chat_fn=lambda p: (chat_fn(p), 0.0),
            jev_policy=policy)
        self.assertIn("REPOSITORY CONTEXT:", seen["prompt"])
        self.assertIn("DECISION CONTEXT", seen["prompt"])
        self.assertIn("Create pkg/types.py", seen["prompt"])
        self.assertIn("pkg/types.py", seen["prompt"])
        self.assertTrue(result["decomposition"].startswith("llm:"))

    def test_compose_plan_without_decompose_does_not_require_pack(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = JevPolicy(settings, evaluator=_FallbackEvaluator())
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="fix a typo in README.md",
            candidate_files=["README.md"],
            decompose_llm=False,
            jev_policy=policy)
        self.assertEqual(result["decomposition"], "heuristic")


if __name__ == "__main__":
    unittest.main()
