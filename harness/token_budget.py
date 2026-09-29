"""Token allowances and accounting: the ONE owner of per-call input/output
maxima and of composable stage/run reservations (HV-3).

Independent of :class:`harness.spend.SpendGovernor`, which owns DOLLARS. The
two limits answer different questions and neither can raise the other: a free
model costs $0.00 and can still overflow a context window, and a dollar
ceiling says nothing about how many tokens a stage may read. This module
never prices anything; :mod:`harness.spend` never counts tokens.

Shape: a run owns an input and an output allowance. A stage is a child
budget that may only *narrow* its parent, so a planning stage handed
successively smaller briefs cannot widen its own limits. A call asks for an
:class:`Allowance`, which is itself the reservation -- the worst case is
held from every budget in the chain at once, and the caller either settles
or cancels it exactly once.

Fail-closed in the four ways that matter:

* a call whose worst case does not fit the remaining allowance, at any level
  of the chain, is refused BEFORE dispatch;
* a stage may not exceed its parent's maxima, so composition cannot inflate
  a budget by nesting;
* a call whose real usage is unknown is charged its FULL reservation and
  labeled ``unavailable`` -- never silently zero;
* an allowance settles or cancels exactly once; a second settle, or settling
  an allowance this budget does not own, is an error, not a silent no-op.

When a provider reports MORE than the worst case that was reserved (an
estimate that undercounts), the real number is recorded and the overrun is
counted in ``over_input_tokens`` instead of being clamped away or raised
after the tokens are already spent.
"""
import threading
from typing import Dict, NamedTuple

from .errors import HarnessError

USAGE_ACTUAL = "actual"
USAGE_ESTIMATED = "estimated"
USAGE_UNAVAILABLE = "unavailable"
_USAGE_SOURCES = frozenset((USAGE_ACTUAL, USAGE_ESTIMATED, USAGE_UNAVAILABLE))

# Generous defaults: a run may read a few hundred thousand tokens before it
# is refused. The point of the cap is a bounded run, not a tight budget.
DEFAULT_RUN_INPUT_TOKENS = 200000
DEFAULT_RUN_OUTPUT_TOKENS = 64000


