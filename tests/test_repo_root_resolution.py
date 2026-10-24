"""#327: gates and the default route pack resolve from the checkout root.

The defect was fail-open: ``compileall harness tests`` run from any other
directory exits 0 after printing ``Can't list``, so ``harness gates --run
compileall`` claimed a green gate having compiled nothing. These tests pin
both halves of the fix: every gate path is rendered against the checkout,
and the real CLI process checks that tree from inside and outside it.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import gate_runner
from harness.cli import _cmd_gates, _cmd_route
from harness.cli_parser import build_parser
from harness.config import load_settings
from harness.errors import HarnessError
from harness.gate_runner import GATE_ORDER, gate_argv, gate_command
from harness import osal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(argv):
    return build_parser().parse_args(argv)


@contextmanager
def _chdir(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _cli_env(config_dir):
    """A child environment that can import this checkout and cannot call Jev."""
    env = os.environ.copy()
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = REPO_ROOT + (
        os.pathsep + inherited if inherited else "")
    env["HARNESS_CONFIG_DIR"] = config_dir
    env["HARNESS_JEV_DISABLE"] = "1"
    return env


def _run_cli(args, cwd, config_dir):
    return subprocess.run(
        [sys.executable, "-m", "harness.cli", *args],
        cwd=cwd, env=_cli_env(config_dir), capture_output=True, text=True,
        timeout=180,
    )


class GateRegistryRootTests(unittest.TestCase):
    def test_every_file_gate_argument_is_absolute(self):
        expected = {
            "ruff": ("harness", "tests", "audits"),
            "compileall": ("harness", "tests"),
            "unittest": ("tests",),
            "audit": ("audits/self/audit.py",),
        }
        for name, relatives in expected.items():
            argv = [osal.norm_path(arg) for arg in
                    gate_argv(name, phase="HV-0")]
            for rel in relatives:
                self.assertIn(
                    osal.norm_path(os.path.join(REPO_ROOT, rel)), argv,
                    f"{name} still points at {rel!r} relative to the caller")

    def test_jev_phase_receives_the_checkout_root(self):
        argv = gate_argv("jev-phase", phase="HV-0")
        root = argv[argv.index("--repo-root") + 1]
        self.assertEqual(osal.norm_path(root), osal.norm_path(REPO_ROOT))

    def test_no_gate_keeps_a_cwd_relative_target(self):
        cwd_relative = {".", "harness", "tests", "audits",
                        "audits/self/audit.py"}
        for name in GATE_ORDER:
            argv = gate_argv(name, phase="HV-0")
            self.assertFalse(cwd_relative.intersection(argv),
                             f"{name} kept a CWD-relative target: {argv}")

    def test_the_root_can_be_pinned_for_hermetic_callers(self):
        with tempfile.TemporaryDirectory() as root:
            argv = gate_argv("compileall", repo_root=root)
        self.assertIn(osal.norm_path(os.path.join(root, "harness")),
                      [osal.norm_path(arg) for arg in argv])

    def test_commands_render_the_same_rooted_argv(self):
        for name in GATE_ORDER:
            command = gate_command(name, phase="HV-0")
            self.assertEqual(gate_runner.split_command(command),
                             gate_argv(name, phase="HV-0"))


class GateFailureTests(unittest.TestCase):
    def test_missing_targets_are_named(self):
        with tempfile.TemporaryDirectory() as root:
            spec = gate_runner.GATES["compileall"]
            self.assertEqual(spec.missing_targets(root), ["harness", "tests"])

    def test_cli_refuses_before_running_a_gate_with_missing_targets(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gate_runner, "REPO_ROOT", root), \
                    mock.patch("harness.cli.run_gate") as runner:
                with self.assertRaises(HarnessError) as ctx:
                    _cmd_gates(_parse(["gates", "--run", "compileall"]), None)
        self.assertIn("missing harness, tests", str(ctx.exception))
        runner.assert_not_called()

    def test_compileall_checks_files_when_called_from_elsewhere(self):
        """A broken file under the pinned root must fail from an outside CWD.

        ``compileall`` exits 0 on an empty/missing target list, which is the
        fail-open this issue is about. A syntax error proves the rooted argv
        actually reached files.
        """
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as outside:
            os.makedirs(os.path.join(root, "harness"))
            os.makedirs(os.path.join(root, "tests"))
            with open(os.path.join(root, "harness", "broken.py"),
                      "w", encoding="utf-8") as handle:
                handle.write("def broken(:\n")
            rc, output = gate_runner.run_gate(
                gate_argv("compileall", repo_root=root),
                cwd=outside, timeout=60)
        self.assertNotEqual(rc, 0)
        self.assertIn("broken.py", output)


class CompileallCliTests(unittest.TestCase):
    """The real console path, from both sides of the checkout boundary."""

    def test_outside_the_checkout_the_gate_checks_the_real_tree(self):
        with tempfile.TemporaryDirectory() as outside, \
                tempfile.TemporaryDirectory() as config:
            proc = _run_cli(["gates", "--run", "compileall"],
                            outside, config)
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("Can't list", combined)

    def test_inside_the_checkout_the_gate_still_passes(self):
        with tempfile.TemporaryDirectory() as config:
            proc = _run_cli(["gates", "--run", "compileall"],
                            REPO_ROOT, config)
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("Can't list", combined)


class RouteDefaultPackTests(unittest.TestCase):
    def test_help_names_the_checkout_root_not_the_current_directory(self):
        parser = build_parser()
        route = parser._subparsers._group_actions[0].choices["route"]
        pack_help = next(action.help for action in route._actions
                         if action.dest == "pack")
        self.assertIn("checkout root", pack_help)
        self.assertIn("not the current directory", pack_help)

    def test_default_pack_resolves_from_a_different_cwd_in_process(self):
        settings = load_settings({"jev_disabled": True})
        with tempfile.TemporaryDirectory() as outside:
            settings.ledger_path = os.path.join(outside, "ledger.jsonl")
            out = os.path.join(outside, "route.json")
            with _chdir(outside):
                _cmd_route(_parse(["route", "--goal", "fix a typo",
                                   "--out", out]), settings)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["pack_id"], "operator-model-route-v1")
        self.assertEqual(payload["rung_id"], "r0_scout")

    def test_default_pack_resolves_from_a_different_cwd_as_a_process(self):
        with tempfile.TemporaryDirectory() as outside, \
                tempfile.TemporaryDirectory() as config:
            out = os.path.join(outside, "route.json")
            proc = _run_cli(["route", "--goal", "fix a typo",
                             "--out", out], outside, config)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["pack_id"], "operator-model-route-v1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
