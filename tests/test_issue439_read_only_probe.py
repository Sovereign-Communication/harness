"""Issue 439: a read-only probe must not need write authority.

The 2026-10-28 oversight audit found the vocabulary calling ``run_probe``
read-only while the registry refused it for a write-gate reason: the action
was declared, no executor was registered, and the only note attached was
"actions with a side effect are not registered because DRIVER_ALLOW_WRITE is
off". A verifier is exactly the caller that must not hold write authority, so
the contradiction made the deterministic probe surface unusable.

These tests pin the two halves of the contract: a probe runs and its output
is evidence in the audit chain, while every mutating action stays refused.
"""
import os
import sys
import unittest
from typing import Any, cast
from unittest import mock

from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.errors import ConsentError, ExecutorError
from driver_core.executor import Consent, Executor
from driver_core.executor_registry import (
    PROBE_ENV, build_driver_registry, executability, probe_argv,
)


class ReadOnlyProbeTests(unittest.TestCase):

    def setUp(self):
        self.audit = MemoryAuditLog()
        self.registry = build_driver_registry(None, allow_write=False)
        self.executor = Executor(vocabulary=DEFAULT_VOCABULARY,
                                 registry=self.registry, audit=self.audit)
        self.env = mock.patch.dict(
            os.environ, {PROBE_ENV: "python -c"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_probe_runs_and_its_output_lands_in_the_audit_chain(self):
        result = self.executor.execute(
            "run_probe",
            {"command": [sys.executable, "-c", "print('state-ok')"]})
        self.assertTrue(result.ok, result.detail)
        assert result.output is not None
        self.assertEqual(result.output["exit_code"], 0)
        self.assertIn("state-ok", result.output["stdout"])
        self.assertEqual(result.output["stderr"], "")

        records = self.audit.read_all()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["kind"], "action")
        self.assertEqual(record["action"], "run_probe")
        self.assertTrue(record["ok"])
        self.assertIn("state-ok", record["output"]["stdout"])
        self.assertEqual(record["output"]["exit_code"], 0)
        self.assertTrue(self.audit.verify().ok)

    def test_a_probe_with_a_non_zero_exit_is_still_recorded_evidence(self):
        result = self.executor.execute(
            "run_probe",
            {"command": [sys.executable, "-c", "raise SystemExit(3)"]})
        self.assertTrue(result.ok, result.detail)
        assert result.output is not None
        self.assertEqual(result.output["exit_code"], 3)
        self.assertEqual(self.audit.read_all()[0]["output"]["exit_code"], 3)

    def test_a_non_allowlisted_shape_is_refused_before_a_process_starts(self):
        result = self.executor.execute(
            "run_probe", {"command": ["definitely-not-allowed-xyz"]})
        self.assertFalse(result.ok)
        self.assertIn("not allowlisted", result.detail)
        # The refusal is an action record, not a silent no-op.
        self.assertEqual(self.audit.read_all()[0]["kind"], "action")

    def test_a_shell_string_is_refused_rather_than_tokenised(self):
        result = self.executor.execute(
            "run_probe", {"command": "python -c print('x')"})
        self.assertFalse(result.ok)
        self.assertIn("argv list", result.detail)

    def test_an_option_that_writes_a_file_is_refused(self):
        result = self.executor.execute(
            "run_probe", {"command": ["git", "diff", "--output=/tmp/x"]})
        self.assertFalse(result.ok)
        self.assertIn("writes a file", result.detail)

    def test_read_only_git_subcommands_are_allowlisted_by_default(self):
        self.assertEqual(probe_argv(["git", "status", "--short"]),
                         ["git", "status", "--short"])
        for declared in (["git", "branch"], ["git", "commit"],
                         ["git", "config", "user.name"]):
            with self.assertRaises(ExecutorError):
                probe_argv(declared)

    def test_the_vocabulary_no_longer_calls_the_probe_write_refused(self):
        probe = DEFAULT_VOCABULARY.resolve("run_probe")
        executable, needs = executability(probe, self.registry)
        self.assertTrue(executable, needs)
        self.assertEqual(needs, [])

    def test_mutating_actions_remain_refused_with_writes_off(self):
        click = DEFAULT_VOCABULARY.resolve("click")
        executable, needs = executability(click, self.registry)
        self.assertFalse(executable)
        self.assertIn("DRIVER_ALLOW_WRITE", " ".join(needs))
        # Even a correctly scoped consent cannot conjure an executor the
        # build did not register; the refusal is the registry, not consent.
        with self.assertRaises(ConsentError):
            self.executor.execute("click", {"target": "OK"},
                                  consent=cast(Any, None))
        consent = Consent(True, "click", {"target": "OK"})
        with self.assertRaises(ExecutorError) as ctx:
            self.executor.execute("click", {"target": "OK"}, consent=consent)
        self.assertIn("not registered", str(ctx.exception))
        self.assertIn("DRIVER_ALLOW_WRITE", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
