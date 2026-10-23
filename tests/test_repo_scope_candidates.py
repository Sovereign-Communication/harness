"""Contracts for the bounded, balanced repository-review sample."""
import unittest

from harness.repo_scope import workspace_review_candidates


class WorkspaceReviewCandidatesTests(unittest.TestCase):
    def test_empty_budget_returns_no_candidates(self):
        self.assertEqual(workspace_review_candidates(
            "audit this repo", ["README.md"], max_files=0), [])

    def test_preferred_paths_are_normalized_deduplicated_and_bounded(self):
        files = ["src/router.py", "README.md"]
        selected = workspace_review_candidates(
            "router review", files, max_files=1,
            preferred=[r"src\router.py", "src/router.py", "missing.py"])
        self.assertEqual(selected, ["src/router.py"])

    def test_review_sample_balances_docs_source_and_tests(self):
        files = [
            "AGENTS.md", "README.md", "docs/architecture.md",
            "docs/roadmap.md", "src/router.py", "src/main.py",
            "tests/test_router.py", "tests/test_other.py", "notes.bin",
        ]
        selected = workspace_review_candidates(
            "audit router and identify remaining risks", files,
            max_files=7, preferred=[r"src\router.py"])
        self.assertLessEqual(len(selected), 7)
        self.assertEqual(len(selected), len(set(selected)))
        self.assertIn("src/router.py", selected)
        self.assertTrue(any(path.startswith("docs/") for path in selected))
        self.assertTrue(any(path.startswith("tests/") for path in selected))
        self.assertNotIn("notes.bin", selected)


if __name__ == "__main__":
    unittest.main()
