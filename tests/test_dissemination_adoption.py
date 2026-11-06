"""The flush report and the adoption page must not drift apart.

`docs/audit/ISSUE_FLUSH_2026-11-05.md` is the lane's dated flush of every open
issue; `docs/audit/DISSEMINATION_ADOPTION_2026-11-05.md` is the adopter-facing
summary of it. A summary that disagrees with its own report is worse than no
summary, and a report whose prose totals disagree with its table cannot be
audited. These pins are structural: they check the tables, the counts, and that
every row carries its evidence. They do not police wording, and they do not
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
    ("landed on main", "landed on main", "landed on main (merged)"),
    ("landed-partial", "landed-partial", "landed-partial"),
    ("owner-only", "owner-only", "owner-only"),
    ("still-open", "still-open", "still-open"),
    ("tracker (stays open)", "tracker", "tracker"),
)

#: `| # | issue | owed | disposition | artifact | proof | state |`
ISSUE_ROW = re.compile(
    r"^\| (\d{3}) \| (?P<issue>.*?) \| (?P<owed>.*?) "
    r"\| (?P<disposition>[a-z][^|]*) \| (?P<artifact>.*?) "
    r"\| (?P<proof>.*?) \| (?P<state>.+?) \|$")


def _flush():
    return FLUSH.read_text(encoding="utf-8")


def _issue_rows(text):
    section = text.split("## Dispositions (all open issues)", 1)[1]
    section = section.split("## Tracker items the lane owes", 1)[0]
    rows = []
    for line in section.splitlines():
        match = ISSUE_ROW.match(line.strip())
        if match:
            rows.append({
                "number": int(match.group(1)),
                "issue": match.group("issue").strip(),
                "owed": match.group("owed").strip(),
                "disposition": match.group("disposition").strip(),
                "artifact": match.group("artifact").strip(),
                "proof": match.group("proof").strip(),
                "state": match.group("state").strip(),
            })
    return rows


def _class_of(disposition):
    for prefix, _flush_label, _adoption_label in CLASSES:
        if disposition.startswith(prefix):
            return prefix
    return None


class FlushReportTests(unittest.TestCase):

    def test_the_report_exists_and_carries_its_read_stack(self):
        text = _flush()
        for code in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9",
                     "R10"):
            self.assertIn(f"| {code} |", text)
        self.assertIn("--paginate", text)

    def test_every_open_issue_has_exactly_one_row_matching_the_live_population(self):
        rows = _issue_rows(_flush())
        numbers = [row["number"] for row in rows]
        self.assertEqual(len(numbers), len(set(numbers)), "duplicate issue rows")
        self.assertEqual(len(rows), 80, "the flush must cover all 80 open issues")
        for row in rows:
            with self.subTest(issue=row["number"]):
                self.assertIsNotNone(_class_of(row["disposition"]),
                                     f"unknown disposition {row['disposition']!r}")
                self.assertTrue(row["issue"], "row names no issue")
                self.assertTrue(row["owed"], "row states no owed deliverable")

    def test_every_row_carries_an_artifact_a_proof_and_a_state(self):
        # The point of the ninth pass: a row is evidence, not a label. Each one
        # must say what the lane's artifact is, how it was proven, and what the
        # issue itself said when the report was written.
        for row in _issue_rows(_flush()):
            with self.subTest(issue=row["number"]):
                self.assertTrue(row["artifact"], "row carries no artifact cell")
                self.assertTrue(row["proof"], "row carries no proof cell")
                self.assertIn("R", row["proof"], "proof cites no read")
                self.assertRegex(row["state"], r"^(open|closed) \(R7",
                                 "state does not name the live read")
                self.assertIn(" - ", row["state"],
                              "state does not say whether to close")

    def test_a_landed_row_cites_the_lane_and_a_commit(self):
        for row in _issue_rows(_flush()):
            if _class_of(row["disposition"]) not in ("landed on lane",
                                                     "landed-partial"):
                continue
            with self.subTest(issue=row["number"]):
                self.assertIn("lane/burndown", row["artifact"])
                self.assertRegex(row["artifact"], r"`[0-9a-f]{7,40}`")

    def test_the_totals_line_matches_the_table(self):
        text = _flush()
        rows = _issue_rows(text)
        counts = {}
        for row in rows:
            key = _class_of(row["disposition"])
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
        for row in flush_rows:
            key = _class_of(row["disposition"])
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

    def test_every_remedy_declares_a_status_and_an_artifact_or_a_request(self):
        text = self._text()
        section = text.split("## 4. Remedies adopted", 1)[1]
        for code in ("R1", "R4", "R5", "R7"):
            with self.subTest(remedy=code):
                self.assertIn(f"| {code} —", section)
        self.assertIn("adopted", section)
        self.assertIn("STEERING REQUEST", section)

    def test_r7_is_not_claimed_beyond_its_evidence(self):
        text = self._text()
        # One non-author re-read of the flush report exists (R6); nothing
        # non-author has graded the code deliverables, so R7 stays partial.
        self.assertIn("partially adopted", text)
        self.assertIn("No non-author verdict covers the queue launcher", text)
        self.assertIn("SR-5", text)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
