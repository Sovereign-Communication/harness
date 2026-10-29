"""Issue 429: a truncated judgment tail must be recovered or refused.

The reproduction: one valid row, a truncated JSON object, then a new valid
row appended. The append landed behind the damaged bytes, and the next load
returned only the first row -- so the new judgment was silently lost while
the state file appeared to accept writes. These tests pin the two permitted
outcomes: a verified recovery at a record boundary, or a refusal that writes
nothing.
"""
import json
import os
import tempfile
import unittest

from harness.repo_summary import (
    JudgmentStateError, append_judgment_row, load_judgment_rows,
)

ROW_A = {"id": "file:a", "cost": 0.0}
ROW_B = {"id": "file:b", "cost": 0.0}
TRUNCATED = '{"id": "file:c", "trunc'


class JudgmentStateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "judged.jsonl")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def _bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def test_a_complete_stream_loads(self):
        self._write(json.dumps(ROW_A) + "\n" + json.dumps(ROW_B) + "\n")
        self.assertEqual([row["id"] for row in load_judgment_rows(self.path)],
                         ["file:a", "file:b"])

    def test_blank_lines_and_a_missing_file_are_not_corruption(self):
        self._write(json.dumps(ROW_A) + "\n\n" + json.dumps(ROW_B) + "\n")
        self.assertEqual(len(load_judgment_rows(self.path)), 2)
        self.assertEqual(load_judgment_rows(None), [])
        self.assertEqual(load_judgment_rows(os.path.join(self.tmp.name, "no")),
                         [])

    def test_a_truncated_tail_is_refused_by_path_and_line(self):
        self._write(json.dumps(ROW_A) + "\n" + TRUNCATED)
        with self.assertRaises(JudgmentStateError) as ctx:
            load_judgment_rows(self.path)
        message = str(ctx.exception)
        self.assertIn(self.path, message)
        self.assertIn("line 2", message)
        self.assertIn("truncated", message)

    def test_explicit_recovery_truncates_at_the_record_boundary(self):
        self._write(json.dumps(ROW_A) + "\n" + TRUNCATED)
        report = []
        rows = load_judgment_rows(self.path, recover_truncated=True,
                                  report=report)
        self.assertEqual([row["id"] for row in rows], ["file:a"])
        self.assertEqual(report and len(report), 1)
        self.assertIn("recovered", report[0])
        # The damaged bytes are gone and the stream is readable again.
        self.assertEqual(self._bytes(), (json.dumps(ROW_A) + "\n").encode())

    def test_append_refuses_without_changing_the_file(self):
        self._write(json.dumps(ROW_A) + "\n" + TRUNCATED)
        before = self._bytes()
        with self.assertRaises(JudgmentStateError):
            append_judgment_row(self.path, ROW_B)
        self.assertEqual(self._bytes(), before)

    def test_append_recovers_then_writes_where_a_load_will_see_it(self):
        self._write(json.dumps(ROW_A) + "\n" + TRUNCATED)
        report = []
        append_judgment_row(self.path, ROW_B, recover_truncated=True,
                            report=report)
        self.assertEqual(
            [row["id"] for row in load_judgment_rows(self.path)],
            ["file:a", "file:b"])
        self.assertIn("recovered", report[0])

    def test_a_malformed_middle_row_always_fails_closed(self):
        self._write(json.dumps(ROW_A) + "\n" + "not json\n"
                    + json.dumps(ROW_B) + "\n")
        before = self._bytes()
        with self.assertRaises(JudgmentStateError) as ctx:
            load_judgment_rows(self.path, recover_truncated=True)
        self.assertIn("line 2", str(ctx.exception))
        self.assertEqual(self._bytes(), before)

    def test_a_json_value_that_is_not_a_judgment_row_is_refused(self):
        for text in ('"a string"\n', '{"no_id": true}\n', "[1, 2]\n"):
            with self.subTest(text=text):
                self._write(json.dumps(ROW_A) + "\n" + text)
                with self.assertRaises(JudgmentStateError):
                    load_judgment_rows(self.path, recover_truncated=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
