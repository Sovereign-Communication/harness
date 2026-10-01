"""Hermetic unit tests for GAP-diff-auth: Pre-write diff authorization.

Validates that proposed candidate diffs undergo structural, hunk-level,
boundary, and path traversal validation before touching the filesystem,
hash-binding the attestation and failing closed on violations.
"""
import os
import tempfile
import unittest

from harness.apply_gate import GatePolicy, authorize_proposed_diff
from harness.apply_state import ApplyRequest, RunState
from harness.errors import HarnessError
from tests._fake import FakeTransport, _gov


class _MockLedger:
    def __init__(self):
        self.entries = []

    def append(self, event, **kwargs):
        self.entries.append((event, kwargs))


class TestDiffAuthorizationDirect(unittest.TestCase):
    """Direct tests for authorize_proposed_diff function."""

    def test_valid_diff_computes_metadata_and_hash(self):
        diff_text = (
            "--- a/harness/apply.py\n"
            "+++ b/harness/apply.py\n"
            "@@ -10,3 +10,4 @@\n"
            " line1\n"
            "-line2\n"
            "+line2_modified\n"
            "+line3_new\n"
        )
        ledger = _MockLedger()
        res = authorize_proposed_diff(
            diff_text,
            instruction="Fix apply logic",
            allowed_files=["harness/apply.py"],
            ledger=ledger,
            task_id="task-100",
        )

        self.assertTrue(res["authorized"])
        self.assertIsNotNone(res["diff_hash"])
        self.assertEqual(len(res["diff_hash"]), 64)
        self.assertEqual(res["touched_files"], ["harness/apply.py"])
        self.assertEqual(res["hunks_count"], 1)
        self.assertEqual(res["lines_added"], 2)
        self.assertEqual(res["lines_deleted"], 1)
        self.assertEqual(res["instruction"], "Fix apply logic")

        # Verify ledger recorded diff_authorized
        self.assertEqual(len(ledger.entries), 1)
        event, fields = ledger.entries[0]
        self.assertEqual(event, "diff_authorized")
        self.assertEqual(fields["task_id"], "task-100")
        self.assertEqual(fields["diff_hash"], res["diff_hash"])

    def test_empty_diff_rejected_by_default(self):
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff("", instruction="empty")
        self.assertIn("diff content is empty", str(ctx.exception))

        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(None, instruction="none")
        self.assertIn("diff content is empty", str(ctx.exception))

    def test_empty_diff_allowed_when_flag_set(self):
        res = authorize_proposed_diff("", instruction="noop", allow_empty=True)
        self.assertTrue(res["authorized"])
        self.assertIsNone(res["diff_hash"])
        self.assertEqual(res["hunks_count"], 0)

    def test_no_hunks_rejected_when_not_empty_allowed(self):
        malformed = "Just some random text\nWithout any unified diff hunks\n"
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(malformed, instruction="test")
        self.assertIn("no valid change hunks", str(ctx.exception))

    def test_path_traversal_rejected_fail_closed(self):
        traversal_diff = (
            "--- a/harness/apply.py\n"
            "+++ b/../../../etc/shadow\n"
            "@@ -1,2 +1,2 @@\n"
            "-root:x\n"
            "+root:pwned\n"
        )
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(traversal_diff, instruction="exploit")
        self.assertIn("path traversal detected", str(ctx.exception))

    def test_relative_path_traversal_rejected(self):
        traversal_diff = (
            "--- a/sub/../../secret.txt\n"
            "+++ b/sub/../../secret.txt\n"
            "@@ -1 +1 @@\n"
            "-secret\n"
            "+leaked\n"
        )
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(traversal_diff, instruction="exploit")
        self.assertIn("path traversal detected", str(ctx.exception))

    def test_unauthorized_file_rejected(self):
        diff_text = (
            "--- a/harness/unauthorized.py\n"
            "+++ b/harness/unauthorized.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(
                diff_text,
                instruction="touch unauthorized file",
                allowed_files=["harness/authorized.py"],
            )
        self.assertIn("not in allowed files", str(ctx.exception))

    def test_max_diff_bytes_limit_enforced(self):
        diff_text = (
            "--- a/harness/large.py\n"
            "+++ b/harness/large.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+b\n"
        )
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(diff_text, max_diff_bytes=20)
        self.assertIn("diff size", str(ctx.exception))

    def test_max_diff_lines_limit_enforced(self):
        diff_text = (
            "--- a/harness/lines.py\n"
            "+++ b/harness/lines.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-a\n"
            "+b\n"
            " c\n"
        )
        with self.assertRaises(HarnessError) as ctx:
            authorize_proposed_diff(diff_text, max_diff_lines=3)
        self.assertIn("diff lines", str(ctx.exception))


class TestGatePolicyDiffAuthIntegration(unittest.TestCase):
    """Test GatePolicy.write_candidate populates state.diff_auth."""

    def test_write_candidate_populates_diff_auth(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "file.py")
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write("def foo():\n    return 1\n")

            state = RunState(
                rounds=[],
                history=[],
                current_content="def foo():\n    return 1\n",
            )
            ledger = _MockLedger()
            fake = FakeTransport()
            governor = _gov(fake)
            gate = GatePolicy(ledger, governor)

            req = ApplyRequest(
                task_id="task-diff-1",
                file_path=target,
                instruction="change return to 2",
                edit_snippet=None,
                verify_cmd="true",
                backend="harness",
                verify_only=False,
                max_lines=500,
                max_rounds=3,
                max_tokens=512,
                task_max_cost=0.1,
                max_rot=3,
                reasoning="auto",
                renew=True,
                allow_escalation=None,
                model="m",
                ordered=None,
                profiles=None,
                want_consent=False,
                original="def foo():\n    return 1\n",
                task_start_spent=0.0,
                continuation={},
                continuation_gate=None,
                task_runner=lambda cmd: (0, "ok"),
                cancel_check=None,
            )

            new_content = "def foo():\n    return 2\n"
            gate.write_candidate(req, state, new_content)

            self.assertIsNotNone(state.diff_auth)
            self.assertTrue(state.diff_auth["authorized"])
            self.assertIsNotNone(state.diff_auth["diff_hash"])
            self.assertEqual(state.diff_auth["hunks_count"], 1)
            self.assertEqual(state.diff_auth["lines_added"], 1)
            self.assertEqual(state.diff_auth["lines_deleted"], 1)

            # Assert file was written correctly
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), new_content)


if __name__ == "__main__":
    unittest.main()
