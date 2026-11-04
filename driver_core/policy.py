"""The gates: what may be acted on, and what may not.

Two questions have to be answered before anything irreversible happens, and
they are answered by code in this module rather than by a model anywhere
above it.

**Is the state real?** :func:`check_agreement` takes the tally's verdict and
the configured policy and returns a decision about whether the state may
reach the decision tier. The mapping is narrow on purpose: only
:data:`~driver_core.consensus.AGREED` survives, and every other outcome gets
its own reason so a caller can tell a shortfall from a disagreement and act
accordingly -- retry the former, escalate the latter.

**Is the decision worth acting on?** :func:`check_decision` applies the
confidence threshold and the guards. A guard is a property of the situation
rather than a vote, so a state that is known to be in flux is refused even
when the model is confident about it -- and, importantly, a guard that
returned nothing is *not* treated as a guard that passed.

The thresholds themselves live in configuration. This module owns the
comparisons; it does not own the numbers, because the right confidence bar
for "click a button" is not obviously the right bar for "read a field".
"""
from .consensus import AGREED, DISAGREED, INSUFFICIENT
from .verdicts import PASS, REVIEW, Verdict

# This gate's own refusal. Distinct values, distinct remedies: ``blocked``
# names *this* gate's refusal, while ``pass`` and ``review`` are the shared
# vocabulary, owned by :mod:`driver_core.verdicts` and re-exported here.
BLOCKED = "blocked"

# Why a state was blocked. Distinct values, distinct remedies.
SHORTFALL = "insufficient_agreement"
DISAGREEMENT = "extraction_disagreement"
LOW_CONFIDENCE = "confidence_below_threshold"
UNSTABLE = "state_not_stable"
UNUSABLE = "decision_not_usable"
NO_ACTION = "no_action_recommended"


def check_agreement(agreement, *, quorum, min_agreement, min_answering=None):
    """Decide whether a verified state may be handed to the decision tier.

    ``min_answering`` is an optional extra floor on *how many* slots must
    have answered, independent of the quorum used for the tally. It exists
    because quorum governs whether a verdict was possible, while this governs
    how much evidence that verdict rests on -- and "two agreed" is weaker
    evidence than "all three agreed", even when both produce ``agreed``.
    """
    if agreement.outcome == INSUFFICIENT:
        return GateResult(BLOCKED, SHORTFALL,
                          f"{agreement.answering} of {agreement.asked} slots "
                          f"answered; quorum is {quorum}. Retry or fall back "
                          f"to a structured source.")
    if agreement.outcome == DISAGREED:
        return GateResult(
            BLOCKED, DISAGREEMENT,
            f"extractors disagreed on {list(agreement.contested_fields)}; "
            f"no state was produced")
    if agreement.outcome != AGREED:
        return GateResult(BLOCKED, SHORTFALL,
                          f"unrecognised tally outcome "
                          f"{agreement.outcome!r}")
    if min_answering is not None and agreement.answering < min_answering:
        return GateResult(
            BLOCKED, SHORTFALL,
            f"only {agreement.answering} extractors answered; this policy "
            f"requires at least {min_answering} even though they agreed")
    if not agreement.state:
        # Structurally unreachable for an AGREED tally, which is the point:
        # this is a tripwire on an invariant, not a branch.
        return GateResult(BLOCKED, SHORTFALL,
                          "tally reported agreement but carried no state")
    return GateResult(PASS, None,
                      f"{agreement.answering} of {agreement.asked} extractors "
                      f"agreed on {len(agreement.agreed_fields)} field(s)")


def check_decision(decision, *, threshold, require_stable=True,
                   action_class=None, ladder=None):
    """Decide whether a recommendation may be executed.

    Order matters: usability is checked before confidence, because an
    unusable envelope has no confidence to compare and reporting it as
    "low confidence" would send an operator looking in the wrong place.

    The per-action ladder runs *last*, after every other gate has passed,
    and only when one is handed in. It can therefore only raise the bar:
    nothing this function refused before can pass with a ladder in play,
    and without one the verdicts are exactly the ones above. ``ladder`` maps
    a declared consequence class to its rung; ``action_class`` is the
    decided action's class, and a class with no rung keeps the global
    ``threshold`` alone.
    """
    if not decision.usable:
        return GateResult(
            BLOCKED, UNUSABLE,
            f"decision status {decision.status!r}: "
            f"{decision.stop_reason or 'not usable'}")

    if decision.recommended_action == "no_action":
        return GateResult(BLOCKED, NO_ACTION,
                          "the correct response is to change nothing")

    if decision.confidence is None or decision.confidence < threshold:
        return GateResult(
            BLOCKED, LOW_CONFIDENCE,
            f"confidence {decision.confidence} is below the {threshold:g} "
            f"threshold")

    if require_stable:
        stable = decision.guard_true("state_is_stable")
        if stable is None:
            return GateResult(BLOCKED, UNUSABLE,
                              "the stability guard returned nothing, which is "
                              "not the same as passing")
        if stable < 0.5:
            return GateResult(
                BLOCKED, UNSTABLE,
                f"the application is still in flight (P(stable)={stable}); "
                f"acting now would act on a transient state")

    rung = None
    if ladder is not None and action_class is not None:
        rung = ladder.get(action_class)
    if rung is None:
        return GateResult(PASS, None,
                          f"confidence {decision.confidence} at or above "
                          f"{threshold:g}")
    if decision.confidence >= rung.act:
        return GateResult(
            PASS, None,
            f"confidence {decision.confidence} clears the {rung.act:g} act "
            f"rung for {action_class}")
    if decision.confidence >= rung.confirm:
        return GateResult(
            REVIEW, LOW_CONFIDENCE,
            f"confidence {decision.confidence} is below the {rung.act:g} act "
            f"rung for {action_class} but at its {rung.confirm:g} "
            f"confirmation rung: a confirmation step is required before this "
            f"action is taken")
    return GateResult(
        BLOCKED, LOW_CONFIDENCE,
        f"confidence {decision.confidence} is below the {rung.act:g} act rung "
        f"for {action_class} and below its {rung.confirm:g} confirmation "
        f"rung")


class GateResult(Verdict):
    """One gate's verdict over the state or the decision.

    Carries a machine-readable reason code beside the verdict, so a caller
    can tell a shortfall from a disagreement and act accordingly. The
    ``passed`` and ``confirmable`` answers are the shared shape's
    (:class:`driver_core.verdicts.Verdict`): a block here is never
    confirmable, and the stability and usability gates answer properties of
    the situation that no confirmation changes.
    """

    __slots__ = ("reason",)

    def __init__(self, verdict, reason, detail):
        super().__init__(verdict, detail)
        self.reason = reason

    def to_dict(self):
        return {"verdict": self.verdict, "reason": self.reason,
                "detail": self.detail}

    def __bool__(self):
        return self.passed

    def __repr__(self):
        return f"GateResult({self.verdict!r}, {self.reason!r})"
