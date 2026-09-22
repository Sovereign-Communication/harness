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
    DEFAULT_COMPLETION_PACK,
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
        if rel.replace("\\", "/").endswith("packs/phase_completion.pack.json"):
            # The completion pack is a data file the loader parses as JSON --
            # a "# stub" placeholder would make it an (honestly) invalid
            # pack. Required-file presence tests still need real pack JSON.
            path.write_text(json.dumps(DEFAULT_COMPLETION_PACK), encoding="utf-8")
        else:
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
        """score_phase_completion's semantic block is now sourced entirely
        through JevPolicy.evaluate_phase_completion (JEV-BAR) -- no more
        direct jev_policy.evaluator.evaluate call / _COMPLETION_PACK."""
        class _FakeResult:
            def __init__(self, model="test/model", verdict="pass"):
                self.model = model
                self.verdict = verdict
                self.cost = 0.0
                self.reasons = ["ok"]

        class _FakePolicy:
            def evaluate_phase_completion(self, state, pack):
                axes = pack["axes"] if isinstance(pack, dict) else {}
                judgment = {
                    "pack_id": pack.get("id") if isinstance(pack, dict) else None,
                    "live_levels": {axis: 4 for axis in axes},  # proven
                    "live_confidence": {axis: 0.95 for axis in axes},
                    "primary_gap": None,
                    "is_fallback": False,
                    "evidence": [],
                }
                return _FakeResult(), {"site": "phase_completion"}, judgment

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
            self.assertEqual(result["semantic"]["site"], "phase_completion")
            self.assertGreaterEqual(result["score"], PHASE_COMPLETE_MIN_SCORE)
            self.assertTrue(result["can_mark_complete"])

            # A fallback live judgment (every axis None) never fakes a live
            # answer; jev-authority axes fall back to the code heuristic.
            class _BadPolicy:
                def evaluate_phase_completion(self, state, pack):
                    axes = pack["axes"] if isinstance(pack, dict) else {}
                    judgment = {
                        "pack_id": pack.get("id") if isinstance(pack, dict) else None,
                        "live_levels": {axis: None for axis in axes},
                        "live_confidence": {axis: None for axis in axes},
                        "primary_gap": None,
                        "is_fallback": True,
                        "evidence": ["unkeyed"],
                    }
                    return (_FakeResult(verdict="fallback"),
                            {"site": "phase_completion"}, judgment)

            result2 = score_phase_completion(evidence, jev_policy=_BadPolicy())
            self.assertTrue(result2["semantic"]["is_fallback"])
            self.assertEqual(
                result2["sentiment"]["axes"]["status_honesty"]["source"], "code")

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
                tests=["tests/test_jev_completion.py", "tests/test_jev_bar_sentiment.py"],
                files=["harness/jev_completion.py", "packs/phase_completion.pack.json"],
            )
            evidence2 = collect_phase_evidence(str(root), "JEV-COMPLETION")
            evidence2["pr_merged"] = True
            evidence2["local_gates_green"] = True
            evidence2["ci_green"] = True
            evidence2["open_blockers"] = []
            result2 = score_phase_completion(evidence2)
            self.assertTrue(result2["hard_gates"]["required_tests_present"])
            self.assertTrue(result2["can_mark_complete"])


