"""The flush report and the adoption page must not drift apart.

`docs/audit/ISSUE_FLUSH_2026-11-05.md` is the lane's dated flush of every open
issue; `docs/audit/DISSEMINATION_ADOPTION_2026-11-05.md` is the adopter-facing
summary of it. A summary that disagrees with its own report is worse than no
summary, and a report whose prose totals disagree with its table cannot be
audited. These pins are structural: they check the tables, the counts, and the
presence of a read on every row. They do not police wording, and they do not
pretend to re-read GitHub (the suite is hermetic).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLUSH = ROOT / "docs" / "audit" / "ISSUE_FLUSH_2026-11-05.md"
ADOPTION = ROOT / "docs" / "audit" / "DISSEMINATION_ADOPTION_2026-11-05.md"
LEDGER = ROOT / "docs" / "audit" / "BURNDOWN_LEDGER_2026-11-03.md"

#: (disposition prefix, label used in the report's totals, adoption label)
CLASSES = (
    ("landed on lane", "landed on lane", "landed on lane (unmerged)"),
    ("landed-partial", "landed-partial", "landed-partial"),
    ("owner-only", "owner-only", "owner-only"),
    ("still-open", "still-open", "still-open"),
    ("tracker (stays open)", "tracker", "tracker"),
)

ISSUE_ROW = re.compile(r"^\| (\d{3}) \| .* \| ([a-z][^|]*) \| .* \| (.+?) \|$")


def _flush():
    return FLUSH.read_text(encoding="utf-8")


def _issue_rows(text):
    section = text.split("## Dispositions (all open issues)", 1)[1]
    section = section.split("## Tracker items the lane owes", 1)[0]
    rows = []
    for line in section.splitlines():
        match = ISSUE_ROW.match(line.strip())
        if match:
            rows.append((int(match.group(1)), match.group(2).strip(),
                         match.group(3).strip()))
    return rows


def _class_of(disposition):
    for prefix, _flush_label, _adoption_label in CLASSES:
        if disposition.startswith(prefix):
            return prefix
    return None


class FlushReportTests(unittest.TestCase):

    def test_the_report_exists_and_carries_its_read_stack(self):
        text = _flush()
        for code in ("R1", "R2", "R3", "R4", "R5"):
            self.assertIn(f"| {code} |", text)
        self.assertIn("--paginate", text)

    def test_every_open_issue_has_exactly_one_row_with_a_read(self):
        rows = _issue_rows(_flush())
        numbers = [row[0] for row in rows]
        self.assertEqual(len(numbers), len(set(numbers)), "duplicate issue rows")
        self.assertEqual(len(rows), 80, "the flush must cover all 80 open issues")
        for number, disposition, read in rows:
            with self.subTest(issue=number):
                self.assertIsNotNone(_class_of(disposition),
                                     f"unknown disposition {disposition!r}")
                self.assertTrue(read.strip(), "row carries no read")

    def test_the_totals_line_matches_the_table(self):
        text = _flush()
        rows = _issue_rows(text)
        counts = {}
        for _number, disposition, _read in rows:
            key = _class_of(disposition)
            counts[key] = counts.get(key, 0) + 1
        totals = re.search(r"^Totals: (\d+) open issues \| (.+)$", text, re.M)
        self.assertIsNotNone(totals, "missing the totals line")
        self.assertEqual(int(totals.group(1)), len(rows))
        declared = {}
        for part in totals.group(2).split("|"):
            name, value = part.strip().split("=")
            declared[name] = int(value)
        expected = {}
        for prefix, flush_label, _adoption_label in CLASSES:
            if counts.get(prefix):
                expected[flush_label] = counts[prefix]
        self.assertEqual(declared, expected)

    def test_the_report_states_the_rail_is_locked_and_asserts_no_green(self):
        text = _flush()
        section = text.split("## What this report does not claim", 1)[1]
        self.assertIn("**No CI-green claim.**", section)
        self.assertIn("billing-locked", section)
        self.assertIn("no check-run evidence", section)

    def test_no_issue_is_reported_as_closed(self):
        text = _flush()
        self.assertIn("The count of issues closed by this report is zero.", text)

    def test_the_disagreements_section_names_both_sides(self):
        text = _flush()
        section = text.split("## Where the recorded state and the repo disagree", 1)[1]
        for named in ("49 seam failures", "#301", "57 -> 54 -> 53",
                      "REFINE-LOOP", "617e6c6"):
            with self.subTest(named=named):
                self.assertIn(named, section)
        self.assertIn("Checked and found consistent", section)


class AdoptionPageTests(unittest.TestCase):

    def _text(self):
        return ADOPTION.read_text(encoding="utf-8")

    def test_the_register_is_named_and_actually_exists(self):
        text = self._text()
        self.assertIn("BURNDOWN_LEDGER_2026-11-03.md", text)
        self.assertTrue(LEDGER.is_file())
        self.assertIn("Assumption register (R5", LEDGER.read_text(encoding="utf-8"))

    def test_the_adoption_counts_equal_the_flush_report_counts(self):
        flush_rows = _issue_rows(_flush())
        counted = {}
        for _number, disposition, _read in flush_rows:
            key = _class_of(disposition)
            counted[key] = counted.get(key, 0) + 1
        text = self._text()
        for prefix, _flush_label, adoption_label in CLASSES:
            count = counted.get(prefix, 0)
            if not count:
                continue
            with self.subTest(label=adoption_label):
                self.assertIn(f"| {adoption_label} | {count} |", text)
        self.assertIn(f"| **total open issues** | **{len(flush_rows)}** |", text)

    def test_the_page_states_what_was_not_adopted(self):
        text = self._text()
        section = text.split("## 6. What is explicitly NOT adopted", 1)[1]
        for named in ("FLUSH-0..FLUSH-3", "S3 calibration", "TM1/TM2/TM3",
                      "instrument amendment", "CI-green"):
            with self.subTest(named=named):
                self.assertIn(named, section)

    def test_r7_is_not_claimed_without_a_non_author_read(self):
        text = self._text()
        self.assertIn("**not adopted here.**", text)
        self.assertIn("No non-author conformance read", text)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
