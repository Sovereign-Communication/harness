"""PLAT-ci-matrix: the three-OS claim is enforced by the workflow file itself.

A matrix in a YAML file is a promise; a matrix nobody checks is a comment.
These tests parse ``.github/workflows/ci.yml`` and assert the properties the
platform-unification row depends on: the hermetic suite runs on Linux, Windows
and macOS; the installed wheel is installed and executed on all three; the
lint/audit/Jev bar stays on Ubuntu (one platform for the verdict, so a lint
finding is never a runner artefact); and no step hardcodes a POSIX-only path
where Windows would need ``Scripts/``.

``tests/test_plat_parity_*.py`` are the reason the matrix exists -- they pin
byte-identical output, so "runs on all three" is what turns those hashes from
opinions into facts.
"""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
GITATTRIBUTES = os.path.join(REPO_ROOT, ".gitattributes")
PLATFORMS = ("ubuntu-latest", "windows-latest", "macos-latest")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _job_block(text, job):
    """The YAML text of one job (indented two spaces under its name)."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.rstrip() == f"  {job}:")
    except StopIteration:
        raise AssertionError(f"job {job!r} is missing from ci.yml") from None
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("   "):
            break
        out.append(line)
    return "\n".join(out)


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(WORKFLOW)

    def test_the_hermetic_suite_runs_on_all_three_platforms(self):
        block = _job_block(self.text, "test")
        self.assertIn("runs-on: ${{ matrix.os }}", block)
        for platform in PLATFORMS:
            self.assertIn(platform, block, f"{platform} is not in the test matrix")
        self.assertIn("-W error::ResourceWarning -m unittest discover -s tests", block)
        self.assertIn("compileall", block)

    def test_python_version_sweep_is_kept(self):
        block = _job_block(self.text, "test")
        for version in ('"3.9"', '"3.11"', '"3.13"'):
            self.assertIn(version, block)

    def test_lint_is_ubuntu_only(self):
        block = _job_block(self.text, "test")
        self.assertIn("python -m ruff check harness tests audits", block)
        self.assertIn("if: runner.os == 'Linux'", block)
        lint_step = block.split("name: Lint (ruff)")[1].split("- name:")[0]
        self.assertIn("runner.os == 'Linux'", lint_step,
                      "the ruff gate must be skipped off Linux")

    def test_audit_and_jev_bar_stay_on_ubuntu(self):
        block = _job_block(self.text, "audit")
        self.assertIn("runs-on: ubuntu-latest", block)
        self.assertIn("audits/self/audit.py", block)
        self.assertIn("jev-phase" in self.text or "capabilities --check-shipped",
                      self.text)

    def test_installed_package_is_smoke_tested_on_all_three(self):
        block = _job_block(self.text, "package-installed")
        self.assertIn("runs-on: ${{ matrix.os }}", block)
        for platform in PLATFORMS:
            self.assertIn(platform, block)
        self.assertIn("pip install dist/*.whl", block)
        self.assertIn("import harness", block)
        self.assertIn("-m harness.cli --help", block)

    def test_venv_interpreter_path_is_per_platform(self):
        block = _job_block(self.text, "package-installed")
        self.assertIn(".smoke-venv/Scripts/python.exe", block,
                      "Windows venvs put the interpreter in Scripts/")
        self.assertIn(".smoke-venv/bin/python", block,
                      "POSIX venvs put it in bin/")

    def test_no_step_assumes_a_posix_shell_implicitly(self):
        """Steps that use POSIX syntax declare `shell: bash` (Windows runners
        default to PowerShell, where `rm -rf` and `$HOME` mean something else)."""
        for step_name in ("Smoke-install the wheel in a clean venv",
                          "Clean up the smoke venv",
                          "Validate changed handoffs",
                          "Version parity (pyproject vs harness.__version__ vs MCP)"):
            self.assertIn(step_name, self.text)
            block = self.text.split(f"name: {step_name}")[1].split("- name:")[0]
            self.assertIn("shell: bash", block,
                          f"step {step_name!r} uses POSIX syntax without declaring bash")

    def test_artifact_handoff_between_the_two_package_jobs(self):
        upload = _job_block(self.text, "package")
        download = _job_block(self.text, "package-installed")
        self.assertIn("upload-artifact", upload)
        self.assertIn("name: dist", upload)
        self.assertIn("needs: package", download)
        self.assertIn("download-artifact", download)

    def test_hermetic_suite_never_reaches_the_network_in_ci(self):
        block = _job_block(self.text, "test")
        self.assertNotIn("OPENROUTER_API_KEY", block)
        self.assertNotIn("pytest", block)

    def test_workflow_keeps_its_original_gates(self):
        for gate in ("validate_handoff_scope.py", "python -m twine check dist/*",
                     "python -m build", "harness.__version__"):
            self.assertIn(gate, self.text, f"CI lost the {gate} gate")


class GitAttributesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(GITATTRIBUTES)

    def test_everything_is_lf_in_the_working_tree(self):
        self.assertIn("* text=auto eol=lf", self.text.splitlines(),
                      "the catch-all rule must state the working-tree form")
    def test_parity_relevant_paths_are_pinned(self):
        for pattern in ("harness/** text eol=lf", "tests/** text eol=lf",
                        "*.jsonl text eol=lf", "*.md text eol=lf"):
            self.assertIn(pattern, self.text)

    def test_windows_scripts_keep_platform_endings(self):
        for pattern in ("*.bat text eol=crlf", "*.cmd text eol=crlf"):
            self.assertIn(pattern, self.text)

    def test_binary_assets_are_never_normalized(self):
        self.assertIn("*.png binary", self.text)
        self.assertIn("*.whl binary", self.text)

    def test_the_repo_has_no_stray_crlf_in_tracked_text(self):
        """A CRLF committed into a fixture is how a parity hash rots."""
        offenders = []
        for rel in ("harness/osal.py", "harness/gate_runner.py", "harness/ledger.py",
                    ".github/workflows/ci.yml", ".gitattributes"):
            with open(os.path.join(REPO_ROOT, rel), "rb") as handle:
                if b"\r\n" in handle.read():
                    offenders.append(rel)
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
