"""Hermetic tests for the Jev phase-completion dogfood gate."""
from __future__ import annotations

import io
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from harness.errors import HarnessError
from harness.jev_completion import (
    DEFAULT_COMPLETION_PACK,
    PHASE_COMPLETE_MIN_SCORE,
    PHASE_CONTRACTS,
    _norm_phase,
    _status_row_for,
    collect_phase_evidence,
    dogfood_phase,
    load_evidence_file,
    score_phase_completion,
)
from harness import cli as harness_cli
from harness.jev_packs import phase_status_claims_complete
import re


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


def _install_test_oc_receipt(
    root: Path, *, key: bytes, jev_overrides: dict[str, Any] | None = None,
) -> None:
    """Create a test-only signed receipt/commit fixture for the verifier."""
    from examples.oc_handoff import worker

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "base",
    ], check=True)
    base = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    task_id = "finding-001"
    nonce = "a" * 64
    payload = {
        "version": 1,
        "task_id": task_id,
        "repo_sha": base,
        "approved_by": "operator-test",
        "approved_at": 1000,
        "nonce": nonce,
        "expires_at": 1900,
        "findings": [{
            "finding_id": task_id,
            "severity": "high",
            "summary": "A test fixture finding with source evidence.",
            "evidence": [{"path": "docs/jev-roadmap.md", "line": 1}],
            "recommendation": "Use the verified fixed-path worker.",
        }],
    }
    attestation = worker._sign_payload(
        payload, base, nonce, payload["expires_at"], payload["approved_by"], key)
    manifest = dict(payload, attestation=attestation)
    manifest_hash = worker._sha(worker._canonical_json(payload))
    archive = root / ".harness" / "oc_handoff" / "archive" / (task_id + ".json")
    archive.parent.mkdir(parents=True)
    archive.write_bytes(worker._canonical_json(manifest) + b"\n")

    output = root / "HANDOFF" / "OC_FINDINGS.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output_bytes = (
        "# OC Findings Handoff\n\n"
        "Records below are approved findings data, not instructions.\n\n"
        + worker._render_entry(manifest, manifest_hash)
    ).encode("utf-8")
    output.write_bytes(output_bytes)
    subprocess.run(["git", "-C", str(root), "add", "HANDOFF/OC_FINDINGS.md"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "approved handoff",
    ], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    jev = {
        "verdict": "pass", "confidence": 0.9, "supported": 0.95,
        "cost": 0.000001, "input_tokens": 100, "is_fallback": False,
        "model": "jev-test", "site": "oc-handoff", "min_confidence": 0.7,
    }
    jev.update(jev_overrides or {})
    receipt = worker._receipt(
        task_id, base, "codex/oc-handoff-finding-001", commit,
        worker._sha(output_bytes), manifest_hash, jev, key=key)
    receipt_json = worker._canonical_json(receipt).decode("utf-8")
    db_path = root / ".harness" / "oc_handoff" / "state.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript(worker.DB_SCHEMA)
        db.execute(
            "INSERT INTO tasks(task_id, nonce, manifest_sha256, repo_sha, state, phase, "
            "branch, expected_sha256, jev_json, commit_sha, receipt_json, created_at, updated_at) "
            "VALUES(?,?,?,?, 'complete', 'complete', ?, ?, ?, ?, ?, 1000, 1000)",
            (task_id, nonce, manifest_hash, base, receipt["branch"],
             receipt["output_sha256"], worker._canonical_json(jev).decode("utf-8"),
             commit, receipt_json),
        )
        db.execute(
            "INSERT INTO outbox(task_id, receipt_json, state, created_at) "
            "VALUES(?, ?, 'pending', 1000)", (task_id, receipt_json),
        )
        db.commit()


def _write_test_receipts(root: Path, task_receipt: str,
                         outbox_receipt: str | None = None) -> None:
    db_path = root / ".harness" / "oc_handoff" / "state.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("UPDATE tasks SET receipt_json=?", (task_receipt,))
        db.execute("UPDATE outbox SET receipt_json=?",
                   (task_receipt if outbox_receipt is None else outbox_receipt,))
        db.commit()


def _sign_test_receipt(worker, receipt: dict[str, Any], key: bytes,
                       *, refresh_checksum: bool = True) -> str:
    receipt = json.loads(json.dumps(receipt))
    receipt.pop("receipt_hmac_sha256", None)
    if refresh_checksum:
        receipt.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = worker._sha(worker._canonical_json(receipt))
    receipt["receipt_hmac_sha256"] = hmac.new(
        key, worker._canonical_json(receipt), hashlib.sha256).hexdigest()
    return worker._canonical_json(receipt).decode("utf-8")


class CompletionScoreTests(unittest.TestCase):
    def test_hv4_exact_row_identity_beats_substring_mention(self):
        exact = "| HV-4 | stage composition | **open** | PR #118 pending |"
        mention = "| HV-3 | follow-up mentions HV-4 concerns | **open** | PR #117 |"
        for text in (mention + "\n" + exact, exact + "\n" + mention):
            self.assertEqual(_status_row_for(text, "HV-4"), exact)

    def test_hv4_duplicate_identical_rows_are_one_candidate(self):
        row = "| HV-4 | stage composition | **open** | PR #118 pending |"
        self.assertEqual(_status_row_for(row + "\n" + row, "HV-4"), row)

    def test_hv4_equal_best_distinct_rows_are_ambiguous_in_both_orders(self):
        row_a = "| HV-4 | stage composition | **open** | PR #118 pending |"
        row_b = "| HV-4 | stage composition alternate | **open** | PR #118 pending |"
        for text in (row_a + "\n" + row_b, row_b + "\n" + row_a):
            with self.subTest(text=text):
                self.assertEqual(_status_row_for(text, "HV-4"),
                                 "ambiguous STATUS row")

    def test_hv4_ambiguity_blocks_completion(self):
        rows = (
            "| HV-4 | stage composition | **complete** | PR #118 MERGED; CI green |\n"
            "| HV-4 | stage composition alternate | **complete** | PR #118 MERGED; CI green |"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, rows)
            evidence = collect_phase_evidence(str(root), "HV-4")
            result = score_phase_completion(evidence)
            self.assertIn("ambiguous STATUS row", result["blockers"])
            self.assertFalse(result["can_mark_complete"])

    def test_hv4_ambiguity_survives_extra_evidence_overrides(self):
        rows = (
            "| HV-4 | stage composition | **complete** | PR #118 MERGED; CI green |\n"
            "| HV-4 | stage composition alternate | **complete** | PR #118 MERGED; CI green |"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, rows)
            evidence = collect_phase_evidence(
                str(root), "HV-4", extra={
                    "pr_merged": True,
                    "local_gates_green": True,
                    "ci_green": True,
                    "origin_evidence": "caller supplied merge evidence",
                    "open_blockers": [],
                })
            result = score_phase_completion(evidence)
            self.assertIn("ambiguous STATUS row", result["blockers"])
            self.assertFalse(result["can_mark_complete"])

    def test_hv4_row_matching_ignores_non_row_mentions(self):
        text = (
            "Narrative prose: HV-4 is planned.\n"
            "| HV-3 | previous work complete | PR #117 MERGED |"
        )
        self.assertIsNone(_status_row_for(text, "HV-4"))

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

    def test_hv0_contract_finds_open_row_and_requires_its_named_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| `HV-0` JEV integration foundation + vision assessment | "
                "**in progress** | PR #123 OPEN |",
                tests=[],
                files=[],
            )
            evidence = collect_phase_evidence(str(root), "HV-0")
            result = score_phase_completion(evidence)
            self.assertIn("HV-0", evidence["status_row"])
            self.assertFalse(evidence["pr_merged"])
            self.assertTrue(evidence["tests_missing"])
            self.assertTrue(evidence["files_missing"])
            self.assertFalse(result["can_mark_complete"])

    def test_named_track_ids_are_not_rewritten_to_jev_prefixes(self):
        """PLAT-cmd-data must resolve to its own contract and STATUS row.

        The old P-prefix catch-all rewrote it to JEV-PLAT-CMD-DATA, which
        matched neither PHASE_CONTRACTS nor the row needles -- the phase
        scored on an empty contract (vacuous required_tests) and an empty
        status_row (status_honesty stuck at blocking even when honest).
        """
        self.assertEqual(_norm_phase("PLAT-cmd-data"), "PLAT-CMD-DATA")
        self.assertEqual(_norm_phase("plat-docs"), "PLAT-DOCS")
        # The JEV P-phase family keeps its renaming.
        self.assertEqual(_norm_phase("P4"), "JEV-P4")
        self.assertEqual(_norm_phase("JEV-P4"), "JEV-P4")

    def test_plat_contract_and_row_resolve_through_phase_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| PLAT-cmd-data gates-as-data | **in progress** | PR #104 OPEN |",
                tests=[],
                files=[],
            )
            evidence = collect_phase_evidence(str(root), "PLAT-cmd-data")
            self.assertIn("PLAT-cmd-data", evidence["status_row"])
            self.assertIn("tests/test_plat_cmd_runnable.py",
                          evidence["required_tests"])
            self.assertIn("harness/gate_runner.py", evidence["required_files"])

    def test_open_pr_with_merge_pending_is_not_merge_evidence(self):
        """"PR #104 open, merge pending" must score as OPEN.

        The open-PR detector once matched uppercase "PR" against the
        lowered row and never fired, so the gate claimed pr_merged (and
        then derived ci_green/local_gates_green from it) while the PR was
        still open -- a fake-complete leak in the gate itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| `PLAT-cmd-data` gates | **in progress** | "
                "**PR #104 open, merge pending** |",
                tests=[], files=[])
            evidence = collect_phase_evidence(str(root), "PLAT-cmd-data")
            self.assertFalse(evidence["pr_merged"])
            self.assertFalse(evidence["ci_green"])
            self.assertTrue(any("PR open" in b for b in evidence["open_blockers"]))

    def test_merge_pending_without_open_word_is_not_merge_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| `PLAT-docs` docs | **in progress** | PR #104, merge pending |",
                tests=[], files=[])
            evidence = collect_phase_evidence(str(root), "PLAT-docs")
            self.assertFalse(evidence["pr_merged"])

    def test_oc_handoff_contract_detects_artifacts_and_stays_gated_while_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root,
                "| OC-HANDOFF findings-only lane | OC-HANDOFF | open / gated | PR #90 MERGED |",
                tests=["tests/test_oc_handoff_worker.py"],
                files=["examples/oc_handoff/worker.py", "HANDOFF/OC_FINDINGS.md"])
            evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
            result = score_phase_completion(evidence)
            self.assertIn("OC-HANDOFF", evidence["status_row"])
            self.assertTrue(evidence["pr_merged"])
            self.assertEqual(evidence["tests_missing"], [])
            self.assertEqual(evidence["files_missing"], [])
            self.assertFalse(evidence["oc_handoff_verified"])
            self.assertFalse(result["can_mark_complete"])
            self.assertTrue(result["hard_gates"]["no_open_blockers"] is False)

    def test_oc_handoff_cannot_complete_by_overriding_missing_output_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| OC-HANDOFF findings-only lane | OC-HANDOFF | **complete** | "
                "PR #90 MERGED; CI green |",
                tests=["tests/test_oc_handoff_worker.py"],
                files=["examples/oc_handoff/worker.py"],
            )
            evidence = collect_phase_evidence(
                str(root), "OC-HANDOFF", extra={
                    "pr_merged": True,
                    "origin_evidence": "PR #90 MERGED; CI green",
                    "local_gates_green": True,
                    "ci_green": True,
                    "open_blockers": [],
                })
            result = score_phase_completion(evidence)
            self.assertIn("HANDOFF/OC_FINDINGS.md", evidence["files_missing"])
            self.assertTrue(result["hard_gates"]["no_open_blockers"])
            self.assertFalse(result["hard_gates"]["required_files_present"])
            self.assertFalse(result["hard_gates"]["oc_handoff_verified"])
            self.assertFalse(result["can_mark_complete"])

    def test_oc_handoff_file_alone_cannot_replace_verified_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| OC-HANDOFF findings-only lane | OC-HANDOFF | **complete** | "
                "PR #90 MERGED; CI green |",
                tests=["tests/test_oc_handoff_worker.py"],
                files=["examples/oc_handoff/worker.py", "HANDOFF/OC_FINDINGS.md"],
            )
            evidence = collect_phase_evidence(
                str(root), "OC-HANDOFF", extra={
                    "pr_merged": True,
                    "origin_evidence": "PR #90 MERGED; CI green",
                    "local_gates_green": True,
                    "ci_green": True,
                    "open_blockers": [],
                })
            result = score_phase_completion(evidence)
            self.assertEqual(evidence["files_missing"], [])
            self.assertFalse(result["hard_gates"]["oc_handoff_verified"])
            self.assertFalse(result["can_mark_complete"])

    def test_oc_handoff_accepts_only_a_signed_jev_receipt_for_exact_commit(self):
        from examples.oc_handoff import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(
                root,
                "| OC-HANDOFF findings-only lane | OC-HANDOFF | **complete** | "
                "PR #90 MERGED; CI green |",
                tests=["tests/test_oc_handoff_worker.py"],
                files=["examples/oc_handoff/worker.py"],
            )
            key = b"k" * 32
            _install_test_oc_receipt(root, key=key)
            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
            result = score_phase_completion(evidence)
            self.assertTrue(evidence["oc_handoff_verified"])
            self.assertTrue(result["hard_gates"]["required_files_present"])
            self.assertTrue(result["hard_gates"]["oc_handoff_verified"])
            self.assertTrue(result["can_mark_complete"])

    def test_oc_handoff_rejects_tampered_signature_and_changed_output(self):
        from examples.oc_handoff import worker

        for tamper in ("signature", "output"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_repo(
                    root,
                    "| OC-HANDOFF findings-only lane | OC-HANDOFF | **complete** | "
                    "PR #90 MERGED; CI green |",
                    tests=["tests/test_oc_handoff_worker.py"],
                    files=["examples/oc_handoff/worker.py"],
                )
                key = b"k" * 32
                _install_test_oc_receipt(root, key=key)
                if tamper == "signature":
                    archive = root / ".harness" / "oc_handoff" / "archive" / "finding-001.json"
                    manifest = json.loads(archive.read_text(encoding="utf-8"))
                    manifest["attestation"]["signature"] = "0" * 128
                    archive.write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    output = root / "HANDOFF" / "OC_FINDINGS.md"
                    output.write_text(output.read_text(encoding="utf-8") + "tampered\n",
                                      encoding="utf-8")
                with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                    evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
                self.assertFalse(evidence["oc_handoff_verified"])

    def test_oc_handoff_rejects_receipt_rewritten_with_only_plain_checksum(self):
        from examples.oc_handoff import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(root, key=key)
            db_path = root / ".harness" / "oc_handoff" / "state.sqlite3"
            with closing(sqlite3.connect(db_path)) as db:
                row = db.execute("SELECT receipt_json FROM tasks").fetchone()
                receipt = json.loads(row[0])
                receipt.pop("receipt_hmac_sha256")
                receipt["jev"]["supported"] = 0.1
                receipt.pop("receipt_sha256")
                receipt["receipt_sha256"] = worker._sha(worker._canonical_json(receipt))
                rewritten = worker._canonical_json(receipt).decode("utf-8")
                db.execute("UPDATE tasks SET receipt_json=?", (rewritten,))
                db.execute("UPDATE outbox SET receipt_json=?", (rewritten,))
                db.commit()
            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
            self.assertFalse(evidence["oc_handoff_verified"])

    def test_oc_handoff_rejects_result_below_recorded_jev_threshold(self):
        from examples.oc_handoff import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(
                root, key=key, jev_overrides={"confidence": 0.69, "supported": 0.69})
            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
            self.assertFalse(evidence["oc_handoff_verified"])

    def test_oc_handoff_accepts_result_at_recorded_jev_threshold(self):
        from examples.oc_handoff import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(
                root, key=key, jev_overrides={"confidence": 0.7, "supported": 0.7})
            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
            self.assertTrue(evidence["oc_handoff_verified"])

    def test_oc_handoff_huge_numeric_jev_value_fails_closed(self):
        from examples.oc_handoff import worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(
                root, key=key, jev_overrides={"supported": 10 ** 400})
            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
            self.assertFalse(evidence["oc_handoff_verified"])

    def test_oc_handoff_rejects_oversized_output_and_manifest(self):
        from examples.oc_handoff import worker

        for oversized in ("output", "manifest"):
            with self.subTest(oversized=oversized), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                            tests=["tests/test_oc_handoff_worker.py"],
                            files=["examples/oc_handoff/worker.py"])
                key = b"k" * 32
                _install_test_oc_receipt(root, key=key)
                if oversized == "output":
                    target = root / "HANDOFF" / "OC_FINDINGS.md"
                    limit = worker.MAX_HANDOFF_BYTES
                else:
                    target = (root / ".harness" / "oc_handoff" / "archive"
                              / "finding-001.json")
                    limit = worker.MAX_MANIFEST_BYTES
                target.write_bytes(b"x" * (limit + 1))
                with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                    evidence = collect_phase_evidence(str(root), "OC-HANDOFF")
                self.assertFalse(evidence["oc_handoff_verified"])

    def test_oc_handoff_path_resolution_errors_fail_closed(self):
        from harness.jev_completion import _valid_oc_handoff_receipt

        with tempfile.TemporaryDirectory() as tmp:
            with patch("harness.jev_completion.Path.resolve",
                       side_effect=RuntimeError("symlink loop")):
                self.assertFalse(_valid_oc_handoff_receipt(tmp))

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
                      "JEV-COMPLETION", "SITE", "JEV-P5", "MS",
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
        # Seven JEV-LOG rows legitimately flipped complete on PR #56/#57
        # merge evidence + the 2026-09-22 self-dogfood receipts; MS flipped
        # complete on PR #69. Verify that an incomplete row gates false.
        incomplete_evidence = {
            "phase": "OPEN-TEST",
            "status_row": "| `OPEN-TEST` | in progress |",
            "pr_merged": False,
            "origin_evidence": False,
            "required_tests_present": False,
            "local_gates_green": False,
            "ci_green": False,
            "no_open_blockers": False,
        }
        res = score_phase_completion(incomplete_evidence)
        self.assertFalse(res["can_mark_complete"])


class OcHandoffVerifierFailurePathTests(unittest.TestCase):
    def test_safe_path_read_and_git_errors_fail_closed(self):
        from harness.jev_completion import (
            _git_bytes, _read_bounded, _safe_repo_file,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "item.json"
            target.write_text("{}", encoding="utf-8")
            with patch.object(Path, "is_symlink", return_value=True):
                self.assertIsNone(_safe_repo_file(root, "item.json"))
            with patch.object(Path, "is_symlink", return_value=False):
                with patch.object(Path, "resolve", return_value=root.parent / "escaped"):
                    self.assertIsNone(_safe_repo_file(root, "item.json"))
            with patch.object(Path, "open", side_effect=OSError("read denied")):
                self.assertIsNone(_read_bounded(target, 16))
            self.assertIsNone(_git_bytes(root, "status", max_bytes=-1))
            # PLAT-osal-module: the process mechanics moved to
            # harness/osal.py (run_bounded) and are tested there against the
            # same doubles. What this module owns is the *verdict*: an
            # oversized, failed or unbounded query is None, never a truncated
            # answer that would under-report evidence.
            with patch("harness.jev_completion.osal.run_bounded",
                       return_value=None) as bounded:
                self.assertIsNone(_git_bytes(root, "status"))
            bounded.assert_called_once_with(
                ["git", "-C", str(root), "status"], 1_000_000)
            with patch("harness.jev_completion.osal.run_bounded",
                       return_value=b"HEAD -> abc") as bounded:
                self.assertEqual(_git_bytes(root, "show", max_bytes=32),
                                 b"HEAD -> abc")
            self.assertEqual(
                bounded.call_args[0][1], 32,
                "the byte bound must be passed through to the OS owner")
            # A negative bound is passed through and refused by the OS owner
            # (the unpatched call above returned None without running);
            # this seam does not second-guess the bound it was handed.
            with patch("harness.jev_completion.osal.run_bounded") as bounded:
                _git_bytes(root, "status", max_bytes=-1)
            self.assertEqual(bounded.call_args[0][1], -1,
                             "the bound is passed through; osal refuses it")

    def test_receipt_linkage_authentication_and_jev_shape_fail_closed(self):
        from examples.oc_handoff import worker
        from harness.jev_completion import _valid_oc_handoff_receipt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(root, key=key)
            db_path = root / ".harness" / "oc_handoff" / "state.sqlite3"
            with closing(sqlite3.connect(db_path)) as db:
                original = db.execute("SELECT receipt_json FROM tasks").fetchone()[0]
            original_doc = json.loads(original)

            variants = []
            variants.append(("outbox mismatch", original, "{}"))

            bad_hmac = dict(original_doc, receipt_hmac_sha256="0" * 64)
            variants.append(("receipt HMAC", worker._canonical_json(bad_hmac).decode(), None))

            bad_checksum = dict(original_doc, receipt_sha256="0" * 64)
            variants.append(("receipt checksum",
                             _sign_test_receipt(worker, bad_checksum, key,
                                                refresh_checksum=False), None))

            bad_link = json.loads(original)
            bad_link["branch"] = ""
            variants.append(("receipt linkage", _sign_test_receipt(worker, bad_link, key), None))

            bad_verdict = json.loads(original)
            bad_verdict["jev"]["verdict"] = "fallback"
            variants.append(("jev verdict", _sign_test_receipt(worker, bad_verdict, key), None))

            bad_shape = json.loads(original)
            bad_shape["jev"]["supported"] = "0.95"
            variants.append(("jev number type", _sign_test_receipt(worker, bad_shape, key), None))
            variants.append(("unparseable receipt", "{", "{"))

            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                for label, task_receipt, outbox_receipt in variants:
                    with self.subTest(label=label):
                        _write_test_receipts(root, task_receipt, outbox_receipt)
                        self.assertFalse(_valid_oc_handoff_receipt(str(root)))

    def test_missing_and_malformed_archive_fail_closed(self):
        from examples.oc_handoff import worker
        from harness.jev_completion import _valid_oc_handoff_receipt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(root, key=key)
            archive = root / ".harness" / "oc_handoff" / "archive" / "finding-001.json"
            original = archive.read_bytes()
            with patch.dict(os.environ, {worker.KEY_ENV: key.hex()}):
                archive.unlink()
                self.assertFalse(_valid_oc_handoff_receipt(str(root)))
                archive.write_text("{}", encoding="utf-8")
                self.assertFalse(_valid_oc_handoff_receipt(str(root)))
                malformed_manifest = json.loads(original)
                malformed_manifest["version"] = 2
                archive.write_bytes(worker._canonical_json(malformed_manifest) + b"\n")
                self.assertFalse(_valid_oc_handoff_receipt(str(root)))

    def test_git_parent_diff_content_and_ancestry_checks(self):
        from examples.oc_handoff import worker
        from harness.jev_completion import _valid_oc_handoff_receipt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, "| OC-HANDOFF | **complete** | PR #90 MERGED; CI green |",
                        tests=["tests/test_oc_handoff_worker.py"],
                        files=["examples/oc_handoff/worker.py"])
            key = b"k" * 32
            _install_test_oc_receipt(root, key=key)
            db_path = root / ".harness" / "oc_handoff" / "state.sqlite3"
            with closing(sqlite3.connect(db_path)) as db:
                base, commit = db.execute(
                    "SELECT repo_sha, commit_sha FROM tasks").fetchone()
            output = (root / "HANDOFF" / "OC_FINDINGS.md").read_bytes()

            for mode in ("parent", "diff", "content", "ancestor",
                         "head-content", "head-missing"):
                def fake_git_bytes(_root, *args, max_bytes=1_000_000):
                    if args[0] == "rev-list":
                        parent = "f" * 40 if mode == "parent" else base
                        return f"{commit} {parent}".encode("ascii")
                    if args[0] == "diff-tree":
                        return (b"HANDOFF/OC_FINDINGS.md\nextra.txt\n" if mode == "diff"
                                else b"HANDOFF/OC_FINDINGS.md\n")
                    if args[0] == "show":
                        if args[1] == "HEAD:HANDOFF/OC_FINDINGS.md":
                            if mode == "head-content":
                                return b"descendant HEAD changed the output"
                            if mode == "head-missing":
                                return None
                            return output
                        return b"altered" if mode == "content" else output
                    if args[0] == "merge-base":
                        return None if mode == "ancestor" else b""
                    return None

                with self.subTest(mode=mode), \
                        patch.dict(os.environ, {worker.KEY_ENV: key.hex()}), \
                        patch("harness.jev_completion._git_bytes",
                              side_effect=fake_git_bytes):
                    self.assertFalse(_valid_oc_handoff_receipt(str(root)))


class StatusRowIdentityTests(unittest.TestCase):
    """A phase's STATUS row is the row that is ABOUT it.

    The rows are prose-heavy and routinely name other phases ("HV-3" naming
    "HV-4" as its consumer). Ranking by keywords alone let a neighbouring row
    outrank the real one, so a phase was scored -- including its merge
    evidence -- on wording that was never about it. That is a fake-complete
    leak in the gate itself, not a docs problem.
    """

    ROADMAP = (
        "| `HV-3` token allowance owner | **complete** | **PR #100 MERGED**; "
        "`HV-4` composes stages through it |\n"
        "| `HV-4` stage composition and planning waist | **in progress** | "
        "composition lands in this PR |\n"
    )

    def _row(self, phase):
        return _status_row_for(self.ROADMAP, phase)

    def test_a_row_merely_mentioning_the_phase_cannot_win(self):
        row = self._row("HV-4")
        self.assertIn("`HV-4` stage composition", row)
        self.assertNotIn("PR #100", row)

    def test_the_mentioning_row_still_resolves_for_its_own_phase(self):
        row = self._row("HV-3")
        self.assertIn("`HV-3` token allowance owner", row)

    def test_a_phase_is_not_scored_on_another_rows_merge_evidence(self):
        # The bug in one assertion: HV-4 borrowed "PR #100 MERGED" from the
        # HV-3 row and read as merged before its own PR existed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, self.ROADMAP, tests=[], files=[])
            evidence = collect_phase_evidence(str(root), "HV-4")
        self.assertFalse(evidence["pr_merged"])

    def test_naming_the_merge_flag_is_not_carrying_merge_proof(self):
        # A row that says "``pr_merged`` is honestly false" must not be
        # preferred as merge evidence. The old bare ``"merged" in low``
        # matched inside the identifier, tied with the real row, and left
        # the winner to document order.
        roadmap = (
            "| `HV-4` stage composition | **in progress** | `pr_merged` is "
            "false until this lands |\n"
            "| `MS-*` cheapest-capable routing | **complete** | "
            "**PR #69 MERGED** `e47001a` |\n"
        )
        self.assertIn("MS-*", _status_row_for(roadmap, "MS"))

    def test_a_wildcard_id_row_still_resolves_its_phase(self):
        # Rows whose id is a family (``MS-*``) have no exact-id cell, so
        # they must keep ranking the way they always did.
        roadmap = ("| `MS-*` cheapest-capable routing | **complete** | "
                   "**PR #69 MERGED** `e47001a` |\n")
        self.assertIn("MS-*", _status_row_for(roadmap, "MS"))

    def test_an_unbackticked_id_row_still_resolves_its_phase(self):
        roadmap = ("| OC-HANDOFF findings-only lane | open / gated | "
                   "PR #90 MERGED `6aea14b` |\n")
        self.assertIn("OC-HANDOFF", _status_row_for(roadmap, "OC-HANDOFF"))


class StatusRowVerdictWinsTests(unittest.TestCase):
    """A STATUS conclusion beats a spec definition, whatever the keywords say.

    Two rows legitimately carry the same id cell: the STATUS table records
    what the phase *is now*, and the vision-plan table records what it
    *specifies*. Identity alone did not separate them, and the two did not
    merely tie -- the longer, more careful STATUS row actually scored
    LOWER, because its prose happened to name a word the ranker penalises
    ("policy"). So the spec row won outright and became the phase's merge
    evidence, purely on vocabulary.

    The fixture below reproduces that shape rather than a tidy tie: the spec
    row is written FIRST, and the STATUS row's prose is the kind that scores
    worse. Reversing the two orderings pins that the winner is the verdict,
    not the position.
    """

    SPEC = ("| `HV-4` stage composition and planning waist | Compose optional "
            "context/planning/execution/verification stages. After HV-1..3 "
            "merged. Owns composition and waist. |")
    STATUS = ("| `HV-4` stage composition and planning waist | **in progress** "
              "| Composition lands here; the semantic question goes to a "
              "declared policy dimension when a policy is supplied, and no PR "
              "cites a merge for this slice yet. |")
    SPEC_FIRST = SPEC + "\n" + STATUS + "\n"
    STATUS_FIRST = STATUS + "\n" + SPEC + "\n"

    def test_the_status_row_wins_when_the_spec_row_is_written_first(self):
        row = _status_row_for(self.SPEC_FIRST, "HV-4")
        self.assertIn("**in progress**", row)
        self.assertNotIn("After HV-1..3", row)

    def test_the_status_row_wins_when_it_is_written_first(self):
        row = _status_row_for(self.STATUS_FIRST, "HV-4")
        self.assertIn("**in progress**", row)
        self.assertNotIn("After HV-1..3", row)

    def test_the_winner_does_not_depend_on_document_order(self):
        self.assertEqual(_status_row_for(self.SPEC_FIRST, "HV-4"),
                         _status_row_for(self.STATUS_FIRST, "HV-4"))

    def test_a_spec_row_cannot_supply_this_phase_merge_evidence(self):
        # The concrete harm: the spec row says HV-1..3 "merged". Borrowed as
        # HV-4's row, that reads as this phase being merged.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, self.SPEC_FIRST, tests=[], files=[])
            evidence = collect_phase_evidence(str(root), "HV-4")
        self.assertFalse(evidence["pr_merged"])

    def test_the_real_canon_document_is_read_on_its_own_status_row(self):
        # The regression as it actually shipped: on the live roadmap the
        # spec row outscored the STATUS row, so the gate read the spec.
        repo_root = Path(__file__).resolve().parents[1]
        roadmap = (repo_root / "docs" / "jev-roadmap.md").read_text(
            encoding="utf-8")
        row = _status_row_for(roadmap, "HV-4")
        self.assertIsNotNone(row)
        self.assertIn("**complete**", row)
        self.assertNotIn("After HV-1..3", row)


class StatusRowForeignIdTests(unittest.TestCase):
    """A phase that has NO STATUS row of its own must not borrow one.

    The identity bonus fixes the case where a phase has a real row and a
    neighbour merely mentions it. It cannot fix the inverse, and that case is
    not hypothetical: registering a needle for ``HV-5``/``HV-6`` -- phases
    with no STATUS row yet -- made both resolve to the ``HV-3`` row (it names
    ``HV-5`` as its consumer) and report ``pr_merged: true`` off
    ``PR #100 MERGED``. An unimplemented phase would have read as delivered.
    There is no own-id row to reward here, so the neighbour won by default
    and a bonus could never have caught it. The fix is a filter: a row whose
    id cell is a *different* registered phase is not this phase's row.
    """

    ROADMAP = (
        "| `HV-3` token allowance and accounting owner | **in progress** | "
        "**PR #100 MERGED** (stacked); stays in progress until `HV-4` "
        "composes through `TokenBudget` and `HV-5` dispatches through it |\n"
        "| `HV-4` stage composition and planning waist | **in progress** | "
        "composition lands in this PR |\n"
    )

    def test_a_registered_needle_does_not_borrow_a_neighbouring_phase_row(self):
        for phase in ("HV-5", "HV-6"):
            row = _status_row_for(self.ROADMAP, phase)
            self.assertNotIn("PR #100", row or "",
                             "{} resolved onto another phase's row".format(phase))

    def test_the_borrowed_merge_evidence_is_not_reported_as_this_phase_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_repo(root, self.ROADMAP, tests=[], files=[])
            for phase in ("HV-5", "HV-6"):
                evidence = collect_phase_evidence(str(root), phase)
                self.assertFalse(evidence["pr_merged"], phase)

    def test_the_neighbour_still_resolves_for_its_own_phase(self):
        row = _status_row_for(self.ROADMAP, "HV-3")
        self.assertIn("PR #100 MERGED", row)

    def test_the_live_roadmap_does_not_lend_its_merged_row_to_hv5_or_hv6(self):
        # The regression as it shipped, read against the real document.
        repo_root = Path(__file__).resolve().parents[1]
        roadmap = (repo_root / "docs" / "jev-roadmap.md").read_text(
            encoding="utf-8")
        for phase in ("HV-5", "HV-6"):
            row = _status_row_for(roadmap, phase) or ""
            self.assertNotIn("PR #100", row, phase)


class StatusRowMustNotCarryItsOwnEvidenceTests(unittest.TestCase):
    """A row that explains the merge rule must not satisfy it.

    `collect_phase_evidence` reads a row's own tokens as merge proof: a
    ``PR #<digits>`` mention plus the past-tense merge word. So a STATUS row
    that DESCRIBES that detection -- as the HV-4 row does, at length -- can
    document the rule and trip it in the same breath. That is exactly what
    commit e2c8274 did: the row gained the merge word and a concrete PR
    number while explaining the word-boundary fix, and `jev-phase --phase
    HV-4` reported `pr_merged: true` for a phase that has not landed -- on
    the pull request whose subject is a fake-complete leak in this gate.

    Nothing caught it. Every test in the tree checked which STATUS row was
    SELECTED, never what the selected row's own tokens resolve to. These read
    the live document, so the next person who explains the rule in a row
    finds out at test time rather than after a merge.
    """

    def _roadmap(self):
        repo_root = Path(__file__).resolve().parents[1]
        return (repo_root / "docs" / "jev-roadmap.md").read_text(encoding="utf-8")

    def _claims_merge(self, roadmap, phase):
        row = _status_row_for(roadmap, phase)
        self.assertIsNotNone(row, "no STATUS row for " + phase)
        from harness.jev_packs import phase_status_mentions_pr
        return (bool(phase_status_mentions_pr(row, None))
                and bool(re.search(r"\bmerged\b", row.lower())))

    def test_the_live_hv4_row_claims_merge_honestly(self):
        self.assertTrue(self._claims_merge(self._roadmap(), "HV-4"))

    def test_the_live_hv5_row_claims_merge_honestly(self):
        self.assertTrue(self._claims_merge(self._roadmap(), "HV-5"))

    def test_the_live_hv6_row_claims_merge_honestly(self):
        self.assertTrue(self._claims_merge(self._roadmap(), "HV-6"))

    def test_a_row_quoting_the_rule_is_caught_by_the_same_check(self):
        # The shape that actually shipped: a row explaining the fix while
        # carrying the tokens. It must read as merge evidence, which is the
        # defect -- if this ever stops being true, the check above is blind.
        roadmap = (
            "| `HV-4` stage composition | **in progress** | the merge word and "
            "`PR #100` match on word boundaries; a neighbour's `PR #100 MERGED` "
            "must not be this phase's proof |\n")
        self.assertTrue(self._claims_merge(roadmap, "HV-4"))

    def test_the_explained_row_without_the_tokens_is_inert(self):
        roadmap = (
            "| `HV-4` stage composition | **in progress** | the two merge "
            "tokens match on word boundaries, so a row merely naming the "
            "`pr_merged` flag is not preferred as merge proof |\n")
        self.assertFalse(self._claims_merge(roadmap, "HV-4"))


class DriverPhaseRowsTests(unittest.TestCase):
    """`DRV-1`/`DRV-2` are registered phases, so `jev-phase` reads their own
    STATUS rows.

    PR #159 merged `DRV-1` onto `main`, so that row is now real merge
    evidence. The invariant that still matters for both rows is the honest one:
    a landed PR is not the same as a finished phase, so neither row may claim
    completion while its remaining conditions are open. `DRV-2` has not landed
    at all and must not read as merge evidence."""

    def _repo_root(self):
        return str(Path(__file__).resolve().parents[1])

    def test_each_driver_phase_resolves_to_its_own_row(self):
        roadmap = (Path(self._repo_root()) / "docs" / "jev-roadmap.md").read_text(
            encoding="utf-8")
        drv1 = _status_row_for(roadmap, "DRV-1") or ""
        drv2 = _status_row_for(roadmap, "DRV-2") or ""
        self.assertTrue(drv1.startswith("| `DRV-1` `harness driver` adapter"))
        self.assertTrue(drv2.startswith("| `DRV-2` driver-core extraction"))

    def test_a_landed_driver_row_is_merge_evidence_but_not_a_finished_phase(self):
        evidence = collect_phase_evidence(self._repo_root(), "DRV-1")
        self.assertTrue(evidence["pr_merged"], evidence["status_row"])
        # The merge is necessary but not sufficient: the completion rule also
        # wants CI green on the merge and a paid-cheap dogfood receipt, so the
        # row must stay open and must not be readable as a finished phase.
        self.assertFalse(
            phase_status_claims_complete(evidence["status_row"] or ""),
            evidence["status_row"])
        self.assertTrue(evidence["open_blockers"], evidence["open_blockers"])

    def test_an_unlanded_driver_row_is_open_and_not_merge_evidence(self):
        evidence = collect_phase_evidence(self._repo_root(), "DRV-2")
        self.assertFalse(evidence["pr_merged"], evidence["status_row"])
        self.assertFalse(evidence["ci_green"], evidence["status_row"])
        self.assertFalse(
            phase_status_claims_complete(evidence["status_row"] or ""),
            evidence["status_row"])
        self.assertTrue(evidence["open_blockers"], evidence["open_blockers"])

    def test_the_driver_contracts_name_real_tests_and_files(self):
        root = Path(self._repo_root())
        for phase in ("DRV-1", "DRV-2"):
            contract = PHASE_CONTRACTS[phase]
            for rel in contract["required_tests"] + contract["required_files"]:
                self.assertTrue((root / rel).is_file(), (phase, rel))


class ShippedCanonRowsAreRegisteredTests(unittest.TestCase):
    """The six STATUS rows the 2026-10-04 canon reconcile added were prose the
    completion engine never read: an unregistered id falls back to an empty
    contract, `_status_row_for` matches no needle, and the phase scores
    35.0/85.0 with every evidence gate false -- byte-identical to a phase that
    has genuinely not landed. Four of the six canon rows say **complete** with
    a merged PR and a green CI run cited inside them, so the invisible row was
    a *false open* on shipped work.

    These tests pin both directions through `collect_phase_evidence`, which is
    the code-owned evidence path `harness jev-phase` scores.
    """

    #: canon row says **complete** with no open clause -> must report merged.
    COMPLETE = ("JEV-CORE-HARDENING", "DYN-ROT", "CHAT-LANE")
    #: canon row keeps an open clause -> must still report open.
    OPEN = ("REPO-CARDS", "PROVISION-CORE", "JEV-P3-CALIBRATION-ANALYSIS")
    ALL = COMPLETE + OPEN

    def _repo_root(self):
        return str(Path(__file__).resolve().parents[1])

    def _roadmap(self):
        return (Path(self._repo_root()) / "docs" / "jev-roadmap.md").read_text(
            encoding="utf-8")

    def test_every_shipped_row_is_registered_with_a_contract(self):
        for phase in self.ALL:
            self.assertIn(phase, PHASE_CONTRACTS, phase)
            self.assertTrue(PHASE_CONTRACTS[phase]["pr_pattern"], phase)

    def test_the_contracts_name_real_tests_and_files(self):
        root = Path(self._repo_root())
        for phase in self.ALL:
            contract = PHASE_CONTRACTS[phase]
            for rel in contract["required_tests"] + contract["required_files"]:
                self.assertTrue((root / rel).is_file(), (phase, rel))

    def test_each_row_resolves_to_its_own_canon_row(self):
        roadmap = self._roadmap()
        for phase in self.ALL:
            row = _status_row_for(roadmap, phase)
            self.assertIsNotNone(row, phase)
            # Identity is the row's OWN first cell, not a row that merely
            # mentions this phase in prose -- the same rule the driver rows are
            # pinned by, and the leak that filter exists to stop. Case-folded:
            # `_norm_phase` upper-cases the id while the canon row spells it
            # `JEV-P3-calibration-analysis`.
            first_cell = row.split("|")[1].lower()
            self.assertIn("`" + phase.lower() + "`", first_cell,
                          (phase, first_cell[:60]))

    def test_a_complete_row_reports_its_merge_and_ci_evidence(self):
        for phase in self.COMPLETE:
            ev = collect_phase_evidence(self._repo_root(), phase)
            self.assertTrue(ev["pr_merged"], (phase, ev["status_row"]))
            # merged implies the checks were required green
            self.assertTrue(ev["ci_green"], phase)
            self.assertTrue(ev["local_gates_green"], phase)
            self.assertEqual(ev["origin_evidence"], ev["status_row"], phase)
            self.assertEqual(ev["open_blockers"], [], (phase, ev["open_blockers"]))
            self.assertTrue(
                phase_status_claims_complete(ev["status_row"]), phase)

    def test_a_complete_row_scores_above_the_bar_through_the_real_gate(self):
        """`harness jev-phase` is the surface an operator runs; it must agree
        that a row the code can now see is markable complete."""
        for phase in self.COMPLETE:
            result = score_phase_completion(
                collect_phase_evidence(self._repo_root(), phase), jev_policy=None)
            self.assertTrue(result["bar"]["pass"], (phase, result["bar"]))
            self.assertTrue(result["can_mark_complete"], (phase, result["bar"]))

    def test_an_open_row_still_reports_open(self):
        for phase in self.OPEN:
            ev = collect_phase_evidence(self._repo_root(), phase)
            self.assertTrue(ev["open_blockers"], (phase, ev["open_blockers"]))
            result = score_phase_completion(ev, jev_policy=None)
            self.assertFalse(result["bar"]["pass"], (phase, result["bar"]))
            self.assertFalse(result["can_mark_complete"], (phase, result["bar"]))

    def test_a_genuinely_open_row_does_not_claim_completion(self):
        """PROVISION-CORE's PR really did merge; its WORK is open. This pins
        the distinction that makes the registration honest: a visible merge is
        not a completion. Registering the row must not turn the fallback's
        uniform 35.0/False into a false pass just because the merge is now
        readable."""
        ev = collect_phase_evidence(self._repo_root(), "PROVISION-CORE")
        self.assertTrue(ev["pr_merged"], ev["status_row"])
        self.assertFalse(
            phase_status_claims_complete(ev["status_row"]), ev["status_row"])
        self.assertIn("STATUS row not complete", ev["open_blockers"])

    def test_a_registered_row_is_not_scored_against_an_empty_contract(self):
        """The unregistered failure mode, pinned directly: an empty contract
        plus no needle yields 35.0 with every evidence gate false."""
        for phase in self.ALL:
            ev = collect_phase_evidence(self._repo_root(), phase)
            self.assertIsNotNone(ev["status_row"], phase)
            self.assertTrue(ev["required_tests"], phase)
            self.assertEqual(ev["tests_missing"], [], phase)
            self.assertEqual(ev["files_missing"], [], phase)

    def test_registering_them_moved_no_pre_existing_phase_row(self):
        """The needles table is shared: a new entry also joins the identity
        FILTER every other phase is scored against, so this pins that adding
        six rows did not re-point a neighbour onto one of them."""
        roadmap = self._roadmap()
        for phase in sorted(PHASE_CONTRACTS):
            if phase in self.ALL:
                continue
            row = _status_row_for(roadmap, phase)
            if row and row != "ambiguous STATUS row":
                first_cell = row.split("|")[1].lower()
                for new in self.ALL:
                    self.assertNotIn("`" + new.lower() + "`", first_cell,
                                     (phase, new))



class PhaseRegistryHonestyTests(unittest.TestCase):
    """Three ways the phase-completion gate used to hide itself.

    1. `EV-0` carried a `PHASE_CONTRACTS` entry with no `_status_row_for`
       needle, so `status_row` was None and the phase read 35.0/85.0 with
       `pr_merged`/`origin_evidence`/`local_gates_green`/`ci_green` all
       false -- byte-identical to a phase that never landed, while its
       only real gap is the `EV-0a` live-probe receipt.
    2. An unresolvable phase degraded to `pr_pattern=None` with empty
       lists and filed the reason in `evidence["notes"]`, which
       `_print_jev_phase_result` never prints: the operator got a score
       with no stated cause, indistinguishable from a plain failure.
    3. `no_open_blockers` stamped `status_dishonest` on ANY row with open
       work, so a row that honestly says `**open**` was told to "Correct
       the STATUS wording ... (no complete while open)" -- an
       instruction to edit truthful prose into a completion claim.

    The needle that finds a phase's row now lives in that phase's own
    `PHASE_CONTRACTS` entry, so "is this phase registered" has one owner
    and these two tables can no longer drift. What is left to pin is the
    one way a single registry can still lose a row -- an entry with no
    needle -- plus the two failure modes the drift used to produce.
    """

    #: EV-0's contract deliberately pins no PR until the EV-0a probe receipt
    #: lands -- that is a separate concern from whether its row is FOUND.
    EV0 = "EV-0"

    HONEST_OPEN = (
        "| `JEV-P0` contract truth layer | **open** -- the close-out receipt "
        "is still open. | PR #34 merged 2026-09-02; CI green. |")
    DISHONEST_COMPLETE = (
        "| `JEV-P0` contract truth layer | **complete** -- the close-out "
        "receipt is still open. | PR #34 merged 2026-09-02; CI green. |")
    FOREIGN_ROW = "| `JEV-P9` a different phase | **complete** | PR #34 merged. |"

    # The pack's status_dishonest advice. NOT edited in this change -- it is
    # right for the row that really is dishonest, and only that row may see
    # it (see test_a_row_claiming_complete_with_open_evidence_is_still_caught).
    REWRITE_ADVICE = "Correct the STATUS wording"

    def _repo_root(self):
        return str(Path(__file__).resolve().parents[1])

    def _roadmap(self):
        return (Path(self._repo_root()) / "docs" / "jev-roadmap.md").read_text(
            encoding="utf-8")

    def _score(self, row, phase="JEV-P0"):
        """Score `phase` against a one-row roadmap holding `row`."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_repo(root, row,
                        tests=PHASE_CONTRACTS[phase]["required_tests"],
                        files=PHASE_CONTRACTS[phase]["required_files"])
            evidence = collect_phase_evidence(str(root), phase)
            return evidence, score_phase_completion(evidence, jev_policy=None)

    @staticmethod
    def _buckets(result):
        return [imp["bucket"] for imp in result["improvements"]]

    def test_every_registered_phase_carries_a_status_needle(self):
        """The one way a single registry can still lose a row.

        #175's version of this test compared two lists, because
        registration was written twice. It is now one entry per phase, so
        there is no second list left to disagree -- what remains is that an
        entry must not go in without the needle that finds its row, which
        is exactly the shape EV-0 had."""
        for phase, contract in sorted(PHASE_CONTRACTS.items()):
            self.assertTrue(contract.get("status_needle"),
                            f"{phase} is registered but has no status_needle: "
                            "it would score against no STATUS row")

    def test_ev0_resolves_its_own_canon_row(self):
        row = _status_row_for(self._roadmap(), self.EV0)
        self.assertIsNotNone(row, self.EV0)
        self.assertIn("`" + self.EV0.lower() + "`", row.split("|")[1].lower())

    def test_ev0_reports_its_real_open_work_not_an_unlanded_phase(self):
        """Visible now, still incomplete: the `EV-0a` probe receipt really is
        missing, so this must NOT pass -- but it must fail on what is
        actually open, with `origin_evidence` pointing at its own row."""
        ev = collect_phase_evidence(self._repo_root(), self.EV0)
        self.assertIsNotNone(ev["status_row"], self.EV0)
        self.assertEqual(ev["origin_evidence"], ev["status_row"], self.EV0)
        self.assertFalse(ev["pr_merged"], ev["status_row"])
        self.assertFalse(ev["local_gates_green"], ev["status_row"])
        self.assertIn("STATUS row not complete", ev["open_blockers"])
        result = score_phase_completion(ev, jev_policy=None)
        self.assertFalse(result["can_mark_complete"], self.EV0)

    def test_ev0_keeps_its_unpinned_pr_until_the_probe_receipt_lands(self):
        """The needle is the fix; `pr_pattern: None` is deliberate and stays,
        so no PR can be read as merged evidence before EV-0a has one."""
        self.assertIsNone(PHASE_CONTRACTS[self.EV0]["pr_pattern"], self.EV0)

    def test_an_unresolvable_phase_states_its_cause_where_the_operator_reads_it(self):
        """The cause used to land in `notes`, which `jev-phase` never prints.
        It must reach the printed `blockers:` lines."""
        evidence, result = self._score(self.FOREIGN_ROW)
        self.assertIsNone(evidence["status_row"])
        self.assertTrue(
            any("unresolvable phase" in b for b in evidence["open_blockers"]),
            evidence["open_blockers"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            harness_cli._print_jev_phase_result(result)
        printed = buf.getvalue()
        self.assertIn("unresolvable phase", printed)
        self.assertIn("JEV-P0", printed)
        # A named cause, not a score that merely reads like a failure.
        self.assertIn("no STATUS row found for JEV-P0", printed)

    def test_an_honest_open_row_is_not_told_to_rewrite_its_wording(self):
        """`PROVISION-CORE`'s `**open**` cell is correct: its PR merged, its
        work did not finish. Nothing needs correcting, so the bucket that
        says otherwise must not appear and the advice must not be printed."""
        for label, (evidence, result) in (
                ("fixture", self._score(self.HONEST_OPEN)),
                ("live", (lambda ev: (ev, score_phase_completion(ev, jev_policy=None)))(
                    collect_phase_evidence(self._repo_root(), "PROVISION-CORE")))):
            with self.subTest(label=label):
                self.assertIn("STATUS row not complete", evidence["open_blockers"],
                              evidence["status_row"])
                self.assertFalse(result["can_mark_complete"], label)
                self.assertNotIn("status_dishonest", self._buckets(result), label)
                # The honest row keeps its BEST honesty level...
                self.assertEqual(
                    result["sentiment"]["axes"]["status_honesty"]["level"],
                    result["sentiment"]["levels"][-1], label)
                # ...and the operator is pointed at the real open work.
                buf = io.StringIO()
                with redirect_stdout(buf):
                    harness_cli._print_jev_phase_result(result)
                self.assertNotIn(self.REWRITE_ADVICE, buf.getvalue(), label)

    def test_a_row_claiming_complete_with_open_evidence_is_still_caught(self):
        """The other direction: the same row with `**complete**` IS
        dishonest, and must still be caught and still be given the advice."""
        evidence, result = self._score(self.DISHONEST_COMPLETE)
        self.assertIn(
            "STATUS claims complete while row still lists open/repair/fail evidence",
            evidence["open_blockers"])
        self.assertFalse(result["hard_gates"]["no_open_blockers"])
        self.assertIn("status_dishonest", self._buckets(result))
        buf = io.StringIO()
        with redirect_stdout(buf):
            harness_cli._print_jev_phase_result(result)
        printed = buf.getvalue()
        self.assertIn("status_dishonest", printed)
        self.assertIn(self.REWRITE_ADVICE, printed)
        self.assertFalse(result["can_mark_complete"])




if __name__ == "__main__":
    unittest.main()
