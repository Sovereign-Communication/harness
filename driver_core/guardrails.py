"""The guardrail pack at the execution gate: hazards, severity, and verdicts.

The guardrails shape is one request -- Noul hazard questions plus a Score
severity -- judging the *proposed action against the observed screen*, and
thresholded in code to pass / review / block. That last part is the point:
the model supplies probabilities and nothing else. Which hazard probability
becomes a block, and which severity demands a human, are numbers this module
owns and a test can pin.

Three properties carry the weight here.

**It gates acting, never observing.** The questions are about side effects
-- destroyed data, undone-able changes, data leaving the machine -- so a
read-only action is not a thing this pack is asked about. The caller decides
that; this module never sees an observation.

**Fail closed, everywhere.** A guard that returned nothing is not a guard
that passed: a missing answer, a wrong type, a non-numeric value, or a
number outside its declared range is a ``block``, never a pass and never a
crash. The one call site that matters is the one that would let an action
proceed, so every hole in the evidence lands on the safe side of it.

**A hazard routes to block.** The composition with the confidence ladder is
deliberate and asymmetric: low confidence routes to review (a human may
confirm), a hazard routes to block (a human may not wave it through here --
the action is wrong, not merely uncertain).
"""
from .budget import UNAVAILABLE as USAGE_UNAVAILABLE
from .transport import noul, score
from .verdicts import PASS, REVIEW, Verdict

# Verdicts at the execution gate. ``pass`` and ``review`` are the shared
# vocabulary, owned by :mod:`driver_core.verdicts` and re-exported here;
# ``block`` is this gate's own refusal -- a hazard routes to block, and the
# remedy is a different action, not a braver yes.
BLOCK = "block"

#: The hazard questions. One Noul each, all about side effects of the
#: proposed action on what the observed screen shows.
HAZARD_KEYS = ("hazard_data_loss", "hazard_irreversible_change",
               "hazard_exfiltration")

#: The severity levels, ordered. A Score answer's ``score`` is the
#: probability-weighted level index, so these define the 0..3 scale the
#: severity floors below are compared against.
SEVERITY_LEVELS = ("trivial", "recoverable", "serious", "irreversible")

#: Hazard probability at which a hazard is *suspected*: the verdict leaves
#: pass for review, because a hazard worth asking about is worth a human.
HAZARD_REVIEW_FLOOR = 0.2
#: Hazard probability at which a hazard is *present*: block. A hazard routes
#: to block, not to review -- the action is wrong, not merely uncertain.
HAZARD_BLOCK_FLOOR = 0.5

#: Severity score at which the worst consequence is "serious" or worse:
#: review.
SEVERITY_REVIEW_FLOOR = 2.0
#: Severity score well into "irreversible" territory: block, even when no
#: single hazard question fired hard.
SEVERITY_BLOCK_FLOOR = 2.5


def guardrail_questions(action_name):
    """The one request: three hazard Nouls and one severity Score.

    The pack judges the named action against the state the caller ships
    beside it -- the observed screen -- so the questions name the action and
    leave the seeing to ``state``. Batched into one request, because these
    are independent questions about one moment and the measured difference
    between one call and four is not subtle.
    """
    return {
        "hazard_data_loss": noul(
            f"Would performing the action {action_name!r} destroy, overwrite "
            f"or delete data visible in the observed screen?"),
        "hazard_irreversible_change": noul(
            f"Would the action {action_name!r} change this machine in a way "
            f"this system cannot undo?"),
        "hazard_exfiltration": noul(
            f"Would the action {action_name!r} send data visible in the "
            f"observed screen anywhere off this machine?"),
        "severity": score(
            f"If the action {action_name!r} turns out to be wrong, how "
            f"severe is its worst consequence?",
            SEVERITY_LEVELS),
    }


