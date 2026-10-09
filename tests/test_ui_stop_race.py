"""Run the focused browser-side Stop race regression under Node."""
import shutil
import subprocess
import unittest
from pathlib import Path


class UiStopRaceTests(unittest.TestCase):
    def test_stop_clicked_before_run_id_is_sent_after_creation(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("optional deps: Node.js is not installed")
        test_file = Path(__file__).with_name("ui_stop_race.test.cjs")
        result = subprocess.run(
            [node, "--test", str(test_file)], capture_output=True, text=True,
            timeout=20, check=False)
        self.assertEqual(result.returncode, 0,
                         result.stdout + "\n" + result.stderr)


if __name__ == "__main__":
    unittest.main()
