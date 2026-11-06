"""The verdict shape both gates answer in.

Two gates judge different things -- :mod:`driver_core.policy` judges the
verified state and the decision, :mod:`driver_core.guardrails` judges a
proposed action against the observed screen -- but their verdicts are one
shape, and they must agree on the single question downstream code must
never re-infer: **may a human's confirmation satisfy this verdict?**

That rule lives here, once. A review is uncertain evidence, so a human is
asked; a block is code's own refusal, and the remedy is a different action
or a different moment -- never a braver yes. Each gate keeps its own value
for its own refusal (``"blocked"`` names the policy gate's, ``"block"`` the
guardrail pack's) because they are different vocabularies answering
different questions; ``pass`` and ``review`` are shared verbatim and
compared here, so no caller matches verdict strings and no second copy of
the rule can drift from the first.
"""

#: The verdict both gates share verbatim: the gate let it through.
PASS = "pass"

#: The verdict both gates share verbatim: not acted on, but the remedy is a
#: confirmation rather than a refusal. It never counts as a pass --
#: :attr:`Verdict.passed` is false for it -- so nothing downstream executes
#: on a review by accident; the caller decides what "confirm" means in its
#: own world.
REVIEW = "review"


class Verdict:
    """One gate's verdict: which verdict, and what it was decided from.

    A verdict whose reasoning cannot be inspected is a coin flip with a
    label, so ``detail`` is part of the shape. Each gate adds the evidence
    it was decided from -- a reason code, hazard probabilities -- beside it,
    but the two questions below are answered the same way at every gate.
    """

    __slots__ = ("verdict", "detail")

    def __init__(self, verdict, detail):
        self.verdict = verdict
        self.detail = detail

    @property
    def passed(self):
        return self.verdict == PASS

    @property
    def confirmable(self):
        """Whether a human's confirmation could satisfy this verdict.

        Only a review is. A review is uncertain evidence and the human is
        asked; a block is code's own refusal -- the action is wrong or the
        situation is, not merely uncertain -- so no confirmation waves it
        through. The comparison lives here so no caller decides
        confirmability by matching verdict strings.
        """
        return self.verdict == REVIEW