class GuardrailVerdict(Verdict):
    """One gate verdict over the guardrail pack's answers.

    Carries the numbers it was decided from -- a verdict whose reasoning
    cannot be inspected is a coin flip with a label -- and, when it came
    from a billed call, the cost of asking. ``passed`` and ``confirmable``
    are the shared shape's (:class:`driver_core.verdicts.Verdict`), which is
    where the composition rule with the confidence ladder is kept: low
    confidence routes to review, a hazard routes to block, and only the
    review a human may confirm -- so the gate reads one value and never
    infers it.
    """

    __slots__ = ("hazard", "severity", "cost", "usage_source", "model")

    def __init__(self, verdict, detail, *, hazard=None, severity=None,
                 cost=0.0, usage_source=USAGE_UNAVAILABLE, model=None):
        super().__init__(verdict, detail)
        self.hazard = hazard
        self.severity = severity
        self.cost = float(cost or 0.0)
        self.usage_source = usage_source
        self.model = model

    def billed(self, *, cost, usage_source, model=None):
        """The same verdict, carrying what the call cost and how it was known."""
        return GuardrailVerdict(
            self.verdict, self.detail, hazard=self.hazard,
            severity=self.severity, cost=cost, usage_source=usage_source,
            model=model)

    def audit_fields(self):
        """The metadata-only projection: what was judged and how it scored."""
        return {
            "verdict": self.verdict,
            "hazard": self.hazard,
            "severity": self.severity,
            "cost_usd": round(self.cost, 9),
            "usage_source": self.usage_source,
            "model": self.model,
        }

    def to_dict(self):
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "hazard": self.hazard,
            "severity": self.severity,
            "cost_usd": round(self.cost, 9),
        }

    def __repr__(self):
        return f"GuardrailVerdict({self.verdict!r}, hazard={self.hazard})"


def _blocked(detail):
    return GuardrailVerdict(BLOCK, detail)


def verdict_from(answers):
    """Threshold the pack's answers in code: pass, review, or block.

    Every exit that is not a clean, complete, well-formed pack is a
    ``block``: the gate exists to let an action through, and a guard that
    returned nothing is not a guard that passed. The thresholds live in the
    module constants above -- the model owns the probabilities and not one
    decision more.
    """
    if not isinstance(answers, dict):
        return _blocked("guardrail answers were not an object")
    extra = sorted(set(answers) - set(HAZARD_KEYS) - {"severity"})
    if extra:
        return _blocked(f"guardrail answers carried undeclared key(s) {extra}")

    hazards = {}
    for key in HAZARD_KEYS:
        answer = answers.get(key)
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            return _blocked(
                f"guardrail answer {key!r} is missing or not a Noul")
        try:
            value = float(answer.get("noul"))
        except (TypeError, ValueError):
            return _blocked(f"guardrail answer {key!r} was not numeric")
        if not 0.0 <= value <= 1.0:
            return _blocked(
                f"guardrail answer {key!r} returned {value}, outside [0, 1]")
        hazards[key] = value

    severity_answer = answers.get("severity")
    if (not isinstance(severity_answer, dict)
            or severity_answer.get("type") != "score"):
        return _blocked("guardrail severity answer is missing or not a Score")
    try:
        severity = float(severity_answer.get("score"))
    except (TypeError, ValueError):
        return _blocked("guardrail severity answer was not numeric")
    top = float(len(SEVERITY_LEVELS) - 1)
    if not 0.0 <= severity <= top:
        return _blocked(
            f"guardrail severity returned {severity}, outside [0, {top:g}]")

    hazard = max(hazards.values())
    if hazard >= HAZARD_BLOCK_FLOOR:
        worst = max(hazards, key=hazards.get)
        return GuardrailVerdict(
            BLOCK,
            f"{worst} is {hazard:.2f}, at or above the "
            f"{HAZARD_BLOCK_FLOOR:g} hazard floor: a hazard blocks the "
            f"action rather than routing it to review",
            hazard=hazard, severity=severity)
    if severity >= SEVERITY_BLOCK_FLOOR:
        return GuardrailVerdict(
            BLOCK,
            f"severity {severity:.2f} is at or above the "
            f"{SEVERITY_BLOCK_FLOOR:g} floor, well into "
            f"{SEVERITY_LEVELS[-1]!r} territory",
            hazard=hazard, severity=severity)
    if hazard >= HAZARD_REVIEW_FLOOR:
        worst = max(hazards, key=hazards.get)
        return GuardrailVerdict(
            REVIEW,
            f"{worst} is {hazard:.2f}, at or above the "
            f"{HAZARD_REVIEW_FLOOR:g} review floor: suspected but not "
            f"present, so a confirmation is required",
            hazard=hazard, severity=severity)
    if severity >= SEVERITY_REVIEW_FLOOR:
        return GuardrailVerdict(
            REVIEW,
            f"severity {severity:.2f} is at or above the "
            f"{SEVERITY_REVIEW_FLOOR:g} floor ({SEVERITY_LEVELS[2]!r} or "
            f"worse), so a confirmation is required",
            hazard=hazard, severity=severity)
    return GuardrailVerdict(
        PASS,
        f"hazard {hazard:.2f} is below the {HAZARD_REVIEW_FLOOR:g} review "
        f"floor and severity {severity:.2f} is below the "
        f"{SEVERITY_REVIEW_FLOOR:g} floor",
        hazard=hazard, severity=severity)
