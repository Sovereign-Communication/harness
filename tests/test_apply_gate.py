"""Direct tests for the gate-transaction and data-contract owners.

harness/apply_gate.py owns the filesystem-facing half of an apply run
(candidate writes, verification, preview, rewind); harness/apply_state.py
owns the data crossing its request/run/outcome phases. The engine-level
tests (test_apply.py) exercise them indirectly; this module pins their own
contracts so the test tree mirrors the product tree.
"""
import os
import tempfile
import unittest

from harness.apply_gate import GatePolicy
from harness.apply_state import ApplyRequest, AttemptOutcome, RunState
from harness.errors import HarnessError, ToolCancelled
from harness.results import _round_entry


class _Ledger:
    def __init__(self):
        self.events = []

    def append(self, event, **fields):
        self.events.append((event, fields))


class _Governor:
    def __init__(self, spent=0.0):
        self.spent = spent


def _request(tmp, *, verify_cmd="true", verify_only=False, original="",
             runner=None, continuation_gate=None, cancel_check=None):
    return ApplyRequest(
        task_id="t", file_path=tmp, instruction="do the thing",
        edit_snippet=None, verify_cmd=verify_cmd, backend="harness",
        verify_only=verify_only, max_lines=500, max_rounds=3, max_tokens=512,
        task_max_cost=0.1, max_rot=3, reasoning="auto", renew=True,
        allow_escalation=None, model="m", ordered=None, profiles=None,
        want_consent=False, original=original, task_start_spent=0.0,
        continuation={}, continuation_gate=continuation_gate,
        task_runner=runner or (lambda cmd: (0, "ok")),
        cancel_check=cancel_check)


def _state(content):
    return RunState(rounds=[], history=[], current_content=content)


class GateRunnerBindingTests(unittest.TestCase):
    def test_runner_is_the_bound_gate(self):
        """A request without a continuation gate runs its own task_runner."""
        calls = []
        req = _request("x", runner=lambda cmd: calls.append(cmd) or (0, "ok"))
        self.assertEqual(GatePolicy(_Ledger(), _Governor()).runner(req)("true"),
                         (0, "ok"))
        self.assertEqual(calls, ["true"])

    def test_gate_change_refuses(self):
        """A continuation pinning a different gate must refuse (#5)."""
        req = _request("x", verify_cmd="python -m pytest -x",
                       continuation_gate="python -m pytest")
        with self.assertRaises(HarnessError):
            GatePolicy(_Ledger(), _Governor()).runner(req)


class CandidateWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write("original\n")
            state = _state("original\n")
            ledger = _Ledger()
            pol = GatePolicy(ledger, _Governor())
            pol.write_candidate(_request(target), state, "edited\n")
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "edited\n")
            self.assertEqual(state.current_content, "edited\n")
            self.assertIsNotNone(state.backup)

    def test_symlink_target_refused(self):
        """The classic dotfile-into-the-repo trick never writes through."""
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.txt")
            link = os.path.join(d, "link.txt")
            with open(real, "w", encoding="utf-8") as f:
                f.write("x")
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this host")
            pol = GatePolicy(_Ledger(), _Governor())
            with self.assertRaises(OSError):
                pol.write_candidate(_request(link), _state("x"), "y")


