"""B6 stale-reference test for the agent-desktop audit report (2026-11-01).

The audit session died before it could write this guard, so the report's claims
were only as fresh as the reader. This test makes staleness a suite failure:
every cited `path:line` must exist in this checkout, all eight launch-gate
fields must stay in the Part C mapping, placeholders may not return, and each
Part D amendment must still name a file that exists.

It is deliberately hermetic and repo-local: it checks the report against the
tree, never against the network or a desktop session.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "agent-desktop-audit-2026-11-01.md"

FIELDS = (
    "app_identity", "backend", "model", "thinking",
    "repo", "build_mode", "mission_queued", "start_state",
)

# path:line references the report leans on, with the token that must appear on
# that line. A rename or a shifted claim fails here instead of aging quietly.
ANCHORS = {
    ("driver_core/actions.py", 167): "focus",
    ("driver_core/actions.py", 169): "click",
    ("driver_core/actions.py", 171): "type_text",
    ("harness/perception_client.py", 183): "def health",
    ("pyproject.toml", 40): "harness-desktop",
    ("docs/driver.md", 355): "Headless key provisioning",
    ("docs/driver.md", 368): "Which surfaces are keyless",
}

PLACEHOLDERS = ("TBD", "TODO", "FIXME", "unconfirmed", "to be written")


def _text():
    return REPORT.read_text(encoding="utf-8")


class ReportExistsTests(unittest.TestCase):
    def test_report_file_exists_and_is_not_empty(self):
        self.assertTrue(REPORT.is_file(), f"missing {REPORT}")
        self.assertGreater(len(_text().split()), 200)


class PartCMappingTests(unittest.TestCase):
    def test_all_eight_launch_gate_fields_are_mapped(self):
        text = _text()
        for field in FIELDS:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", text)

    def test_mapping_states_the_measured_ax_verdict(self):
        text = _text()
        self.assertIn("8 of 8 fields absent", text)
        self.assertIn("13 nodes", text)

    def test_mapping_does_not_claim_the_fields_are_modelled(self):
        # The honest finding is that no field name exists in the repo; the
        # report must say so rather than implying a schema owner.
        self.assertIn("None of the eight field names is modelled", _text())


class StaleReferenceTests(unittest.TestCase):
    def test_every_cited_path_and_line_exists(self):
        text = _text()
        cited = set(re.findall(r"([A-Za-z0-9_./-]+\.(?:py|md|json|toml)):(\d+)", text))
        self.assertTrue(cited, "report cites no path:line references")
        for path, line in sorted(cited):
            with self.subTest(path=path, line=line):
                target = ROOT / path
                self.assertTrue(target.is_file(), f"{path} does not exist")
                lines = target.read_text(encoding="utf-8").splitlines()
                self.assertLessEqual(int(line), len(lines),
                                     f"{path}:{line} is past EOF ({len(lines)})")

    def test_anchored_lines_still_carry_their_claim(self):
        for (path, line), token in ANCHORS.items():
            with self.subTest(path=path, line=line):
                lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
                self.assertIn(token, lines[line - 1])

    def test_no_placeholder_markers_remain(self):
        lowered = _text().lower()
        for marker in PLACEHOLDERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker.lower(), lowered)


class PartDTargetTests(unittest.TestCase):
    def test_each_amendment_names_an_existing_target(self):
        text = _text()
        for target in ("docs/jev-roadmap.md", "docs/driver.md"):
            with self.subTest(target=target):
                self.assertIn(target, text)
                self.assertTrue((ROOT / target).is_file())

    def test_amendments_are_explicitly_proposed_not_applied(self):
        text = _text()
        self.assertIn("proposed, not applied", text)
        self.assertIn("Nothing in `docs/` was amended by this pass", text)


class EvidenceHonestyTests(unittest.TestCase):
    def test_recorded_measurements_are_labelled(self):
        text = _text()
        self.assertIn("recorded 2026-11-01", text)
        self.assertIn("not re-run", text)

    def test_report_does_not_claim_a_vision_call_was_made(self):
        self.assertIn("No vision (Jev) call was made", _text())


if __name__ == "__main__":
    unittest.main()
