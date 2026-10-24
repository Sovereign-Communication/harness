"""#278: one external-tool resolver for every gate.

The live failure was a bench/apply gate written as ``python check.py`` on a
python3-only host. These tests pin the resolver itself, the real gate runner,
the apply preflight, and the fizz bench task on a PATH with only ``python3``.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harness import tools
from harness.bench import load_manifest, run_bench
from harness.errors import HarnessError
from harness.gate_runner import run_gate, validate_gate
from tests._applyfixture import ApplyFixture, CHANGED
from tests._fake import comp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIZZ_CORRECT = """def fizzbuzz(n):
    out = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    return out
"""


def _env_without(*names):
    env = dict(os.environ)
    for name in names:
        env.pop(name, None)
    return env


def _python3_shim(directory):
    """A real ``python3`` executable that a PATH-only lookup can find.

    Windows cannot run a copy of ``python.exe`` from another directory
    without the runtime DLLs beside it (exit 0xC0000135), so the base
    interpreter's DLLs travel with the shim. ``PYTHONHOME`` in the test env
    points the shim's copied binary back at the base stdlib; POSIX gets a
    symlink where possible.
    """
    dest = os.path.join(directory, "python3.exe" if os.name == "nt" else "python3")
    if os.name == "nt":
        base = getattr(sys, "_base_executable", sys.executable)
        shutil.copy2(base, dest)
        base_dir = os.path.dirname(base) or "."
        for name in os.listdir(base_dir):
            if name.lower().endswith(".dll"):
                try:
                    shutil.copy2(os.path.join(base_dir, name),
                                 os.path.join(directory, name))
                except OSError:  # pragma: no cover - locked/in-use DLL
                    pass
    else:
        try:
            os.symlink(sys.executable, dest)
        except OSError:  # pragma: no cover - exotic filesystems
            shutil.copy2(sys.executable, dest)
    return dest


def _run_child(script, env):
    """Run `script` in a fresh interpreter (the real process boundary)."""
    return subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


class ResolvePythonTests(unittest.TestCase):
    def test_the_running_interpreter_is_first(self):
        self.assertEqual(tools.resolve_python(), sys.executable)

    def test_falls_back_to_python3_anywhere_on_path(self):
        """The tool need not be at /usr/bin/python; PATH is the lookup."""
        def which(name):
            return "/opt/odd/bin/python3" if name == "python3" else None
        with mock.patch.object(tools.osal, "python_exe",
                               return_value="/missing/running/python"), \
                mock.patch.object(tools.osal, "which", side_effect=which):
            self.assertEqual(tools.resolve_python(), "/opt/odd/bin/python3")

    def test_environment_override_is_authoritative(self):
        with mock.patch.object(tools.osal, "python_exe",
                               return_value="/missing/running/python"), \
                mock.patch.object(tools.osal, "which") as which, \
                mock.patch.dict(os.environ, {tools.PYTHON_ENV: sys.executable}):
            self.assertEqual(tools.resolve_python(), sys.executable)
        which.assert_not_called()

    def test_a_bad_override_fails_instead_of_falling_back(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-python-278")
        with mock.patch.dict(os.environ, {tools.PYTHON_ENV: missing}):
            with self.assertRaisesRegex(HarnessError, "HARNESS_PYTHON"):
                tools.resolve_python()

    def test_a_bare_override_is_looked_up_on_path(self):
        with mock.patch.object(tools.osal, "which",
                               return_value="/opt/odd/bin/python3.11"), \
                mock.patch.dict(os.environ,
                                {tools.PYTHON_ENV: "python3.11"}):
            self.assertEqual(tools.resolve_python(), "/opt/odd/bin/python3.11")

    def test_a_missing_bare_override_is_named(self):
        with mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.dict(os.environ,
                                {tools.PYTHON_ENV: "no-such-python-278"}):
            with self.assertRaisesRegex(HarnessError, "was not found on PATH"):
                tools.resolve_python()

    def test_no_interpreter_is_one_named_error(self):
        with mock.patch.object(tools.osal, "python_exe",
                               return_value="/missing/running/python"), \
                mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.dict(os.environ, _env_without(tools.PYTHON_ENV),
                                clear=True):
            with self.assertRaisesRegex(HarnessError,
                                        "no Python interpreter found"):
                tools.resolve_python()

    def test_an_explicit_interpreter_path_is_not_rewritten(self):
        explicit = os.path.join(tempfile.gettempdir(), "venv", "python")
        self.assertEqual(tools.resolve_gate_argv([explicit, "-c", "pass"]),
                         [explicit, "-c", "pass"])

    def test_python_token_quotes_a_path_with_spaces(self):
        spaced = os.path.join(tempfile.gettempdir(), "Program Files", "python")
        with mock.patch.object(tools, "resolve_python", return_value=spaced):
            self.assertEqual(tools.python_token(), f'"{spaced}"')


class RepoScopeGatePythonTests(unittest.TestCase):
    def test_code_owned_gate_commands_use_the_one_resolver(self):
        from harness import repo_scope
        with mock.patch.object(tools, "python_token",
                               return_value="/resolved/python") as token:
            self.assertEqual(repo_scope._gate_python(), "/resolved/python")
        token.assert_called_once_with()


class ResolveRuffTests(unittest.TestCase):
    def test_ruff_anywhere_on_path_is_used(self):
        with mock.patch.object(tools.osal, "which", return_value="/opt/odd/ruff"), \
                mock.patch.dict(os.environ, _env_without(tools.RUFF_ENV),
                                clear=True):
            self.assertEqual(tools.ruff_argv(strict=True), ("/opt/odd/ruff",))

    def test_environment_override_is_authoritative(self):
        with mock.patch.dict(os.environ, {tools.RUFF_ENV: sys.executable}), \
                mock.patch.object(tools.osal, "which") as which:
            self.assertEqual(tools.ruff_argv(strict=True), (sys.executable,))
        which.assert_not_called()

    def test_module_fallback_uses_the_resolved_interpreter(self):
        with mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.object(tools, "_module_available", return_value=True), \
                mock.patch.dict(os.environ, _env_without(tools.RUFF_ENV),
                                clear=True):
            self.assertEqual(tools.ruff_argv(strict=True),
                             (tools.resolve_python(), "-m", "ruff"))

    def test_missing_ruff_is_named_when_strict(self):
        with mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.object(tools, "_module_available", return_value=False), \
                mock.patch.dict(os.environ, _env_without(tools.RUFF_ENV),
                                clear=True):
            with self.assertRaisesRegex(HarnessError, "ruff not found"):
                tools.ruff_argv(strict=True)

    def test_module_probe_failure_counts_as_unavailable(self):
        with mock.patch.object(tools.importlib.util, "find_spec",
                               side_effect=ImportError("broken")), \
                mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.dict(os.environ, _env_without(tools.RUFF_ENV),
                                clear=True):
            with self.assertRaisesRegex(HarnessError, "ruff not found"):
                tools.ruff_argv(strict=True)

    def test_empty_gate_argv_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "gate command is empty"):
            tools.resolve_gate_argv([])


class GateRunnerResolutionTests(unittest.TestCase):
    def test_run_gate_executes_a_bare_python_alias(self):
        rc, output = run_gate("python -c \"print('alias-ok')\"")
        self.assertEqual(rc, 0, output)
        self.assertIn("alias-ok", output)

    def test_preflight_resolves_bare_python_before_execution(self):
        argv = validate_gate("python -c pass", require_executable=False)
        self.assertEqual(argv[0], tools.resolve_python())

    def test_run_gate_reports_a_missing_interpreter_as_127(self):
        """Execution keeps the runner's vocabulary; preflight is the loud one."""
        with mock.patch.object(tools.osal, "python_exe",
                               return_value="/missing/running/python"), \
                mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.dict(os.environ, _env_without(tools.PYTHON_ENV),
                                clear=True):
            rc, output = run_gate("python -c pass")
        self.assertEqual(rc, 127)
        self.assertIn("no Python interpreter found", output)
        self.assertIn("HARNESS_PYTHON", output)

    def test_python_env_override_runs_the_real_gate(self):
        with mock.patch.object(tools.osal, "python_exe",
                               return_value="/missing/running/python"), \
                mock.patch.dict(os.environ, {tools.PYTHON_ENV: sys.executable}):
            rc, output = run_gate("python -c \"print('override-ok')\"")
        self.assertEqual(rc, 0, output)
        self.assertIn("override-ok", output)

    def test_ruff_env_override_runs_the_real_gate(self):
        with mock.patch.dict(os.environ, {tools.RUFF_ENV: sys.executable}), \
                mock.patch.object(tools.osal, "which", return_value=None):
            rc, output = run_gate("ruff -c \"print('ruff-override')\"")
        self.assertEqual(rc, 0, output)
        self.assertIn("ruff-override", output)


