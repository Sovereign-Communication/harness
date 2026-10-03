"""The deterministic consensus tally: the only thing allowed to decide agreement.

This module is the reason the whole design works, so it is worth being blunt
about what it is for.

A System One model (Jev) returns calibrated confidence about *the question it
was asked*. If it is handed a hallucinated screen extraction it will return a
beautifully calibrated, confidently wrong answer about the wrong state -- and
it cannot detect the error, because the error happened upstream of it and is
invisible in the payload. So agreement between independent extractors has to
be established *before* the decision tier ever sees a state, and it has to be
established by arithmetic, not by another model.

Three rules, each of which exists because violating it produces a specific
and dangerous lie:

1. **A slot that did not answer is not a vote.** Three extractors where two
   answer and one dies is not "unanimous agreement", it is a *shortfall*.
   The two are reported as different outcomes, because the remedy is
   different: shortfall means retry or fall back, disagreement means escalate.
   Conflating them teaches an operator to trust a green light that was lit
   by two votes out of three.
2. **The tally owns the verdict.** The specialist's prose can describe,
   summarise and explain the tally, but cannot overrule it. A specialist that
   says "these are obviously the same" does not get to merge two disagreeing
   field values.
3. **Per-field, not per-document.** A five-field state where four fields are
   unanimous and one is contested is not agreement and not disagreement; it
   is *four agreed fields and one contested field*, and that distinction is
   what lets a caller use the four and escalate the one.

Nothing here touches the network, a model, a clock or the filesystem. Given
the same votes it returns the same verdict, which is what makes the
guarantee testable rather than aspirational.
"""
from .errors import SchemaError
from .schema import normalize, validate_state

#: Tally outcomes. A caller may branch on these; nothing else about the
#: extraction is allowed to be treated as a verdict.
AGREED = "agreed"
DISAGREED = "disagreed"
INSUFFICIENT = "insufficient"

OUTCOMES = (AGREED, DISAGREED, INSUFFICIENT)

#: How an individual slot fared. Deliberately three-valued, because
#: "crashed" and "answered with garbage" are different operational problems.
SLOT_OK = "ok"
SLOT_MALFORMED = "malformed"
SLOT_ERROR = "error"


class Vote:
    """One extractor's answer, or the reason it has none.

    A ``state`` is only ever read after :func:`tally` has validated it against
    the schema, so a malformed extraction can never be compared, counted, or
    partially believed.
    """

    __slots__ = ("slot", "status", "state", "reason", "cost", "usage_source")

    def __init__(self, slot, status, *, state=None, reason="", cost=0.0,
                 usage_source="unavailable"):
        if status not in (SLOT_OK, SLOT_MALFORMED, SLOT_ERROR):
            raise ValueError(f"unknown slot status {status!r}")
        if status == SLOT_OK and not isinstance(state, dict):
            raise ValueError("an ok Vote must carry a state mapping")
        if status != SLOT_OK and state is not None:
            raise ValueError(
                f"a {status} Vote must not carry a state; a slot that did not "
                f"answer must not leave something behind that looks like data")
        self.slot = slot
        self.status = status
        self.state = state
        self.reason = reason
        self.cost = float(cost or 0.0)
        # ``unavailable`` is the honest default: a provider that reports no
        # usage must never be recorded as costing zero, because zero is the
        # one number a spend guard may not be given as fact.
        self.usage_source = usage_source

    def to_dict(self):
        return {
            "slot": self.slot,
            "status": self.status,
            "reason": self.reason,
            "cost": self.cost,
            "usage_source": self.usage_source,
        }

    @classmethod
    def ok(cls, slot, state, *, cost=0.0, usage_source="unavailable"):
        return cls(slot, SLOT_OK, state=state, cost=cost,
                   usage_source=usage_source)

    @classmethod
    def error(cls, slot, reason, *, cost=0.0, usage_source="unavailable"):
        return cls(slot, SLOT_ERROR, reason=reason, cost=cost,
                   usage_source=usage_source)

    @classmethod
    def malformed(cls, slot, reason, *, cost=0.0, usage_source="unavailable"):
        return cls(slot, SLOT_MALFORMED, reason=reason, cost=cost,
                   usage_source=usage_source)

    def __repr__(self):
        return f"Vote({self.slot!r}, {self.status})"


