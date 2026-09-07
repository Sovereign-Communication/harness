"""Readiness-marker and content-extraction parsing (harness/prompts.py)."""
import unittest

from harness.prompts import _parse_ready, _extract_file_content


class MarkerLeakTests(unittest.TestCase):
    """Regression (live playtest, openrouter/free): the readiness marker was
    emitted after blank lines and leaked verbatim into the written file."""

    def test_ready_marker_after_blank_lines_is_parsed_and_stripped(self):
        body = '\n\nHARNESS_READY: confident\n"""doc"""\ndef f():\n    pass\n'
        decision, reason, rest = _parse_ready(body)
        self.assertEqual(decision, "confident")
        self.assertNotIn("HARNESS_READY", rest)
        self.assertNotIn("HARNESS_READY", _extract_file_content(rest))

    def test_ready_marker_anywhere_never_lands_in_file(self):
        for placement in (
            'HARNESS_READY: confident\ncode\n',
            '\n\n\nHARNESS_READY: confident\ncode\n',
            'code before marker\nHARNESS_READY: confident\nmore code\n',
        ):
            content = _extract_file_content(placement)
            self.assertNotIn("HARNESS_READY", content,
                             f"marker leaked for {placement!r}")

    def test_no_marker_unchanged_behavior(self):
        decision, _, rest = _parse_ready("plain code\n")
        self.assertEqual(decision, "missing")
        self.assertEqual(rest, "plain code\n")
        self.assertEqual(_extract_file_content("```python\ncode\n```\n"), "code\n")


if __name__ == "__main__":
    unittest.main()
