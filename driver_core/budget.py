"""Pre-flight spend and token accounting for driver-core.

The rule this module exists to enforce: **a ceiling is checked before the
call, not after it.** A guard that settles a bill it could not have refused is
a report, not a control, and a driver that clicks things on a local machine
will eventually find a model that bills more than the estimate.

Two independent ceilings, deliberately not able to raise each other:

* dollars (:mod:`driver_core.budget`), because that is what the operator
  actually pays;
* tokens, because a free or cheap route can still overflow a context window,
  and a dollar ceiling says nothing about how much text a step may read.

Reservation is the unit of accounting. One logical step reserves its worst
case up front, dispatches at most once, and then settles exactly once --
recording ``actual`` when the provider told us, ``estimated`` when it did
not, and never ``0`` for a call it could not measure. A cost the driver
cannot read is charged at the full reservation, because zero is the one
number a spend guard may not be handed as fact.
"""
import threading

from .errors import BudgetRefused

#: How the real usage for a dispatched call was obtained.
ACTUAL = "actual"
ESTIMATED = "estimated"
UNAVAILABLE = "unavailable"


class Budget:
    """A dollar ceiling with reserve-then-settle accounting.

    Thread-safe: the whole point of a ceiling is that it holds when several
    workers race against the same remaining balance, and a check-then-act
    race would let each of N workers preflight against the full remainder and
    collectively overspend it.
    """

    def __init__(self, ceiling_usd, *, step_ceiling_usd=None):
        if ceiling_usd <= 0:
            raise ValueError("ceiling_usd must be positive")
        self._ceiling = float(ceiling_usd)
        self._step_ceiling = float(step_ceiling_usd or ceiling_usd)
        self._lock = threading.RLock()
        self._spent = 0.0
        self._reserved = 0.0
        self._entries = []
        #: Live reservations, tracked from the moment they are taken. Without
        #: this a leaked reservation is invisible: it holds its share of the
        #: balance forever and nothing reports it, which is the precise
        #: failure ``open_reservations`` exists to make visible.
        self._live = []

    # -- queries ---------------------------------------------------------

    @property
    def ceiling(self):
        return self._ceiling

    @property
    def spent(self):
        with self._lock:
            return self._spent

    @property
    def reserved(self):
        with self._lock:
            return self._reserved

    @property
    def remaining(self):
        with self._lock:
            return max(0.0, self._ceiling - self._spent - self._reserved)

    def snapshot(self):
        with self._lock:
            return {
                "ceiling_usd": self._ceiling,
                "spent_usd": round(self._spent, 9),
                "reserved_usd": round(self._reserved, 9),
                "remaining_usd": round(max(0.0, self._ceiling - self._spent
                                           - self._reserved), 9),
                "entries": list(self._entries),
            }

    # -- the two operations ---------------------------------------------

    def reserve(self, estimated_usd, label=""):
        """Hold ``estimated_usd`` against the ceiling, or raise.

        Returns a :class:`Reservation`. The reservation is the accounting
        token: it settles or cancels exactly once, and it cannot be settled
        after it has been cancelled. A leaked reservation is visible through
        :meth:`open_reservations` rather than holding the balance forever.
        """
        estimate = float(estimated_usd)
        if estimate < 0:
            raise ValueError("estimated_usd must be >= 0")
        with self._lock:
            if estimate > self._step_ceiling:
                raise BudgetRefused(estimate, self._step_ceiling, label)
            if estimate > self._ceiling - self._spent - self._reserved:
                raise BudgetRefused(estimate, self._remaining_locked(), label)
            self._reserved += estimate
            reservation = Reservation(self, estimate, label)
            self._live.append(reservation)
            return reservation

    def _remaining_locked(self):
        return max(0.0, self._ceiling - self._spent - self._reserved)

    def open_reservations(self):
        with self._lock:
            return list(self._live)

    def _release(self, reservation, amount, source):
        """The single mutation point. Both settle and cancel route here."""
        with self._lock:
            reservation._finish(amount, source)
            self._reserved -= reservation.estimate
            self._spent += amount
            if reservation in self._live:
                self._live.remove(reservation)
            self._entries.append({
                "label": reservation.label,
                "estimated_usd": round(reservation.estimate, 9),
                "charged_usd": round(amount, 9),
                "source": source,
                "state": reservation.state,
            })


RESERVED = "reserved"
SETTLED = "settled"
CANCELLED = "cancelled"


class Reservation:
    """A held amount. Settles or cancels exactly once."""

    __slots__ = ("_budget", "estimate", "label", "state", "charged",
                 "usage_source")

    def __init__(self, budget, estimate, label):
        self._budget = budget
        self.estimate = estimate
        self.label = label
        self.state = RESERVED
        self.charged = 0.0
        self.usage_source = None

    def settle(self, actual_usd=None, source=ACTUAL):
        """Charge the call.

        ``actual_usd=None`` means the provider reported no usage. That is
        charged at the **full reservation** and labelled ``unavailable`` --
        never at zero. ``source`` may be passed explicitly when the caller
        knows the number is an estimate rather than a report.
        """
        if self.state != RESERVED:
            raise RuntimeError(
                f"reservation {self.label!r} is {self.state}; it settles or "
                f"cancels exactly once")
        if actual_usd is None:
            amount, resolved = self.estimate, UNAVAILABLE
        else:
            amount = float(actual_usd)
            if amount < 0:
                raise ValueError("actual_usd must be >= 0")
            resolved = source
        self._budget._release(self, amount, resolved)
        self.charged = amount
        self.usage_source = resolved
        return amount

    def cancel(self):
        """The pre-dispatch refusal. Charges nothing."""
        if self.state != RESERVED:
            raise RuntimeError(
                f"reservation {self.label!r} is {self.state}; it settles or "
                f"cancels exactly once")
        self._budget._release(self, 0.0, CANCELLED)
        return 0.0

    def _finish(self, amount, source):
        self.state = SETTLED if source != CANCELLED else CANCELLED

    def __repr__(self):
        return (f"Reservation({self.label!r}, {self.estimate:.6f}, "
                f"{self.state})")


def estimate_call_cost(input_tokens, *, price_per_million):
    """Worst-case cost of one call, priced on input only."""
    if input_tokens < 0:
        raise ValueError("input_tokens must be >= 0")
    return input_tokens * float(price_per_million) / 1_000_000
