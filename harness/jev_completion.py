"""Jev phase-completion accountability (dogfood gate).

Code owns hard mechanical facts (PR/merge, required tests present, named
gates, open blockers). Jev owns a bounded semantic 0-100 judgment on whether
the evidence actually shows the phase is done. STATUS may claim complete only
when every hard gate passes AND the combined score clears the threshold.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import math
import os
import re
import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .errors import HarnessError
from .jev_packs import (
    DEFAULT_PHASE_COMPLETION_PACK,
    PHASE_COMPLETION_SITE,
    heuristic_completion_sentiment,
    load_completion_pack,
    phase_status_claims_complete,
    phase_status_has_blocker,
    phase_status_mentions_pr,
    validate_completion_pack,
)

PHASE_COMPLETE_MIN_SCORE = 85.0
COMPLETION_SCORE_MAX = 100.0
# Keep these limits aligned with examples/oc_handoff/worker.py. Reads below are
# capped as well as preflighted so a concurrent size change cannot expand them.
_OC_HANDOFF_MAX_BYTES = 1_000_000
_OC_MANIFEST_MAX_BYTES = 16_384
_OC_RECEIPT_MAX_BYTES = 16_384
_OC_RECEIPT_ROW_LIMIT = 128
_OC_STATE_MAX_BYTES = 64 * 1024 * 1024

# Mechanical points when hard gates pass. Sum == 100.
_HARD_GATE_POINTS = {
    "pr_merged": 25,
    "origin_evidence": 10,
    "required_tests_present": 20,
    "required_files_present": 0,
    "oc_handoff_verified": 0,
    "local_gates_green": 15,
    "ci_green": 15,
    "no_open_blockers": 15,
}

# JEV-BAR: the fixed 5-level default pack, kept identical to
# packs/phase_completion.pack.json (the JSON file is canonical; the module
# constant here is only the hermetic fallback used when a temp/test repo has
# no packs/ directory of its own -- test_jev_bar_sentiment.py asserts the two
# stay equal).
DEFAULT_COMPLETION_PACK: Dict[str, Any] = {
    "id": "harness-phase-completion-v1",
    "sentiment": {
        "levels": [
            "blocking — evidence contradicts completion",
            "at_risk — open gap likely to fail review",
            "mixed — evidence partial or ambiguous",
            "confident — evidenced with minor gaps",
            "proven — evidenced and consistent",
        ],
        "ordinals": [0, 35, 60, 85, 100],
        "blocking_max_index": 0,
        "improve_below_index": 3,
    },
    "axes": {
        "merge_evidence": {"authority": "code", "bucket": "merge_pending",
            "instructions": "Rate the merge evidence: the phase PR is merged, cited by PR number or merge SHA in the STATUS row."},
        "gate_tests": {"authority": "code", "bucket": "tests_missing",
            "instructions": "Rate whether named hermetic gate tests exist for this phase and are cited."},
        "verification": {"authority": "code", "bucket": "gates_unverified",
            "instructions": "Rate the verification evidence: local gates, CI, and the self-audit bar."},
        "status_honesty": {"authority": "jev", "bucket": "status_dishonest",
            "instructions": "Rate whether the STATUS row is honest: no complete claim while open, repair, or failing language remains."},
        "residual_scope": {"authority": "jev", "bucket": "residual_untracked",
            "instructions": "Rate whether residual or deferred work is tracked as its own row rather than hidden inside a complete claim."},
        "dogfood": {"authority": "jev", "bucket": "dogfood_missing",
            "instructions": "Rate live dogfood evidence for user-facing lanes: receipts, cost, fallback rate."},
    },
    "buckets": {
        "merge_pending": {"label": "Merge evidence missing", "path_id": "bar/merge",
            "keywords": ["no pr", "not merged", "this pr", "draft"],
            "suggested_next_action": "Drive the PR to green CI, merge it, and cite PR #/merge SHA in the STATUS row."},
        "tests_missing": {"label": "Named gate tests missing", "path_id": "bar/tests",
            "keywords": ["missing required tests", "no tests"],
            "suggested_next_action": "Add the named hermetic gate tests to the phase contract and make them pass."},
        "gates_unverified": {"label": "Gates or CI unverified", "path_id": "bar/gates",
            "keywords": ["audit", "coverage", "ci red", "bar not met"],
            "suggested_next_action": "Run the local battery and audits/self/audit.py to BAR MET; cite the CI run."},
        "status_dishonest": {"label": "STATUS claim contradicts evidence", "path_id": "bar/status",
            "keywords": ["in progress", "repair", "blocked"],
            "suggested_next_action": "Correct the STATUS wording so the claim matches the evidence (no complete while open)."},
        "residual_untracked": {"label": "Residual work hidden in a complete claim", "path_id": "bar/residual",
            "keywords": ["residual", "deferred", "follow-up", "remaining"],
            "suggested_next_action": "Split residual/deferred work into its own open STATUS row with an owner, or close it with evidence."},
        "dogfood_missing": {"label": "Live dogfood evidence missing", "path_id": "bar/dogfood",
            "keywords": ["dogfood", "smoke", "receipt"],
            "suggested_next_action": "Run paid-cheap live dogfood on the user-facing lane; record receipt, cost, and fallback rate."},
    },
}

# Bucket ids a failed hard (code) gate maps to -- always the pack's own
# declared buckets; never invented. Multiple gates may share one bucket.
_HARD_GATE_BUCKETS = {
    "pr_merged": "merge_pending",
    "origin_evidence": "merge_pending",
    "required_tests_present": "tests_missing",
    "required_files_present": "gates_unverified",
    "oc_handoff_verified": "dogfood_missing",
    "local_gates_green": "gates_unverified",
    "ci_green": "gates_unverified",
    "no_open_blockers": "status_dishonest",
}

# Canonical phase contracts for mission STATUS dogfooding.
PHASE_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "JEV-P0": {
        "pr_pattern": r"PR #34|d042d70",
        "required_tests": ["tests/test_jev.py", "tests/test_jev_smoke.py"],
        "required_files": ["harness/jev.py"],
    },
    "JEV-P1": {
        "pr_pattern": r"PR #35|9d5ff14",
        "required_tests": [
            "tests/test_jev_policy.py",
            "tests/test_jev_lane_parity.py",
            "tests/test_jev_ledger_spend.py",
        ],
        "required_files": ["harness/jev_policy.py"],
    },
    "JEV-P2": {
        "pr_pattern": r"PR #36",
        "required_tests": [
            "tests/test_consent_confidence.py",
            "tests/test_min_confidence_gating.py",
            "tests/test_jev_triage.py",
            "tests/test_jev_policy.py",
            "tests/test_jev_lane_parity.py",
            "tests/test_jev_ledger_spend.py",
        ],
        "required_files": [],
    },
    "JEV-P3": {
        "pr_pattern": r"PR #43|7eb18ea",
        "required_tests": [
            "tests/test_jev_util_coverage.py",
            "tests/test_jev_util_paths.py",
            "tests/test_jev_util_route.py",
        ],
        "required_files": [],
    },
    "JEV-P4": {
        "pr_pattern": r"PR #46|a021cfc",
        "required_tests": ["tests/test_jev_p4_ops_exit.py"],
        "required_files": [],
        "user_facing": True,
    },
    "JEV-COMPLETION": {
        "pr_pattern": r"PR #39|5e15f8d",
        "required_tests": ["tests/test_jev_completion.py", "tests/test_jev_bar_sentiment.py"],
        "required_files": ["harness/jev_completion.py", "packs/phase_completion.pack.json"],
    },
    "SITE": {
        "pr_pattern": r"PR #60|a9ae53f",
        "required_tests": [
            "tests/test_site_export.py",
            "tests/test_site_aggregate.py",
            "tests/test_route_pack.py",
            "tests/test_route_faces.py",
            "tests/test_site_server.py",
            "tests/test_site_cli_faces.py",
            "tests/test_site_fold_parity.py",
            "tests/test_site_parity_directives.py",
        ],
        "required_files": [
            "harness/site_export.py",
            "harness/site_aggregate.py",
            "harness/route_pack.py",
        ],
    },
    "JEV-P5": {
        "pr_pattern": r"PR #42|c9e1c67",
        "required_tests": ["tests/test_jev_issue_sort.py"],
        "required_files": ["harness/jev_packs.py"],
        "user_facing": True,
    },
    "HUL-A": {
        "pr_pattern": r"PR #41|64e63a3",
        "required_tests": ["tests/test_hul_mission_record.py"],
        "required_files": ["harness/mission_record.py"],
    },
    "HUL-B": {
        "pr_pattern": r"PR #47|536e75c",
        "required_tests": ["tests/test_hul_budget_reserve.py"],
        "required_files": ["harness/spend.py"],
    },
    "HUL-C": {
        "pr_pattern": r"PR #48|469f34f",
        "required_tests": ["tests/test_hul_jev_scope_gate.py"],
        "required_files": ["harness/jev_policy.py"],
    },
    "HUL-D": {
        "pr_pattern": r"PR #48|469f34f",
        "required_tests": ["tests/test_hul_driver_findings_resume.py"],
        "required_files": ["harness/mission_driver.py"],
        "user_facing": True,
    },
    "JEV-LOG-SCHEMA": {
        "pr_pattern": r"PR #56|431836d",
        "required_tests": ["tests/test_jev_log_pack.py"],
        "required_files": ["harness/jev_packs.py"],
    },
    "JEV-LOG-PARSE": {
        "pr_pattern": r"PR #56|431836d",
        "required_tests": [
            "tests/test_jev_log_pack.py",
            "tests/test_jev_log_envelope.py",
        ],
        "required_files": ["harness/log_items.py"],
    },
    "JEV-LOG-FACTOR-PASS": {
        "pr_pattern": r"PR #57|e15a723",
        "required_tests": ["tests/test_jev_log_envelope.py"],
        "required_files": ["harness/log_analysis.py"],
    },
    "JEV-LOG-JUDGMENT": {
        "pr_pattern": r"PR #56|PR #57|431836d|e15a723",
        "required_tests": ["tests/test_jev_log_judgment.py"],
        "required_files": ["harness/jev_policy.py"],
    },
    "JEV-LOG-ENVELOPE": {
        "pr_pattern": r"PR #57|e15a723",
        "required_tests": ["tests/test_jev_log_envelope.py"],
        "required_files": ["harness/log_analysis.py"],
    },
    "JEV-LOG-CLI": {
        "pr_pattern": r"PR #57|e15a723",
        "required_tests": ["tests/test_jev_log_envelope.py"],
        "required_files": ["harness/cli.py"],
        "user_facing": True,
    },
    "JEV-LOG-DOGFOOD": {
        "pr_pattern": r"PR #57|e15a723",
        "required_tests": ["tests/test_jev_log_envelope.py"],
        "required_files": [],
        "user_facing": True,
    },
    "MS": {
        # Not yet cited by a real PR: the generic pr_pattern=None rule
        # (STATUS row must match PR #\d+ AND the word MERGED) applies --
        # never the bare "PR #" pattern (that let any PR mention pass).
        "pr_pattern": None,
        "required_tests": [
            "tests/test_hg_ms_parity.py",
            "tests/test_model_envelope.py",
        ],
        "required_files": ["harness/config.py"],
    },
    "JEV-P6": {
        "pr_pattern": r"PR #65|1936ed4",
        "required_tests": [
            "tests/test_repo_items.py",
            "tests/test_jev_repo_pack.py",
            "tests/test_jev_repo_judgment.py",
            "tests/test_jev_repo_envelope.py",
        ],
        "required_files": [
            "harness/repo_items.py",
            "harness/repo_summary.py",
            "packs/repo_summary.pack.json",
        ],
        "user_facing": True,
    },
    "HG": {
        "pr_pattern": r"PR #44|f22accb",
        "required_tests": [
            "tests/test_hg_cli_resume_coverage.py",
            "tests/test_hg_composed_ceiling.py",
            "tests/test_hg_condense_decompose.py",
            "tests/test_hg_extra_coverage.py",
            "tests/test_hg_final_gate.py",
            "tests/test_hg_hourglass_defaults.py",
            "tests/test_hg_hybrid_isolate.py",
            "tests/test_hg_ms_parity.py",
            "tests/test_hg_plan_consensus.py",
            "tests/test_hg_pyramid_resume.py",
            "tests/test_hg_waist_unreachable_refuses.py",
        ],
        "required_files": [],
    },
    "JEV-BAR": {
        # PR number not known yet: generic pr_pattern=None rule.
        "pr_pattern": None,
        "required_tests": ["tests/test_jev_bar_sentiment.py"],
        "required_files": ["packs/phase_completion.pack.json", "harness/jev_completion.py"],
    },
    "CLAUDE-LANE": {
        # PR lands in a sibling PR; missing files must fail the bar honestly
        # until both PRs merge (operator ruling, JEV-BAR spec).
        "pr_pattern": None,
        "required_tests": [],
        "required_files": [
            "CLAUDE.md",
            ".claude/skills/isolated-mission/SKILL.md",
            ".claude/skills/isolated-request/SKILL.md",
        ],
        "user_facing": True,
    },
    "OC-HANDOFF": {
        "pr_pattern": r"PR #90|6aea14b",
        "required_tests": ["tests/test_oc_handoff_worker.py"],
        "required_files": [
            "examples/oc_handoff/worker.py",
            "docs/jev-roadmap.md",
            "HANDOFF/OC_FINDINGS.md",
        ],
    },
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_repo_file(root: Path, relative: str) -> Optional[Path]:
    """Resolve one file under root without following symlinks."""
    try:
        cursor = root
        for part in Path(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        resolved = cursor.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return None
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() else None


def _read_bounded(path: Path, limit: int) -> Optional[bytes]:
    """Read at most limit bytes, refusing oversized files and growth races."""
    try:
        if path.stat().st_size > limit:
            return None
        with path.open("rb") as handle:
            value = handle.read(limit + 1)
    except (OSError, RuntimeError):
        return None
    return value if len(value) <= limit else None


def _git_bytes(root: Path, *args: str, max_bytes: int = 1_000_000) -> Optional[bytes]:
    """Run a Git query while keeping captured stdout within a fixed bound."""
    if max_bytes < 0:
        return None
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False,
        )
    except OSError:
        return None
    if process.stdout is None:
        process.kill()
        process.wait()
        return None

    captured = bytearray()
    oversized = threading.Event()

    def drain_stdout() -> None:
        while True:
            remaining = max_bytes + 1 - len(captured)
            reader = getattr(process.stdout, "read1", None) or process.stdout.read
            chunk = reader(min(65_536, remaining))
            if not chunk:
                return
            captured.extend(chunk)
            if len(captured) > max_bytes:
                oversized.set()
                process.kill()
                return

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        returncode = None
    reader.join(timeout=1)
    if reader.is_alive():
        process.kill()
        process.stdout.close()
        reader.join(timeout=1)
        return None
    process.stdout.close()
    if oversized.is_set() or returncode != 0:
        return None
    return bytes(captured)


def _valid_oc_handoff_receipt(root_value: str) -> bool:
    """Require the worker's signed-manifest receipt and audited commit.

    The worker only creates a receipt after native Jev passes and the isolated
    commit changes exactly HANDOFF/OC_FINDINGS.md. Recheck the persisted
    receipt, archived manifest digest, commit parent/diff, and current output
    here so STATUS or --evidence overrides cannot manufacture completion.
    """
    try:
        root = Path(root_value).resolve(strict=True)
        output = _safe_repo_file(root, "HANDOFF/OC_FINDINGS.md")
        db_path = _safe_repo_file(root, ".harness/oc_handoff/state.sqlite3")
        if output is None or db_path is None:
            return False
        output_bytes = _read_bounded(output, _OC_HANDOFF_MAX_BYTES)
        if output_bytes is None:
            return False
        output_hash = _sha256(output_bytes)
        if db_path.stat().st_size > _OC_STATE_MAX_BYTES:
            return False
        db_wal = db_path.with_name(db_path.name + "-wal")
        if (db_wal.is_symlink()
                or (db_wal.exists() and db_wal.stat().st_size > _OC_STATE_MAX_BYTES)):
            return False
        key_hex = os.environ.get("HARNESS_OC_HANDOFF_HMAC_KEY", "")
        if not re.fullmatch(r"[0-9a-f]{64}", key_hex):
            return False
        key = bytes.fromhex(key_hex)
        db_uri = "file:" + quote(db_path.as_posix(), safe="/:") + "?mode=ro"
        with closing(sqlite3.connect(db_uri, uri=True, timeout=2)) as db:
            rows = db.execute(
                "SELECT t.task_id, t.state, t.phase, t.repo_sha, t.commit_sha, "
                "t.manifest_sha256, t.receipt_json, o.receipt_json, o.state "
                "FROM tasks AS t JOIN outbox AS o ON o.task_id=t.task_id "
                "WHERE t.state='complete' AND t.phase='complete' "
                "AND length(CAST(t.receipt_json AS BLOB)) BETWEEN 1 AND ? "
                "AND length(CAST(o.receipt_json AS BLOB)) BETWEEN 1 AND ? "
                "ORDER BY t.updated_at DESC, t.task_id DESC LIMIT ?",
                (_OC_RECEIPT_MAX_BYTES, _OC_RECEIPT_MAX_BYTES,
                 _OC_RECEIPT_ROW_LIMIT),
            ).fetchall()
    except (OSError, RuntimeError, sqlite3.Error, ValueError, TypeError):
        return False

    for (task_id, task_state, phase, base, commit, manifest_hash,
         task_receipt, outbox_receipt, outbox_state) in rows:
        try:
            receipt = json.loads(task_receipt)
            outbox_doc = json.loads(outbox_receipt)
            if (not isinstance(receipt, dict) or not isinstance(outbox_doc, dict)
                    or receipt != outbox_doc):
                continue
            receipt_hmac = receipt.pop("receipt_hmac_sha256", None)
            if (not isinstance(receipt_hmac, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt_hmac)):
                continue
            expected_hmac = hmac.new(
                key, _canonical_json(receipt), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_hmac, receipt_hmac):
                continue
            checksum = receipt.pop("receipt_sha256", None)
            if not isinstance(checksum, str) or _sha256(_canonical_json(receipt)) != checksum:
                continue
            receipt["receipt_sha256"] = checksum
            receipt["receipt_hmac_sha256"] = receipt_hmac
            base = str(base or "")
            commit = str(commit or "")
            manifest_hash = str(manifest_hash or "")
            if (not isinstance(task_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", task_id)
                    or task_state != "complete" or phase != "complete"
                    or outbox_state not in {"pending", "delivered"}
                    or receipt.get("task_id") != task_id
                    or receipt.get("state") != "committed_for_review"
                    or receipt.get("output") != "HANDOFF/OC_FINDINGS.md"
                    or receipt.get("base_commit") != base
                    or receipt.get("commit") != commit
                    or receipt.get("manifest_sha256") != manifest_hash
                    or not re.fullmatch(r"[0-9a-f]{40}", base)
                    or not re.fullmatch(r"[0-9a-f]{40}", commit)
                    or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash)
                    or not isinstance(receipt.get("branch"), str)
                    or not receipt["branch"].strip()):
                continue

            jev = receipt.get("jev")
            if (not isinstance(jev, dict) or jev.get("verdict") != "pass"
                    or jev.get("is_fallback") is not False
                    or jev.get("site") != "oc-handoff"
                    or not isinstance(jev.get("model"), str) or not jev["model"].strip()):
                continue
            confidence = jev.get("confidence")
            supported = jev.get("supported")
            cost = jev.get("cost")
            min_confidence = jev.get("min_confidence")
            input_tokens = jev.get("input_tokens")
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in (confidence, supported, cost, min_confidence)):
                continue
            try:
                confidence_value = float(confidence)
                supported_value = float(supported)
                cost_value = float(cost)
                threshold_value = float(min_confidence)
            except (OverflowError, ValueError):
                continue
            if (not all(math.isfinite(value) for value in (
                        confidence_value, supported_value, cost_value, threshold_value))
                    or not 0.0 <= confidence_value <= 1.0
                    or not 0.0 <= supported_value <= 1.0
                    or not 0.0 <= threshold_value <= 1.0
                    or supported_value < threshold_value
                    or (confidence_value > 0.0 and confidence_value < threshold_value)
                    or not 0.0 <= cost_value <= 0.05
                    or isinstance(input_tokens, bool)
                    or not isinstance(input_tokens, int)
                    or not 0 <= input_tokens <= 1024):
                continue

            archive_path = _safe_repo_file(
                root, ".harness/oc_handoff/archive/" + str(task_id) + ".json")
            if archive_path is None:
                continue
            manifest_bytes = _read_bounded(archive_path, _OC_MANIFEST_MAX_BYTES)
            if manifest_bytes is None:
                continue
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            payload_keys = (
                "version", "task_id", "repo_sha", "approved_by", "approved_at",
                "nonce", "expires_at", "findings",
            )
            if not isinstance(manifest, dict) or any(key not in manifest for key in payload_keys):
                continue
            payload = {key: manifest[key] for key in payload_keys}
            attestation = manifest.get("attestation")
            if (payload["task_id"] != task_id or payload["repo_sha"] != base
                    or not isinstance(payload["approved_by"], str)
                    or not payload["approved_by"].strip()
                    or isinstance(payload["version"], bool)
                    or payload["version"] != 1
                    or isinstance(payload["approved_at"], bool)
                    or not isinstance(payload["approved_at"], int)
                    or isinstance(payload["expires_at"], bool)
                    or not isinstance(payload["expires_at"], int)
                    or not payload["approved_at"] < payload["expires_at"]
                    or payload["expires_at"] - payload["approved_at"] > 900
                    or not isinstance(payload["nonce"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", payload["nonce"])
                    or not isinstance(payload["findings"], list)
                    or not 1 <= len(payload["findings"]) <= 4
                    or _sha256(_canonical_json(payload)) != manifest_hash
                    or not isinstance(attestation, dict)
                    or attestation.get("verifier_id") != payload["approved_by"]
                    or attestation.get("expires_at") != payload["expires_at"]):
                continue
            from .attest import compute_diff_sha256, parse_attestation

            def verify_signature(signed_payload: bytes, signature: str) -> bool:
                expected = hmac.new(key, signed_payload, hashlib.sha512).hexdigest()
                return hmac.compare_digest(expected, signature)

            try:
                parse_attestation(
                    json.dumps(attestation, sort_keys=True),
                    diff_sha256=compute_diff_sha256(_canonical_json(payload)),
                    base_sha256=_sha256(base.encode("ascii")),
                    round_nonce=payload["nonce"],
                    now=payload["approved_at"] + 1,
                    verify_signature=verify_signature,
                )
            except (HarnessError, ValueError, TypeError, KeyError):
                continue

            marker = ("## Approved OC handoff " + str(task_id) + "\n").encode("utf-8")
            manifest_marker = ("\"manifest_sha256\": \"" + manifest_hash + "\"").encode("ascii")
            if (receipt.get("output_sha256") != output_hash
                    or marker not in output_bytes or manifest_marker not in output_bytes):
                continue

            parent_line = _git_bytes(
                root, "rev-list", "--parents", "-n", "1", commit, max_bytes=128)
            if parent_line is None or parent_line.decode("ascii", "ignore").split() != [commit, base]:
                continue
            changed = _git_bytes(
                root, "diff-tree", "--no-commit-id", "--name-only", "-r", base,
                commit, max_bytes=256)
            if changed is None or changed.decode("utf-8", "replace").splitlines() != [
                    "HANDOFF/OC_FINDINGS.md"]:
                continue
            committed_output = _git_bytes(
                root, "show", commit + ":HANDOFF/OC_FINDINGS.md",
                max_bytes=_OC_HANDOFF_MAX_BYTES)
            if committed_output is None or _sha256(committed_output) != output_hash:
                continue
            if _git_bytes(
                    root, "merge-base", "--is-ancestor", commit, "HEAD",
                    max_bytes=0) is None:
                continue
            return True
        except (OSError, RuntimeError, sqlite3.Error, OverflowError,
                ValueError, TypeError, KeyError, UnicodeError):
            continue
    return False


def _norm_phase(phase_id: str) -> str:
    raw = (phase_id or "").strip().upper().replace(" ", "-")
    if not raw:
        raise HarnessError("phase id is required")
    if raw.startswith("P") and raw[1:2].isdigit():
        return f"JEV-P{raw[1:2]}"
    if not raw.startswith("JEV-"):
        return f"JEV-{raw}" if raw.startswith("P") else raw
    return raw


def _status_row_for(roadmap_text: str, phase_id: str) -> Optional[str]:
    needles = {
        "JEV-P0": re.compile(r"JEV-P0|0 Contract|contract truth", re.I),
        "JEV-P1": re.compile(r"JEV-P1|One owner|one owner \+ lanes", re.I),
        "JEV-P2": re.compile(r"JEV-P2|2 Pillars|System One pillars", re.I),
        "JEV-P3": re.compile(r"JEV-P3|3 Utilization|utilization", re.I),
        "JEV-P4": re.compile(r"JEV-P4|4 Ops|ops / exit", re.I),
        "JEV-COMPLETION": re.compile(r"JEV-COMPLETION|completion score|dogfood 0-100|Accountability", re.I),
        "SITE": re.compile(r"SITE-\*|SITE-1|SITE-3\.\.9|proof bench site", re.I),
        "JEV-P5": re.compile(r"JEV-P5|issue-sort buckets", re.I),
        "HUL-A": re.compile(r"\bHUL-A\b", re.I),
        "HUL-B": re.compile(r"\bHUL-B\b", re.I),
        "HUL-C": re.compile(r"\bHUL-C\b", re.I),
        "HUL-D": re.compile(r"\bHUL-D\b", re.I),
        "JEV-LOG-SCHEMA": re.compile(r"JEV-LOG-schema", re.I),
        "JEV-LOG-PARSE": re.compile(r"JEV-LOG-parse", re.I),
        "JEV-LOG-FACTOR-PASS": re.compile(r"JEV-LOG-factor-pass", re.I),
        "JEV-LOG-JUDGMENT": re.compile(r"JEV-LOG-judgment", re.I),
        "JEV-LOG-ENVELOPE": re.compile(r"JEV-LOG-envelope", re.I),
        "JEV-LOG-CLI": re.compile(r"JEV-LOG-cli", re.I),
        "JEV-LOG-DOGFOOD": re.compile(r"JEV-LOG-dogfood", re.I),
        "MS": re.compile(r"`MS-\*`|cheapest-capable", re.I),
        "JEV-P6": re.compile(r"JEV-P6", re.I),
        "HG": re.compile(r"HG-\*|hourglass composition", re.I),
        "JEV-BAR": re.compile(r"JEV-BAR", re.I),
        "CLAUDE-LANE": re.compile(r"CLAUDE-LANE", re.I),
        "OC-HANDOFF": re.compile(r"OC-HANDOFF", re.I),
    }
    pat = needles.get(phase_id)
    if not pat or not roadmap_text:
        return None
    statusish = re.compile(
        r"complete|in progress|blocked|repair|\bopen\b|planned|MERGED|PR #", re.I)
    candidates: List[str] = []
    for line in roadmap_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not pat.search(stripped):
            continue
        if not statusish.search(stripped):
            continue
        # Skip pure work-item definition rows (ID | work text) without status language.
        if re.search(r"^\|\s*`?JEV-P\d-[a-z]", stripped, re.I) and "complete" not in stripped.lower() \
                and "in progress" not in stripped.lower():
            continue
        candidates.append(stripped)
    if not candidates:
        return None
    # Prefer rows that look like STATUS/tracker conclusions.
    def rank(row: str) -> int:
        low = row.lower()
        score = 0
        if "**complete**" in low or "**in progress" in low or "**open**" in low:
            score += 10
        if "pr #" in low or "merged" in low:
            score += 5
        # The canonical STATUS row spells out the merge; the tracker row often
        # cites only "PR #NN <sha>". Prefer the row that carries merge proof.
        if "merged" in low:
            score += 3
        if phase_id.lower() in low or "pillar" in low or "owner" in low or "accountability" in low:
            score += 2
        if re.search(r"policy|consent-confidence|min-confidence|triage|apply\|", low):
            score -= 5
        return score
    candidates.sort(key=rank, reverse=True)
    return candidates[0]


def collect_phase_evidence(repo_root: str, phase_id: str,
                           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Gather code-owned phase evidence from the repo + optional overrides."""
    phase = _norm_phase(phase_id)
    # A phase with no declared contract falls back to the generic
    # pr_pattern=None rule (PR #\d+ AND MERGED) -- never the bare "PR #"
    # substring, which let any PR mention pass regardless of merge state.
    contract = PHASE_CONTRACTS.get(
        phase, {"pr_pattern": None, "required_tests": [], "required_files": []})
    root = os.path.abspath(repo_root or os.getcwd())
    roadmap_path = os.path.join(root, "docs", "jev-roadmap.md")
    status_row = None
    if os.path.isfile(roadmap_path):
        try:
            with open(roadmap_path, encoding="utf-8") as fh:
                status_row = _status_row_for(fh.read(), phase)
        except OSError:
            status_row = None

    required_tests = list(contract.get("required_tests") or [])
    tests_present = []
    tests_missing = []
    for rel in required_tests:
        path = os.path.join(root, rel.replace("/", os.sep))
        (tests_present if os.path.isfile(path) else tests_missing).append(rel)

    required_files = list(contract.get("required_files") or [])
    files_missing = [
        rel for rel in required_files
        if not os.path.isfile(os.path.join(root, rel.replace("/", os.sep)))
    ]

    evidence: Dict[str, Any] = {
        "phase": phase,
        "repo_root": root,
        "status_row": status_row,
        "pr_pattern": contract.get("pr_pattern"),
        "user_facing": bool(contract.get("user_facing")),
        "required_tests": required_tests,
        "tests_present": tests_present,
        "tests_missing": tests_missing,
        "required_files": required_files,
        "files_missing": files_missing,
        "pr_merged": False,
        "origin_evidence": "",
        "local_gates_green": False,
        "ci_green": False,
        "gate_output": None,
        "ci_run": None,
        "open_blockers": [],
        "notes": [],
    }

    if status_row:
        pattern = contract.get("pr_pattern")
        lowered = status_row.lower()
        mentions_pr = phase_status_mentions_pr(status_row, pattern)
        open_pr = bool(
            re.search(r"\b(?:PR|pull request)\s*(?:#\d+)?\s*(?:is\s+)?open\b", lowered)
            or re.search(r"\bopen\s+(?:PR|pull request)(?:\s+#\d+)?\b", lowered)
            or re.search(r"\bno pr\b", lowered)
        )
        merged_word = "merged" in lowered or "merge" in lowered
        # Presence of a PR id is not merge evidence while the row still says open.
        evidence["pr_merged"] = bool(mentions_pr and merged_word and not open_pr)
        evidence["origin_evidence"] = status_row
        claims_complete = phase_status_claims_complete(status_row)
        has_blocker = phase_status_has_blocker(status_row)
        if claims_complete and has_blocker:
            evidence["open_blockers"].append(
                "STATUS claims complete while row still lists open/repair/fail evidence")
        elif has_blocker:
            evidence["open_blockers"].append("STATUS row not complete")
        if open_pr:
            evidence["open_blockers"].append("STATUS says PR open or no PR")
        if evidence["pr_merged"]:
            evidence["ci_green"] = True  # merged PR implies checks were required green
            evidence["local_gates_green"] = True  # operator claimed green on merge path
    else:
        evidence["notes"].append(f"no STATUS row found for {phase} in docs/jev-roadmap.md")

    if tests_missing:
        evidence["open_blockers"].append("missing required tests: " + ", ".join(tests_missing))
        evidence["local_gates_green"] = False
        evidence["ci_green"] = False
    if files_missing:
        evidence["open_blockers"].append("missing required files: " + ", ".join(files_missing))

    if phase == "OC-HANDOFF":
        evidence["oc_handoff_verified"] = _valid_oc_handoff_receipt(root)
        if not evidence["oc_handoff_verified"]:
            evidence["open_blockers"].append(
                "no verified signed OC handoff receipt with a passing native Jev result "
                "and exact-file audited commit")
    else:
        evidence["oc_handoff_verified"] = True

    if extra:
        for key in ("pr_merged", "local_gates_green", "ci_green", "origin_evidence"):
            if key in extra:
                evidence[key] = bool(extra[key]) if key != "origin_evidence" else extra[key]
        for key in ("gate_output", "ci_run"):
            if extra.get(key):
                evidence[key] = extra[key]
        if isinstance(extra.get("open_blockers"), list):
            evidence["open_blockers"] = list(extra["open_blockers"])
        if extra.get("status_row"):
            evidence["status_row"] = extra["status_row"]
        if extra.get("gate_output"):
            evidence["notes"].append(str(extra["gate_output"])[:500])

    # Deduplicate blockers
    seen = set()
    uniq = []
    for b in evidence["open_blockers"]:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    evidence["open_blockers"] = uniq
    return evidence


