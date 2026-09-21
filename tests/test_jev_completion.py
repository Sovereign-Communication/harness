"""Hermetic tests for the Jev phase-completion dogfood gate."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness.errors import HarnessError
from harness.jev_completion import (
    PHASE_COMPLETE_MIN_SCORE,
    collect_phase_evidence,
    dogfood_phase,
    load_evidence_file,
    score_phase_completion,
)
from harness import cli as harness_cli


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

    def test_dogfood_phase_local_only_and_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = [
                "tests/test_jev_policy.py",
                "tests/test_jev_lane_parity.py",
                "tests/test_jev_ledger_spend.py",
            ]
            _write_repo(
                root,
                "| 1 One owner `JEV-P1-*` | **complete** — PR #35 MERGED to origin/main `9d5ff14` | tests + CI green |",
                tests=tests,
                files=["harness/jev_policy.py"],
            )
            result = dogfood_phase(str(root), "P1", use_live_jev=False)
            self.assertTrue(result["can_mark_complete"])
            ev_path = Path(tmp) / "ev.json"
            ev_path.write_text(json.dumps({"local_gates_green": True}), encoding="utf-8")
            loaded = load_evidence_file(str(ev_path))
            self.assertTrue(loaded["local_gates_green"])
            result2 = dogfood_phase(str(root), "JEV-P1", evidence_path=str(ev_path),
                                    use_live_jev=False)
            self.assertTrue(result2["can_mark_complete"])

    def test_cli_jev_phase_exit_codes_and_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = [
                "tests/test_jev_policy.py",
                "tests/test_jev_lane_parity.py",
                "tests/test_jev_ledger_spend.py",
            ]
            _write_repo(
                root,
                "| 1 One owner `JEV-P1-*` | **complete** — PR #35 MERGED `9d5ff14` | CI green |",
                tests=tests,
                files=["harness/jev_policy.py"],
            )
            out = str(Path(tmp) / "phase.json")
            stdout = io.StringIO()
            with patch("harness.config.load_settings", return_value=None):
                with redirect_stdout(stdout):
                    harness_cli.main([
                        "jev-phase", "--phase", "JEV-P1", "--repo-root", str(root),
                        "--local-only", "--json", "--out", out,
                    ])
            self.assertTrue(Path(out).is_file())
            payload = json.loads(Path(out).read_text(encoding="utf-8"))
            self.assertTrue(payload["can_mark_complete"])
            self.assertGreaterEqual(payload["score"], PHASE_COMPLETE_MIN_SCORE)

            _write_repo(
                root,
                "| 2 Pillars | **in progress / repair** | PR #36 OPEN |",
                tests=["tests/test_jev_triage.py"],
            )
            err = io.StringIO()
            out2 = str(Path(tmp) / "p2.json")
            with patch("harness.config.load_settings", return_value=None):
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        harness_cli.main([
                            "jev-phase", "--phase", "JEV-P2", "--repo-root", str(root),
                            "--local-only", "--out", out2,
                        ])
            self.assertNotEqual(ctx.exception.code, 0)
            self.assertTrue(Path(out2).is_file())

    def test_cli_jev_phase_human_text_path_and_json_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = [
                "tests/test_jev_policy.py",
                "tests/test_jev_lane_parity.py",
                "tests/test_jev_ledger_spend.py",
            ]
            _write_repo(
                root,
                "| 1 One owner `JEV-P1-*` | **complete** — PR #35 MERGED `9d5ff14` | CI green |",
                tests=tests,
                files=["harness/jev_policy.py"],
            )
            # Non-JSON human path (prints phase/score/hard_gates/semantic).
            stdout = io.StringIO()
            with patch("harness.config.load_settings", return_value=None):
                with redirect_stdout(stdout):
                    harness_cli.main([
                        "jev-phase", "--phase", "JEV-P1", "--repo-root", str(root),
                        "--local-only", "--min-score", "85",
                    ])
            text = stdout.getvalue()
            self.assertIn("phase=JEV-P1", text)
            self.assertIn("hard_gates:", text)
            self.assertIn("semantic:", text)

            # Incomplete phase: blockers printed + HarnessError exit.
            _write_repo(
                root,
                "| 2 Pillars | **in progress / repair** | PR #36 OPEN; audit red |",
                tests=["tests/test_jev_triage.py"],
            )
            err_out = io.StringIO()
            with patch("harness.config.load_settings", return_value=None):
                with redirect_stdout(io.StringIO()), redirect_stderr(err_out):
                    with self.assertRaises(SystemExit):
                        harness_cli.main([
                            "jev-phase", "--phase", "JEV-P2", "--repo-root", str(root),
                            "--local-only",
                        ])

    def test_load_evidence_file_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.json")
            with self.assertRaises(HarnessError):
                load_evidence_file(missing)
            bad = Path(tmp) / "bad.json"
            bad.write_text("[1,2]", encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_evidence_file(str(bad))

    def test_jev_semantic_score_with_mock_policy(self):
        class _FakeResult:
            def __init__(self):
                self.answers = {"phase_evidence_quality": {"score": 0.92}}
                self.is_fallback = False
                self.model = "test/model"
                self.verdict = "pass"
                self.cost = 0.0
                self.reasons = ["ok"]

        class _FakeEval:
            def evaluate(self, payload, pack):
                return _FakeResult()

        class _FakePolicy:
            evaluator = _FakeEval()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = [
                "tests/test_jev_policy.py",
                "tests/test_jev_lane_parity.py",
                "tests/test_jev_ledger_spend.py",
            ]
            _write_repo(
                root,
                "| 1 One owner `JEV-P1-*` | **complete** — PR #35 MERGED `9d5ff14` | CI green |",
                tests=tests,
                files=["harness/jev_policy.py"],
            )
            evidence = collect_phase_evidence(str(root), "JEV-P1")
            result = score_phase_completion(evidence, jev_policy=_FakePolicy())
            self.assertFalse(result["semantic"]["is_fallback"])
            self.assertEqual(result["semantic"]["model"], "test/model")
            self.assertGreaterEqual(result["score"], PHASE_COMPLETE_MIN_SCORE)
            self.assertTrue(result["can_mark_complete"])

            # Non-numeric jev answer falls back to local heuristic.
            class _BadResult:
                answers = {"phase_evidence_quality": {"score": None}}
                is_fallback = True
                model = "test/model"
                verdict = "fallback"
                cost = 0.0
                reasons = []

            class _BadEval:
                def evaluate(self, payload, pack):
                    return _BadResult()

            class _BadPolicy:
                evaluator = _BadEval()

            result2 = score_phase_completion(evidence, jev_policy=_BadPolicy())
            self.assertIn("heuristic", str(result2["semantic"].get("note", "")))

    def test_collect_phase_evidence_missing_roadmap_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            (root / "harness" / "jev_completion.py").write_text("# stub\n", encoding="utf-8")
            evidence = collect_phase_evidence(str(root), "JEV-COMPLETION")
            self.assertIsNone(evidence["status_row"])
            self.assertTrue(any("no STATUS row" in n for n in evidence["notes"]))

    def test_phase_contract_completion_required_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| JEV-COMPLETION | **in progress** | this PR |",
                tests=[],
                files=[],
            )
            evidence = collect_phase_evidence(str(root), "JEV-COMPLETION")
            result = score_phase_completion(evidence)
            self.assertFalse(result["can_mark_complete"])
            self.assertTrue(any("missing required tests" in b for b in result["blockers"]))
            self.assertTrue(any("missing required files" in b for b in result["blockers"]))

            # With contract artifacts present + complete STATUS + PR merged.
            _write_repo(
                root,
                "| JEV-COMPLETION | **complete** — PR #39 MERGED | jev-phase gate |",
                tests=["tests/test_jev_completion.py"],
                files=["harness/jev_completion.py"],
            )
            evidence2 = collect_phase_evidence(str(root), "JEV-COMPLETION")
            evidence2["pr_merged"] = True
            evidence2["local_gates_green"] = True
            evidence2["ci_green"] = True
            evidence2["open_blockers"] = []
            result2 = score_phase_completion(evidence2)
            self.assertTrue(result2["hard_gates"]["required_tests_present"])
            self.assertTrue(result2["can_mark_complete"])


if __name__ == "__main__":
    unittest.main()
