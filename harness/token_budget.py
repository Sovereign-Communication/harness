"""HV-3: the one owner for Hourglass token allowance and token accounting.

Two independent budgets govern a run. ``harness.spend.SpendGovernor`` owns
*money*; this module owns *tokens*. A run is bounded by whichever runs out
first, and neither budget is relaxed because the other one had headroom: a
generous dollar ceiling does not buy extra tokens, and a generous token
ceiling does not buy extra dollars. This owner never reads, writes, or
weakens the spend owner's state; it only composes with it
(:func:`reserve_composed`).

The contract:

* **Per-call maxima are issued here, not chosen by the caller.** Every call
  declares an input maximum and an output maximum. A caller may ask for
  less than this owner allows; a request *above* the issued ceiling is
  refused rather than silently clamped, because a clamped limit is a limit
  nobody chose and a lane would keep running against a number it never
  declared.
* **Stage and run ceilings compose, and default to bounded.** Aggregate
  ceilings are checked by reservation across both scopes at once: a call in
  ``planning`` draws on the run budget *and* on the planning budget. They
  default to the same validated allowance this owner issues per call
  (:data:`harness.config.MAX_HOURGLASS_TOKEN_ALLOWANCE`), so a real run is
  bounded in every dimension without anyone picking a number nobody chose --
  a run may not accumulate more of a dimension than the largest single
  allowance the policy is willing to issue. Unbounded is a deliberate,
  explicit opt-out, never what a caller gets by leaving a ceiling alone, and
  ``None`` (not a fabricated large number) is what "unbounded" looks like in
  every accessor. An owner that knows its own stages states its own
  ceilings, and a tighter one is always allowed -- including ``0``, which is
  an exhausted budget rather than an absent one.
* **Reservations, not check-then-record.** Parallel dispatch cannot rely on
  per-call preflight: N workers can each check the full remaining budget
  before any of them records. A reservation is real liability --
  ``used + outstanding + requested <= ceiling`` holds for every configured
  scope and dimension at every instant, or the reservation is refused.
  Refuse, never trim: quietly lowering what a lane asked for is how a run
  silently stops doing the work it was asked to do.
* **Honest usage labels.** ``measured`` means the provider reported the
  number, ``estimated`` means we are settling our own preflight estimate
  because the provider reported nothing, ``unavailable`` means there was
  neither a report nor an estimate. An estimate is never promoted to
  measured, and a dimension nobody could bound is never reported as zero
  we "know".
* **Failure honesty.** A call abandoned *before* dispatch releases its
  reservation and records nothing -- nothing was consumed. A call that was
  dispatched and then failed still settles, because those tokens were real;
  a partial report settles what was reported and estimates the rest.
"""
import threading
from dataclasses import dataclass, field
from typing import Optional

from .config import MAX_HOURGLASS_TOKEN_ALLOWANCE
from .errors import HarnessError
from .tokens import estimate_prompt_tokens
from .validation import bounded_int, finite_number

#: Token dimensions this owner accounts for. Both are independent ceilings:
#: a wide prompt is not a licence to ask for a wide answer, and a run that
#: bounds answers but not prompts can still be buried in input.
INPUT = "input"
OUTPUT = "output"
DIMENSIONS = (INPUT, OUTPUT)

#: How a token count came to be known. These strings are part of the
#: published envelope vocabulary and mirror the JEV policy's usage-source
#: labelling; surfaces (HV-6) report them verbatim.
MEASURED = "measured"        # the provider reported it
ESTIMATED = "estimated"      # our own preflight estimate, provider said nothing
UNAVAILABLE = "unavailable"  # no report and no estimate to fall back on

_SOURCES = (MEASURED, ESTIMATED, UNAVAILABLE)

#: Worst label last: a run containing one unavailable contribution understates
#: its total, which is strictly less honest than an over-estimating one.
_SOURCE_RANK = {MEASURED: 0, ESTIMATED: 1, UNAVAILABLE: 2}

_MAX_STAGE_LEN = 64


def _stage_name(stage):
    """Normalize a stage identifier.

    Stages are named by code, so an odd name here is a programming error,
    not user input -- but it still must not silently create a fresh scope
    (``"plan"`` and ``"planning"`` would each get a full stage budget) nor
    aggregate everything under a wildcard.
    """
    if not isinstance(stage, str) or not stage.strip():
        raise HarnessError("stage must be a non-empty string")
    name = stage.strip()
    if len(name) > _MAX_STAGE_LEN:
        raise HarnessError(f"stage must be at most {_MAX_STAGE_LEN} characters")
    if "*" in name or name in {".", ".."}:
        raise HarnessError(f"stage {name!r} is not a valid stage identifier")
    return name


