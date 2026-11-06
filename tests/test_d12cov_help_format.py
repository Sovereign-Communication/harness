"""The CLI help surface renders identically at any terminal width.

Regression pin for the pipeline-7 finding: argparse sizes its help
layout from the ambient terminal (COLUMNS, then the tty), so the
economics parser-surface test saw the receipt filename hyphen-split
on the one CI leg whose job exported a narrow COLUMNS, while every
other leg passed. build_parser pins the layout via
_FixedWidthHelpFormatter; these tests prove the pin holds at widths
on both sides of the layout, through the real parser.
"""
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness.cli_parser import build_parser


def _economics_help():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        try:
            build_parser().parse_args(["economics", "--help"])
        except SystemExit:
            pass
    return buffer.getvalue()


class HelpFormatWidthTests(unittest.TestCase):
    def test_help_identical_across_terminal_widths(self):
        renders = []
        for width in ("25", "80", "200"):
            with mock.patch.dict(os.environ, {"COLUMNS": width}):
                renders.append(_economics_help())
        self.assertEqual(renders[0], renders[1])
        self.assertEqual(renders[1], renders[2])

    def test_asserted_phrases_survive_at_narrow_width(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "25"}):
            text = _economics_help()
        self.assertIn("EV0A_DISCOUNT_SEMANTICS.json", text)
        self.assertIn("repo evidence", text)
        self.assertNotIn("~/.config", text)
