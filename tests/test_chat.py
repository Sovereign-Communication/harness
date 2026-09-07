"""Response extraction: content/cost pulls and the reasoning-only fallback."""
import unittest

from harness.chat import extract_content_and_cost
from tests._fake import comp


class ExtractionTests(unittest.TestCase):
    def test_reasoning_fallback(self):
        content, finish, cost, is_byok = extract_content_and_cost(
            comp(None, reasoning="deep thinking here"))
        self.assertIn("[NOTE]", content)
        self.assertIn("deep thinking", content)

    def test_extraction_garbage(self):
        content, finish, cost, is_byok = extract_content_and_cost({})
        self.assertIsNone(content)
        self.assertIsNone(finish)


if __name__ == "__main__":
    unittest.main()
