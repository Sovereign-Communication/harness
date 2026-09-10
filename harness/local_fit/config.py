"""Flags and scorer loading for the local-fit advisory layer.

Only two functions here are on the live path (via :mod:`dispatch`):
:func:`is_enabled` and :func:`load_scorer`. Everything else the prototype
seam needed (per-seat scoring helpers, tiebreak keys) died with it: the
live path scores through the scorer object directly and orders through
:func:`dispatch.maybe_order_pool`, which owns the tier-preserving policy.

The flags are read dynamically from the environment each call so that
tests can toggle them at runtime without reloading the module.
"""

import os
from typing import Any, Optional


def _env_enabled() -> bool:
    return os.environ.get("HARNESS_LOCAL_FIT_ENABLE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _env_model_dir() -> str:
    return os.environ.get("HARNESS_LOCAL_FIT_MODEL_DIR", "").strip()


def is_enabled() -> bool:
    return _env_enabled() and bool(_env_model_dir())


def load_scorer() -> Optional[Any]:
    if not is_enabled():
        return None
    try:
        from .model_loader import LocalScorer

        return LocalScorer(_env_model_dir())
    except Exception:
        return None
