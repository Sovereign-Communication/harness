"""The repo philosophy card scanner.

The rule under test throughout: a card row exists only when it can name the
committed file it was parsed from, and rendering is a pure function of disk
content so a card diff always means a source file moved.
"""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.repo_cards import (
    RATIONALE_SUBDIR,
    _toml_has_section,
    _toml_value,
    default_root,
    discover_repos,
    generate,
    main,
    render_card,
    scan_repo,
    write_card,
)

ROOT = Path(__file__).resolve().parent.parent


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

    def test_skips_versioned_planning_checkouts(self):
        """A ``-v<NNN>-`` planning checkout duplicates its base repo's card."""
        self._repo("Real")
        self._repo("Real-v040-harness-plan")
        self.assertEqual([n for n, _ in discover_repos(self.root)], ["Real"])

    def test_keeps_a_repo_whose_name_merely_contains_v(self):
        """The rule matches the checkout shape, not a stray ``v`` substring."""
        for name in ("Rev", "Survey", "DevOps"):
            self._repo(name)
        self.assertEqual([n for n, _ in discover_repos(self.root)],
                         ["DevOps", "Rev", "Survey"])

    def test_base_repo_is_kept_when_a_planning_sibling_exists(self):
        """Dropping the checkout must never drop the repo it plans against."""
        self._repo("SCMessenger")
        self._repo("SCMessenger-v040-harness-plan")
        names = [n for n, _ in discover_repos(self.root)]
        self.assertEqual(names, ["SCMessenger"])
        self.assertIn("SCMessenger", names)

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

    def test_prose_edits_do_not_churn_the_card(self):
        """Cards record that a convention file exists, not what it says.

        Rewriting AGENTS.md must not produce a card diff, or every doc edit
        would look like a convention change.
        """
        self._repo("Demo")
        generate(self.root, self.cards)
        before = (self.cards / "Demo.md").read_bytes()
        (self.root / "Demo" / "AGENTS.md").write_text("rewritten", encoding="utf-8")
        generate(self.root, self.cards)
        self.assertEqual((self.cards / "Demo.md").read_bytes(), before)

    def test_a_new_convention_file_does_change_the_card(self):
        self._repo("Demo")
        generate(self.root, self.cards)
        before = (self.cards / "Demo.md").read_bytes()
        _write(self.root / "Demo", "CONTRIBUTING.md", "## rules")
        generate(self.root, self.cards)
        self.assertNotEqual((self.cards / "Demo.md").read_bytes(), before)


class RareBranchTests(unittest.TestCase):
    """Branches a real checkout rarely reaches, pinned so they cannot rot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_unreadable_workflow_dir_yields_no_ci_rows(self):
        _write(self.repo, ".github/workflows/ci.yml", "n")
        with mock.patch.object(Path, "iterdir", side_effect=OSError("boom")):
            self.assertEqual([r for r in scan_repo(self.repo) if r[0] == "CI"], [])

    def test_requires_python_without_a_name_still_names_the_package(self):
        _write(self.repo, "pyproject.toml", "[project]\nrequires-python = \">=3.9\"\n")
        rows = [r for r in scan_repo(self.repo) if r[0] == "Build"]
        self.assertIn("requires-python >=3.9", rows[0][1])

    def test_requirements_txt_is_a_build_row(self):
        _write(self.repo, "requirements.txt", "flask\n")
        rows = [r for r in scan_repo(self.repo) if r[0] == "Build"]
        self.assertEqual(rows[0][2], "requirements.txt")

    def test_unreadable_existing_card_is_rewritten_not_trusted(self):
        """A card we cannot read must not be mistaken for an up-to-date one."""
        cards = self.repo / "cards"
        cards.mkdir()
        target = cards / "Demo.md"
        target.write_text("stale", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            changed = write_card(cards, "Demo", "fresh\n")
        self.assertTrue(changed)
        self.assertEqual(target.read_text(encoding="utf-8"), "fresh\n")


class CliTests(unittest.TestCase):
    """The operator entry point, including the ``--check`` staleness gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "repos"
        self.cards = self.base / "cards"
        self.root.mkdir()
        (self.root / "Demo" / ".git").mkdir(parents=True)
        (self.root / "Demo" / "AGENTS.md").write_text("x", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--root", str(self.root), "--cards", str(self.cards), *argv])
        return code, out.getvalue()

    def test_write_mode_reports_wrote_then_unchanged(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("wrote Demo", out)
        code, out = self._run()
        self.assertIn("unchanged Demo", out)

    def test_check_mode_fails_when_a_card_is_missing(self):
        code, out = self._run("--check")
        self.assertEqual(code, 1)
        self.assertIn("stale: Demo", out)
        self.assertNotIn("up to date", out)

    def test_check_mode_fails_when_a_new_convention_file_appears(self):
        """Staleness comes from a new citable source, not from prose edits.

        Cards record that a convention file exists, so editing AGENTS.md
        cannot change one -- only a new origin-bearing file can.
        """
        self._run()
        (self.root / "Demo" / "AGENTS.md").write_text("completely rewritten", encoding="utf-8")
        self.assertEqual(self._run("--check")[0], 0)
        (self.root / "Demo" / "CONTRIBUTING.md").write_text("## rules", encoding="utf-8")
        code, out = self._run("--check")
        self.assertEqual(code, 1)
        self.assertIn("stale: Demo", out)

    def test_check_mode_passes_when_cards_are_current(self):
        self._run()
        code, out = self._run("--check")
        self.assertEqual(code, 0)
        self.assertIn("cards up to date", out)

    def test_module_entry_point_is_runnable(self):
        """``python -m harness.repo_cards`` is how an operator actually runs it."""
        proc = subprocess.run(
            [sys.executable, "-m", "harness.repo_cards",
             "--root", str(self.root), "--cards", str(self.cards), "--check"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("stale: Demo", proc.stdout)

    def test_default_root_is_the_parent_of_this_checkout(self):
        self.assertEqual(default_root(), ROOT.parent)


if __name__ == "__main__":
    unittest.main()
