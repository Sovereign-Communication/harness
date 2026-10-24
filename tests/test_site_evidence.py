"""Rendered site evidence: the DOM facts an audit is allowed to cite."""
import unittest

from harness.errors import HarnessError
from harness.site_evidence import evidence_summary, render_site_evidence


class EvidenceSummaryTests(unittest.TestCase):
    def test_progress_modes_and_driven_actions_are_reported(self):
        summary = evidence_summary({
            "engine": "chromium headless",
            "final_url": "https://example.test/",
            "title": "Example",
            "progress": [{"id": "guidedProgress",
                          "class": "guided-progress",
                          "text": "4. Your result"}],
            "modes": [{"label": "Off-grid / Backup", "class": "active"}],
            "actions": [{"target": "Off-grid / Backup",
                         "result": "class=active"}],
            "steps": [{"step": "1. Location", "status": "initial"},
                      {"step": "4. Your result", "status": "done"}],
            "status": "ready",
            "result_excerpt": "Best pick",
            "input_count": 12,
            "screenshot": "shot.png",
            "text": "body",
        })
        self.assertIn("guidedProgress='4. Your result'", summary)
        self.assertIn("Off-grid / Backup -> class=active", summary)
        self.assertIn("1. Location (initial)", summary)
        self.assertIn("Screenshot: shot.png", summary)

    def test_plain_http_is_refused_before_a_browser_starts(self):
        with self.assertRaises(HarnessError):
            render_site_evidence("http://example.test/")


if __name__ == "__main__":
    unittest.main()
