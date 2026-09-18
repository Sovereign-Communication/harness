"""Verification-gate transaction policy for the apply engine.

This module owns the filesystem-facing half of an apply run: candidate writes,
verification, preview results, and failed-run rewind. Model selection and
provider dispatch stay in ``apply.py``; this policy receives request and run
state records and returns the shared result shapes.
"""
import os

from . import events as _events
from . import attest as attest_policy
from . import trust as trust_policy
from .continuation import bound_gate, gate_id
from .errors import ToolCancelled
from .filesafety import _atomic_write, backup_file, file_content_hash
from .output import eprint
from .results import _content_diff, _round_entry, _terminal_result


class GatePolicy:
    """Own the candidate-to-gate transaction without owning run state."""

    def __init__(self, ledger, governor, transport=None, api_key=None):
        self.ledger = ledger
        self.governor = governor
        # The LLM diff verifier (M4 phase 2) calls the provider directly,
        # so the gate needs the session credentials when the opt-in
        # require_diff_authorization flag is set.
        self.transport = transport
        self.api_key = api_key

    def runner(self, req):
        return bound_gate(req.continuation_gate, req.verify_cmd,
                          req.task_runner)

    def write_candidate(self, req, state, content, marker=None):
        """Back up and atomically write a changed candidate."""
        # Write-time trust gate: unknown trust with no verification gate
        # never lands unreviewed bytes on disk (consent/readiness/deferral
        # paths return before this point, so honest deferrals are unaffected).
        trust_policy.check_mutation(
            ledger=self.ledger, combined=getattr(req, "trust_combined", 0),
            verify_cmd=req.verify_cmd, task_id=req.task_id,
            model=getattr(req, "model", None))
        # Diff-bound independent authorization (M4 phase 2, opt-in): the
        # verifier model sees the EXACT bytes about to be written and must
        # allow them. Deny, unparseable verdict, and transport error all
        # refuse the write -- intent approval is never a fallback.
        if getattr(req, "require_diff_authorization", False):
            attest_policy.authorize_diff(
                self.transport, self.api_key, self.governor, self.ledger,
                task_id=req.task_id, model=req.attest_model,
                file_path=req.file_path, instruction=req.instruction,
                current_content=state.current_content, new_content=content,
                round_no=state.round_no, max_tokens=req.max_tokens)
        if state.backup is None:
            state.backup = backup_file(req.file_path, req.task_id,
                                       marker or state.round_no)
        _atomic_write(req.file_path, content)
        state.current_content = content
        _events.emit("gate_start", task_id=req.task_id, phase="candidate_write",
                     round=state.round_no, changed=True)

    def run_gate(self, req):
        """Run the request's bound verification command."""
        if req.cancel_check and req.cancel_check():
            raise ToolCancelled()
        return self.runner(req)(req.verify_cmd)

    def apply_candidate(self, req, state, outcome, new_content):

        """Write a candidate or preview it, then run its gate when required."""
        changed = new_content != state.current_content
        if req.verify_only:
            return self.preview(req, state, outcome, new_content, changed)
        if changed:
            self.write_candidate(req, state, new_content)
        return self.write_and_verify(req, state, outcome, changed)

    def preview(self, req, state, outcome, new_content, changed):
        state.rounds.append(_round_entry(
            state.round_no, outcome.model_used, "preview", changed=changed,
            verify_passed=None, cost=outcome.cost, verify_output=""))
        state.history.append({"round": state.round_no, "model": outcome.model_used,
                              "status": "preview"})
        self.ledger.append("complete", task_id=req.task_id, model=outcome.model_used,
                           rounds=state.round_no, status="preview", backend=req.backend,
                           note="verify-only; proposal not written")
        return _terminal_result(
            "preview", task_id=req.task_id, rounds=state.rounds,
            cost=self.governor.spent, rotations=state.rotations,
            backend=req.backend, verify_only=True, changed=changed,
            proposed_content=new_content, backup=None,
            file=req.file_path, diff=_content_diff(state.current_content,
                                                   new_content))

    def write_and_verify(self, req, state, outcome, changed):
        """Run the authoritative gate after a candidate write.

        ``None`` means the caller should build another proposal. A result
        means the transaction reached terminal success.
        """
        round_no = state.round_no
        if not req.verify_cmd:
            state.rounds.append(_round_entry(
                round_no, outcome.model_used, "ok", changed=changed,
                verify_passed=None, cost=outcome.cost, verify_output=""))
            state.history.append({"round": round_no, "model": outcome.model_used,
                                  "status": "ok"})
            self.ledger.append(
                "complete", task_id=req.task_id, model=outcome.model_used,
                rounds=round_no, status="ok", note="no verification gate",
                rotations=state.rotations)
            result = _terminal_result(
                "ok", task_id=req.task_id, rounds=state.rounds,
                cost=self.governor.spent, rotations=state.rotations,
                backend=req.backend, changed=changed, backup=state.backup,
                note="no verification gate supplied",
                file=req.file_path, diff=_content_diff(req.original,
                                                       state.current_content))
            _events.emit("terminal", task_id=req.task_id, status="ok",
                         cost=self.governor.spent, rounds=round_no,
                         note="no verification gate")
            return result

        if req.cancel_check and req.cancel_check():
            raise ToolCancelled()
        _events.emit("gate_start", task_id=req.task_id, round=round_no,
                     command=req.verify_cmd)
        rc, out = self.runner(req)(req.verify_cmd)
        _events.emit("gate_end", task_id=req.task_id, round=round_no,
                     passed=(rc == 0), rc=rc,
                     output_tail=(out or "")[-2000:])
        self.ledger.append(
            "verify_round", task_id=req.task_id, round=round_no,
            passed=(rc == 0), model=outcome.model_used, readiness=outcome.ready)
        if rc == 0:
            if not changed:
                state.rounds.append(_round_entry(
                    round_no, outcome.model_used, "vacuous", changed=False,
                    verify_passed=True, cost=outcome.cost,
                    verify_output="verify passed but no changes were applied"))
                return None
            state.rounds.append(_round_entry(
                round_no, outcome.model_used, "ok", changed=True,
                verify_passed=True, cost=outcome.cost, verify_output=""))
            state.history.append({"round": round_no, "model": outcome.model_used,
                                  "status": "ok"})
            self.ledger.append(
                "complete", task_id=req.task_id, model=outcome.model_used,
                rounds=round_no, status="ok", rotations=state.rotations)
            result = _terminal_result(
                "ok", task_id=req.task_id, rounds=state.rounds,
                cost=self.governor.spent, rotations=state.rotations,
                backend=req.backend, changed=True, backup=state.backup,
                verify={"command": req.verify_cmd, "passed": True},
                file=req.file_path, diff=_content_diff(req.original,
                                                       state.current_content))
            _events.emit("terminal", task_id=req.task_id, status="ok",
                         cost=self.governor.spent, rounds=round_no, passed=True)
            return result
        state.rounds.append(_round_entry(
            round_no, outcome.model_used, "verify_failed", changed=changed,
            verify_passed=False, cost=outcome.cost, verify_output=out))
        state.history.append({"round": round_no, "model": outcome.model_used,
                              "status": "verify_failed"})
        return None

    def finish_escalation(self, req, state, model, new_content, cost,
                          content_available):
        """Finish the escalation candidate through the same gate transaction."""
        _events.emit("escalation_rung", task_id=req.task_id, model=model,
                     phase="gate")
        changed = bool(content_available) and new_content != state.current_content
        if changed:
            self.write_candidate(req, state, new_content, marker="esc")
        if content_available:
            rc, out = self.run_gate(req)
        else:
            rc, out = 1, "escalation returned no usable content"
        if rc == 0 and changed:
            self.ledger.append("complete", task_id=req.task_id, model=model,
                               rounds="escalation", status="ok")
            state.rounds.append(_round_entry(
                "escalation", model, "ok", changed=True,
                verify_passed=True, cost=cost, verify_output=""))
            result = _terminal_result(
                "ok", task_id=req.task_id, rounds=state.rounds,
                cost=self.governor.spent, rotations=state.rotations,
                backend=req.backend, changed=True, backup=state.backup,
                verify={"command": req.verify_cmd, "passed": True},
                escalated=True,
                file=req.file_path, diff=_content_diff(req.original,
                                                       new_content))
            _events.emit("terminal", task_id=req.task_id, status="ok",
                         cost=self.governor.spent, escalated=True, passed=True)
            return result
        state.rounds.append(_round_entry(
            "escalation", model, "verify_failed", changed=changed,
            verify_passed=False, cost=cost, verify_output=out))
        return None

    def terminal_failure(self, req, state):
        """Rewind a failed run and build its honest continuation result."""
        self.ledger.append(
            "abort", task_id=req.task_id, model=req.model,
            reason=("preview exhausted its rounds" if req.verify_only
                    else "verify rounds exhausted"), rotations=state.rotations)
        if not req.verify_only and os.path.isfile(req.file_path):
            with open(req.file_path, encoding="utf-8") as stream:
                tree_now = stream.read()
            if tree_now != req.original:
                _atomic_write(req.file_path, req.original)
                eprint("[apply] rewound target to its pre-run content "
                       "(run did not pass its verification gate).")

        last_out = ""
        for entry in reversed(state.rounds):
            if entry.get("verify_output"):
                last_out = entry["verify_output"][-6000:]
                break
        gate_ran = not req.verify_only
        if req.verify_only:
            terminal_status = "preview_exhausted"
            verify_block = {"command": req.verify_cmd, "passed": None,
                            "note": "gate not run (verify-only preview)",
                            "output_tail": last_out}
            reason = ("preview exhausted its rounds without a clean proposal; "
                      "no gate ran (verify-only)")
            remaining = f"Produce a clean proposal for: {req.instruction}"
        else:
            terminal_status = "verify_failed"
            verify_block = {"command": req.verify_cmd, "passed": False,
                            "output_tail": last_out}
            reason = "verification did not pass on the free tier; continue and fix"
            remaining = f"Fix the verification failures for: {req.instruction}"
        continuation = {
            "schema_version": 1,
            "file_path": req.file_path,
            "task_id": req.task_id,
            "backend": req.backend,
            "verify_only": req.verify_only,
            "max_lines": req.max_lines,
            "edit_snippet": req.edit_snippet,
            "verify_cmd": req.verify_cmd,
            "verify_gate_id": gate_id(req.verify_cmd) if req.verify_cmd else None,
            "verification_required": not req.verify_only,
            "target_hash": file_content_hash(req.file_path),
            "remaining_scope": remaining,
            "reason": reason,
            "history": state.history,
        }
        result = _terminal_result(
            terminal_status, task_id=req.task_id, rounds=state.rounds,
            cost=self.governor.spent, rotations=state.rotations,
            backend=req.backend, verify_only=req.verify_only,
            backup=state.backup, verify_cmd=req.verify_cmd,
            verify=verify_block, gate_ran=gate_ran,
            continuation=continuation)
        _events.emit("terminal", task_id=req.task_id, status=terminal_status,
                     cost=self.governor.spent, rounds=state.round_no,
                     gate_ran=gate_ran, passed=verify_block.get("passed"))
        return result