class BenchPython3OnlyTests(ApplyFixture):
    def test_fizz_completes_with_only_python3_on_path(self):
        """The task's bare ``python`` gate is resolved to the python3 on PATH.

        ``sys.executable`` is forced unusable so the fallback is genuinely
        exercised; without the resolver the task would fail identically on
        every round with the historical ``gate_broken``.
        """
        with tempfile.TemporaryDirectory() as td:
            task_dir = os.path.join(td, "fizz")
            shutil.copytree(os.path.join(ROOT, "bench", "tasks", "fizz"),
                            task_dir)
            tasks = load_manifest(os.path.join(task_dir, "task.json"))
            path_dir = os.path.join(td, "path")
            os.makedirs(path_dir)
            shim = _python3_shim(path_dir)
            self.assertTrue(os.path.isfile(shim))
            alias = "python.exe" if os.name == "nt" else "python"
            self.assertFalse(os.path.exists(os.path.join(path_dir, alias)),
                             "this PATH must not provide a `python` alias")
            _, _, _, engine = self.make_env(posts=[comp(FIZZ_CORRECT)],
                                            default_consent=False)
            with mock.patch.object(tools.osal, "python_exe",
                                   return_value=os.path.join(td, "missing")), \
                    mock.patch.dict(os.environ, {
                        "PATH": path_dir, "PYTHONHOME": sys.base_prefix,
                        tools.PYTHON_ENV: "", tools.RUFF_ENV: ""}):
                report = run_bench(engine, tasks)
        bench = report["bench"]
        self.assertEqual(bench["statuses"], {"ok": 1})
        self.assertEqual(bench["pass_rate"], 1.0)


