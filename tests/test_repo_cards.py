"""The repo philosophy card scanner.

The rule under test throughout: a card row exists only when it can name the
committed file it was parsed from, and rendering is a pure function of disk
content so a card diff always means a source file moved.
"""
import tempfile
import unittest
from pathlib import Path

from harness.repo_cards import (
    RATIONALE_SUBDIR,
    _toml_has_section,
    _toml_value,
    discover_repos,
    generate,
    render_card,
    scan_repo,
)


def _write(root, name, body=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TomlReaderTests(unittest.TestCase):
    """No tomllib: the package floor is 3.9, so section/scalar reads are ours."""

    DOC = "[project]\nname = \"demo\"\nrequires-python = \">=3.9\"\n\n[tool.ruff]\nline-length = 100\n"

    def test_reads_scalar_from_named_section(self):
        self.assertEqual(_toml_value(self.DOC, "project", "name"), "demo")
        self.assertEqual(_toml_value(self.DOC, "tool.ruff", "line-length"), "100")

    def test_absent_section_and_key_are_none(self):
        self.assertIsNone(_toml_value(self.DOC, "tool.black", "line-length"))
        self.assertIsNone(_toml_value(self.DOC, "project", "missing"))

    def test_detects_section_presence(self):
        self.assertTrue(_toml_has_section(self.DOC, "tool.ruff"))
        self.assertFalse(_toml_has_section(self.DOC, "tool.black"))

    def test_malformed_document_does_not_raise(self):
        self.assertFalse(_toml_has_section("not = toml at all", "project"))
        self.assertEqual(_toml_value("[unterminated\n", "project", "name"), None)


class ScanRepoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_empty_repo_yields_no_rows(self):
        """Nothing committed means nothing asserted -- not a guessed row."""
        self.assertEqual(scan_repo(self.repo), [])

    def test_agent_context_row_names_its_origin(self):
        _write(self.repo, "AGENTS.md", "# context")
        rows = scan_repo(self.repo)
        self.assertEqual([r[2] for r in rows], ["AGENTS.md"])

    def test_contributing_row_names_its_origin(self):
        _write(self.repo, "CONTRIBUTING.md", "## c")
        rows = [r for r in scan_repo(self.repo) if r[0] == "Contribution"]
        self.assertEqual(rows[0][2], "CONTRIBUTING.md")

    def test_every_row_origin_is_a_file_that_exists(self):
        """The grounding rule: no row may cite a file that is not there."""
        _write(self.repo, "AGENTS.md", "x")
        _write(self.repo, "pyproject.toml", '[project]\nname = "demo"\n[tool.ruff]\nline-length = 100\n')
        _write(self.repo, "package.json", '{"name": "demo", "scripts": {"test": "jest"}}')
        _write(self.repo, ".github/workflows/ci.yml", "name: ci\n")
        for _axis, _norm, origin in scan_repo(self.repo):
            if origin.startswith("harness/") or "[" in origin or ":" in origin:
                continue
            self.assertTrue((self.repo / origin).is_file(), origin)

    def test_ruff_and_project_slots_cite_toml_sections(self):
        _write(self.repo, "pyproject.toml", '[project]\nname = "demo"\n[tool.ruff]\nline-length = 100\n')
        origins = [r[2] for r in scan_repo(self.repo)]
        self.assertIn("pyproject.toml:[tool.ruff]", origins)
        self.assertIn("pyproject.toml:[project]", origins)

    def test_npm_test_script_is_cited_at_its_key(self):
        _write(self.repo, "package.json", '{"name": "d", "scripts": {"test": "jest"}}')
        rows = [r for r in scan_repo(self.repo) if r[0] == "Tests"]
        self.assertEqual(rows[0][2], "package.json:scripts.test")

    def test_package_without_test_script_emits_no_test_row(self):
        _write(self.repo, "package.json", '{"name": "d", "scripts": {"build": "x"}}')
        self.assertEqual([r for r in scan_repo(self.repo) if r[0] == "Tests"], [])

    def test_malformed_package_json_is_survivable(self):
        _write(self.repo, "package.json", "{not json")
        rows = scan_repo(self.repo)
        self.assertEqual([r[2] for r in rows], ["package.json"])

    def test_cargo_crate_and_test_command(self):
        _write(self.repo, "Cargo.toml", '[package]\nname = "demo"\n')
        origins = [r[2] for r in scan_repo(self.repo)]
        self.assertIn("Cargo.toml:[package]", origins)

    def test_ci_workflows_are_listed_in_sorted_order(self):
        _write(self.repo, ".github/workflows/zeta.yml", "n")
        _write(self.repo, ".github/workflows/alpha.yml", "n")
        ci = [r[2] for r in scan_repo(self.repo) if r[0] == "CI"]
        self.assertEqual(ci, [".github/workflows/alpha.yml", ".github/workflows/zeta.yml"])

    def test_gate_runner_is_recognised_as_the_gate_registry(self):
        _write(self.repo, "harness/gate_runner.py", 'GATES = {"ruff": 1, "unittest": 2}')
        origins = [r[2] for r in scan_repo(self.repo)]
        self.assertIn("harness/gate_runner.py:GATES", origins)

    def test_plain_gate_runner_without_gates_emits_no_gate_row(self):
        _write(self.repo, "harness/gate_runner.py", "# nothing here")
        self.assertEqual([r for r in scan_repo(self.repo) if r[0] == "Lint"], [])


class RenderTests(unittest.TestCase):
    def test_row_appears_with_its_origin_in_the_table(self):
        body = render_card("Demo", [("Lint", "ruff", "pyproject.toml:[tool.ruff]")])
        self.assertIn("| Lint | ruff | `pyproject.toml:[tool.ruff]` |", body)

    def test_no_rows_says_so_instead_of_inventing_one(self):
        self.assertIn("No conventions could be resolved", render_card("Demo", []))

    def test_rationale_is_appended_when_present(self):
        body = render_card("Demo", [("Lint", "ruff", "a")], "Because the team chose it.")
        self.assertIn("## Rationale", body)
        self.assertIn("Because the team chose it.", body)

    def test_rationale_omitted_when_absent(self):
        self.assertNotIn("## Rationale", render_card("Demo", [("Lint", "ruff", "a")], None))

    def test_render_is_pure(self):
        rows = [("CI", "workflow `ci.yml` runs on push/pull request", ".github/workflows/ci.yml")]
        self.assertEqual(render_card("Demo", rows), render_card("Demo", rows))

    def test_pipes_are_escaped_so_a_cell_cannot_break_the_table(self):
        body = render_card("Demo", [("Lint", "a | b", "c")])
        self.assertIn("a \\| b", body)


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _repo(self, name):
        (self.root / name / ".git").mkdir(parents=True)

    def test_discovers_only_git_checkouts(self):
        self._repo("Real")
        (self.root / "Plain").mkdir()
        self.assertEqual([n for n, _ in discover_repos(self.root)], ["Real"])

    def test_skips_worktrees_and_test_scratch_dirs(self):
        for name in ("Harness-hv-2", "wt-harness-canary", "test", "test7", "scratch"):
            self._repo(name)
        self._repo("Real")
        self.assertEqual([n for n, _ in discover_repos(self.root)], ["Real"])

    def test_results_are_sorted_by_name(self):
        for name in ("Zebra", "Alpha", "Mid"):
            self._repo(name)
        self.assertEqual([n for n, _ in discover_repos(self.root)],
                         ["Alpha", "Mid", "Zebra"])

    def test_missing_root_is_empty_not_an_error(self):
        self.assertEqual(discover_repos(self.root / "nope"), [])


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "repos"
        self.cards = self.base / "cards"
        self.root.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _repo(self, name):
        (self.root / name / ".git").mkdir(parents=True)
        (self.root / name / "AGENTS.md").write_text("x", encoding="utf-8")

    def test_regeneration_is_byte_identical(self):
        """The property a reviewer depends on: no source change, no card diff."""
        self._repo("Demo")
        first_changed, _ = generate(self.root, self.cards)
        self.assertEqual(first_changed, ["Demo"])
        original = (self.cards / "Demo.md").read_bytes()

        changed, unchanged = generate(self.root, self.cards)
        self.assertEqual(changed, [])
        self.assertEqual(unchanged, ["Demo"])
        self.assertEqual((self.cards / "Demo.md").read_bytes(), original)

    def test_generated_card_uses_lf_endings(self):
        self._repo("Demo")
        generate(self.root, self.cards)
        self.assertNotIn(b"\r\n", (self.cards / "Demo.md").read_bytes())

    def test_rationale_directory_file_flows_into_the_card(self):
        self._repo("Demo")
        _rationale = self.cards / RATIONALE_SUBDIR / "Demo.md"
        _rationale.parent.mkdir(parents=True, exist_ok=True)
        _rationale.write_text("Intent that config cannot express.", encoding="utf-8")
        generate(self.root, self.cards)
        body = (self.cards / "Demo.md").read_text(encoding="utf-8")
        self.assertIn("Intent that config cannot express.", body)

    def test_changing_a_source_file_changes_the_card(self):
        self._repo("Demo")
        generate(self.root, self.cards)
        before = (self.cards / "Demo.md").read_bytes()
        (self.root / "Demo" / "AGENTS.md").write_text("y", encoding="utf-8")
        generate(self.root, self.cards)
        self.assertEqual((self.cards / "Demo.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