class ExtendedPhaseContractTests(unittest.TestCase):
    """Contracts + STATUS-row needles for canon phases whose rows previously
    had no way through the gate: JEV-P5, HUL-A..D, JEV-LOG-*, MS."""

    def test_hul_row_with_merge_evidence_can_mark_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| HUL-A | mission pack | **complete** | "
                "**PR #41 MERGED** `64e63a3`; CI green |",
                tests=["tests/test_hul_mission_record.py"],
                files=["harness/mission_record.py"],
            )
            result = score_phase_completion(
                collect_phase_evidence(str(root), "HUL-A"))
            self.assertTrue(result["hard_gates"]["pr_merged"])
            self.assertTrue(result["can_mark_complete"])

    def test_hul_open_row_cannot_mark_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| HUL-B | dual budget | **open** | PR #47 pending |",
                tests=["tests/test_hul_budget_reserve.py"],
                files=["harness/spend.py"],
            )
            result = score_phase_completion(
                collect_phase_evidence(str(root), "HUL-B"))
            self.assertFalse(result["hard_gates"]["pr_merged"])
            self.assertFalse(result["can_mark_complete"])

    def test_jev_log_needles_find_each_open_row(self):
        rows = "\n".join([
            "| `JEV-LOG-schema` | schema work | **open** |",
            "| `JEV-LOG-parse` | parse work | **open** |",
            "| `JEV-LOG-factor-pass` | factor work | **open** |",
            "| `JEV-LOG-judgment` | judgment work | **open** |",
            "| `JEV-LOG-envelope` | envelope work | **open** |",
            "| `JEV-LOG-cli` | cli work | **open** |",
            "| `JEV-LOG-dogfood` | dogfood work | **open** |",
        ])
        phases = ("JEV-LOG-SCHEMA", "JEV-LOG-PARSE", "JEV-LOG-FACTOR-PASS",
                  "JEV-LOG-JUDGMENT", "JEV-LOG-ENVELOPE", "JEV-LOG-CLI",
                  "JEV-LOG-DOGFOOD")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, rows)
            for phase in phases:
                evidence = collect_phase_evidence(str(root), phase)
                self.assertIsNotNone(evidence["status_row"], phase)
                self.assertFalse(
                    score_phase_completion(evidence)["can_mark_complete"],
                    f"open {phase} row must not gate complete")

    def test_ms_row_needle_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| `MS-*` cheapest-capable + context | **open** | lanes still "
                "ad-hoc |")
            evidence = collect_phase_evidence(str(root), "MS")
            self.assertIsNotNone(evidence["status_row"])
            self.assertFalse(
                score_phase_completion(evidence)["can_mark_complete"])

    def test_rank_prefers_the_row_carrying_merge_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = (
                "| HUL-C | scope gate | **complete** — PR #48 `469f34f` |\n"
                "| HUL-C | scope gate status | **complete** | "
                "**PR #48 MERGED** `469f34f`; CI green |")
            _write_repo(root, rows,
                        tests=["tests/test_hul_jev_scope_gate.py"],
                        files=["harness/jev_policy.py"])
            evidence = collect_phase_evidence(str(root), "HUL-C")
            self.assertIn("MERGED", evidence["status_row"])
            self.assertTrue(score_phase_completion(evidence)["can_mark_complete"])

    def test_p5_contract_requires_named_gate_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| `JEV-P5-*` issue-sort buckets | **complete** | "
                "**PR #42 MERGED** `c9e1c67`; bucket packs |",
                tests=[],  # gate test missing from the tree
                files=["harness/jev_packs.py"],
            )
            result = score_phase_completion(
                collect_phase_evidence(str(root), "JEV-P5"))
            self.assertFalse(result["hard_gates"]["required_tests_present"])
            self.assertFalse(result["can_mark_complete"])

    def test_real_canon_complete_rows_gate_true(self):
        """Integration: every canon STATUS row that claims complete with merge
        evidence must pass the dogfood gate on this very tree."""
        repo_root = Path(__file__).resolve().parents[1]
        for phase in ("JEV-P0", "JEV-P1", "JEV-P2", "JEV-P3", "JEV-P4",
                      "JEV-COMPLETION", "SITE", "JEV-P5",
                      "HUL-A", "HUL-B", "HUL-C", "HUL-D",
                      "JEV-LOG-SCHEMA", "JEV-LOG-PARSE", "JEV-LOG-FACTOR-PASS",
                      "JEV-LOG-JUDGMENT", "JEV-LOG-ENVELOPE", "JEV-LOG-CLI",
                      "JEV-LOG-DOGFOOD"):
            result = score_phase_completion(
                collect_phase_evidence(str(repo_root), phase))
            self.assertTrue(
                result["can_mark_complete"],
                f"{phase} claims complete but gates false: {result['blockers']}")

    def test_real_canon_open_rows_are_found_but_not_complete(self):
        """Open canon rows must be FOUND by the gate (honest `false`), not
        invisible. Update the negatives here when those rows legitimately
        flip to complete with evidence."""
        repo_root = Path(__file__).resolve().parents[1]
        # Seven JEV-LOG rows legitimately flipped complete on PR #56/#57
        # merge evidence + the 2026-09-22 self-dogfood receipts; only MS
        # stays negative.
        for phase in ("MS",):
            evidence = collect_phase_evidence(str(repo_root), phase)
            self.assertIsNotNone(evidence["status_row"], phase)
            self.assertFalse(
                score_phase_completion(evidence)["can_mark_complete"], phase)


if __name__ == "__main__":
    unittest.main()
