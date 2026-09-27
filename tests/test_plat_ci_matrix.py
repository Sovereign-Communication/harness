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
import subprocess
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


def _jobs(text):
    """Every job in the workflow, as ``{name: block}``."""
    names = [line.strip()[:-1] for line in text.splitlines()
             if line.startswith("  ") and not line.startswith("   ")
             and line.rstrip().endswith(":") and not line.strip().startswith("#")]
    return {name: _job_block(text, name) for name in names}


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
        lint_line = [ln.strip().removeprefix("run:").strip() for ln in block.splitlines()
                     if "ruff check" in ln]
        self.assertTrue(lint_line, "the test job lost its ruff gate")
        self.assertTrue(lint_line[0].startswith("python -m ruff check"),
                        f"ruff must be invoked through the interpreter: {lint_line[0]}")
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
        self.assertIn("Scripts/python.exe", block,
                      "Windows venvs put the interpreter in Scripts/")
        self.assertIn("bin/python", block,
                      "POSIX venvs put it in bin/")
        # The smoke run leaves the repo, so the interpreter must be absolute
        # BEFORE the cd: a relative venv path silently stops resolving there.
        self.assertIn('VENV="$GITHUB_WORKSPACE/.smoke-venv"', block)
        self.assertIn('PY="$VENV/', block)
        self.assertLess(block.index("PY="), block.index('cd "$HOME"'),
                        "resolve the venv path before leaving the repo")
        self.assertIn("${{ matrix.venv_python }}", block)

    POSIX_ONLY = ("rm -rf", "$HOME", "set -euo pipefail", "<<'PY'", "seq 1 30",
                  "grep -oE")

    def test_no_step_assumes_a_posix_shell_implicitly(self):
        """POSIX-only syntax is legal in an ubuntu job (its default shell is
        bash) and ILLEGAL anywhere else -- Windows runners default to
        PowerShell, where `rm -rf` and `$HOME` mean something else. So every
        step using such syntax must either live in a single-platform ubuntu
        job or declare `shell: bash` explicitly."""
        checked = 0
        for job_name, job in _jobs(self.text).items():
            runs_on = job.split("runs-on:")[1].splitlines()[0] if "runs-on:" in job else ""
            ubuntu_only = "ubuntu" in runs_on
            for step in job.split("      - name:")[1:]:
                body = step.split("- name:")[0]
                if not any(token in body for token in self.POSIX_ONLY):
                    continue
                checked += 1
                if "shell: bash" in body:
                    continue
                self.assertTrue(ubuntu_only,
                                f"step in job {job_name!r} uses POSIX-only syntax "
                                "on a matrix job without declaring `shell: bash`")
                self.assertIn("ubuntu", runs_on,
                              f"step in job {job_name!r} uses POSIX-only syntax "
                              "outside an ubuntu job without `shell: bash`")
        self.assertGreaterEqual(checked, 3,
                                "the POSIX-syntax scan found nothing to check -- "
                                "the workflow changed shape and this test is now vacuous")

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
        # The gates that shipped on origin/main BEFORE this branch existed.
        # A platform-matrix rewrite that quietly drops one of these is a
        # regression, not a simplification -- they are the only evidence
        # that a released wheel can actually serve and speak MCP.
        for gate in ("python -m twine check dist/*", "python -m build",
                     "harness.__version__", "serve --port 0",
                     '"method": "initialize"'):
            self.assertIn(gate, self.text, f"CI lost the {gate} gate")

    def test_the_serve_and_mcp_smoke_survives_in_the_ubuntu_package_job(self):
        block = _job_block(self.text, "package")
        self.assertIn("runs-on: ubuntu-latest", block)
        self.assertIn("serve --port 0", block)
        self.assertIn("McpServer", block)


class CheckoutHygieneTest(unittest.TestCase):
    """A fresh checkout must be CLEAN, and `.gitattributes` is what decides.

    The 2026-09-27 Windows matrix run caught a defect no local gate could:
    the new `text eol=lf` rules were applied to eight `bench/tasks/*`
    fixtures that git classifies as binary (`i/-text` in `ls-files --eol`),
    so every fresh checkout reported them as modified. That is not cosmetic --
    the apply engine reads `git status --porcelain` as a node's diff, so the
    files surfaced as "undeclared writes outside target_files" and isolated
    plan runs failed. These tests ask git directly, so the class cannot come
    back on any platform.
    """

    def _ls_files_eol(self):
        try:
            out = subprocess.run(["git", "ls-files", "--eol"], cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("optional-deps: git is not available in this environment")
        if out.returncode != 0:
            self.skipTest("platform: not a git checkout (source tree install)")
        return out.stdout.splitlines()

    def test_no_file_is_forced_to_text_that_git_calls_binary(self):
        """`text` overrides git's binary heuristic -- so it must never be
        applied to a path git already refuses to normalize."""
        offenders = []
        for line in self._ls_files_eol():
            fields = line.split()
            if len(fields) < 3:
                continue
            index_eol, _worktree_eol, attrs = fields[0], fields[1], fields[2]
            path = "\t".join(fields[3:]) or fields[-1]
            if index_eol == "i/-text" and "text" in attrs and "eol=" in attrs:
                offenders.append(f"{path} ({attrs})")
        self.assertEqual(
            offenders, [],
            "these paths are binary to git but forced to text by "
            ".gitattributes, so a fresh checkout is dirty:\n  "
            + "\n  ".join(offenders))

    def test_no_committed_blob_carries_crlf(self):
        """The index is the authority the parity hashes describe; a CRLF blob
        in a text file is a parity hazard waiting for the next checkout."""
        offenders = [line for line in self._ls_files_eol()
                     if line.startswith("i/crlf")]
        self.assertEqual(offenders, [],
                         "text files committed with CRLF line endings:\n  "
                         + "\n  ".join(offenders))

    def test_the_declared_binary_exclusions_are_still_binary(self):
        text = _read(".gitattributes")
        self.assertIn("bench/tasks/** -text", text,
                      "the bench fixtures must stay excluded from normalization")


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
