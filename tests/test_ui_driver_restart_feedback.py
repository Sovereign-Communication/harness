"""Regression coverage for Driver restart failures in the desktop UI."""

import re
import unittest
from pathlib import Path


UI = Path(__file__).resolve().parents[1] / "harness" / "ui" / "panes.js"


class DriverRestartFeedbackTests(unittest.TestCase):
    def test_restart_failures_are_inline_and_do_not_block_the_pane(self):
        src = UI.read_text(encoding="utf-8")
        match = re.search(
            r"startBtn\.onclick = async \(\) => \{[\s\S]*?\n      \};", src)
        self.assertIsNotNone(match)
        handler = match.group(0)

        self.assertNotIn("alert(", handler)
        self.assertIn('aspectHost.querySelector(".driver-start-error")?.remove()', handler)
        self.assertIn('role: "alert"', handler)
        self.assertIn("aspectHost.append", handler)
        self.assertIn("Driver restart failed:", handler)
        self.assertIn("finally {", handler)
        self.assertIn("startBtn.disabled = false;", handler)


if __name__ == "__main__":
    unittest.main()
