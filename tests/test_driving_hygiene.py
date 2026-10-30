"""Fixtures for the deterministic driving-hygiene checks."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.driving_hygiene import (
    check_close_keywords,
    check_credentials,
    check_negated_close_language,
    check_planning_structure,
    main,
)


class DrivingHygieneTests(unittest.TestCase):

    def test_negated_close_language_is_flagged_in_pr_text_and_diff(self):
        phrase = "This does not " + "close" + " #306."
        self.assertEqual(len(check_negated_close_language(phrase, "")), 1)
        diff = (
            "--- a/notes.md\n+++ b/notes.md\n@@ -1 +1 @@\n"
            "-old text\n+" + phrase + "\n"
        )
        findings = check_negated_close_language("", diff)
        self.assertEqual(len(findings), 1)
        self.assertIn("negated-close-language", findings[0].check)
        self.assertEqual(check_negated_close_language("Issue #306 remains open.", ""), [])

    def test_close_keyword_is_flagged_and_removed_reference_passes(self):
        phrase = "Fix" + "es" + " #42"
        self.assertEqual(len(check_close_keywords(phrase, "")), 1)
        added_diff = (
            "--- a/notes.md\n+++ b/notes.md\n@@ -0,0 +1 @@\n+" + phrase + "\n"
        )
        self.assertEqual(len(check_close_keywords("", added_diff)), 1)
        diff = (
            "--- a/notes.md\n+++ b/notes.md\n@@ -1 +1 @@\n"
            "-" + phrase + "\n+Related to #42\n"
        )
        self.assertEqual(check_close_keywords("", diff), [])
        self.assertEqual(check_close_keywords("Related to #42", ""), [])

    def test_credential_fixture_fails_without_echoing_the_value(self):
        seeded = "gh" + "p_" + ("A" * 36)
        findings = check_credentials("", "+token=" + seeded)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].check, "credential")
        self.assertNotIn(seeded, str(findings[0]))
        self.assertEqual(check_credentials("token removed", ""), [])

    def test_credential_patterns_scan_pr_text_as_well(self):
        seeded = "sk-" + ("b" * 32)
        self.assertEqual(len(check_credentials("key=" + seeded, "")), 1)
        assignment_value = "C" * 32
        assignment = 'OPENAI_API_KEY = "' + assignment_value + '"'
        findings = check_credentials(assignment, "")
        self.assertEqual(len(findings), 1)
        self.assertNotIn(assignment_value, str(findings[0]))

    def test_planning_structure_accepts_current_shape_and_rejects_malformed_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plans = root / "docs" / "issue-plans"
            plans.mkdir(parents=True)
            valid = plans / "42.md"
            valid.write_text(
                "# Issue #42 - Example\n\n## Status\n\nOpen.\n\n"
                "## Verification\n\nPending.\n",
                encoding="utf-8",
            )
            self.assertEqual(check_planning_structure(root), [])

            valid.write_text("# Example\n\n## Status\n", encoding="utf-8")
            findings = check_planning_structure(root)
            self.assertTrue(findings)
            self.assertTrue(all(f.check == "planning-structure" for f in findings))

    def test_all_existing_issue_plan_documents_meet_the_structure_rule(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(check_planning_structure(root), [])

    def test_cli_fails_on_seeded_violations_and_passes_after_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plans = root / "docs" / "issue-plans"
            plans.mkdir(parents=True)
            (plans / "42.md").write_text(
                "# Issue #42 - Example\n\n## Status\n\nOpen.\n\n"
                "## Verification\n\nPending.\n",
                encoding="utf-8",
            )
            event_path = root / "event.json"
            event_path.write_text(json.dumps({
                "pull_request": {
                    "base": {"sha": "a" * 40},
                    "title": "Fixture PR",
                    "body": "No closing reference here.",
                },
            }), encoding="utf-8")

            close_ref = "Fix" + "es" + " #42"
            token = "gh" + "p_" + ("A" * 36)
            diff_cases = (
                (
                    "--- a/notes.md\n+++ b/notes.md\n@@ -0,0 +1 @@\n+"
                    + close_ref + "\n",
                    "Related to #42",
                ),
                (
                    "--- a/notes.md\n+++ b/notes.md\n@@ -0,0 +1 @@\n"
                    "+token=" + token + "\n",
                    "+token removed\n",
                ),
            )
            for violating_diff, clean_diff in diff_cases:
                with self.subTest(violation=bool(violating_diff)):
                    with patch("scripts.driving_hygiene._pull_request_diff",
                               return_value=violating_diff):
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(main(
                                repo_root=root, event_path=event_path,
                                head_sha="b" * 40), 1)
                    with patch("scripts.driving_hygiene._pull_request_diff",
                               return_value=clean_diff):
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(main(
                                repo_root=root, event_path=event_path,
                                head_sha="b" * 40), 0)


if __name__ == "__main__":
    unittest.main()
