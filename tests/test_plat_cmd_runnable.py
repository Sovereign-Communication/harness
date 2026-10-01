"""PLAT-cmd-data: the documented gates are data, and they run everywhere.

The bar for this row is blunt: *every* documented gate must run successfully
on every operating system, with the same exit code and the same stdout shape.
This file is that bar, expressed as tests. The expensive part of the claim --
macOS and Linux -- is answered by the CI matrix; what this file guarantees
everywhere is that the gate definitions are platform-independent data
(``{python}`` resolved from ``sys.executable``), that they tokenize the same
way regardless of which separator the reader's platform prefers, and that the
one runner never hands a string to a shell.

The ``unittest`` gate is deliberately never executed from inside the suite:
it *is* this suite. Recursion is not evidence, so that gate is verified by
construction (it is in the registry, and CI runs it as its own step) and
this file asserts the registry shape instead.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import osal
from harness.errors import HarnessError
from harness.filesafety import default_run_verify, validate_verify_command
from harness.gate_runner import (GATES, GATE_ORDER, VERIFY_TIMEOUT, gate,
                                 gate_argv, gate_command, run_gate, split_command)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Gates this suite may execute: fast, hermetic, and not the suite itself.
SELF_EXECUTABLE = ("import", "compileall", "ruff")


class GateRegistryTest(unittest.TestCase):
    def test_every_documented_gate_is_registered(self):
        self.assertEqual(sorted(GATES), sorted(GATE_ORDER))
        for name in GATE_ORDER:
            spec = gate(name)
            self.assertEqual(spec.name, name)
            self.assertTrue(spec.summary.strip(),
                            f"gate {name} must say what it proves")

    def test_gates_are_argv_data_not_command_strings(self):
        for name in GATE_ORDER:
            argv = gate_argv(name, phase="PLAT-cmd-data")
            self.assertIsInstance(argv, list)
            self.assertTrue(all(isinstance(part, str) for part in argv))
            self.assertNotIn("&&", argv)
            self.assertNotIn("|", argv)
            self.assertNotIn(";", argv)

    def test_python_is_resolved_from_sys_executable_on_every_platform(self):
        """The whole portability trick: no hardcoded .venv/Scripts path."""
        for name in GATE_ORDER:
            self.assertEqual(gate_argv(name, phase="PLAT-cmd-data")[0],
                             sys.executable,
                             f"gate {name} must start at the running interpreter")
            # The *template* is the portability claim: an interpreter path
            # (or any platform-shaped literal) baked in here is what made
            # each doc correct on exactly one OS.
            template = " ".join(gate(name).template)
            self.assertNotIn(".venv", template)
            self.assertNotIn("Scripts", template)
            self.assertNotIn("bin/python", template)
            self.assertIn("{python}", template)

    def test_phase_is_a_parameter_not_a_hardcoded_id(self):
        argv = gate_argv("jev-phase", phase="HV-5")
        self.assertIn("HV-5", argv)
        self.assertNotIn("{phase}", argv)
        self.assertNotIn("None", argv)

    def test_unknown_gate_is_refused(self):
        with self.assertRaises(HarnessError):
            gate("not-a-gate")

    def test_command_form_round_trips_through_the_tokenizer(self):
        """`harness gates` prints a command; feeding it back must be lossless."""
        for name in GATE_ORDER:
            command = gate_command(name, phase="PLAT-cmd-data")
            self.assertEqual(split_command(command), gate_argv(name, phase="PLAT-cmd-data"))

    def test_gate_listing_names_every_gate(self):
        from harness.gate_runner import gate_help
        text = gate_help()
        for name in GATE_ORDER:
            self.assertIn(name, text)


class GateRunsOnThisOSTest(unittest.TestCase):
    """The Jev bar for PLAT-cmd-data: the gates actually run, here."""

    def test_import_gate(self):
        rc, out = run_gate(gate_argv("import"))
        self.assertEqual(rc, 0, out)
        from harness import __version__
        self.assertIn(__version__, out)

    def test_detect_version_fallback(self):
        from unittest.mock import patch
        import harness
        with patch("importlib.metadata.version", side_effect=Exception("no dist")), \
             patch("pathlib.Path.is_file", return_value=False):
            ver = harness._detect_version()
            self.assertEqual(ver, harness._FALLBACK_VERSION)
            self.assertEqual(ver, "0.4.2")

    def test_compileall_gate(self):
        rc, out = run_gate(gate_argv("compileall"), cwd=REPO_ROOT)
        self.assertEqual(rc, 0, out)

    def test_ruff_gate_when_available(self):
        try:
            import ruff  # noqa: F401
        except ImportError:
            self.skipTest("optional-deps: ruff is not installed in this environment")
        rc, out = run_gate(gate_argv("ruff"), cwd=REPO_ROOT, timeout=300)
        self.assertEqual(rc, 0, out)

    def test_jev_phase_gate_is_runnable_even_when_the_row_is_not_done(self):
        """A gate that cannot run is not a gate; a failing bar is a verdict."""
        rc, out = run_gate(gate_argv("jev-phase", phase="PLAT-cmd-data"),
                           cwd=REPO_ROOT, timeout=600)
        self.assertIn(rc, (0, 1), out[-2000:])
        self.assertIn("PLAT", out)

    def test_unittest_gate_is_never_run_from_inside_the_suite(self):
        self.assertIn("unittest", GATES, "the suite gate must stay documented")
        self.assertNotIn("unittest", SELF_EXECUTABLE,
                         "running the suite from inside the suite is recursion, "
                         "not a gate")


class WindowsAwareTokenizerTest(unittest.TestCase):
    """The two tokenizer claims, pinned without needing a Windows host."""

    def test_drive_letter_path_survives_shlex(self):
        argv = split_command(r"C:\Users\scm\gate.bat --flag value")
        self.assertEqual(argv[0], r"C:\Users\scm\gate.bat")
        self.assertEqual(argv[1:], ["--flag", "value"])

    def test_forward_slash_windows_path_survives(self):
        argv = split_command("C:/Users/scm/gate.bat --flag")
        self.assertEqual(argv[0], "C:/Users/scm/gate.bat")

    def test_unc_path_survives(self):
        argv = split_command(r"\\build\share\gate.ps1 -Mode Fast")
        self.assertEqual(argv, [r"\\build\share\gate.ps1", "-Mode", "Fast"])

    def test_quoted_path_with_spaces_is_one_token(self):
        argv = split_command(r'"C:\Program Files\Harness\gate.exe" --flag')
        self.assertEqual(argv, [r"C:\Program Files\Harness\gate.exe", "--flag"])

    def test_posix_paths_are_untouched(self):
        argv = split_command("/usr/bin/env python3 -m pytest tests")
        self.assertEqual(argv, ["/usr/bin/env", "python3", "-m", "pytest", "tests"])

    def test_a_posix_gate_command_still_tokenizes(self):
        argv = split_command(
            'python -m unittest discover -s tests -p "test_*.py"')
        self.assertEqual(argv[-3:], ["tests", "-p", "test_*.py"])
        self.assertEqual(argv[:3], ["python", "-m", "unittest"])

    def test_unbalanced_quoting_fails_closed(self):
        with self.assertRaises(HarnessError):
            split_command('python -c "print(1)')

    def test_empty_command_is_refused(self):
        for bad in ("", "   ", None, []):
            with self.assertRaises(HarnessError):
                split_command(bad)

    def test_an_argv_list_passes_through(self):
        self.assertEqual(split_command([sys.executable, "-c", "print(1)"]),
                         [sys.executable, "-c", "print(1)"])


class SingleRunnerTest(unittest.TestCase):
    """One runner for verify gates and stage gates alike."""

    def test_verify_gate_returns_exit_code_and_output(self):
        command = f"{sys.executable} -c \"import sys; print('gate-out'); sys.exit(3)\""
        rc, out = default_run_verify(command)
        self.assertEqual(rc, 3)
        self.assertIn("gate-out", out)

    def test_verify_gate_runs_from_a_cwd(self):
        rc, out = run_gate([sys.executable, "-c", "import os; print(os.getcwd())"],
                           cwd=REPO_ROOT)
        self.assertEqual(rc, 0)
        self.assertEqual(osal.norm_path(out.strip()), osal.norm_path(REPO_ROOT))

    def test_no_shell_means_no_chaining(self):
        """The regression this row exists for: shell=True ran `&&`."""
        command = f"{sys.executable} -c \"print(1)\" && echo PWNED"
        rc, out = default_run_verify(command)
        self.assertEqual(rc, 0)
        self.assertNotIn("PWNED", out)

    def test_no_shell_means_forward_slash_paths_are_fine(self):
        """cmd.exe used to reject this exact argv; argv lists do not care."""
        script = os.path.join(REPO_ROOT, "audits", "self", "audit.py")
        self.assertTrue(os.path.isfile(script))
        argv = [sys.executable, script, "--help"]
        result = osal.run(argv, cwd=REPO_ROOT, timeout=300)
        self.assertNotIn(result.returncode, (126, 127),
                         f"forward-slash script path failed: {result.combined[:300]}")

    def test_missing_executable_is_127_not_a_traceback(self):
        rc, out = default_run_verify("harness-definitely-not-a-real-binary-xyz")
        self.assertEqual(rc, 127)
        self.assertIn("not found", out)

    def test_preflight_reports_a_missing_tool_without_running_it(self):
        with self.assertRaises(HarnessError):
            validate_verify_command("harness-definitely-not-a-real-binary-xyz")
        # Engine callers opt out of the PATH check for hermetic stubs.
        self.assertEqual(validate_verify_command("some-tool --flag",
                                                require_executable=False),
                         ["some-tool", "--flag"])

    def test_timeout_budgets_are_ordered(self):
        self.assertEqual(VERIFY_TIMEOUT, 300)
        from harness.gate_runner import STAGE_GATE_TIMEOUT
        self.assertGreater(STAGE_GATE_TIMEOUT, VERIFY_TIMEOUT)


def _read(name):
    """Module source as text -- context-managed: R13 fails the whole audit
    on an unraisable ``ResourceWarning: unclosed file`` (the v0.3.2
    release-gate flake class), and a bare ``open(...).read()`` is exactly
    that leak."""
    with open(os.path.join(REPO_ROOT, "harness", name), encoding="utf-8") as handle:
        return handle.read()


class VerifyAndStageGateShareOneRunnerTest(unittest.TestCase):
    def test_filesafety_delegates_to_the_one_runner(self):
        import harness.filesafety as fs
        self.assertIs(fs.default_run_verify.__wrapped__ if hasattr(
            fs.default_run_verify, "__wrapped__") else None, None)
        source = _read("filesafety.py")
        self.assertIn("run_gate", source)
        self.assertNotIn("subprocess", source,
                         "the verify gate must not launch a process itself")

    def test_cli_stage_gate_uses_the_one_runner(self):
        source = _read("cli.py")
        self.assertIn("run_gate(stage_gate", source)
        self.assertNotIn("shell=True", source,
                         "PLAT-cmd-data: no shell=True anywhere in the CLI")
        self.assertNotIn("subprocess", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
