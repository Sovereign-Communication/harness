"""PLAT-cmd-data: the ``harness gates`` surface is the documented entry point.

CLAUDE.md used to carry five raw command lines, each of which was only
correct on the platform that wrote it. The commands now live in
:mod:`harness.gate_runner` and this CLI surface prints or runs them, so the
document can name a gate and stay true. These tests pin the surface itself:
the listing, the JSON shape, running a gate, and the refusal for a name that
does not exist.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.cli import _DISPATCH, _cmd_gates
from harness.cli_parser import build_parser
from harness.errors import HarnessError
from harness.gate_runner import GATE_ORDER

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(argv):
    return build_parser().parse_args(argv)


class GatesCommandTest(unittest.TestCase):
    def test_command_is_registered(self):
        self.assertIn("gates", _DISPATCH)
        self.assertIs(_DISPATCH["gates"], _cmd_gates)

    def test_listing_names_every_gate(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_gates(_parse(["gates"]), None)
        text = buf.getvalue()
        for name in GATE_ORDER:
            self.assertIn(name, text)
        self.assertIn(sys.executable, text)

    def test_json_listing_is_machine_readable_and_complete(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_gates(_parse(["gates", "--json"]), None)
        payload = json.loads(buf.getvalue())
        names = [entry["name"] for entry in payload["gates"]]
        self.assertEqual(names, list(GATE_ORDER))
        for entry in payload["gates"]:
            self.assertEqual(entry["argv"][0], sys.executable)
            self.assertTrue(entry["command"])
            self.assertTrue(entry["summary"])

    def test_jev_phase_gate_shows_the_requested_phase(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_gates(_parse(["gates", "--json", "--phase", "HV-5"]), None)
        payload = json.loads(buf.getvalue())
        jev = [e for e in payload["gates"] if e["name"] == "jev-phase"][0]
        self.assertIn("HV-5", jev["argv"])
        self.assertNotIn("{phase}", " ".join(jev["argv"]))

    def test_run_executes_the_gate_and_propagates_failure(self):
        with mock.patch("harness.cli.run_gate", return_value=(0, "clean")) as runner:
            _cmd_gates(_parse(["gates", "--run", "import"]), None)
        self.assertEqual(runner.call_count, 1)
        argv = runner.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)

    def test_run_reports_a_failing_gate_as_an_error(self):
        with mock.patch("harness.cli.run_gate", return_value=(2, "lint errors")):
            with self.assertRaises(HarnessError) as ctx:
                _cmd_gates(_parse(["gates", "--run", "ruff"]), None)
        self.assertIn("exit code 2", str(ctx.exception))

    def test_run_emits_output_for_the_human_to_read(self):
        stderr = io.StringIO()
        with mock.patch("harness.cli.run_gate", return_value=(0, "All checks passed!")):
            with mock.patch("sys.stderr", stderr):
                _cmd_gates(_parse(["gates", "--run", "ruff"]), None)
        self.assertIn("All checks passed!", stderr.getvalue())

    def test_unknown_gate_is_refused_with_the_known_names(self):
        with self.assertRaises(HarnessError) as ctx:
            _cmd_gates(_parse(["gates", "--run", "not-a-gate"]), None)
        message = str(ctx.exception)
        self.assertIn("not-a-gate", message)
        for name in GATE_ORDER:
            self.assertIn(name, message)

    def test_timeout_is_honored(self):
        with mock.patch("harness.cli.run_gate", return_value=(0, "")) as runner:
            _cmd_gates(_parse(["gates", "--run", "compileall", "--timeout", "12"]), None)
        self.assertEqual(runner.call_args[1]["timeout"], 12)

    def test_real_end_to_end_run_of_the_cheapest_gate(self):
        """One real subprocess through the real command: the smoke proof."""
        _cmd_gates(_parse(["gates", "--run", "import"]), None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