class PreviewAndGateTests(unittest.TestCase):
    def test_preview_writes_nothing_and_runs_no_gate(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("original\n")
            ran = []
            req = _request(target, verify_only=True,
                           runner=lambda cmd: ran.append(cmd) or (0, "ok"))
            outcome = AttemptOutcome(model="m", model_used="m", cost=0.0)
            res = GatePolicy(_Ledger(), _Governor()).apply_candidate(
                req, _state("original\n"), outcome, "proposed\n")
            self.assertEqual(res["status"], "preview")
            self.assertEqual(res["proposed_content"], "proposed\n")
            self.assertTrue(res["verify_only"])
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "original\n")
            self.assertEqual(ran, [], "preview must never run the gate")

    def test_vacuous_pass_asks_for_another_round(self):
        """A gate that passes with no changes is not success; the engine
        should propose again."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("same\n")
            req = _request(target, runner=lambda cmd: (0, "ok"))
            outcome = AttemptOutcome(model="m", model_used="m", cost=0.0)
            res = GatePolicy(_Ledger(), _Governor()).apply_candidate(
                req, _state("same\n"), outcome, "same\n")
            self.assertIsNone(res, "vacuous pass must not terminate the run")

    def test_gate_failure_returns_none_and_records(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("a\n")
            ledger = _Ledger()
            req = _request(target, runner=lambda cmd: (1, "boom"))
            outcome = AttemptOutcome(model="m", model_used="m", cost=0.0)
            res = GatePolicy(ledger, _Governor()).apply_candidate(
                req, _state("a\n"), outcome, "b\n")
            self.assertIsNone(res)
            self.assertEqual(ledger.events[-1][0], "verify_round")
            self.assertFalse(ledger.events[-1][1]["passed"])

    def test_gate_pass_with_change_is_terminal_ok(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("a\n")
            req = _request(target, runner=lambda cmd: (0, "ok"))
            outcome = AttemptOutcome(model="m", model_used="m", cost=0.0)
            res = GatePolicy(_Ledger(), _Governor()).apply_candidate(
                req, _state("a\n"), outcome, "b\n")
            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["verify"]["passed"])
            self.assertEqual(res["verify"]["command"], "true")

    def test_cancelled_gate_raises(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("a\n")
            req = _request(target, cancel_check=lambda: True)
            pol = GatePolicy(_Ledger(), _Governor())
            with self.assertRaises(ToolCancelled):
                pol.run_gate(req)


class RewindTests(unittest.TestCase):
    def test_terminal_failure_rewinds_to_original(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write("original\n")
            req = _request(target, original="original\n",
                           runner=lambda cmd: (1, "boom"))
            state = _state("a\n")
            state.rounds.append(_round_entry(1, "m", "verify_failed", cost=0.0,
                                             verify_output="boom"))
            res = GatePolicy(_Ledger(), _Governor()).terminal_failure(req, state)
            self.assertEqual(res["status"], "verify_failed")
            self.assertTrue(res["gate_ran"])
            with open(target, encoding="utf-8", newline="") as f:
                self.assertEqual(f.read(), "original\n")
            cont = res["continuation"]
            self.assertEqual(cont["schema_version"], 1)
            self.assertTrue(cont["verification_required"])
            self.assertEqual(cont["verify_gate_id"],
                             __import__("harness.continuation", fromlist=["gate_id"])
                             .gate_id("true"))

    def test_preview_exhaustion_never_rewinds_and_is_honest(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write("original\n")
            req = _request(target, verify_only=True, original="original\n")
            state = _state("original\n")
            state.rounds.append(_round_entry(1, "m", "preview", cost=0.0,
                                             verify_output=""))
            res = GatePolicy(_Ledger(), _Governor()).terminal_failure(req, state)
            self.assertEqual(res["status"], "preview_exhausted")
            self.assertFalse(res["gate_ran"])
            self.assertIsNone(res["verify"]["passed"])
            with open(target, encoding="utf-8", newline="") as f:
                self.assertEqual(f.read(), "original\n")


class DataContractTests(unittest.TestCase):
    def test_request_is_frozen(self):
        req = _request("x")
        with self.assertRaises(Exception):
            req.task_id = "other"

    def test_run_state_defaults(self):
        s = RunState(rounds=[], history=[], current_content="")
        self.assertEqual(s.round_no, 0)
        self.assertFalse(s.gate_broken)
        self.assertEqual(s.rotations, 0)
        self.assertIsNone(s.backup)
        self.assertEqual(s.failed_models, set())

    def test_outcome_defaults_are_honest(self):
        o = AttemptOutcome()
        self.assertIsNone(o.model)
        self.assertEqual(o.ready, "missing")
        self.assertEqual(o.cost, 0.0)

    def test_round_entry_shape_is_stable(self):
        e = _round_entry(1, "m", "ok", cost=0.0, verify_output="",
                         changed=True, verify_passed=True)
        self.assertEqual(set(e), {"round", "model", "status", "cost",
                                  "verify_output", "changed", "verify_passed"})


class EscalationTests(unittest.TestCase):
    def test_escalation_without_content_fails_the_gate(self):
        """An escalation that returns no usable content must not pass."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("a\n")
            req = _request(target, runner=lambda cmd: (0, "ok"))
            state = _state("a\n")
            res = GatePolicy(_Ledger(), _Governor()).finish_escalation(
                req, state, "esc-model", None, 0.0, content_available=False)
            self.assertIsNone(res, "no content => no terminal success")
            self.assertTrue(all(r.get("status") == "verify_failed"
                                for r in state.rounds), "failed escalation "
                               "must record its verify_failed round")

    def test_escalation_pass_writes_through_gate(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("a\n")
            req = _request(target, runner=lambda cmd: (0, "ok"))
            state = _state("a\n")
            res = GatePolicy(_Ledger(), _Governor()).finish_escalation(
                req, state, "esc-model", "b\n", 0.0, content_available=True)
            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["escalated"])
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "b\n")


if __name__ == "__main__":
    unittest.main()
