"""Hermetic contracts for the governed script runner (SCR-1).

No network, no provider calls: the Jev seam is a double, and every executed
script is a local two-line program inside a temp workspace.
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import script
from harness.errors import HarnessError


def _workspace(test):
    td = tempfile.TemporaryDirectory()
    test.addCleanup(td.cleanup)
    return Path(td.name)


class PolicyTests(unittest.TestCase):
    def test_scripting_is_off_unless_the_operator_turns_it_on(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(script.resolve_script_policy().enabled)
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "1"}):
            self.assertTrue(script.resolve_script_policy().enabled)

    def test_settings_win_over_the_environment(self):
        settings = mock.Mock(script_enabled=True, script_timeout_s=12,
                             script_max_output_bytes=4096,
                             script_allow_network=True)
        with mock.patch.dict(os.environ, {"HARNESS_SCRIPT_ENABLED": "0"}):
            policy = script.resolve_script_policy(settings)
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.timeout_s, 12)
        self.assertEqual(policy.max_output_bytes, 4096)
        self.assertTrue(policy.allow_network)

    def test_values_are_clamped_to_the_hard_caps(self):
        with mock.patch.dict(os.environ, {
                "HARNESS_SCRIPT_TIMEOUT_S": "99999",
                "HARNESS_SCRIPT_MAX_OUTPUT_BYTES": "999999999"}):
            policy = script.resolve_script_policy()
        self.assertEqual(policy.timeout_s, script.MAX_TIMEOUT_S)
        self.assertEqual(policy.max_output_bytes, script.MAX_OUTPUT_BYTES_CAP)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.root = _workspace(self)

    def test_disabled_refuses_before_anything_runs(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=self.root, allow_exec=True,
                              policy=script.ScriptPolicy(enabled=False))
        self.assertIn("scripting is disabled", str(ctx.exception))

    def test_without_consent_nothing_runs(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=self.root,
                              policy=script.ScriptPolicy(enabled=True))
        self.assertIn("explicit consent", str(ctx.exception))

    def test_an_explicit_workspace_is_required(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=None, allow_exec=True,
                              policy=script.ScriptPolicy(enabled=True))
        self.assertIn("workspace root", str(ctx.exception))

    def test_a_missing_workspace_is_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=str(self.root / "nope"),
                              allow_exec=True,
                              policy=script.ScriptPolicy(enabled=True))
        self.assertIn("not a directory", str(ctx.exception))

    def test_consent_is_bound_to_the_exact_bytes(self):
        policy = script.ScriptPolicy(enabled=True)
        other = hashlib.sha256(b"print('something else')").hexdigest()
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=self.root, allow_exec=True,
                              consent_sha=other, policy=policy)
        self.assertIn("different script", str(ctx.exception))

    def test_a_matching_consent_sha_runs(self):
        source = "print('approved')"
        policy = script.ScriptPolicy(enabled=True)
        receipt = script.run_script(
            source, root=self.root, allow_exec=True,
            consent_sha=script.script_sha256(source), policy=policy)
        self.assertEqual(receipt["status"], "ok")
        self.assertIn("approved", receipt["stdout"])

    def test_a_syntax_error_is_a_refusal_with_a_line_number(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("def broken(:\n    pass", root=self.root,
                              allow_exec=True,
                              policy=script.ScriptPolicy(enabled=True))
        self.assertIn("syntax error", str(ctx.exception))
        self.assertIn("line 1", str(ctx.exception))

    def test_an_empty_script_is_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("   ", root=self.root, allow_exec=True,
                              policy=script.ScriptPolicy(enabled=True))
        self.assertIn("empty", str(ctx.exception))


class RefusalBranchTests(unittest.TestCase):
    """The refusal branches that only a hostile or broken environment hits."""

    def setUp(self):
        self.root = _workspace(self)
        self.policy = script.ScriptPolicy(enabled=True)

    def test_an_oversized_script_is_refused(self):
        with mock.patch.object(script, "MAX_SOURCE_CHARS", 8):
            with self.assertRaises(HarnessError) as ctx:
                script.run_script("print('far too long')", root=self.root,
                                  allow_exec=True, policy=self.policy)
        self.assertIn("larger than", str(ctx.exception))

    def test_a_malformed_consent_sha_is_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=self.root, allow_exec=True,
                              consent_sha="not-a-digest", policy=self.policy)
        self.assertIn("not a sha256 digest", str(ctx.exception))

    def test_cancellation_before_start_runs_nothing(self):
        with self.assertRaises(HarnessError) as ctx:
            script.run_script("print(1)", root=self.root, allow_exec=True,
                              policy=self.policy,
                              cancel_check=lambda: True)
        self.assertIn("cancelled before", str(ctx.exception))
        self.assertFalse((self.root / ".harness").exists())

    def test_a_run_directory_outside_the_workspace_is_refused(self):
        with mock.patch.object(script, "_within", return_value=False):
            with self.assertRaises(HarnessError) as ctx:
                script.run_script("print(1)", root=self.root, allow_exec=True,
                                  policy=self.policy)
        self.assertIn("escape the workspace", str(ctx.exception))

    def test_within_rejects_a_path_outside_the_root(self):
        self.assertTrue(script._within(self.root / "a", self.root))
        self.assertFalse(script._within(self.root.parent, self.root))

    def test_an_unwritable_run_directory_is_a_refusal(self):
        with mock.patch.object(script.Path, "mkdir",
                               side_effect=OSError("denied")):
            with self.assertRaises(HarnessError) as ctx:
                script.run_script("print(1)", root=self.root, allow_exec=True,
                                  policy=self.policy)
        self.assertIn("cannot create the run directory", str(ctx.exception))

    def test_a_failed_script_write_is_a_refusal(self):
        with mock.patch.object(script.osal, "write_text",
                               side_effect=OSError("disk full")):
            with self.assertRaises(HarnessError) as ctx:
                script.run_script("print(1)", root=self.root, allow_exec=True,
                                  policy=self.policy)
        self.assertIn("cannot write the script", str(ctx.exception))

    def test_a_platform_that_cannot_set_modes_still_runs(self):
        with mock.patch.object(script.os, "chmod", side_effect=OSError("nope")):
            receipt = script.run_script("print('ok')", root=self.root,
                                        allow_exec=True, policy=self.policy)
        self.assertEqual(receipt["status"], "ok")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root = _workspace(self)
        self.policy = script.ScriptPolicy(enabled=True, timeout_s=10,
                                          max_output_bytes=4096)

    def test_receipt_fields_and_the_script_lands_inside_the_workspace(self):
        source = "print('hello from script')"
        receipt = script.run_script(source, root=self.root, allow_exec=True,
                                    goal="say hello", policy=self.policy)
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIn("hello from script", receipt["stdout"])
        self.assertEqual(receipt["script_sha256"],
                         hashlib.sha256(source.encode()).hexdigest())
        path = Path(receipt["script_path"])
        self.assertTrue(path.exists())
        self.assertTrue(str(path.resolve()).startswith(str(self.root.resolve())))
        self.assertEqual(path.read_text(encoding="utf-8"), source)
        self.assertFalse(receipt["truncated"])
        self.assertEqual(receipt["goal"], "say hello")

    def test_a_nonzero_exit_is_reported_not_swallowed(self):
        receipt = script.run_script("import sys; sys.exit(3)", root=self.root,
                                    allow_exec=True, policy=self.policy)
        self.assertEqual(receipt["exit_code"], 3)
        self.assertEqual(receipt["status"], "failed")

    def test_a_timeout_kills_the_process_tree(self):
        receipt = script.run_script("import time; time.sleep(30)", root=self.root,
                                    allow_exec=True, timeout_s=1,
                                    policy=self.policy)
        self.assertTrue(receipt["killed"])
        self.assertEqual(receipt["status"], "timeout")
        self.assertLess(receipt["duration_s"], 10)

    def test_output_is_capped_and_flagged(self):
        policy = script.ScriptPolicy(enabled=True, timeout_s=10,
                                     max_output_bytes=1000)
        receipt = script.run_script("print('x' * 5000)", root=self.root,
                                    allow_exec=True, policy=policy)
        self.assertTrue(receipt["truncated"])
        self.assertLessEqual(len(receipt["stdout"]), 1000)

    def test_the_environment_is_scrubbed(self):
        os.environ["HARNESS_TEST_SECRET"] = "sk-should-never-reach-the-script"
        self.addCleanup(os.environ.pop, "HARNESS_TEST_SECRET", None)
        receipt = script.run_script(
            "import os; print('HARNESS_TEST_SECRET' in os.environ, "
            "'OPENROUTER_API_KEY' in os.environ)",
            root=self.root, allow_exec=True, policy=self.policy)
        self.assertEqual(receipt["stdout"].strip(), "False False")

    def test_stderr_is_captured(self):
        receipt = script.run_script(
            "import sys; sys.stderr.write('boom\\n'); raise SystemExit(1)",
            root=self.root, allow_exec=True, policy=self.policy)
        self.assertIn("boom", receipt["stderr"])
        self.assertEqual(receipt["exit_code"], 1)


class AlignmentTests(unittest.TestCase):
    """Jev scores the script; the score is evidence, never the sandbox."""

    def setUp(self):
        self.root = _workspace(self)
        self.policy = script.ScriptPolicy(enabled=True, timeout_s=10)

    def _receipt(self, jev_policy):
        return script.run_script(
            "print('scrape done')", root=self.root, allow_exec=True,
            goal="scrape the page", consent_sha=None, policy=self.policy,
            jev_policy=jev_policy)

    def test_an_aligned_score_is_recorded(self):
        result = mock.Mock(answers={"answer_sufficient": {"type": "noul",
                                                          "noul": 0.92}},
                           confidence=0.0, verdict="fail", is_fallback=False,
                           model="jev-test", reasons=["answer_sufficient: 0.92"])
        jev = mock.Mock()
        jev.evaluate_answer.return_value = (result, {"pack_version": "p"})
        receipt = self._receipt(jev)
        self.assertTrue(receipt["alignment"]["aligned"])
        self.assertEqual(receipt["alignment"]["verdict"], "aligned")
        self.assertEqual(receipt["alignment"]["sufficient"], 0.92)
        # The plan-shaped packing must not make an aligned script look empty.
        self.assertEqual(receipt["alignment"]["provided_verdict"], "fail")

    def test_a_low_score_is_reported_as_revise(self):
        result = mock.Mock(answers={"answer_sufficient": {"type": "noul",
                                                          "noul": 0.2}},
                           confidence=0.2, verdict="fail", is_fallback=False,
                           model="jev-test", reasons=[])
        jev = mock.Mock()
        jev.evaluate_answer.return_value = (result, {"pack_version": "p"})
        receipt = self._receipt(jev)
        self.assertFalse(receipt["alignment"]["aligned"])
        self.assertEqual(receipt["alignment"]["verdict"], "revise")

    def test_a_jev_outage_never_fails_a_completed_run(self):
        jev = mock.Mock()
        jev.evaluate_answer.side_effect = HarnessError("jev unreachable")
        receipt = self._receipt(jev)
        self.assertEqual(receipt["status"], "ok")
        self.assertFalse(receipt["alignment"]["available"])
        self.assertIn("jev unreachable", receipt["alignment"]["reason"])

    def test_the_prompt_carries_the_goal_and_the_script(self):
        result = mock.Mock(answers={"answer_sufficient": 0.9}, confidence=0.9,
                           verdict="pass", is_fallback=False, model="jev-test",
                           reasons=[])
        jev = mock.Mock()
        jev.evaluate_answer.return_value = (result, {"pack_version": "p"})
        self._receipt(jev)
        args = jev.evaluate_answer.call_args
        self.assertIn("scrape the page", args.args[0])
        self.assertIn("print('scrape done')", args.args[1])


if __name__ == "__main__":
    unittest.main()
