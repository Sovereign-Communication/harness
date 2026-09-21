"""JEV-P3-triage-files: noul relevance over orchestrator candidates."""
import os
import tempfile
import unittest
from pathlib import Path

from harness import orchestrator as orch
from harness.config import load_settings
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m

_FILES = [
    "harness/agent.py",
    "harness/dag.py",
    "harness/web.py",
    "tests/test_agent.py",
    "README.md",
]


class _FileTriageTransport:
    def __init__(self, relevant_indexes):
        self.relevant_indexes = set(relevant_indexes)
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        files = payload["state"]["files"]
        answers = {}
        for i, _path in enumerate(files):
            noul = 0.92 if i in self.relevant_indexes else 0.08
            answers[f"file_{i}_relevant"] = {"type": "noul", "noul": noul}
        return 200, {
            "model": "jev-test",
            "answers": answers,
            "usage": {"input_tokens": 30, "output_tokens": 2},
        }


def _unkeyed():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


class FileTriagePolicyTests(unittest.TestCase):
    def test_unkeyed_keyword_fallback_is_honest(self):
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_file_triage(
            "fix the dag planner", _FILES, known_files=_FILES,
            site="triage-files")
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertTrue(result.answers.get("heuristic"))
        picked = result.answers["files"]
        self.assertTrue(picked)
        self.assertTrue(set(picked) <= set(_FILES))
        self.assertIn("harness/dag.py", picked)

    def test_keyed_noul_relevance_validates_against_real_listing(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ledger = AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.10)
        transport = _FileTriageTransport(relevant_indexes={1, 2})
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport,
                            governor=governor, ledger=ledger)
        # Candidate includes a hallucinated path not in the real listing.
        candidates = _FILES + ["harness/ghost.py"]
        result, structural = policy.evaluate_file_triage(
            "fix the dag planner", candidates, known_files=_FILES,
            site="triage-files")
        self.assertFalse(result.is_fallback)
        picked = result.answers["files"]
        self.assertEqual(picked, ["harness/dag.py", "harness/web.py"])
        self.assertNotIn("harness/ghost.py", picked)
        self.assertEqual(structural["files"], picked)
        # Pack only saw listing-valid candidates.
        posted = transport.calls[0]["state"]["files"]
        self.assertNotIn("harness/ghost.py", posted)

    def test_empty_candidates_returns_honest_empty(self):
        policy = policy_for(_unkeyed())
        result, structural = policy.evaluate_file_triage(
            "goal", [], known_files=[], site="triage-files")
        self.assertEqual(result.answers["files"], [])
        self.assertEqual(structural["files"], [])


class OrchestratorTriagePolicyTests(unittest.TestCase):
    def test_orch_triage_files_uses_policy_when_attached(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        settings = load_settings({"jev_api_key": "jev-key"})
        transport = _FileTriageTransport(relevant_indexes={0})
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.50)
        policy = policy_for(settings, transport=transport, governor=governor)
        picked = orch.triage_files(
            "fix the agent lane", _FILES, chat_fn=None,
            jev_policy=policy)
        self.assertEqual(picked, ["harness/agent.py"])

    def test_orch_triage_files_without_policy_keeps_model_validation(self):
        def chat_fn(prompt):
            return '{"files": ["harness/dag.py", "harness/ghost.py"]}'
        picked = orch.triage_files("fix the dag", _FILES, chat_fn)
        self.assertEqual(picked, ["harness/dag.py"])

    def test_orch_keyword_fallback_still_available(self):
        picked = orch.keyword_fallback("update the web tools", _FILES)
        self.assertIn("harness/web.py", picked)


class RealListingValidationTests(unittest.TestCase):
    def test_policy_drops_paths_absent_from_disk_listing(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "pkg").mkdir()
        (root / "pkg" / "types.py").write_text("x = 1\n", encoding="utf-8")
        (root / "pkg" / "unused.py").write_text("y = 2\n", encoding="utf-8")
        listing = ["pkg/types.py", "pkg/unused.py"]
        settings = load_settings({"jev_api_key": "jev-key"})
        transport = _FileTriageTransport(relevant_indexes={0})
        governor = SpendGovernor(
            FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
            "sk-test", max_cost=0.50)
        policy = policy_for(settings, transport=transport, governor=governor)
        result, _ = policy.evaluate_file_triage(
            "edit pkg/types.py",
            ["pkg/types.py", "pkg/missing.py"],
            known_files=listing,
            site="triage-files")
        self.assertEqual(result.answers["files"], ["pkg/types.py"])


if __name__ == "__main__":
    unittest.main()
