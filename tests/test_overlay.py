"""Overlay verification (#434): compose a tree and run one suite against it.

The contract is small and worth pinning exactly: the base is never written to,
the overlay really is what the command sees, a red suite is a reported result
rather than an exception, and the refusals (missing trees, a string command, a
cwd that escapes the scratch, a symlinked overlay entry) happen before any
process starts.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness.cli_parser import build_parser
from harness.errors import HarnessError
from harness.overlay import run_overlay

CHECK = """import pathlib
import sys
expected = sys.argv[1]
actual = pathlib.Path("value.txt").read_text(encoding="utf-8").strip()
print("expected=" + expected + " actual=" + actual)
raise SystemExit(0 if actual == expected else 1)
"""


class _Trees(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.dir.name, "base")
        self.overlay = os.path.join(self.dir.name, "overlay")
        os.makedirs(self.base)
        os.makedirs(self.overlay)
        self._write(self.base, "value.txt", "base\n")
        self._write(self.base, "check.py", CHECK)

    def tearDown(self):
        self.dir.cleanup()

    @staticmethod
    def _write(root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


class CompositionTests(_Trees):
    def test_the_overlay_is_what_the_command_sees(self):
        """Base alone is red; the same command on the composed tree is green.

        This is the whole point of an overlay: the suite must be running
        against the change, not against the shipped tree with a hopeful
        report."""
        bare = run_overlay(self.base, self.overlay,
                           [sys.executable, "check.py", "overlay"])
        self.assertFalse(bare["ok"], bare["stdout"])
        self.assertEqual(bare["returncode"], 1)

        self._write(self.overlay, "value.txt", "overlay\n")
        composed = run_overlay(self.base, self.overlay,
                               [sys.executable, "check.py", "overlay"])
        self.assertTrue(composed["ok"], composed["stdout"] + composed["stderr"])
        self.assertEqual(composed["returncode"], 0)
        self.assertEqual(composed["overlay_files"], 1)
        self.assertIn("actual=overlay", composed["stdout"])

    @staticmethod
    def _read(path):
        # Closed explicitly: the suite runs with ResourceWarning as an error,
        # and an unclosed read here would fail the whole battery.
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_base_tree_is_never_written_to(self):
        value = os.path.join(self.base, "value.txt")
        before = self._read(value)
        self._write(self.overlay, "value.txt", "overlay\n")
        run_overlay(self.base, self.overlay,
                    [sys.executable, "check.py", "overlay"])
        self.assertEqual(self._read(value), before)

    def test_a_red_suite_is_a_result_not_an_exception(self):
        payload = run_overlay(self.base, self.overlay,
                              [sys.executable, "-c", "raise SystemExit(3)"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], 3)
        self.assertFalse(payload["timed_out"])

    def test_cwd_is_interpreted_inside_the_composed_tree(self):
        self._write(self.base, "pkg/check.py", CHECK)
        self._write(self.base, "pkg/value.txt", "base\n")
        self._write(self.overlay, "pkg/value.txt", "overlay\n")
        payload = run_overlay(self.base, self.overlay,
                              [sys.executable, "check.py", "overlay"],
                              cwd="pkg")
        self.assertTrue(payload["ok"], payload["stdout"] + payload["stderr"])
        self.assertEqual(payload["cwd"], os.path.join("pkg"))

    def test_scratch_is_removed_unless_kept(self):
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            made.append(path)
            return path

        with mock.patch("harness.overlay.tempfile.mkdtemp",
                        side_effect=recording):
            payload = run_overlay(self.base, self.overlay,
                                  [sys.executable, "-c", "pass"])
        self.assertIsNone(payload["scratch"])
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))

        kept = run_overlay(self.base, self.overlay,
                           [sys.executable, "-c", "pass"], keep_scratch=True)
        self.addCleanup(shutil.rmtree, kept["scratch"], True)
        self.assertTrue(os.path.isdir(kept["scratch"]))


class RefusalTests(_Trees):
    def test_a_missing_tree_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "base is not a directory"):
            run_overlay(os.path.join(self.dir.name, "nope"), self.overlay,
                        [sys.executable, "-c", "pass"])
        with self.assertRaisesRegex(HarnessError, "overlay is not a directory"):
            run_overlay(self.base, os.path.join(self.dir.name, "nope"),
                        [sys.executable, "-c", "pass"])

    def test_a_string_command_is_refused_before_anything_runs(self):
        with self.assertRaisesRegex(HarnessError, "argv list, not a string"):
            run_overlay(self.base, self.overlay, "python -c pass")

    def test_an_empty_command_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "command is empty"):
            run_overlay(self.base, self.overlay, [])

    def test_a_cwd_that_escapes_the_scratch_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "escapes the composed tree"):
            run_overlay(self.base, self.overlay,
                        [sys.executable, "-c", "pass"], cwd="..")

    def test_a_symlinked_overlay_entry_is_refused(self):
        link = os.path.join(self.overlay, "sneaky.txt")
        try:
            os.symlink(os.path.join(self.base, "value.txt"), link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available here")
        with self.assertRaisesRegex(HarnessError, "is a symlink"):
            run_overlay(self.base, self.overlay,
                        [sys.executable, "-c", "pass"])


class CliSurfaceTests(_Trees):
    def test_the_flag_parses_against_the_real_parser(self):
        opts = build_parser().parse_args(
            ["overlay", "--base", "b", "--overlay", "o",
             "--", "python", "-V"])
        self.assertEqual(opts.base, "b")
        self.assertEqual(opts.overlay, "o")
        self.assertEqual(list(opts.command)[-2:], ["python", "-V"])

    def test_the_handler_reports_the_payload_and_the_exit_status(self):
        from harness.cli import _cmd_overlay
        self._write(self.overlay, "value.txt", "overlay\n")
        report = os.path.join(self.dir.name, "report.json")
        opts = build_parser().parse_args(
            ["overlay", "--base", self.base, "--overlay", self.overlay,
             "--out", report, "--", sys.executable, "check.py", "overlay"])
        with redirect_stdout(__import__("io").StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                _cmd_overlay(opts, mock.Mock())
        self.assertEqual(ctx.exception.code, 0)
        with open(report, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["returncode"], 0)
        self.assertEqual(payload["overlay_files"], 1)

    def test_a_red_run_exits_one(self):
        from harness.cli import _cmd_overlay
        opts = build_parser().parse_args(
            ["overlay", "--base", self.base, "--overlay", self.overlay,
             "--", sys.executable, "-c", "raise SystemExit(3)"])
        with redirect_stdout(__import__("io").StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                _cmd_overlay(opts, mock.Mock())
        self.assertEqual(ctx.exception.code, 1)