def _hard_gates(evidence: Dict[str, Any]) -> Dict[str, bool]:
    tests_ok = not bool(evidence.get("tests_missing"))
    files_ok = not bool(evidence.get("files_missing"))
    phase = str(evidence.get("phase") or "").upper()
    oc_handoff_ok = (phase != "OC-HANDOFF"
                     or evidence.get("oc_handoff_verified") is True)
    blockers = list(evidence.get("open_blockers") or [])
    return {
        "pr_merged": bool(evidence.get("pr_merged")),
        "origin_evidence": bool(str(evidence.get("origin_evidence") or "").strip()),
        "required_tests_present": tests_ok,
        "required_files_present": files_ok,
        "oc_handoff_verified": oc_handoff_ok,
        "local_gates_green": bool(evidence.get("local_gates_green")),
        "ci_green": bool(evidence.get("ci_green")),
        "no_open_blockers": not blockers,
    }


def _mechanical_score(gates: Dict[str, bool]) -> float:
    return float(sum(pts for name, pts in _HARD_GATE_POINTS.items() if gates.get(name)))


def _resolve_completion_pack(repo_root: Optional[str], path: Optional[str]) -> Dict[str, Any]:
    """Load the operator completion pack, falling back to the in-code
    default only when the caller did not name a path AND the canonical file
    is absent (hermetic temp repos in tests). An explicitly-named path that
    is missing or invalid still raises ``HarnessError``."""
    root = os.path.abspath(repo_root or os.getcwd())
    if path is None:
        rel = DEFAULT_PHASE_COMPLETION_PACK
        full = rel if os.path.isabs(rel) else os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return validate_completion_pack(DEFAULT_COMPLETION_PACK)
    return load_completion_pack(root, path)


