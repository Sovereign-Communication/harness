import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from examples.oc_handoff import worker
from harness.errors import HarnessError


class FakeJevResult:
    def __init__(self, verdict="pass", is_fallback=False):
        self.verdict = verdict
        self.is_fallback = is_fallback

    def is_passing(self, _threshold):
        return self.verdict == "pass"


class FakeJevPolicy:
    settings = type("Settings", (), {"min_confidence": 0.7})()

    def __init__(self, *, verdict="pass", fallback=False):
        self.result = FakeJevResult(verdict, fallback)
        self.calls = []

    def evaluate_candidate(self, original, candidate, instruction, file_path,
                           **kwargs):
        self.calls.append((original, candidate, instruction, file_path, kwargs))
        return self.result, {
            "verdict": self.result.verdict,
            "confidence": 0.9,
            "supported": 0.95,
            "cost": 0.0,
            "input_tokens": 100,
            "is_fallback": self.result.is_fallback,
            "model": "jev-test",
            "site": "oc-handoff",
        }


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.root / "HANDOFF").mkdir()
        (self.root / "HANDOFF" / "CEO_STATE.md").write_text(
            "seat state stays untouched\n", encoding="utf-8")
        (self.root / "evidence.md").write_text("Observed failure at line two.\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.root), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "base",
        ], check=True)
        self.head = worker._repo_head(self.root)
        self.key = b"k" * 32
        self.approver = "operator-1"
        paths = worker._state_paths(self.root)
        self.candidate = {
            "task_id": "finding-001",
            "repo_sha": self.head,
            "findings": [{
                "finding_id": "issue-001",
                "severity": "high",
                "summary": "The worker accepts an untrusted command.",
                "evidence": [{"path": "evidence.md", "line": 1}],
                "recommendation": "Use the fixed local verifier.",
            }],
        }
        (paths["candidates"] / "finding-001.json").write_text(
            json.dumps(self.candidate), encoding="utf-8")

    def _approve(self, *, task_id="finding-001", now=1_000):
        return worker.approve_candidate(
            task_id, root=self.root, key=self.key, approver=self.approver,
            confirmation="APPROVE " + task_id, now=now,
        )

    def test_fixed_root_and_single_output_path_with_exact_commit_audit(self):
        self._approve()
        jev = FakeJevPolicy()
        receipt = worker.run_once(
            root=self.root, key=self.key, expected_approver=self.approver,
            jev_policy=jev, now=1_001,
        )
        self.assertEqual(receipt["state"], "committed_for_review")
        self.assertEqual(receipt["output"], "HANDOFF/OC_FINDINGS.md")
        worktree = Path(self.root / ".harness" / "wt" / receipt["branch"].split("/")[-1])
        changed = subprocess.check_output(
            ["git", "-C", str(worktree), "diff-tree", "--no-commit-id",
             "--name-only", "-r", receipt["commit"]], text=True).splitlines()
        self.assertEqual(changed, ["HANDOFF/OC_FINDINGS.md"])
        self.assertEqual((self.root / "HANDOFF" / "CEO_STATE.md").read_text(),
                         "seat state stays untouched\n")
        self.assertEqual(jev.calls[0][3], "HANDOFF/OC_FINDINGS.md")
        self.assertFalse(jev.calls[0][4].get("verify_argv"))

    def test_manifest_cannot_select_repo_file_state_or_executable(self):
        bad = dict(self.candidate, repo=str(self.root), file="README.md",
                   verify_argv=["python", "-c", "print('x')"])
        with self.assertRaisesRegex(ValueError, "candidate_fields_invalid"):
            worker._validate_candidate(bad, self.root)

    def test_worktree_parent_symlink_is_refused_before_state_setup(self):
        (self.root / ".harness").mkdir(exist_ok=True)
        path_type = type(self.root)
        original_is_symlink = path_type.is_symlink
        redirected = self.root / ".harness" / "wt"
        redirected.rmdir()

        def reports_link(path):
            return path == redirected or original_is_symlink(path)

        with mock.patch.object(path_type, "is_symlink", reports_link):
            with self.assertRaisesRegex(ValueError, "worktree_parent_symlink_refused"):
                worker._state_paths(self.root)
        self.assertFalse(redirected.exists())

    def test_worktree_handle_outside_checkout_is_refused(self):
        self._approve()
        outside = Path(self.temp.name) / "outside-worktree"
        outside.mkdir()
        handle = {"node_id": "oc-handoff-finding-001", "path": str(outside),
                  "branch": "harness/outside", "base": self.head}
        with mock.patch("harness.worktree.WorktreeIsolation.create", return_value=handle):
            with self.assertRaisesRegex(RuntimeError, "worktree_out_of_scope"):
                worker.run_once(
                    root=self.root, key=self.key,
                    expected_approver=self.approver,
                    jev_policy=FakeJevPolicy(), now=1_001,
                )
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((outside / "HANDOFF").exists())

    def test_wrong_confirmation_does_not_sign_candidate(self):
        with self.assertRaisesRegex(ValueError, "approval_confirmation_mismatch"):
            worker.approve_candidate(
                "finding-001", root=self.root, key=self.key,
                approver=self.approver, confirmation="APPROVE something else",
                now=1_000,
            )
        self.assertFalse((self.root / worker.APPROVED_REL / "finding-001.json").exists())

    def test_candidate_changed_after_review_is_not_signed(self):
        paths = worker._state_paths(self.root)
        candidate = paths["candidates"] / "finding-001.json"
        reviewed_sha = worker._sha(candidate.read_bytes())
        changed = dict(self.candidate)
        changed["findings"] = [dict(self.candidate["findings"][0],
                                    summary="changed after review")]
        candidate.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "candidate_changed_after_review"):
            worker.approve_candidate(
                "finding-001", root=self.root, key=self.key,
                approver=self.approver, confirmation="APPROVE finding-001",
                now=1_000, expected_candidate_sha256=reviewed_sha,
            )

    def test_signature_tamper_expiry_and_approver_mismatch_fail_closed(self):
        approved = self._approve(now=1_000)
        signed = json.loads(approved.read_text(encoding="utf-8"))
        changed = dict(signed)
        changed["findings"] = [dict(signed["findings"][0], summary="tampered")]
        with self.assertRaises(HarnessError):
            worker._validate_manifest(
                changed, root=self.root, key=self.key,
                expected_approver=self.approver, now=1_001,
            )
        with self.assertRaisesRegex(ValueError, "approver_mismatch"):
            worker._validate_manifest(
                signed, root=self.root, key=self.key,
                expected_approver="different-operator", now=1_001,
            )
        with self.assertRaises(ValueError):
            worker._validate_manifest(
                signed, root=self.root, key=self.key,
                expected_approver=self.approver, now=1_901,
            )

    def test_invalid_evidence_and_manifest_commands_are_rejected(self):
        bad = dict(self.candidate)
        bad["findings"] = [dict(self.candidate["findings"][0], evidence=[
            {"path": "../outside", "line": 1}])]
        with self.assertRaisesRegex(ValueError, "path_scope"):
            worker._validate_candidate(bad, self.root)
        bad = dict(self.candidate)
        bad["findings"] = [dict(self.candidate["findings"][0], verify_argv=["sh"])]
        with self.assertRaisesRegex(ValueError, "finding_fields_invalid"):
            worker._validate_candidate(bad, self.root)

    def test_jev_fallback_or_failure_never_writes(self):
        self._approve()
        with self.assertRaisesRegex(RuntimeError, "native_jev_refused"):
            worker.run_once(
                root=self.root, key=self.key, expected_approver=self.approver,
                jev_policy=FakeJevPolicy(fallback=True), now=1_001,
            )
        self.assertFalse((self.root / "HANDOFF" / "OC_FINDINGS.md").exists())

    def test_oversized_jev_request_is_refused_before_policy_call(self):
        policy = FakeJevPolicy()
        manifest = {"findings": [{
            "finding_id": "finding-001", "severity": "high",
            "summary": "🧭" * 180,
            "evidence": [{"path": "evidence.md", "line": 1}],
            "recommendation": "Use a fixed verifier.",
        }]}
        candidate = worker._jev_candidate(manifest)
        self.assertGreater(worker._jev_request_size(policy, candidate),
                           worker.MAX_JEV_REQUEST_BYTES)
        with self.assertRaisesRegex(RuntimeError, "jev_request_too_large"):
            worker._evaluate_jev(policy, manifest, "finding-001")
        self.assertEqual(policy.calls, [])

    def test_native_jev_cap_never_raises_operator_ceiling(self):
        for configured, expected in ((0.01, 0.01), (0.50, worker.MAX_JEV_COST)):
            with self.subTest(configured=configured):
                settings = SimpleNamespace(jev_api_key="test-key", max_cost=configured)
                with mock.patch("harness.config.load_settings", return_value=settings), \
                        mock.patch("harness.session.jev_face_governor",
                                   return_value=object()) as governor, \
                        mock.patch("harness.session.jev_for", return_value=object()), \
                        mock.patch("harness.session.ledger_for", return_value=object()):
                    worker._native_jev_policy()
                governor.assert_called_once_with(
                    settings, max_cost_override=expected)

    def test_replay_rejected_and_receipt_acknowledged(self):
        self._approve()
        first = worker.run_once(
            root=self.root, key=self.key, expected_approver=self.approver,
            jev_policy=FakeJevPolicy(), now=1_001,
        )
        self.assertEqual(worker.pending_receipts(root=self.root), [first])
        worker.acknowledge(first["task_id"], first["receipt_sha256"],
                           root=self.root, now=1_002)
        self.assertEqual(worker.pending_receipts(root=self.root), [])
        paths = worker._state_paths(self.root)
        manifest = json.loads((paths["archive"] / "finding-001.json").read_text())
        with worker._connect(paths["db"]) as db:
            with self.assertRaisesRegex(ValueError, "manifest_replay_or_duplicate"):
                worker._process_manifest(
                    self.root, paths, db, manifest,
                    paths["archive"] / "finding-001.json", key=self.key,
                    expected_approver=self.approver, now=1_003,
                    jev_policy=FakeJevPolicy(),
                )

    def test_crash_after_commit_recovers_once_without_second_jev_call(self):
        self._approve()
        jev = FakeJevPolicy()
        with self.assertRaisesRegex(RuntimeError, "injected_interruption"):
            worker.run_once(
                root=self.root, key=self.key, expected_approver=self.approver,
                jev_policy=jev, now=1_001, crash_at="after_commit",
            )
        recovered = worker.run_once(
            root=self.root, key=self.key, expected_approver=self.approver,
            jev_policy=FakeJevPolicy(), now=1_002,
        )
        self.assertEqual(recovered["task_id"], "finding-001")
        self.assertEqual(len(worker.pending_receipts(root=self.root)), 1)
        self.assertEqual(len(jev.calls), 1)

    def test_crash_after_write_recovers_commit_without_reapplying(self):
        self._approve()
        jev = FakeJevPolicy()
        with self.assertRaisesRegex(RuntimeError, "injected_interruption"):
            worker.run_once(
                root=self.root, key=self.key, expected_approver=self.approver,
                jev_policy=jev, now=1_001, crash_at="after_write",
            )
        recovered = worker.run_once(
            root=self.root, key=self.key, expected_approver=self.approver,
            jev_policy=FakeJevPolicy(), now=1_002,
        )
        self.assertEqual(recovered["state"], "committed_for_review")
        self.assertEqual(len(worker.pending_receipts(root=self.root)), 1)
        self.assertEqual(len(jev.calls), 1)

    def test_crash_before_write_becomes_uncertain_and_is_not_replayed(self):
        self._approve()
        with self.assertRaisesRegex(RuntimeError, "injected_interruption"):
            worker.run_once(
                root=self.root, key=self.key, expected_approver=self.approver,
                jev_policy=FakeJevPolicy(), now=1_001, crash_at="before_write",
            )
        self.assertFalse((self.root / "HANDOFF" / "OC_FINDINGS.md").exists())
        with self.assertRaisesRegex(RuntimeError, "prior_task_requires_review"):
            worker.run_once(
                root=self.root, key=self.key, expected_approver=self.approver,
                jev_policy=FakeJevPolicy(), now=1_002,
            )
        rows = worker.status(root=self.root)
        self.assertEqual(rows[0]["state"], "uncertain")

    def test_unexpected_worktree_file_blocks_commit(self):
        self._approve()
        original_write = worker._write_atomic

        def add_extra(path, content):
            original_write(path, content)
            (path.parent / "unexpected.txt").write_text("no", encoding="utf-8")

        with mock.patch.object(worker, "_write_atomic", side_effect=add_extra):
            with self.assertRaisesRegex(RuntimeError, "exact_file_precommit_audit_failed"):
                worker.run_once(
                    root=self.root, key=self.key, expected_approver=self.approver,
                    jev_policy=FakeJevPolicy(), now=1_001,
                )
        self.assertFalse((self.root / "HANDOFF" / "OC_FINDINGS.md").exists())

    def test_squash_integrated_handoff_allows_the_next_task(self):
        self._approve()
        receipt = worker.run_once(
            root=self.root, key=self.key, expected_approver=self.approver,
            jev_policy=FakeJevPolicy(), now=1_001,
        )
        worktree = Path(self.root / ".harness" / "wt" / receipt["branch"].split("/")[-1])
        output = worktree / "HANDOFF" / "OC_FINDINGS.md"
        (self.root / "HANDOFF" / "OC_FINDINGS.md").write_bytes(output.read_bytes())
        subprocess.run(["git", "-C", str(self.root), "add", "HANDOFF/OC_FINDINGS.md"],
                       check=True)
        subprocess.run([
            "git", "-C", str(self.root), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "squash integration",
        ], check=True)
        paths = worker._state_paths(self.root)
        with worker._connect(paths["db"]) as db:
            worker._assert_ready_for_new_task(self.root, db)


if __name__ == "__main__":
    unittest.main()
