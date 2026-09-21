"""Hermetic tests for the Jev phase-completion dogfood gate."""
import tempfile
import unittest
from pathlib import Path

from harness.errors import HarnessError
from harness.jev_completion import (
    PHASE_COMPLETE_MIN_SCORE,
    collect_phase_evidence,
    score_phase_completion,
)


def _write_repo(root: Path, status_line: str, tests=None, files=None):
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "jev-roadmap.md").write_text(
        "## Canonical STATUS\n\n"
        "| Track | Phase | Status | Evidence |\n"
        "|---|---|---|---|\n"
        f"{status_line}\n",
        encoding="utf-8")
    for rel in tests or []:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import unittest\n", encoding="utf-8")
    for rel in files or []:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stub\n", encoding="utf-8")


class CompletionScoreTests(unittest.TestCase):
    def test_p2_incomplete_status_cannot_mark_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| `JEV-P2-*` pillars | **in progress / repair** | PR #36 OPEN; local gates red |",
                tests=["tests/test_jev_triage.py"],
            )
            evidence = collect_phase_evidence(str(root), "JEV-P2")
            result = score_phase_completion(evidence)
            self.assertFalse(result["can_mark_complete"])
            self.assertLess(result["score"], PHASE_COMPLETE_MIN_SCORE)
            self.assertFalse(result["hard_gates"]["pr_merged"])

    def test_p2_complete_without_required_tests_fails_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| 2 Pillars `JEV-P2-*` | **complete** | PR #36; CI green |",
                tests=["tests/test_jev_triage.py"],
            )
            evidence = collect_phase_evidence(str(root), "JEV-P2")
            result = score_phase_completion(evidence)
            self.assertFalse(result["can_mark_complete"])
            self.assertFalse(result["hard_gates"]["required_tests_present"])
            self.assertTrue(any("missing required tests" in b for b in result["blockers"]))

    def test_p1_complete_with_evidence_can_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = [
                "tests/test_jev_policy.py",
                "tests/test_jev_lane_parity.py",
                "tests/test_jev_ledger_spend.py",
            ]
            _write_repo(
                root,
                "| 1 One owner `JEV-P1-*` | **complete** — PR #35 merged to origin/main `9d5ff14` | required tests + CI green |",
                tests=tests,
                files=["harness/jev_policy.py"],
            )
            evidence = collect_phase_evidence(str(root), "JEV-P1")
            result = score_phase_completion(evidence)
            self.assertTrue(result["hard_gates"]["pr_merged"])
            self.assertTrue(result["hard_gates"]["required_tests_present"])
            self.assertTrue(result["hard_gates"]["ci_green"])
            self.assertTrue(result["can_mark_complete"])
            self.assertGreaterEqual(result["score"], PHASE_COMPLETE_MIN_SCORE)

    def test_status_complete_with_blocker_markers_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = [
                "tests/test_jev_policy.py",
                "tests/test_jev_lane_parity.py",
                "tests/test_jev_ledger_spend.py",
            ]
            _write_repo(
                root,
                "| 1 One owner | **complete** — PR #35; but audit FAIL / missing tests still listed |",
                tests=tests,
                files=["harness/jev_policy.py"],
            )
            evidence = collect_phase_evidence(str(root), "JEV-P1")
            # Force the contradiction path
            evidence["open_blockers"] = ["STATUS claims complete while row still lists open/repair/fail evidence"]
            result = score_phase_completion(evidence)
            self.assertFalse(result["hard_gates"]["no_open_blockers"])
            self.assertFalse(result["can_mark_complete"])

    def test_extra_evidence_overrides_gate_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| JEV-P2 | **in progress** | PR #36 |",
                        tests=["tests/test_jev_triage.py"])
            evidence = collect_phase_evidence(
                str(root), "JEV-P2",
                extra={
                    "pr_merged": True,
                    "origin_evidence": "PR #36 merged",
                    "local_gates_green": True,
                    "ci_green": True,
                    "open_blockers": [],
                    "tests_missing": [],
                })
            # Presence of other required P2 tests still missing in repo
            result = score_phase_completion(evidence)
            # Still not complete: required P2 tests absent on disk
            self.assertFalse(result["hard_gates"]["required_tests_present"])

    def test_missing_phase_raises(self):
        with self.assertRaises(HarnessError):
            score_phase_completion({})


if __name__ == "__main__":
    unittest.main()