def _coerce_pack(repo_root: Optional[str], pack: Any) -> Dict[str, Any]:
    """Accept a raw/validated pack dict, a pack file path, or ``None``
    (canonical default) -- one call site, one pack owner."""
    if isinstance(pack, dict):
        return validate_completion_pack(pack)
    return _resolve_completion_pack(repo_root, pack)


def score_phase_completion(
    evidence: Dict[str, Any],
    *,
    jev_policy=None,
    min_score: float = PHASE_COMPLETE_MIN_SCORE,
    pack: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return the dogfoodable phase completion judgment: hard gates (code)
    plus the JEV-BAR full sentiment pass -- every evidence axis gets a
    declared level, every axis below the bar maps to a declared improvement
    bucket, so the result is a prioritized improvement list that drives work
    to bar pass (0-hallucination, fail-closed: Jev may only lower a
    code-authority axis, never raise it past the code-owned fact)."""
    if not isinstance(evidence, dict) or not evidence.get("phase"):
        raise HarnessError("phase evidence requires a phase id")
    gates = _hard_gates(evidence)
    mechanical = _mechanical_score(gates)

    pack_doc = _coerce_pack(evidence.get("repo_root"), pack)
    levels = pack_doc["sentiment"]["levels"]
    ordinals = pack_doc["sentiment"]["ordinals"]
    blocking_max_index = pack_doc["sentiment"]["blocking_max_index"]
    improve_below_index = pack_doc["sentiment"]["improve_below_index"]

    code_levels = heuristic_completion_sentiment(evidence, pack_doc)

    live_result = live_judgment = None
    if jev_policy is not None:
        live_result, _live_structural, live_judgment = jev_policy.evaluate_phase_completion(
            evidence, pack_doc)

    def live_index(axis: str) -> Optional[int]:
        if live_judgment is None:
            return None
        return live_judgment.get("live_levels", {}).get(axis)

    def live_valid(axis: str) -> bool:
        return (live_judgment is not None and not live_judgment.get("is_fallback")
                and live_index(axis) is not None)

    axis_info: Dict[str, Dict[str, Any]] = {}
    for axis, spec in pack_doc["axes"].items():
        code_idx = int(code_levels.get(axis, 2))
        l_idx = live_index(axis)
        valid = live_valid(axis)
        if spec["authority"] == "code":
            if valid:
                effective = min(code_idx, l_idx)
                source = "live" if l_idx < code_idx else "code"
            else:
                effective, source = code_idx, "code"
        else:  # authority == "jev"
            if valid:
                effective, source = l_idx, "live"
            else:
                effective, source = code_idx, "code"
        confidence = (live_judgment.get("live_confidence", {}).get(axis)
                     if (live_judgment is not None and valid) else None)
        axis_info[axis] = {
            "bucket": spec["bucket"],
            "level": levels[effective],
            "index": effective,
            "ordinal": ordinals[effective],
            "source": source,
            "code_index": code_idx,
            "live_index": l_idx,
            "confidence": confidence,
        }

    semantic_score = (sum(info["ordinal"] for info in axis_info.values()) / len(axis_info)
                      if axis_info else 0.0)

    all_hard_pass = all(gates.values())
    if not all_hard_pass:
        # Hard fail: score cannot clear the gate no matter how nice the prose.
        combined = min(mechanical, semantic_score, min_score - 0.01)
        combined = max(0.0, combined)
    else:
        combined = 0.7 * mechanical + 0.3 * semantic_score
        combined = max(0.0, min(COMPLETION_SCORE_MAX, combined))

    blocking_axes = sorted(axis for axis, info in axis_info.items()
                           if info["index"] <= blocking_max_index)
    bar_pass = bool(all_hard_pass and combined >= float(min_score) and not blocking_axes)
    can_mark_complete = bar_pass

    blockers = list(evidence.get("open_blockers") or [])
    if not gates.get("pr_merged"):
        blockers.append("hard gate failed: pr_merged")
    if not gates.get("origin_evidence"):
        blockers.append("hard gate failed: origin_evidence")
    if not gates.get("required_tests_present"):
        blockers.append("hard gate failed: required_tests_present")
    if not gates.get("required_files_present"):
        blockers.append("hard gate failed: required_files_present")
    if not gates.get("oc_handoff_verified"):
        blockers.append("hard gate failed: oc_handoff_verified")
    if not gates.get("local_gates_green"):
        blockers.append("hard gate failed: local_gates_green")
    if not gates.get("ci_green"):
        blockers.append("hard gate failed: ci_green")
    if not gates.get("no_open_blockers"):
        blockers.append("hard gate failed: open_blockers")

    # --- improvements: sentiment axes below the bar + failed hard gates,
    # deduped by bucket (most severe wins), sorted ordinal asc then axis id.
    improvement_by_bucket: Dict[str, Dict[str, Any]] = {}

    def add_improvement(bucket_id: Optional[str], *, axis: Optional[str],
                        level: Optional[str], ordinal: Optional[float], source: str):
        if not bucket_id or bucket_id not in pack_doc["buckets"]:
            return
        entry = pack_doc["buckets"][bucket_id]
        candidate = {
            "axis": axis, "bucket": bucket_id, "label": entry["label"],
            "path_id": entry["path_id"],
            "suggested_next_action": entry["suggested_next_action"],
            "level": level, "ordinal": ordinal, "source": source,
            "confidence": (axis_info[axis]["confidence"] if axis in axis_info else None),
        }
        existing = improvement_by_bucket.get(bucket_id)
        if existing is None or (ordinal is not None and (
                existing["ordinal"] is None or ordinal < existing["ordinal"])):
            improvement_by_bucket[bucket_id] = candidate

    for axis, info in axis_info.items():
        if info["index"] < improve_below_index:
            add_improvement(info["bucket"], axis=axis, level=info["level"],
                            ordinal=info["ordinal"], source=info["source"])

    for gate_name, bucket_id in _HARD_GATE_BUCKETS.items():
        if not gates.get(gate_name):
            # A code-certain hard-gate failure outranks any semantic axis
            # flagging the same bucket -- ordinal -1 always dedupes to it.
            add_improvement(bucket_id, axis=None, level=None, ordinal=-1.0,
                            source="hard_gate")

    primary_gap = live_judgment.get("primary_gap") if live_judgment else None
    if primary_gap and primary_gap not in improvement_by_bucket and primary_gap in pack_doc["buckets"]:
        entry = pack_doc["buckets"][primary_gap]
        improvement_by_bucket[primary_gap] = {
            "axis": None, "bucket": primary_gap, "label": entry["label"],
            "path_id": entry["path_id"],
            "suggested_next_action": entry["suggested_next_action"],
            "level": None, "ordinal": None, "source": "live", "confidence": None,
        }

    improvements = sorted(
        improvement_by_bucket.values(),
        key=lambda item: (item["ordinal"] if item["ordinal"] is not None else 50.0,
                          item["axis"] or ""))
    for item in improvements:
        item["primary"] = bool(primary_gap and item["bucket"] == primary_gap)

    # "Overall" sentiment: the declared level nearest the aggregate score --
    # the honest single-word read of where the combined evidence sits.
    overall_index = min(range(len(ordinals)),
                        key=lambda i: (abs(ordinals[i] - semantic_score), i))

    if jev_policy is not None and live_result is not None:
        semantic = {
            "score": round(semantic_score, 2),
            "is_fallback": bool(live_judgment.get("is_fallback")) if live_judgment else True,
            "model": live_result.model,
            "site": PHASE_COMPLETION_SITE,
            "verdict": live_result.verdict,
            "cost": float(live_result.cost or 0.0),
            "reasons": list(live_result.reasons or []),
        }
    else:
        semantic = {
            "score": round(semantic_score, 2),
            "is_fallback": True,
            "model": "local-heuristic",
            "site": PHASE_COMPLETION_SITE,
            "verdict": "fallback",
        }

    return {
        "phase": evidence.get("phase"),
        "score": round(combined, 2),
        "min_score": float(min_score),
        "can_mark_complete": bool(can_mark_complete),
        "hard_gates": gates,
        "mechanical_score": round(mechanical, 2),
        "semantic": semantic,
        "blockers": blockers,
        "status_row": evidence.get("status_row"),
        "evidence": {
            "pr_merged": evidence.get("pr_merged"),
            "origin_evidence": evidence.get("origin_evidence"),
            "tests_missing": evidence.get("tests_missing") or [],
            "open_blockers": evidence.get("open_blockers") or [],
        },
        "sentiment": {
            "pack_id": pack_doc["id"],
            "levels": list(levels),
            "axes": {axis: {k: v for k, v in info.items() if k != "bucket"}
                     for axis, info in axis_info.items()},
            "overall": {"ordinal": ordinals[overall_index], "level": levels[overall_index]},
        },
        "improvements": improvements,
        "bar": {"pass": bar_pass, "blocking_axes": blocking_axes,
                "min_score": float(min_score)},
    }


def load_evidence_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read evidence file: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessError("evidence file must contain a JSON object")
    return data


def dogfood_phase(
    repo_root: str,
    phase_id: str,
    *,
    evidence_path: Optional[str] = None,
    settings=None,
    transport=None,
    governor=None,
    ledger=None,
    use_live_jev: bool = True,
    min_score: float = PHASE_COMPLETE_MIN_SCORE,
    pack_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect evidence and score one phase for mission dogfooding."""
    extra = load_evidence_file(evidence_path) if evidence_path else None
    evidence = collect_phase_evidence(repo_root, phase_id, extra=extra)
    jev_policy = None
    if use_live_jev and settings is not None:
        from .jev_policy import policy_for
        jev_policy = policy_for(settings, transport=transport, governor=governor, ledger=ledger)
    return score_phase_completion(
        evidence, jev_policy=jev_policy, min_score=min_score, pack=pack_path)


def score_all_phases(
    repo_root: str,
    *,
    jev_policy=None,
    min_score: float = PHASE_COMPLETE_MIN_SCORE,
    pack: Optional[Any] = None,
) -> Dict[str, Any]:
    """Dogfood the JEV bar across every declared phase contract in one pass
    (JEV-BAR ``--all``). ``false_complete`` names phases whose STATUS row
    claims complete while the bar honestly fails -- the exact accountability
    gap this gate exists to surface; an honestly-open phase failing the bar
    is expected, not an error."""
    root = os.path.abspath(repo_root or os.getcwd())
    pack_doc = _coerce_pack(root, pack)
    phases: Dict[str, Any] = {}
    passing: List[str] = []
    failing: List[str] = []
    false_complete: List[str] = []
    for phase_id in sorted(PHASE_CONTRACTS):
        evidence = collect_phase_evidence(root, phase_id)
        result = score_phase_completion(
            evidence, jev_policy=jev_policy, min_score=min_score, pack=pack_doc)
        phases[phase_id] = result
        if result["bar"]["pass"]:
            passing.append(phase_id)
        else:
            failing.append(phase_id)
            if phase_status_claims_complete(evidence.get("status_row") or ""):
                false_complete.append(phase_id)
    return {"phases": phases, "passing": passing, "failing": failing,
            "false_complete": false_complete}
