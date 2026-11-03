"""Harness's local Needle 3 lane: opt-in, advisory, OBSERVE-only.

The short version, because this package exists mainly to make one measured fact
impossible to forget: **this checkpoint is not earned to influence anything.**
Best paired accuracy across three context levels was 0.4167 against a 0.90 floor.
Context moves the model's operating point but creates no discrimination, and
confidence does not separate the classes. See ``verdict`` for the full table and
for why the level that scored 10/10 on drift rows was in fact firing on 14 of 14
on-goal rows.

So this lane is packaged the same way ``harness/local_fit`` is -- a pure-stdlib
interface in the wheel, the heavy runtime as an optional extra, behaviour off by
default -- with one difference in emphasis. ``local_fit`` is a candidate-ordering
nudge. This is **evidence only**: it records what a local model claimed and
discards it, because the measurement says the claim carries no information worth
acting on. The pinned verdict is read from ``verdict.AUTHORITY_EARNED``, not from
any individual call, so no lucky run can unlock the authority path.

If a future checkpoint is measured and earned, the only thing that has to change
is the constant in ``verdict`` and a fresh evidence file. No call site moves.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import osal

from . import config, verdict
from .probe import (
    NO_SIGNAL_REASONS,
    NeedleSignal,
    Runner,
    build_prompt,
    observe,
)


def _osal_runner(argv: List[str], input_text: str,
                 timeout: float) -> Tuple[int, str]:
    """Wheel adapter: OS contact belongs to :mod:`harness.osal`, one owner.

    ``osal.run`` already normalises a timeout, a missing executable and a
    missing file into exit codes, so this adapter is three lines and needs none
    of the exit-code constants the standalone adapter uses. Keeping the launch
    here rather than in ``probe`` is what satisfies
    ``tests/test_osal_boundary.py``, which walks the package with ``ast`` and
    fails on a process-launch import anywhere outside the owner.
    """
    result = osal.run(argv, timeout=timeout, input_text=input_text)
    return result.returncode, result.stdout


#: The process-launch seam this host provides.
RUNNER: Runner = _osal_runner

__all__ = [
    "NO_SIGNAL_REASONS",
    "NeedleSignal",
    "RUNNER",
    "authority_earned",
    "VERDICT",
    "build_prompt",
    "config",
    "describe",
    "escalate",
    "observe",
    "verdict",
]

#: Re-exported so a caller never has to reach into a submodule to ask the
#: question that governs whether anything here may decide anything. Read through
#: the module so the owner stays the owner; the name is bound once here for
#: convenience, but ``describe``/``escalate`` read ``verdict`` directly.
def authority_earned() -> bool:
    return verdict.AUTHORITY_EARNED


VERDICT = verdict.VERDICT


def escalate(text: str, goal: str = "") -> Dict[str, Any]:
    """The only correct use of this lane today: record, then hand over.

    Returns a receipt and an explicit ``escalate`` directive. A caller that
    wants a decision uses this and lets the authoritative lane make it. The
    local model is not consulted for permission to defer -- it is consulted only
    to leave a trace, and its answer is not permitted to change the outcome.
    """
    signal = observe(goal, text, RUNNER) if goal else NeedleSignal(
        False, 0.0, "no_goal", 0.0, "not_observed")
    return {
        "escalate": True,
        "reason": "local_lane_not_earned",
        "authority_earned": authority_earned(),
        "signal": signal.to_dict(),
    }


def describe() -> Dict[str, Any]:
    """Operator-facing status: the resolution, the stage, and the verdict.

    Shape and intent match the local_fit status face. It reports paths and the
    measured verdict; it never reports the weights, and never prints a provider
    configuration.
    """
    return {
        "lane": "needle_fit",
        "authority_earned": authority_earned(),
        "verdict": verdict.VERDICT,
        "measured_accuracy": verdict.NEEDLE_MEASURED_ACCURACY,
        "min_accuracy_required": verdict.LOCAL_MIN_ACCURACY,
        "measured_p95_s": verdict.NEEDLE_MEASURED_P95_S,
        "no_signal_reasons": list(NO_SIGNAL_REASONS),
        "config": config.describe(),
    }
