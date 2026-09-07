"""Free-tier saturation: one policy for "the whole tier is down, tell the user
what to do."

Every lane already records per-attempt evidence (panel ``panel_failures``,
engine ``rounds``, both ledgered as ``model_result`` events). This module is
the one owner of turning that evidence into a plain-language verdict, so the
terminal surfaces (verify, apply/continue, dogfood) stop printing tally
jargon at the moment a run fail-closes on a saturated tier.

A run is *saturated* when every attempted model failed the recoverable way:
rate limits / daily caps (HTTP 429) or reasoning-only responses the engine
and panel already refuse to treat as content. Those are tier conditions, not
code conditions -- the guidance names the real options instead of leaving the
user with a bare status.
"""
import re

from .output import eprint

_RATE_LIMIT_RE = re.compile(r"\b429\b", re.IGNORECASE)

# Pre-run look-ahead: how far back to read the ledger, and how many 429s in
# that window count as "the tier is saturated right now" rather than noise.
# Only 429s count toward the trigger (401s are a key/config condition, not a
# tier condition, and the warning must count what it prints).
_PRE_RUN_WINDOW = 100
_PRE_RUN_MIN_429S = 3
_KEY_NEAR_LIMIT = 0.02   # key remaining below which paid routing has no headroom

_warned_this_process = False

_PRE_RUN_NOTE = ("[saturation] Free tier looks rate-limited right now "
                 "({n} 429s in recent runs); this run will likely fail-close. "
                 "Consider waiting for the tier reset, a paid/BYOK key, or "
                 "--no-consent under a strong verification gate.")

_KEY_HEADROOM_NOTE = (" (your key has only ${remaining:.2f} left -- "
                      "paid routing has no headroom).")

_SATURATION_NOTE = (
    "[saturation] Every attempted model on the free tier is saturated "
    "(rate limits / daily caps / reasoning-only responses) -- this is a tier "
    "condition, not a problem with your task. Options: wait for the daily "
    "tier reset and retry, retry later when the tier recovers, run with "
    "--no-consent only under a strong verification gate, or add a paid/BYOK "
    "key to route around the free tier.")


def is_saturated(*, panel_failures=None, engine_rounds=None):
    """True when every attempted model failed the recoverable, tier-condition
    way (429s / reasoning-only bodies) -- never when a gate failed, a model
    returned real content, or the evidence is missing. One predicate for all
    surfaces: panels pass ``panel_failures``, the apply engine passes
    ``engine_rounds``."""
    attempts = []
    for failure in (panel_failures or []):
        status = str(failure.get("status") or "")
        reason = str(failure.get("reason") or "")
        attempts.append("saturated" if (
            _RATE_LIMIT_RE.search(status)
            or (status == "invalid_output" and "reasoning-only" in reason)) else None)
    for round_entry in (engine_rounds or []):
        error = str(round_entry.get("error") or "")
        attempts.append("saturated" if (
            round_entry.get("status") == "api_error"
            and (_RATE_LIMIT_RE.search(error) or "no model reachable" in error
                 or "no usable content" in error)) else None)
    return bool(attempts) and all(a == "saturated" for a in attempts)


def advise(panel_failures=None, engine_rounds=None):
    """Print the plain-language saturation guidance when -- and only when --
    the terminal evidence says the whole attempted pool was saturated."""
    if is_saturated(panel_failures=panel_failures, engine_rounds=engine_rounds):
        eprint(_SATURATION_NOTE)
        return True
    return False


def pre_run_warning(governor=None, ledger=None, *, use_free):
    """The look-ahead half of the policy: BEFORE a run spends anything, read
    the evidence the session already holds -- recent ledger ``model_result``
    events and the key's remaining budget -- and warn in plain language when
    the free tier looks rate-limited right now. Advice, never a gate: the run
    always proceeds and the reactive ``advise`` stays the backstop.

    Trigger: at least _PRE_RUN_MIN_429S rate-limited model results in the last
    _PRE_RUN_WINDOW ledger events (401/auth faults are deliberately not
    counted -- the message must count what it names). A key near its limit
    never triggers the warning alone (a $0 free-tier run needs no headroom)
    but qualifies the BYOK advice when the tier evidence does fire. Warns at
    most once per process (dogfood's verify + apply phases share one run);
    every failure mode degrades to silence.
    """
    global _warned_this_process
    try:
        if _warned_this_process or not use_free:
            return False
        rate_limited = 0
        for e in (ledger.tail(_PRE_RUN_WINDOW) if ledger is not None else []):
            if e.get("event") != "model_result":
                continue
            if _RATE_LIMIT_RE.search(
                    f"{e.get('status') or ''} {e.get('reason') or ''}"):
                rate_limited += 1
        if rate_limited < _PRE_RUN_MIN_429S:
            return False
        note = _PRE_RUN_NOTE.format(n=rate_limited)
        try:
            remaining = float(
                (getattr(governor, "key_info", None) or {}).get("remaining"))
        except (AttributeError, TypeError, ValueError):
            remaining = None   # key-state is garnish; never suppress the warning
        if remaining is not None and remaining < _KEY_NEAR_LIMIT:
            note += _KEY_HEADROOM_NOTE.format(remaining=remaining)
        eprint(note)
        _warned_this_process = True
        return True
    except Exception:
        # A bug in the warning must never break the run it advises.
        return False
