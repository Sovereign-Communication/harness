"""Regression coverage for the Driver's empty-goal submit path."""

import re
import unittest
from pathlib import Path


UI = Path(__file__).resolve().parents[1] / "harness" / "ui" / "panes.js"


class DriverEmptyGoalTests(unittest.TestCase):
    def test_blank_or_whitespace_goal_stops_before_starting_a_run(self):
        src = UI.read_text(encoding="utf-8")
        match = re.search(
            r"driveForm\.onsubmit = async \(ev\) => \{([\s\S]*?)\n  \};", src)
        self.assertIsNotNone(match, "Driver submit handler must exist")
        handler = match.group(1)

        guard = handler.index("if (!goal)")
        self.assertLess(guard, handler.index("driveBtn.disabled = true"))
        self.assertLess(guard, handler.index('postJson("/api/runs"'))
        self.assertIn("const goal = goalInput.value.trim()", handler)
        self.assertIn('role: "alert"', handler)
        self.assertIn("goalInput.focus()", handler)
        self.assertIn("        goal,", handler)
        self.assertNotIn("goalInput.value.trim() ||", handler)


if __name__ == "__main__":
    unittest.main()
