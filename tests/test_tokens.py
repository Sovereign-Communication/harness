"""Token estimation honesty: dense code must not be undercounted."""
import unittest

from harness.tokens import estimate_prompt_tokens


class TokenEstimateTests(unittest.TestCase):
    def test_symbol_dense_text_not_undercounted(self):
        """Regression: words*1.5 undercounts symbol-dense source; chars/4 must
        dominate there so preflight ceilings stay honest."""
        code = "{" * 300 + "x" + "}" * 300  # 1 'word', 601 chars
        est = estimate_prompt_tokens(code)
        self.assertGreaterEqual(est, len(code) // 4,
                                "chars/4 floor must apply to dense code")
        prose = " ".join(["word"] * 300)
        self.assertGreater(estimate_prompt_tokens(prose), 400)

    def test_empty_and_prose(self):
        self.assertEqual(estimate_prompt_tokens(""), 50)
        self.assertGreater(estimate_prompt_tokens("hello world"), 0)


if __name__ == "__main__":
    unittest.main()
