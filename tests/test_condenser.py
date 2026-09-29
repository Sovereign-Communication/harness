"""Unit tests for context distillation and micro-brief condensation (harness/condenser.py)."""
import unittest
from unittest import mock

from harness.condenser import (
    MicroBrief,
    condense_error_log,
    distill_context,
    extract_python_signatures,
    extract_source_evidence,
)


SAMPLE_PYTHON = """\"\"\"Sample module docstring for testing.\"\"\"
import os
import sys

MAX_COUNT = 100
DEFAULT_NAME = "test"

class Widget:
    \"\"\"Represents a test widget.\"\"\"
    def __init__(self, name: str):
        self.name = name
        print("initializing widget")

    def run(self, speed: int = 1) -> bool:
        \"\"\"Runs the widget.\"\"\"
        if speed < 0:
            return False
        return True

async def fetch_widget(widget_id: int) -> Widget:
    \"\"\"Fetches widget asynchronously.\"\"\"
    return Widget(str(widget_id))

def helper():
    # internal helper
    x = 1 + 2
    return x
"""

SAMPLE_ERROR_LOG = """
running 5 tests
test_add (tests.test_math) ... ok
test_sub (tests.test_math) ... FAIL
======================================================================
FAIL: test_sub (tests.test_math)
Traceback (most recent call last):
  File "tests/test_math.py", line 42, in test_sub
    self.assertEqual(sub(5, 3), 2)
AssertionError: 3 != 2
----------------------------------------------------------------------
Ran 2 tests in 0.010s
FAILED (failures=1)
"""


class CondenserTests(unittest.TestCase):
    def test_extract_python_signatures(self):
        sigs = extract_python_signatures(SAMPLE_PYTHON)
        self.assertIn("class Widget:", sigs)
        self.assertIn("def __init__(self, name: str): ...", sigs)
        self.assertIn("def run(self, speed: int", sigs)
        self.assertIn("-> bool: ...", sigs)
        self.assertIn("async def fetch_widget(widget_id: int) -> Widget: ...", sigs)
        self.assertIn("MAX_COUNT = 100", sigs)
        self.assertIn("DEFAULT_NAME = 'test'", sigs)
        # Function bodies should be pruned
        self.assertNotIn("print(\"initializing widget\")", sigs)
        self.assertNotIn("x = 1 + 2", sigs)

    def test_extract_python_signatures_with_focus(self):
        sigs = extract_python_signatures(SAMPLE_PYTHON, focus_symbols=["run"])
        self.assertIn("[FOCUS SYMBOL]", sigs)
        self.assertIn("Runs the widget.", sigs)

    def test_heuristic_signatures_fallback(self):
        non_python = """
        // Rust code
        pub struct Foo {
            pub bar: i32,
        }
        pub fn do_work() -> bool {
            let x = 1;
            true
        }
        """
        sigs = extract_python_signatures(non_python)
        self.assertIn("pub fn do_work()", sigs)

    def test_condense_error_log(self):
        condensed = condense_error_log(SAMPLE_ERROR_LOG)
        self.assertIn("FAIL: test_sub", condensed)
        self.assertIn("AssertionError: 3 != 2", condensed)
        self.assertIn("FAILED (failures=1)", condensed)
        # Verify empty error log
        self.assertEqual(condense_error_log(""), "")

    def test_distill_context(self):
        files = {
            "widget.py": SAMPLE_PYTHON,
            "other.rs": "pub fn hello() {}",
        }
        brief = distill_context(files, error_log=SAMPLE_ERROR_LOG, max_tokens=1500, summary="Refactor widgets")
        self.assertIsInstance(brief, MicroBrief)
        self.assertEqual(brief.summary, "Refactor widgets")
        self.assertEqual(len(brief.file_signatures), 2)
        self.assertGreater(brief.estimated_tokens, 0)
        self.assertLessEqual(brief.estimated_tokens, 1500)

        prompt_ctx = brief.to_prompt_context()
        self.assertIn("CONTEXT SUMMARY:", prompt_ctx)
        self.assertIn("CONDENSED FILE INTERFACES:", prompt_ctx)
        self.assertIn("CONDENSED ERROR / FAILURE TRACE:", prompt_ctx)

    def test_extract_source_evidence_reports_the_extractor_cap(self):
        # The seam the context brief builds on: retained text plus a flag saying
        # whether an extractor cap cut it. No truncation marker is added here --
        # that marker is part of the brief artifact, not of extraction.
        text, truncated = extract_source_evidence("widget.py", SAMPLE_PYTHON)
        self.assertFalse(truncated)
        self.assertIn("class Widget:", text)

        unparseable = "def oops(:\n    pass\n"
        text, truncated = extract_source_evidence("broken.py", unparseable)
        self.assertFalse(truncated)
        self.assertIn("def oops(:", text)

        capped = "\n".join(f"fn api_{index}() {{}}" for index in range(81))
        text, truncated = extract_source_evidence("api.rs", capped)
        self.assertTrue(truncated)
        self.assertEqual(len(text.splitlines()), 80)
        self.assertNotIn("truncated", text)

    def test_extract_source_evidence_falls_back_when_the_ast_extractor_fails(self):
        # A source that parses cleanly can still defeat the AST extractor (deep
        # unparse recursion, a tree shape ast.unparse rejects). The seam must
        # degrade to the heuristic extractor and report the cap rather than
        # propagate, so one bad source cannot take down a whole brief.
        capped = "\n".join(f"def api_{index}():\n    pass" for index in range(90))
        for error in (ValueError("unparse refused"), RecursionError("too deep")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch(
                        "harness.condenser.extract_python_signatures",
                        side_effect=error,
                    ) as patched:
                text, truncated = extract_source_evidence("api.py", capped)
            self.assertIn("def api_0():", text)
            self.assertEqual(len(text.splitlines()), 80)
            self.assertTrue(truncated)
            self.assertEqual(patched.call_count, 1)

        # The fallback is a degradation, not a silent success: the retained
        # text is the heuristic extractor's, so AST-only structure is gone.
        with mock.patch("harness.condenser.extract_python_signatures",
                        side_effect=ValueError("unparse refused")):
            text, _ = extract_source_evidence("api.py", capped)
        self.assertNotIn("...", text)
        self.assertNotIn("truncated", text)

    def test_distill_context_budget_truncation(self):
        # Stress test token budget clamping with tiny max_tokens
        files = {"large.py": SAMPLE_PYTHON * 5}
        brief = distill_context(files, error_log=SAMPLE_ERROR_LOG, max_tokens=50)
        self.assertLessEqual(brief.estimated_tokens, 150)


if __name__ == "__main__":
    unittest.main()