@dataclass(frozen=True)
class TokenAllowance:
    """The maxima this owner issues for one call.

    ``max_output_tokens`` is ``None`` only when the owner issued no per-call
    output ceiling at all; a caller may then omit its output bound, and
    that dimension settles ``UNAVAILABLE`` unless the provider reports it.
    """

    max_input_tokens: int
    max_output_tokens: Optional[int] = None

    def as_dict(self):
        return {"max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens}


@dataclass
class TokenUsage:
    """What one settled call consumed, and how each dimension is known.

    ``measured``/``estimated``/``unavailable`` are per dimension on purpose:
    providers routinely report completion tokens but not prompt tokens, and
    a single blended label would have to lie about one of them.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    input_source: str = UNAVAILABLE
    output_source: str = UNAVAILABLE
    settled: bool = True
    label: str = ""

    @property
    def measured(self):
        """True when the provider itself reported at least one dimension."""
        return MEASURED in (self.input_source, self.output_source)

    @property
    def estimated(self):
        """True when at least one dimension is our own preflight estimate."""
        return ESTIMATED in (self.input_source, self.output_source)

    def as_dict(self):
        return {"input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "input_source": self.input_source,
                "output_source": self.output_source,
                "settled": self.settled,
                "label": self.label}


@dataclass
class TokenReservation:
    """A held token claim, released by :meth:`TokenBudget.settle`/``cancel``."""

    call_id: str
    stage: str
    input_tokens: int
    output_tokens: Optional[int] = None
    released: bool = field(default=False)

    @property
    def dimensions(self):
        """``{dimension: reserved amount}`` for every bounded dimension."""
        out = {INPUT: self.input_tokens}
        if self.output_tokens is not None:
            out[OUTPUT] = self.output_tokens
        return out


def _zero():
    return {INPUT: 0, OUTPUT: 0}


def _ceiling(value, name):
    """Validate one optional aggregate ceiling. ``None`` means 'no ceiling'."""
    if value is None:
        return None
    return bounded_int(value, name, 0, MAX_HOURGLASS_TOKEN_ALLOWANCE)


class TokenBudget:
    """Run-scoped token allowance and accounting owner (HV-3).

    One instance owns one run. Stage and run ceilings compose through
    reservations; per-call maxima are issued by this owner; settlement keeps
    measured/estimated/unavailable apart. Thread-safe: every mutation and
    every ceiling check happens under one re-entrant lock, so the
    ``used + outstanding + requested <= ceiling`` invariant is checked and
    updated in the same critical section rather than re-derived by callers.
    """

    def __init__(self, max_input_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE,
                 max_output_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE,
                 run_max_input_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE,
                 run_max_output_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE,
                 stage_max_input_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE,
                 stage_max_output_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE,
                 *, label="run"):
        """Construct an owner. Fails closed: every per-call dimension is
        bounded unless the caller *deliberately* declines to bound one.

        Both per-call maxima default to the repository's hard token
        maximum -- the same value ``load_settings`` supplies -- so a
        consumer who constructs this owner directly gets the same output
        enforcement as one built through :meth:`from_settings`, rather than
        discovering it has none when a call blows through. ``None`` remains
        a legal, explicit opt-out for a run that genuinely cannot bound a
        dimension; it is never what you get by accident.

        The four aggregate ceilings fail closed the same way. Left alone
        they default to that same validated allowance, which is the point:
        a run that consumed more of a dimension than the policy issues for
        a single call is out of policy by definition, so the default needs
        no number chosen for it. Nothing here requires the aggregate to be
        looser or tighter than the per-call ceiling -- an owner may pin a
        stage to a fraction of the run, or to ``0`` to refuse a stage
        outright -- so no consistency constraint is imposed on it.
        """
        self.label = str(label or "run")
        # Per-call ceilings this owner issues. ``max_output_tokens=None``
        # means this owner issues no per-call output ceiling.
        self.max_input_tokens = bounded_int(
            max_input_tokens, "hourglass_max_input_tokens", 1,
            MAX_HOURGLASS_TOKEN_ALLOWANCE)
        self.max_output_tokens: Optional[int] = (
            None if max_output_tokens is None else bounded_int(
                max_output_tokens, "hourglass_max_output_tokens", 1,
                MAX_HOURGLASS_TOKEN_ALLOWANCE))
        # Aggregate ceilings. They default to the same validated allowance
        # as the per-call maxima (see the signature), so a run is bounded
        # in every dimension without a chosen number. ``None`` still means
        # a deliberately unbounded dimension, reported as ``None`` rather
        # than as a big number.
        self.run_max_input_tokens: Optional[int] = _ceiling(
            run_max_input_tokens, "hourglass_run_max_input_tokens")
        self.run_max_output_tokens: Optional[int] = _ceiling(
            run_max_output_tokens, "hourglass_run_max_output_tokens")
        self.stage_max_input_tokens: Optional[int] = _ceiling(
            stage_max_input_tokens, "hourglass_stage_max_input_tokens")
        self.stage_max_output_tokens: Optional[int] = _ceiling(
            stage_max_output_tokens, "hourglass_stage_max_output_tokens")
        self._used = _zero()
        self._outstanding = _zero()
        self._stages = {}
        self._reservations = []
        self._counts = {dim: {src: 0 for src in _SOURCES} for dim in DIMENSIONS}
        self._calls_settled = 0
        self._seq = 0
        self._budget_lock = threading.RLock()

    # ---- construction ----------------------------------------------------

    @classmethod
    def from_settings(cls, settings, *, label="run"):
        """Build the run's budget from validated ``load_settings`` output.

        The configured per-call values are *this owner's issued ceilings*,
        so a lane that asks for more is refused rather than served.
        """
        return cls(
            max_input_tokens=settings.hourglass_max_input_tokens,
            max_output_tokens=settings.hourglass_max_output_tokens,
            run_max_input_tokens=settings.hourglass_run_max_input_tokens,
            run_max_output_tokens=settings.hourglass_run_max_output_tokens,
            stage_max_input_tokens=settings.hourglass_stage_max_input_tokens,
            stage_max_output_tokens=settings.hourglass_stage_max_output_tokens,
            label=label)

    def per_call_allowance(self):
        """The maxima this owner issues for the next call."""
        return TokenAllowance(self.max_input_tokens, self.max_output_tokens)

    # ---- internal scope helpers -----------------------------------------

    def _stage_totals(self, stage, create=False):
        with self._budget_lock:
            if stage not in self._stages:
                if not create:
                    return None
                self._stages[stage] = {"used": _zero(), "outstanding": _zero()}
            return self._stages[stage]

    def _scopes(self, stage, create=False):
        """``[(scope name, totals, ceilings)]`` -- run first, then stage.

        Read-only by default. A stage with no entry yet is represented by a
        detached zero view, so a check against it does the same arithmetic
        without leaving an empty stage behind in the snapshot -- a run that
        only *asked* whether a stage would fit did not run that stage.

        Only :meth:`reserve` passes ``create=True``. A claim is what makes a
        stage real, and a stage that never held a claim never happened.
        """
        totals = self._stage_totals(stage, create=create)
        return [
            ("run", {"used": self._used, "outstanding": self._outstanding},
             {INPUT: self.run_max_input_tokens,
              OUTPUT: self.run_max_output_tokens}),
            (f"stage {stage}",
             totals if totals is not None
             else {"used": _zero(), "outstanding": _zero()},
             {INPUT: self.stage_max_input_tokens,
              OUTPUT: self.stage_max_output_tokens}),
        ]

    def _require_room(self, stage, amounts):
        """Refuse a request that any configured scope/dimension cannot hold.

        Called under the lock; raises before any mutation, so a refused
        reservation leaves no partial claim in any scope.
        """
        for scope, totals, caps in self._scopes(stage):
            for dim in DIMENSIONS:
                amount = amounts.get(dim)
                cap = caps.get(dim)
                if amount is None or cap is None:
                    continue
                committed = totals["used"][dim] + totals["outstanding"][dim]
                if committed + amount > cap:
                    raise HarnessError(
                        f"reservation of {amount} {dim} tokens ({scope}) would put "
                        f"used+outstanding at {committed + amount} over the {cap} "
                        f"ceiling (stage={stage}, "
                        f"{max(0, cap - committed)} still unreserved). Refusing.")

    def _amounts(self, prompt_text, input_tokens, max_output_tokens):
        """Validate one call's requested maxima into ``{dimension: amount}``.

        Two refusals happen here, before dispatch: a caller asking above the
        ceiling this owner issued, and a caller asking to exceed an
        aggregate ceiling. Neither is repaired by clamping.
        """
        if input_tokens is None:
            if prompt_text is None:
                raise HarnessError(
                    "a call needs prompt_text or input_tokens to account for")
            input_tokens = estimate_prompt_tokens(prompt_text)
        amount_in = bounded_int(input_tokens, "max_input_tokens", 1,
                                self.max_input_tokens)
        if self.max_output_tokens is None:
            # The owner issues no per-call output ceiling, so a caller may
            # decline to bound its output; that dimension then settles
            # UNAVAILABLE unless the provider reports it.
            if max_output_tokens is None:
                amount_out = None
            else:
                amount_out = bounded_int(
                    max_output_tokens, "max_output_tokens", 1,
                    MAX_HOURGLASS_TOKEN_ALLOWANCE)
        else:
            if max_output_tokens is None:
                raise HarnessError(
                    "max_output_tokens is required: this run issues a per-call "
                    f"output ceiling of {self.max_output_tokens}")
            amount_out = bounded_int(max_output_tokens, "max_output_tokens", 1,
                                     self.max_output_tokens)
        amounts = {INPUT: amount_in}
        if amount_out is not None:
            amounts[OUTPUT] = amount_out
        return amounts

    def _validated_request(self, stage, prompt_text, input_tokens,
                           max_output_tokens, call_id, label):
        stage = _stage_name(stage)
        amounts = self._amounts(prompt_text, input_tokens, max_output_tokens)
        with self._budget_lock:
            self._require_room(stage, amounts)
        return stage, amounts, (str(call_id or label or stage)
                                if (call_id or label) else None)

    # ---- preflight and reservation --------------------------------------

    def preflight_call(self, stage, *, prompt_text=None, input_tokens=None,
                       max_output_tokens=None, call_id=None, label=None):
        """Check one call against every ceiling without claiming anything.

        Named ``preflight_call`` rather than ``preflight``: ``spend`` owns
        ``preflight`` (the money preflight), and one policy has one owner.

        Returns the per-call ``TokenAllowance`` this request would be
        served under. This is a *check*: two concurrent callers can both
        pass it and still not both fit, which is why dispatch goes through
        :meth:`reserve`.
        """
        _, amounts, _ = self._validated_request(
            stage, prompt_text, input_tokens, max_output_tokens, call_id, label)
        return TokenAllowance(amounts[INPUT], amounts.get(OUTPUT))

    def reserve(self, stage, *, prompt_text=None, input_tokens=None,
                max_output_tokens=None, call_id=None, label=None):
        """Claim worst-case tokens for one in-flight call, or refuse.

        The claim lands on the run scope and on the stage scope at the same
        time, so neither budget can be overrun by the other's headroom.
        Every in-flight call's claim is visible to later callers as
        outstanding liability.
        """
        stage, amounts, base_id = self._validated_request(
            stage, prompt_text, input_tokens, max_output_tokens, call_id, label)
        with self._budget_lock:
            # Re-check under the lock: the validated check above is only a
            # courtesy for the error message; the reservation itself must be
            # decided against committed + outstanding at this instant.
            self._require_room(stage, amounts)
            # Every reservation gets its own identity even when the caller
            # supplies none: two calls in one stage must be distinguishable
            # in a refusal message and in the ledger.
            self._seq += 1
            call_id = base_id or f"{stage}#{self._seq}"
            reservation = TokenReservation(
                call_id=call_id, stage=stage, input_tokens=amounts[INPUT],
                output_tokens=amounts.get(OUTPUT))
            # The claim is what makes the stage real; a scope view alone
            # never creates one.
            self._stage_totals(stage, create=True)
            for _scope, totals, _caps in self._scopes(stage):
                for dim, amount in reservation.dimensions.items():
                    totals["outstanding"][dim] += amount
            self._reservations.append(reservation)
            return reservation

    # ---- settlement ------------------------------------------------------

    def _release(self, reservation):
        """Drop a reservation's liability. Raises if it is not held."""
        with self._budget_lock:
            for held in self._reservations:
                if held is reservation:
                    self._reservations.remove(held)
                    for _scope, totals, _caps in self._scopes(reservation.stage):
                        for dim, amount in held.dimensions.items():
                            totals["outstanding"][dim] = max(
                                0, totals["outstanding"][dim] - amount)
                    held.released = True
                    return
        raise HarnessError("settlement of an unknown token reservation")

    def cancel(self, reservation):
        """Release a call that was never dispatched; record nothing.

        This is the pre-dispatch path (a declined dispatch, or a
        ``ToolCancelled`` raised before the request left). Nothing was
        consumed, so nothing is recorded -- but the liability is released,
        or one abandoned call would strand budget for the whole run.
        """
        self._release(reservation)
        return TokenUsage(input_source=UNAVAILABLE, output_source=UNAVAILABLE,
                          settled=False, label=reservation.call_id)

    def settle(self, reservation, *, input_tokens=None, output_tokens=None,
               dispatched=True, error=None):
        """Settle a dispatched call: release the claim, record the truth.

        Reported numbers settle as ``MEASURED``. A dimension the provider
        said nothing about settles our preflight estimate as ``ESTIMATED``,
        which over-counts rather than under-counts. A dimension with neither
        report nor estimate settles ``UNAVAILABLE`` at zero, and says so, so
        a downstream total is never mistaken for a complete one.

        A dispatched call settles even when ``error`` is set: the request
        was sent, so its tokens were real. Use :meth:`cancel` for the
        pre-dispatch case instead.
        """
        if not dispatched:
            return self.cancel(reservation)
        settled = {}
        for dim, reported in ((INPUT, input_tokens), (OUTPUT, output_tokens)):
            if reported is not None:
                value = bounded_int(reported, f"reported {dim} tokens", 0,
                                    MAX_HOURGLASS_TOKEN_ALLOWANCE)
                source = MEASURED
            elif getattr(reservation, f"{dim}_tokens") is not None:
                value = getattr(reservation, f"{dim}_tokens")
                source = ESTIMATED
            else:
                value = 0
                source = UNAVAILABLE
            settled[dim] = (value, source)
        # Release first: the liability was already counted against the
        # ceilings, so a breach below must not also strand the claim.
        self._release(reservation)
        breach = None
        with self._budget_lock:
            for scope, totals, caps in self._scopes(reservation.stage):
                for dim, (value, _source) in settled.items():
                    totals["used"][dim] += value
                    cap = caps.get(dim)
                    if (breach is None and cap is not None
                            and totals["used"][dim] > cap):
                        breach = (scope, dim, totals["used"][dim], cap)
            # Tallied once per settled call, not once per scope it touched.
            for dim, (_value, source) in settled.items():
                self._counts[dim][source] += 1
            self._calls_settled += 1
        usage = TokenUsage(
            input_tokens=settled[INPUT][0], output_tokens=settled[OUTPUT][0],
            input_source=settled[INPUT][1], output_source=settled[OUTPUT][1],
            settled=True, label=reservation.call_id)
        if breach is not None:
            # Real consumption cannot be un-spent, so it is recorded and
            # then the run is stopped. The dollar owner can refuse a
            # settled actual because its reservation already covered it;
            # these tokens were spent outside any reservation's mercy.
            scope, dim, total, cap = breach
            detail = f" ({error})" if error else ""
            raise HarnessError(
                f"actual {total} {dim} tokens exceeded the {cap} {scope} ceiling "
                f"after '{reservation.call_id}'{detail}. Aborting.")
        return usage

    # ---- accessors -------------------------------------------------------

    def _totals(self, stage=None):
        with self._budget_lock:
            if stage is None:
                return {"used": dict(self._used),
                        "outstanding": dict(self._outstanding)}
            key = _stage_name(stage)
            totals = self._stages.get(key)
            if totals is None:
                return {"used": _zero(), "outstanding": _zero()}
            return {"used": dict(totals["used"]),
                    "outstanding": dict(totals["outstanding"])}

    def _remaining(self, totals, caps):
        out = {}
        for dim in DIMENSIONS:
            cap = caps.get(dim)
            out[dim] = None if cap is None else max(
                0, cap - (totals["used"][dim] + totals["outstanding"][dim]))
        return out

    def used(self, stage=None):
        """Tokens settled so far (run-wide, or for one stage)."""
        return self._totals(stage)["used"]

    def outstanding(self, stage=None):
        """Worst-case tokens claimed by in-flight calls."""
        return self._totals(stage)["outstanding"]

    def remaining(self, stage=None):
        """Tokens this run (or one stage) can still claim.

        ``None`` in a dimension means no ceiling is configured there -- not
        a large number. Concurrent claims are counted, so this is the
        honest single answer to "what is left".
        """
        with self._budget_lock:
            if stage is None:
                return self._remaining(
                    {"used": self._used, "outstanding": self._outstanding},
                    {INPUT: self.run_max_input_tokens,
                     OUTPUT: self.run_max_output_tokens})
            name = _stage_name(stage)
            totals = self._stage_totals(name)
            return self._remaining(
                totals if totals is not None
                else {"used": _zero(), "outstanding": _zero()},
                {INPUT: self.stage_max_input_tokens,
                 OUTPUT: self.stage_max_output_tokens})

    def usage_source(self, dimension=INPUT):
        """The honest label for a dimension's settled total.

        Worst-wins: one ``unavailable`` contribution means the total
        understates reality, one ``estimated`` means part of it is our own
        arithmetic, and only an all-measured total is ``measured``.
        """
        with self._budget_lock:
            counts = self._counts.get(dimension)
            if not counts:
                raise HarnessError(f"unknown token dimension {dimension!r}")
            present = [src for src in _SOURCES if counts[src]]
            if not present:
                return UNAVAILABLE
            return max(present, key=_SOURCE_RANK.__getitem__)

    def usage_counts(self):
        """``{dimension: {source: count}}`` for surfaces that report detail."""
        with self._budget_lock:
            return {dim: dict(counts) for dim, counts in self._counts.items()}

    def snapshot(self):
        """Synchronized token state for result envelopes.

        Aggregate keys appear only when those ceilings are configured, so a
        run with no run/stage budget keeps the minimal per-call + used +
        remaining shape. ``remaining`` is ``None`` for an unbounded
        dimension, and ``usage_source`` is the honest worst label rather
        than an implied measurement.
        """
        with self._budget_lock:
            run_caps = {INPUT: self.run_max_input_tokens,
                        OUTPUT: self.run_max_output_tokens}
            stage_caps = {INPUT: self.stage_max_input_tokens,
                          OUTPUT: self.stage_max_output_tokens}
            out = {
                "per_call": self.per_call_allowance().as_dict(),
                "used": dict(self._used),
                "outstanding": dict(self._outstanding),
                "remaining": self._remaining(
                    {"used": self._used, "outstanding": self._outstanding},
                    run_caps),
                "usage_source": {dim: self.usage_source(dim) for dim in DIMENSIONS},
                "usage_counts": self.usage_counts(),
                "calls_settled": self._calls_settled,
                "calls_reserved": len(self._reservations),
            }
            if self.run_max_input_tokens is not None \
                    or self.run_max_output_tokens is not None:
                out["run_ceiling"] = {k: v for k, v in run_caps.items()
                                      if v is not None}
            if self.stage_max_input_tokens is not None \
                    or self.stage_max_output_tokens is not None:
                out["stage_ceiling"] = {k: v for k, v in stage_caps.items()
                                        if v is not None}
            if self._stages:
                out["stages"] = {
                    name: {"used": dict(t["used"]),
                           "outstanding": dict(t["outstanding"]),
                           "remaining": self._remaining(t, stage_caps)}
                    for name, t in sorted(self._stages.items())}
            return out


class ComposedCall:
    """One dispatch bounded by both owners at once.

    The token claim and the dollar claim are independent liabilities held in
    independent budgets. Each is settled through the owner that created it:
    token usage through :class:`TokenBudget`, money through
    ``SpendGovernor``. Settling the token side can never move the dollar
    side, and vice versa.

    Because the two legs are independent, they are also closed
    *independently*: either can be finished without the other, and a leg
    that is already closed is skipped rather than raising and stranding the
    one that is still open. That is what makes a refused settlement
    recoverable.

    This lane accounts for tokens and refuses to account for money. A dollar
    actual must arrive from the caller (which owns cost estimation and the
    provider's reported ``usage``); when it does not, the settlement is
    refused rather than recorded as a measured ``$0.00``.
    """

    def __init__(self, budget, governor, tokens, dollars, label):
        self.budget = budget
        self.governor = governor
        self.tokens = tokens
        self.dollars = dollars
        self.label = label
        # The dollar leg is the one this class cannot ask the spend owner
        # about, so it tracks it itself. A token reservation, by contrast,
        # reports "unknown" if it has already been released.
        self._dollars_open = True
        self._usage = None

    @property
    def stage(self):
        return self.tokens.stage

    @property
    def dollar_outstanding(self):
        """True while the dollar liability is still held by the governor."""
        return self._dollars_open

    def _close_dollars(self, cost):
        """Release the dollar liability, recording ``cost`` as the actual."""
        if not self._dollars_open:
            return False
        # The spend owner releases the claim before it applies the ceiling
        # check, so the leg is closed either way.
        self._dollars_open = False
        self.governor.reconcile(self.dollars, cost)
        return True

    def settle(self, *, cost, input_tokens=None, output_tokens=None, error=None):
        """Settle a dispatched call through both owners.

        ``cost`` is required and is handed to the spend owner untouched.
        ``cost=None`` is a refusal, not a zero: the token side still settles
        (those tokens were really spent) and the dollar reservation is left
        outstanding so the run cannot respend it on a guess. Finish it with
        :meth:`settle_dollar`, or abandon the call with :meth:`cancel`.

        When a real cost *is* given, the dollar claim is still released if
        token settlement raises -- a broken call must not strand the run's
        money budget as well as its token budget -- but a failure closing
        the money leg never replaces the error that explains the failure.

        For a call abandoned before dispatch use :meth:`cancel`, which
        records a true ``$0.00`` because nothing was ever sent.
        """
        if cost is None:
            self._usage = self.budget.settle(
                self.tokens, input_tokens=input_tokens,
                output_tokens=output_tokens, error=error)
            raise HarnessError(
                f"refusing to settle '{self.label}' with an unknown dollar "
                f"amount. The token side is settled and the "
                f"${self.dollars[1]:.6f} dollar reservation is left "
                f"outstanding, so the run cannot respend it on a guess. "
                f"Call settle_dollar(cost) with the provider's reported "
                f"amount, or cancel() if nothing was sent.")
        try:
            usage = self.budget.settle(
                self.tokens, input_tokens=input_tokens,
                output_tokens=output_tokens, error=error)
        except BaseException:
            try:
                self._close_dollars(cost)
            except BaseException:
                # The token error is the one that explains what went
                # wrong; a follow-on "unknown reservation" from the money
                # leg is a symptom. Do not let it mask the cause.
                pass
            raise
        self._usage = usage
        self._close_dollars(cost)
        return usage

    def settle_dollar(self, cost):
        """Close only the dollar leg, with the caller's real cost.

        The supported way to finish a settlement that :meth:`settle`
        refused for an unknown dollar amount: the token leg is already
        settled, so the money is the only thing still outstanding. This
        lane does not estimate money -- ``cost`` is the caller's number.
        """
        if not self._dollars_open:
            raise HarnessError(
                f"'{self.label}' has no outstanding dollar reservation; "
                f"its money leg was already settled.")
        self._close_dollars(cost)
        return cost

    def cancel(self):
        """Abandon: release every leg still open, recording nothing.

        Safe to call at any point in the call's life, including after a
        settlement that :meth:`settle` refused. A leg that is already
        closed is skipped rather than raising, so the other one is never
        stranded. ``$0.00`` is recorded for the money leg only when it is
        still held, because a true zero is the cost of a call that was
        never sent -- which is exactly what an unknown cost is not.
        """
        try:
            usage = self.budget.cancel(self.tokens)
        except HarnessError:
            # The token leg is already settled; there is nothing to
            # release there. The money leg is what this call still owes.
            usage = self._usage
        self._close_dollars(0.0)
        return usage


def reserve_composed(budget, governor, stage, *, cost, prompt_text=None,
                     input_tokens=None, max_output_tokens=None, call_id=None,
                     label=None):
    """Take the token reservation and the dollar reservation, or neither.

    Both budgets are enforced before dispatch and neither is allowed to
    substitute for the other. The token claim is taken first purely because
    it is the cheaper thing to unwind: if the spend owner refuses, the
    token claim is released, so a refused call leaves no liability behind
    in either budget.

    ``cost`` is the call's worst-case dollar amount and is validated before
    either claim is taken.
    """
    amount = finite_number(cost, "reservation", 0.0)
    tokens = budget.reserve(
        stage, prompt_text=prompt_text, input_tokens=input_tokens,
        max_output_tokens=max_output_tokens, call_id=call_id, label=label)
    try:
        dollars = governor.reserve(amount, label or tokens.call_id)
    except BaseException:
        budget.cancel(tokens)
        raise
    return ComposedCall(budget, governor, tokens, dollars,
                        label or tokens.call_id)
