"""A pure-stdlib caller for a locally-installed Needle 3.

Three rules shape this file.

**No ``import needle`` in the import graph.** The package is third-party and
lives in a separate virtualenv on a newer interpreter than the wheel supports
(3.9). A direct import would either fail at install time or, worse, succeed on
one box and vanish on the next. So the model is reached by running a tiny script
in the interpreter that actually has it and reading JSON back. Availability is
a question about *this box*, answered per call, rather than an import that
resolved once at startup.

**No ``import subprocess`` here either.** Harness owns OS contact in
``harness/osal.py`` and enforces that with an AST test over the whole package.
So the process launch is injected: this module takes a ``runner`` callable, and
each host wires the one its own boundary allows -- ``osal.run`` inside the
wheel, ``subprocess.run`` in the standalone OC copy. The interface is
``(argv, input_text, timeout) -> (returncode, stdout_text)``, and
``osal.run``'s documented exit codes (124 timeout, 126 not executable, 127 not
found) map straight onto this module's named reasons, so the boundary-clean path
reports the same vocabulary as the standalone one.

**A claim is never an answer.** Every return carries ``provenance`` naming what
actually ran, and the default on the dataclass says ``not_observed`` -- a
default that described a model which was never invoked would be the same
silence as a defaulted value, one layer out. A timeout, a missing checkpoint or
a crash all produce *no signal*, and a caller with no signal escalates. That is
the same posture ``runtime/needle_jev_bridge.py`` takes, and the same reason.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from . import verdict as _verdict

#: The injected process-launch seam. Returns ``(returncode, stdout_text)``.
Runner = Callable[[List[str], str, float], Tuple[int, str]]

#: ``osal.run``'s documented exit codes, reused so both hosts agree.
RC_TIMEOUT = 124
RC_NOT_EXECUTABLE = 126
RC_NOT_FOUND = 127

#: Reasons a probe can return no signal. Named, so an operator face can say
#: which one applies instead of showing an empty score.
NO_SIGNAL_REASONS = (
    "disabled",                 # stage is OFF
    "interpreter_absent",       # the venv python is not on this box
    "interpreter_not_executable",
    "checkpoint_absent",        # the .cact weights are not on this box
    "needle_absent",            # interpreter ran, cactus-needle not importable
    "timeout",                  # slower than the sub-second budget
    "child_unreadable",         # non-JSON, or the child died
    "empty_context",
    "no_goal",                  # nothing to measure drift against
    "runner_not_configured",    # host did not wire its process seam
)


@dataclass(frozen=True)
class NeedleSignal:
    """One observation. Advisory, and structurally incapable of authorising.

    ``claimed`` is whatever the model claimed about the context. It is a *vote*,
    not a verdict: nothing in Harness reads it as permission, and
    ``may_influence`` is False unless a future measurement earns it.
    """

    claimed: bool
    confidence: float
    reason: str
    latency_ms: float
    provenance: str
    observed_chars: int = 0
    truncated: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.provenance != "not_observed"

    @property
    def may_influence(self) -> bool:
        """Whether this signal may reach anything that decides.

        Read from the pinned verdict rather than from this call's own numbers,
        on purpose: a single lucky run must not be able to unlock the authority
        path. Only a re-measurement of the whole labelled set can, and that is a
        deliberate act with its own evidence file.

        Read through the module, not through a value captured at import, so the
        single owner stays the single owner even under test.
        """
        return _verdict.AUTHORITY_EARNED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claimed": self.claimed,
            "confidence": self.confidence,
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 1),
            "provenance": self.provenance,
            "observed_chars": self.observed_chars,
            "truncated": self.truncated,
            "may_influence": self.may_influence,
            "notes": list(self.notes),
        }


def _no_signal(reason: str, started: float, **kwargs: Any) -> NeedleSignal:
    if reason not in NO_SIGNAL_REASONS:
        raise ValueError(f"unregistered no-signal reason: {reason!r}")
    return NeedleSignal(False, 0.0, reason, (time.time() - started) * 1000.0,
                        "not_observed", **kwargs)


#: The child program. Kept deliberately tiny: it imports the model, runs one
#: prompt from stdin, and prints one JSON object. It is a string rather than a
#: file because the interpreter that can run it is a different interpreter from
#: the one running this module, so there is no importable file to point at.
_CHILD = r'''
import json, os, sys
os.environ.setdefault("NEEDLE_TELEMETRY", "0")  # cactus-needle phones home
checkpoint = sys.argv[1]
prompt = sys.stdin.read()
try:
    import needle
except Exception as exc:
    print(json.dumps({"ok": False, "reason": "needle_absent",
                      "error": type(exc).__name__}))
    raise SystemExit(0)
fired = []
def is_drifted() -> str:
    """Call ONLY if the context has DRIFTED from the stated goal. Do NOT call
    for on-goal work, greetings or small talk."""
    fired.append(1)
    return "drifted"
try:
    agent = needle.Needle(tools=[needle.tool(is_drifted)], weights=checkpoint)
    result = agent.run(prompt, max_steps=2, max_new_tokens=48)
except Exception as exc:
    print(json.dumps({"ok": False, "reason": "model_error",
                      "error": type(exc).__name__}))
    raise SystemExit(0)
suppressed = result.get("suppressed_calls") or []
try:
    confidence = float(result.get("confidence"))
except (TypeError, ValueError):
    confidence = 0.0
print(json.dumps({
    "ok": True,
    "claimed": bool(fired) and bool(result.get("success")) and not suppressed,
    "confidence": confidence,
    "suppressed": len(suppressed),
}))
'''


def build_prompt(goal: str, text: str) -> str:
    """The L1 shape, which measured best.

    Deliberately *not* the richest prompt. Adding the plan and the accepted
    claims measurably made drift detection worse (5 missed drifts against 0),
    so the lane sends the one piece of context that earned its place -- the
    goal -- and nothing else. See ``verdict`` for the numbers.
    """
    if not goal or not goal.strip():
        raise ValueError(
            "a goal is required; without one there is nothing to drift from")
    return f"GOAL: {goal.strip()}\n\nLATEST: {text.strip()}"


def observe(goal: str, text: str, runner: Optional[Runner] = None) -> NeedleSignal:
    """One advisory observation. Never raises, never authorizes.

    Every failure mode is a named reason on a no-signal return rather than an
    exception, because a missing local model must never be able to break the
    authoritative lane behind it.
    """
    started = time.time()
    if not config.enabled():
        return _no_signal("disabled", started)
    if runner is None:
        return _no_signal("runner_not_configured", started)
    if not goal or not str(goal).strip():
        return _no_signal("no_goal", started)
    if not text or not str(text).strip():
        return _no_signal("empty_context", started)

    interpreter = config.interpreter()
    if not os.path.exists(interpreter):
        return _no_signal("interpreter_absent", started)

    checkpoint = config.checkpoint()
    if not os.path.exists(checkpoint):
        return _no_signal("checkpoint_absent", started)

    window = str(text)[:config.max_context_chars()]
    prompt = build_prompt(str(goal), window)

    try:
        code, out = runner([interpreter, "-c", _CHILD, checkpoint], prompt,
                           config.timeout_s())
    except OSError:
        return _no_signal("interpreter_not_executable", started)

    if code == RC_TIMEOUT:
        # A slow call is indistinguishable from no call for a lane whose entire
        # purpose is a sub-second answer, so it is reported as no signal.
        return _no_signal("timeout", started, observed_chars=len(window),
                          truncated=len(window) < len(str(text)))
    if code == RC_NOT_FOUND:
        return _no_signal("interpreter_absent", started)
    if code == RC_NOT_EXECUTABLE:
        return _no_signal("interpreter_not_executable", started)

    try:
        payload = json.loads((out or "").strip() or "{}")
    except (ValueError, TypeError):
        return _no_signal("child_unreadable", started)

    if not payload.get("ok"):
        reason = payload.get("reason") or "child_unreadable"
        if reason == "needle_absent":
            return _no_signal("needle_absent", started,
                              observed_chars=len(window))
        return _no_signal("child_unreadable", started,
                          observed_chars=len(window), notes=[str(reason)])

    notes = []
    suppressed = int(payload.get("suppressed") or 0)
    if suppressed:
        notes.append("tool_call_suppressed")
    # A claim requires the tool to have fired AND not been suppressed. The
    # child already ANDs these, but the child is a *separate program* on a
    # separate interpreter, so this side re-applies the rule rather than
    # trusting a field it did not compute.
    claimed = bool(payload.get("claimed")) and not suppressed
    return NeedleSignal(
        claimed=claimed,
        confidence=float(payload.get("confidence") or 0.0),
        reason="model_claim" if claimed else (
            "tool_call_suppressed" if suppressed else "no_claim"),
        latency_ms=(time.time() - started) * 1000.0,
        provenance="cactus-needle-subprocess",
        observed_chars=len(window),
        truncated=len(window) < len(str(text)),
        notes=notes,
    )
