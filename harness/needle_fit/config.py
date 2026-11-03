"""Feature-flag entrypoint for the local Needle 3 lane.

Modelled directly on ``harness/local_fit/config.py``: read dynamically so tests
can toggle a flag without reimporting, and so a missing flag is OFF rather than
an error. The local model is a third-party package in a separate virtualenv, so
nothing here imports it -- these are resolved *paths*, checked before use.

The three gates are the same OFF / OBSERVE / INFLUENCE stages ``local_fit``
uses. ``INFLUENCE`` is unreachable on its own: see ``verdict.py`` for why, and
for the measurement that keeps it unreachable.
"""
from __future__ import annotations

import os

#: Stage names, in increasing authority. Kept as constants because the ledger
#: event and the tests both key off these exact strings.
OFF = "off"
OBSERVE = "observe"
INFLUENCE = "influence"
STAGES = (OFF, OBSERVE, INFLUENCE)

DEFAULT_INTERPRETER = "runtime/needle-venv/Scripts/python.exe"
DEFAULT_CHECKPOINT = "~/needle3.cact"


def _flag(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def stage() -> str:
    """The configured stage, or ``off`` for anything unrecognised.

    An unrecognised value resolving to the *quietest* stage matters: a typo in a
    deployment config must not be the thing that turns a local model loose.
    """
    value = _flag("HARNESS_NEEDLE_FIT_STAGE", OFF).lower()
    return value if value in STAGES else OFF


def enabled() -> bool:
    return stage() != OFF


def interpreter() -> str:
    """Python that actually has ``cactus-needle`` importable.

    The wheel supports 3.9 and the model needs 3.10+; a single interpreter
    cannot be both, which is why this is a path and not an import.
    """
    return os.path.expanduser(
        _flag("HARNESS_NEEDLE_FIT_PYTHON", DEFAULT_INTERPRETER))


def checkpoint() -> str:
    """The ``.cact`` weights file.

    Fetched, never shipped in the wheel: it is ~35MB and it is not ours to
    redistribute. A search list rather than one home-directory absolute path,
    because an absolute path is how a measurement ends up describing a
    checkpoint nothing else on the box is using.
    """
    override = _flag("HARNESS_NEEDLE_FIT_CHECKPOINT")
    if override:
        return os.path.expanduser(override)
    for candidate in (os.path.expanduser(DEFAULT_CHECKPOINT),
                      "needle3.cact",
                      os.path.join(os.path.expanduser("~"), ".cache",
                                   "harness", "needle3.cact")):
        if os.path.exists(candidate):
            return candidate
    return os.path.expanduser(DEFAULT_CHECKPOINT)


def timeout_s() -> float:
    """Wall clock for one local call.

    The whole point of this lane is a sub-second answer, so a slow call is
    indistinguishable from no answer and is treated as no answer. See
    ``verdict.NEEDLE_MEASURED_P95_S`` for the measured figure this budget is
    set against.
    """
    try:
        return max(0.05, float(_flag("HARNESS_NEEDLE_FIT_TIMEOUT_S", "1.5")))
    except ValueError:
        return 1.5


def max_context_chars() -> int:
    """How much context the prompt may carry.

    A bound on the model run only, not on the judgment lane, exactly as
    ``PREFILTER_MAX_CHARS`` is in the OC bridge. Prefill cost is per character.
    """
    try:
        return max(0, int(_flag("HARNESS_NEEDLE_FIT_MAX_CHARS", "1000")))
    except ValueError:
        return 1000


def describe() -> dict:
    """The resolution report an operator face shows.

    Returns the paths and the stage, never the weights themselves and never a
    provider configuration.
    """
    return {
        "stage": stage(),
        "interpreter": interpreter(),
        "checkpoint": checkpoint(),
        "timeout_s": timeout_s(),
        "max_context_chars": max_context_chars(),
    }