class FieldAgreement:
    """The verdict for one field across the answering slots."""

    __slots__ = ("name", "value", "agreeing", "answering", "distribution",
                 "agreed", "contested")

    def __init__(self, name, value, agreeing, answering, distribution,
                 contested):
        self.name = name
        self.value = value
        self.agreeing = agreeing
        self.answering = answering
        self.distribution = distribution
        self.agreed = not contested
        self.contested = contested

    @property
    def ratio(self):
        if not self.answering:
            return 0.0
        return self.agreeing / self.answering

    def to_dict(self, include_value=False):
        """The wire form.

        The agreed *value* and the per-value distribution are omitted by
        default for the same reason the state is: this object exists to
        report how strongly a field was agreed, and "window_title was agreed
        2-of-2" is the useful fact while "window_title was 'Confidential
        Payroll'" is the one that must not travel. Both reappear together
        under ``include_value``.
        """
        payload = {
            "field": self.name,
            "agreeing": self.agreeing,
            "answering": self.answering,
            "ratio": round(self.ratio, 6),
            "agreed": self.agreed,
        }
        if include_value:
            payload["value"] = self.value
            payload["distribution"] = {str(k): v
                                       for k, v in self.distribution.items()}
        return payload

    def __repr__(self):
        return (f"FieldAgreement({self.name!r}, agreed={self.agreed}, "
                f"{self.agreeing}/{self.answering})")


class Agreement:
    """The tally's verdict on one extraction round.

    ``state`` is populated only on :data:`AGREED`. A caller that ignores
    ``outcome`` and reads ``state`` is relying on a property the object
    guarantees structurally: for any non-``agreed`` outcome ``state`` is
    ``None``, not a partial or best-guess value.
    """

    __slots__ = ("outcome", "state", "fields", "asked", "answering",
                 "unanswered", "slots", "cost", "detail")

    def __init__(self, outcome, *, state=None, fields=(), asked=0,
                 answering=0, unanswered=(), slots=(), cost=0.0, detail=""):
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        if outcome != AGREED and state is not None:
            raise ValueError(
                f"a {outcome} Agreement must not carry a state; downstream "
                f"tiers must not receive an unverified extraction")
        self.outcome = outcome
        self.state = state
        self.fields = tuple(fields)
        self.asked = asked
        self.answering = answering
        self.unanswered = tuple(unanswered)
        self.slots = tuple(slots)
        self.cost = float(cost or 0.0)
        self.detail = detail

    @property
    def agreed_fields(self):
        return tuple(f.name for f in self.fields if f.agreed)

    @property
    def contested_fields(self):
        return tuple(f.name for f in self.fields if f.contested)

    @property
    def is_usable(self):
        """The ONLY sanctioned signal that a state may reach a decision tier."""
        return self.outcome == AGREED and self.state is not None

    def to_dict(self, include_state=False):
        """The wire form.

        ``state`` is omitted by default, and that default is the whole point:
        the agreed state is a description of what was on a screen, and a
        response that carried it would make every API response a transcript
        of the operator's desktop. The field-level verdicts below are the
        audit-safe projection -- they say *which* fields were agreed and how
        strongly, without reproducing their values.

        ``include_state=True`` exists for the decision tier, which genuinely
        needs the state, and for an operator debugging a disagreement. A
        caller that turns it on is choosing to handle screen content.
        """
        payload = {
            "outcome": self.outcome,
            "fields": [f.to_dict(include_value=include_state)
                       for f in self.fields],
            "asked": self.asked,
            "answering": self.answering,
            "unanswered": list(self.unanswered),
            "slots": [s.to_dict() for s in self.slots],
            "cost": round(self.cost, 9),
            "detail": self.detail,
        }
        if include_state:
            payload["state"] = self.state
        return payload

    def receipt(self, schema_identity, extraction_id):
        """The provenance record that travels with an agreed state.

        An agreed state handed to a decision tier without this receipt is
        unauditable, so the extraction tier is expected to attach it to
        every downstream record.
        """
        return {
            "extraction_id": extraction_id,
            "schema": schema_identity,
            "outcome": self.outcome,
            "asked": self.asked,
            "answering": self.answering,
            "unanswered": list(self.unanswered),
            "agreed_fields": list(self.agreed_fields),
            "contested_fields": list(self.contested_fields),
            "cost": round(self.cost, 9),
        }

    def __repr__(self):
        return (f"Agreement({self.outcome!r}, {self.answering}/{self.asked} "
                f"answered)")