def _tokens(value, name):
    """A whole, non-negative token count, or a refusal.

    Integral floats are accepted because configured limits arrive through
    ``config._num`` as numbers; a fractional token count is nonsense and is
    refused rather than rounded into a silently different limit.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessError(f"{name} must be a whole token count; got {value!r}")
    if isinstance(value, float) and not float(value).is_integer():
        raise HarnessError(f"{name} must be a whole token count; got {value!r}")
    count = int(value)
    if count < 0:
        raise HarnessError(f"{name} must not be negative; got {value!r}")
    return count


class Usage(NamedTuple):
    """What one settled call actually cost, and how honestly we know it."""

    input_tokens: int
    output_tokens: int
    source: str


class Allowance:
    """One call's reserved worst case, held from every budget in the chain.

    The object IS the reservation: it cannot be settled twice, and it carries
    the budgets it was taken from so settlement releases exactly what
    ``allowance()`` took.
    """

    def __init__(self, owner, chain, label, input_tokens, max_output_tokens):
        self.owner = owner
        self.chain = chain
        self.label = label
        self.input_tokens = input_tokens
        self.max_output_tokens = max_output_tokens
        self.settled = False
        self.cancelled = False

    @property
    def worst_case(self):
        return self.input_tokens + self.max_output_tokens

    def __repr__(self):
        state = "cancelled" if self.cancelled else (
            "settled" if self.settled else "open")
        return (f"<Allowance {self.label} {self.input_tokens}+"
                f"{self.max_output_tokens} {state}>")


class TokenBudget:
    """An input/output allowance for one run, or one stage of one run."""

    def __init__(self, label="run", *, max_input_tokens=DEFAULT_RUN_INPUT_TOKENS,
                 max_output_tokens=DEFAULT_RUN_OUTPUT_TOKENS, parent=None):
        self._label = str(label)
        self._max_input = _tokens(max_input_tokens, "max_input_tokens")
        self._max_output = _tokens(max_output_tokens, "max_output_tokens")
        self._parent = parent
        if parent is not None:
            if self._max_input > parent._max_input:
                raise HarnessError(
                    f"stage {self._label!r} input maximum {self._max_input} "
                    f"exceeds its parent's {parent._max_input}; a stage may "
                    f"only narrow the budget, never widen it.")
            if self._max_output > parent._max_output:
                raise HarnessError(
                    f"stage {self._label!r} output maximum {self._max_output} "
                    f"exceeds its parent's {parent._max_output}; a stage may "
                    f"only narrow the budget, never widen it.")
        # ONE lock for a whole chain (the root's), so a nested reservation can
        # check and take from every level without lock-ordering deadlock.
        self._lock = parent._lock if parent is not None else threading.RLock()
        self._used_input = 0
        self._used_output = 0
        self._reserved_input = 0
        self._reserved_output = 0
        self._calls = 0
        self._cancelled = 0
        self._open = 0
        self._over_input = 0
        self._over_output = 0
        self._by_label: Dict[str, Dict[str, int]] = {}
        self._usage = {src: 0 for src in sorted(_USAGE_SOURCES)}

    # -- identity -------------------------------------------------------
    @property
    def label(self):
        return self._label

    @property
    def max_input_tokens(self):
        return self._max_input

    @property
    def max_output_tokens(self):
        return self._max_output

    # -- accounting -----------------------------------------------------
    def remaining_input(self):
        """Input tokens this budget may still commit (reservations count)."""
        with self._lock:
            return max(0, self._max_input - self._used_input
                       - self._reserved_input)

    def remaining_output(self):
        """Output tokens this budget may still commit (reservations count)."""
        with self._lock:
            return max(0, self._max_output - self._used_output
                       - self._reserved_output)

    def used_input(self):
        with self._lock:
            return self._used_input

    def used_output(self):
        with self._lock:
            return self._used_output

    def reserved(self):
        """Worst-case tokens held by calls that have not settled yet."""
        with self._lock:
            return self._reserved_input + self._reserved_output

    @property
    def open_allowances(self):
        """Reservations taken and not yet settled or cancelled.

        A run that ends with any left is leaking its worst case: the tokens
        are held against the allowance forever, and the caller has no record
        of them.
        """
        with self._lock:
            return self._open

    def stage(self, label, *, max_input_tokens=None, max_output_tokens=None):
        """A child budget that may only narrow this one.

        Composable: a stage's calls are counted here AND in every ancestor, so
        a run can never be spent twice by the same tokens, and a stage that
        wants less than it has asks for a smaller stage budget rather than
        trimming at dispatch.
        """
        return TokenBudget(
            label,
            max_input_tokens=(self._max_input if max_input_tokens is None
                              else max_input_tokens),
            max_output_tokens=(self._max_output if max_output_tokens is None
                               else max_output_tokens),
            parent=self)

    def allowance(self, input_tokens, *, max_output_tokens=None, label="call"):
        """Reserve one call's worst case, or refuse before dispatch.

        ``input_tokens`` is the call's real or estimated prompt size;
        ``max_output_tokens`` defaults to this budget's per-call output
        maximum. Both are checked against this budget's maxima AND against
        the remaining allowance of every budget in the chain, then taken from
        all of them under one lock.
        """
        want_in = _tokens(input_tokens, "input_tokens")
        want_out = (self._max_output if max_output_tokens is None
                    else _tokens(max_output_tokens, "max_output_tokens"))
        if want_out > self._max_output:
            raise HarnessError(
                f"call output maximum {want_out} exceeds the {self._label} "
                f"per-call output maximum {self._max_output}; refusing.")
        chain = self._chain()
        with self._lock:
            for budget in chain:
                if want_in > budget.remaining_input():
                    raise HarnessError(
                        f"call input {want_in} token(s) exceeds the "
                        f"{budget._label} remaining input allowance "
                        f"{budget.remaining_input()}; refusing.")
                if want_out > budget.remaining_output():
                    raise HarnessError(
                        f"call output {want_out} token(s) exceeds the "
                        f"{budget._label} remaining output allowance "
                        f"{budget.remaining_output()}; refusing.")
            for budget in chain:
                budget._reserved_input += want_in
                budget._reserved_output += want_out
                budget._open += 1
        return Allowance(self, chain, str(label), want_in, want_out)

    def settle(self, allowance, *, input_tokens=None, output_tokens=None,
               source=USAGE_ACTUAL):
        """Release a reservation and record what was really spent.

        ``source`` is the honesty label and is required to be one of
        ``actual``/``estimated``/``unavailable``. With ``unavailable`` the
        full reservation is charged: a call whose usage we cannot read may
        well have billed its worst case, and undercounting it is the one
        answer this owner must not give.
        """
        # Checking the state, releasing the reservation, recording usage and
        # closing the allowance are one transaction. Otherwise two callers
        # can both pass _check_open before either one marks it settled.
        with self._lock:
            self._check_open(allowance, "settle")
            if source not in _USAGE_SOURCES:
                raise HarnessError(
                    f"usage source must be one of {sorted(_USAGE_SOURCES)}; "
                    f"got {source!r}")
            if source == USAGE_UNAVAILABLE:
                spent_in = allowance.input_tokens
                spent_out = allowance.max_output_tokens
            else:
                spent_in = _tokens(
                    allowance.input_tokens if input_tokens is None
                    else input_tokens, "input_tokens")
                spent_out = _tokens(
                    allowance.max_output_tokens if output_tokens is None
                    else output_tokens, "output_tokens")
            for budget in allowance.chain:
                budget._release(allowance)
            for budget in allowance.chain:
                budget._record(spent_in, spent_out, allowance.label, source,
                               allowance)
            allowance.settled = True
        return Usage(spent_in, spent_out, source)

    def cancel(self, allowance):
        """Release a reservation without charging it (pre-dispatch refusal).

        Distinct from settling with ``unavailable``: nothing was billed, and
        saying so is the point.
        """
        with self._lock:
            self._check_open(allowance, "cancel")
            for budget in allowance.chain:
                budget._release(allowance)
            allowance.settled = True
            allowance.cancelled = True
            self._cancelled += 1

    def snapshot(self):
        """The full honest position of this budget, for envelopes and logs."""
        with self._lock:
            return {
                "label": self._label,
                "parent": self._parent.label if self._parent else None,
                "max_input_tokens": self._max_input,
                "max_output_tokens": self._max_output,
                "used_input_tokens": self._used_input,
                "used_output_tokens": self._used_output,
                "reserved_input_tokens": self._reserved_input,
                "reserved_output_tokens": self._reserved_output,
                "remaining_input_tokens": self.remaining_input(),
                "remaining_output_tokens": self.remaining_output(),
                "calls": self._calls,
                "cancelled": self._cancelled,
                "open_allowances": self._open,
                "over_input_tokens": self._over_input,
                "over_output_tokens": self._over_output,
                "usage_sources": dict(self._usage),
                "by_label": {k: dict(v)
                             for k, v in sorted(self._by_label.items())},
            }

    # -- internals ------------------------------------------------------
    def _chain(self):
        """This budget and every ancestor, nearest first."""
        chain, budget = [], self
        while budget is not None:
            chain.append(budget)
            budget = budget._parent
        return chain

    def _release(self, allowance):
        self._reserved_input = max(0, self._reserved_input
                                   - allowance.input_tokens)
        self._reserved_output = max(0, self._reserved_output
                                    - allowance.max_output_tokens)
        self._open = max(0, self._open - 1)

    def _record(self, spent_in, spent_out, label, source, allowance):
        self._used_input += spent_in
        self._used_output += spent_out
        self._calls += 1
        self._usage[source] += 1
        self._over_input += max(0, spent_in - allowance.input_tokens)
        self._over_output += max(0, spent_out - allowance.max_output_tokens)
        row = self._by_label.setdefault(
            label, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                    USAGE_ACTUAL: 0, USAGE_ESTIMATED: 0, USAGE_UNAVAILABLE: 0})
        row["calls"] += 1
        row["input_tokens"] += spent_in
        row["output_tokens"] += spent_out
        row[source] += 1

    def _check_open(self, allowance, verb):
        if not isinstance(allowance, Allowance) or allowance.owner is not self:
            raise HarnessError(
                f"cannot {verb} an allowance this budget does not own")
        if allowance.settled:
            raise HarnessError(
                f"allowance for {allowance.label!r} was already "
                f"{'cancelled' if allowance.cancelled else 'settled'}")


def budget_from_settings(settings, *, label="run"):
    """The run budget declared by configuration (the settings seam)."""
    return TokenBudget(
        label,
        max_input_tokens=getattr(settings, "token_budget_input",
                                 DEFAULT_RUN_INPUT_TOKENS),
        max_output_tokens=getattr(settings, "token_budget_output",
                                  DEFAULT_RUN_OUTPUT_TOKENS))
