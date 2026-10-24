"""Jev identity block: mention detection, repo excerpts, and fallbacks."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import jev_identity


class JevMentionTests(unittest.TestCase):
    def test_names_the_component(self):
        for prompt in ("what is Jev?", "explain System One",
                       "TypeSafe returns typed answers", "JEV GATE"):
            with self.subTest(prompt=prompt):
                self.assertTrue(jev_identity.jev_mentioned(prompt))

    def test_ordinary_words_do_not_trigger(self):
        for prompt in ("javascript event loop", "the judge said no",
                       "what is a harness?", ""):
            with self.subTest(prompt=prompt):
                self.assertFalse(jev_identity.jev_mentioned(prompt))


class IdentityBlockTests(unittest.TestCase):
    def setUp(self):
        jev_identity.jev_identity_block.cache_clear()

    def tearDown(self):
        jev_identity.jev_identity_block.cache_clear()

    def test_repository_policy_and_skill_are_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            (root / "docs" / "JEV_GATE_POLICY_V1.md").write_text(
                "POLICY-MARKER", encoding="utf-8")
            skill = root / ".agents" / "skills" / "typesafe-ai"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("SKILL-MARKER", encoding="utf-8")
            with mock.patch.object(jev_identity, "_repo_root", return_value=root):
                block = jev_identity.jev_identity_block()
        self.assertIn("JEV IDENTITY", block)
        self.assertIn("POLICY-MARKER", block)
        self.assertIn("SKILL-MARKER", block)
        self.assertNotIn("_POLICY_FALLBACK", block)

    def test_missing_files_degrade_to_fallbacks(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(jev_identity, "_repo_root",
                                   return_value=Path(td)):
                block = jev_identity.jev_identity_block()
        self.assertIn("Gate on risk, not on numbers", block)
        self.assertIn("TypeSafe makes units of AI intelligence", block)
        self.assertIn("JEV IDENTITY", block)

    def test_missing_read_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.md"
            self.assertEqual(jev_identity._read_bounded(missing, 100), "")


if __name__ == "__main__":
    unittest.main()