def tally(votes, schema, *, quorum=2, min_agreement=1.0):
    """Decide whether independent extractions reached a usable state.

    ``quorum`` is the minimum number of slots that must *answer* -- not agree,
    answer -- before agreement is even considered. ``min_agreement`` is the
    fraction of answering slots that must match on a field for that field to
    be called agreed; the default of ``1.0`` means unanimity among those who
    answered, which is the only setting that makes a partial shortfall
    unambiguous.

    The order of the three checks matters and is deliberate:

    1. Validate every answer. A malformed answer is not a dissenting answer;
       it is a non-answer, and it is excluded from the denominator rather
       than counted as disagreement.
    2. Count answers. Fewer than ``quorum`` is :data:`INSUFFICIENT`, and it is
       checked *before* agreement so a 1-of-3 round can never be reported as
       a clean unanimous 1-of-1.
    3. Tally per field over the answering slots only.
    """
    if quorum < 1:
        raise ValueError("quorum must be at least 1")
    if not 0 < min_agreement <= 1:
        raise ValueError("min_agreement must be in (0, 1]")

    votes = list(votes)
    asked = len(votes)
    cost = sum(v.cost for v in votes)
    answered = []
    unanswered = []
    detail = []

    for vote in votes:
        if vote.status == SLOT_OK:
            try:
                answered.append((vote, validate_state(vote.state, schema)))
            except SchemaError as exc:
                # An answer that does not satisfy the schema is a non-answer.
                # Recording it as a dissent would let a model that returns
                # garbage dilute a field that everyone else agreed on.
                vote.status = SLOT_MALFORMED
                vote.reason = str(exc)
                unanswered.append(vote.slot)
                detail.append(f"slot {vote.slot!r} malformed: {exc}")
        else:
            unanswered.append(vote.slot)
            detail.append(f"slot {vote.slot!r} {vote.status}: {vote.reason}")

    answering = len(answered)
    if answering < quorum:
        return Agreement(
            INSUFFICIENT, asked=asked, answering=answering,
            unanswered=unanswered, slots=votes, cost=cost,
            detail=(f"{answering} of {asked} slots answered; quorum is "
                    f"{quorum}. This is a shortfall, not agreement."))

    fields = []
    for field in schema.fields:
        distribution = {}
        for _, state in answered:
            if field.name in state:
                key = normalize(field, state[field.name])
                distribution[key] = distribution.get(key, 0) + 1
        if not distribution:
            # Optional and absent everywhere. Not a disagreement.
            continue
        value, agreeing = max(distribution.items(), key=lambda kv: (kv[1],))
        # Deterministic tie-break: equal counts resolve to the
        # lexicographically smaller key, so two runs over the same votes
        # always name the same winner regardless of dict ordering.
        tied = sorted(k for k, n in distribution.items() if n == agreeing)
        value = tied[0]
        agreeing = distribution[value]
        present = sum(distribution.values())
        contested = (agreeing / present) < min_agreement
        fields.append(FieldAgreement(field.name, value, agreeing, present,
                                     distribution, contested))

    contested_fields = [f.name for f in fields if f.contested]
    if contested_fields:
        detail.append(
            f"contested field(s) {contested_fields} below the {min_agreement:g} "
            f"agreement threshold")
        return Agreement(DISAGREED, fields=fields, asked=asked,
                         answering=answering, unanswered=unanswered,
                         slots=votes, cost=cost, detail="; ".join(detail))

    # Agreement is unanimous over the answering slots, so any single answered
    # state *is* the agreed state. Taking the first rather than merging
    # guarantees the result is byte-identical to what one of the extractors
    # actually produced, which is what makes the outcome auditable.
    state = dict(answered[0][1])
    return Agreement(AGREED, state=state, fields=fields, asked=asked,
                     answering=answering, unanswered=unanswered, slots=votes,
                     cost=cost, detail="; ".join(detail))