class RealProcessResolutionTests(unittest.TestCase):
    """The three acceptance scenarios, across a real process boundary."""

    def test_a_python3_only_path_runs_a_bare_python_gate(self):
        with tempfile.TemporaryDirectory() as td:
            path_dir = os.path.join(td, "path")
            os.makedirs(path_dir)
            _python3_shim(path_dir)
            env = _env_without(tools.PYTHON_ENV, tools.RUFF_ENV)
            env["PATH"] = path_dir
            env["PYTHONHOME"] = sys.base_prefix
            missing = os.path.join(td, "missing")
            script = (
                f"import sys; sys.executable = {missing!r}\n"
                "from harness.gate_runner import run_gate\n"
                "rc, out = run_gate(\"python -c \\\"print('ok')\\\"\")\n"
                "print(rc, out.strip())\n")
            result = _run_child(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 ok", result.stdout)

    def test_no_interpreter_is_one_named_preflight_error(self):
        with tempfile.TemporaryDirectory() as td:
            env = _env_without(tools.PYTHON_ENV, tools.RUFF_ENV)
            env["PATH"] = td
            missing = os.path.join(td, "missing")
            script = (
                f"import sys; sys.executable = {missing!r}\n"
                "from harness.errors import HarnessError\n"
                "from harness.gate_runner import validate_gate\n"
                "try:\n"
                "    validate_gate('python -c pass')\n"
                "except HarnessError as exc:\n"
                "    print(exc); raise SystemExit(3)\n")
            result = _run_child(script, env)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("no Python interpreter found", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_environment_override_is_used_and_authoritative(self):
        with tempfile.TemporaryDirectory() as td:
            env = _env_without(tools.PYTHON_ENV, tools.RUFF_ENV)
            env["PATH"] = td
            env[tools.PYTHON_ENV] = sys.executable
            missing = os.path.join(td, "missing")
            script = (
                f"import sys; sys.executable = {missing!r}\n"
                "from harness.gate_runner import run_gate\n"
                "rc, out = run_gate(\"python -c \\\"print('override')\\\"\")\n"
                "print(rc, out.strip())\n")
            result = _run_child(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 override", result.stdout)

    def test_a_bad_override_is_a_named_startup_error(self):
        with tempfile.TemporaryDirectory() as td:
            env = _env_without(tools.PYTHON_ENV, tools.RUFF_ENV)
            env["PATH"] = td
            env[tools.PYTHON_ENV] = os.path.join(td, "no-such-interpreter")
            script = (
                "from harness.errors import HarnessError\n"
                "from harness.gate_runner import validate_gate\n"
                "try:\n"
                "    validate_gate('python -c pass')\n"
                "except HarnessError as exc:\n"
                "    print(exc); raise SystemExit(3)\n")
            result = _run_child(script, env)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("HARNESS_PYTHON", result.stdout)


class ApplyPreflightTests(ApplyFixture):
    def test_bad_python_override_refuses_before_any_model_call(self):
        target = self.make_file()
        fake, _, _, engine = self.make_env(posts=[comp(CHANGED)])
        missing = os.path.join(self.dir.name, "no-such-python")
        with mock.patch.dict(os.environ, {tools.PYTHON_ENV: missing}):
            with self.assertRaisesRegex(HarnessError, "HARNESS_PYTHON"):
                engine.apply_edit(task_id="t", file_path=target,
                                  instruction="change",
                                  verify_cmd="python -c pass",
                                  require_consent=False)
        self.assertEqual(fake.calls, [])

    def test_missing_interpreter_refuses_before_any_model_call(self):
        target = self.make_file()
        fake, _, _, engine = self.make_env(posts=[comp(CHANGED)])
        with mock.patch.object(tools.osal, "python_exe",
                               return_value="/missing/running/python"), \
                mock.patch.object(tools.osal, "which", return_value=None), \
                mock.patch.dict(os.environ, _env_without(tools.PYTHON_ENV),
                                clear=True):
            with self.assertRaisesRegex(HarnessError,
                                        "no Python interpreter found"):
                engine.apply_edit(task_id="t", file_path=target,
                                  instruction="change",
                                  verify_cmd="python -c pass",
                                  require_consent=False)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
