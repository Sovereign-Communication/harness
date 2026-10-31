"""The v1 assessment gate as code: five aspects plus the flexibility rule.

`docs/JEV_GATE_POLICY_V1.md` describes the lane's assessment gate in prose:
five aspects, relevance-first, and a flexibility rule where four aspects at
90 or above plus one at 85-89 auto-investigates rather than blocking. Issue
#325 found no code counterpart anywhere -- the policy was operator procedure,
so two lanes could read the same scores and reach different dispositions.

The hard part is that the prose is written in point scores while a live Jev
answer is a probability distribution over bands, and the 2026-10-28 probe
showed the mass spreads: a crisp point score never materialises. Rather than
invent one, this module declares the aggregation rule and applies thresholds
to *probabilities of bands*:

    an aspect is in band B when P(B) is a strict majority (> 0.5) of the
    declared distribution; no strict majority means the aspect is UNCERTAIN and is
    investigated, never scored.

That is the whole rule. It keeps the declared distribution in the verdict
(``probabilities``) so a reader can see what the judgement actually said, and
it never converts a Score answer's probabilities into a band: callers pass
the band distribution they were given, or nothing at all (IRRELEVANT).

Authority is deliberately narrow. This gate answers for **lane workflow**
dispositions. It is not the audit gate, not the phase-completion gate, and
not a `compose_decision_verdict` replacement: the canon is explicit that a v1
investigate-cleared disposition must never be presented as a production
composer ``proceed``. A composer verdict passed in is carried through
untouched, under its own key, and never feeds the decision here.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: The five aspects, in the policy's order. Declared once: a caller cannot
#: add a sixth aspect, and a missing one is a refusal rather than a smaller
#: denominator.
ASPECTS = (
    "completeness",
    "confidence",
    "correctness",
    "no_regression",
    "evidence_quality",
)

BAND_GREEN = ">=90"
BAND_FLEX = "85-89"
BAND_BELOW = "<85"
BANDS = (BAND_GREEN, BAND_FLEX, BAND_BELOW)

#: A band holds the aspect when strictly more than this share of the declared
#: mass is in it. A 50/50 tie is uncertain; band declaration order must not
#: turn a tied judgement into a green classification.
MAJORITY = 0.5

#: The policy's own numbers, named so a change to them is a visible edit.
GREEN_MIN = 90
FLEX_MIN = 85
STALLED_ROUNDS = 3

PASS = "PASS"
PASS_WITH_NOTE = "PASS_WITH_NOTE"
INVESTIGATE = "INVESTIGATE"
BLOCK = "BLOCK"
STOP = "STOP"

#: What this gate is allowed to decide. Stated in every verdict so a reader
#: cannot mistake it for the audit bar.
AUTHORITY = "lane-workflow assessment gate (docs/JEV_GATE_POLICY_V1.md)"
PRODUCTION_GATE = "unchanged: audit and phase-completion bars are absolute"


class AssessmentError(ValueError):
    """A judgement that cannot be read, so no disposition may be derived."""


def _checked_probabilities(probabilities: Any, aspect: str) -> Dict[str, float]:
    """Validate one band distribution, or refuse it.

    Refusal is the only safe behaviour for a malformed judgement: a gate that
    guesses at the missing mass is a gate that passes on evidence nobody
    supplied.
    """
    if not isinstance(probabilities, dict) or not probabilities:
        raise AssessmentError(
            f"aspect {aspect!r} has no probabilities; an aspect that was "
            f"not judged must be declared irrelevant with a reason instead")
    unknown = sorted(set(probabilities) - set(BANDS))
    if unknown:
        raise AssessmentError(
            f"aspect {aspect!r} declares undeclared band(s) {unknown}; "
            f"declared: {list(BANDS)}")
    missing = sorted(set(BANDS) - set(probabilities))
    if missing:
        raise AssessmentError(
            f"aspect {aspect!r} is missing band(s) {missing}; a distribution "
            f"that omits a band is not a distribution over the bands")
    checked = {}
    for band in BANDS:
        value = probabilities[band]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AssessmentError(
                f"aspect {aspect!r}: band {band!r} is not a number")
        if value < 0 or value > 1:
            raise AssessmentError(
                f"aspect {aspect!r}: band {band!r} is outside [0, 1]")
        checked[band] = float(value)
    total = sum(checked.values())
    if abs(total - 1.0) > 1e-6:
        raise AssessmentError(
            f"aspect {aspect!r}: band probabilities sum to {total!r}, not 1")
    return checked


@dataclass
class AspectJudgment:
    """One aspect's judgement: a distribution, or an irrelevant reason.

    ``investigation`` and ``risk`` are the v1 investigation's own findings.
    The gate does not perform the investigation -- it cannot read a diff --
    so it requires the finding to be recorded here before it will clear a
    flex or uncertain aspect. ``risk`` is a *named actual risk*; an empty
    string or null means the investigation found none, which is what the
    policy allows to clear an investigation.
    """

    name: str
    probabilities: Optional[Dict[str, float]] = None
    relevant: bool = True
    reason: Optional[str] = ""
    investigation: Optional[str] = ""
    risk: Optional[str] = ""

    def __post_init__(self):
        if self.name not in ASPECTS:
            raise AssessmentError(
                f"{self.name!r} is not one of the five aspects {list(ASPECTS)}")
        if not isinstance(self.relevant, bool):
            raise AssessmentError(
                f"aspect {self.name!r} relevant must be a boolean")
        for field_name in ("reason", "investigation", "risk"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise AssessmentError(
                    f"aspect {self.name!r} {field_name} must be text or null")
        if not self.relevant:
            if not (self.reason or "").strip():
                raise AssessmentError(
                    f"aspect {self.name!r} is declared irrelevant without a "
                    f"reason; relevance-first means the reason is required")
            self.probabilities = None
        else:
            self.probabilities = _checked_probabilities(
                self.probabilities, self.name)

    @property
    def band(self) -> str:
        """The majority band, ``UNCERTAIN``, or ``IRRELEVANT``."""
        if not self.relevant or self.probabilities is None:
            return "IRRELEVANT" if not self.relevant else "UNCERTAIN"
        for band in BANDS:
            if self.probabilities[band] > MAJORITY:
                return band
        return "UNCERTAIN"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aspect": self.name,
            "relevant": self.relevant,
            "reason": self.reason,
            "band": self.band,
            "probabilities": dict(self.probabilities or {}),
            "investigation": self.investigation,
            "risk": self.risk,
        }


@dataclass
class AssessmentVerdict:
    """The disposition, with the evidence it was derived from."""

    decision: str
    aspects: List[AspectJudgment]
    notes: List[str] = field(default_factory=list)
    investigate: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    stalled: bool = False
    composer: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        """True only for a pass. INVESTIGATE is not a pass."""
        return self.decision in (PASS, PASS_WITH_NOTE)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "ok": self.ok,
            "authority": AUTHORITY,
            "production_gate": PRODUCTION_GATE,
            "stalled": self.stalled,
            "notes": list(self.notes),
            "investigate": list(self.investigate),
            "risks": list(self.risks),
            "aspects": [aspect.to_dict() for aspect in self.aspects],
            # Carried, never merged: the caller must publish both, and the
            # policy forbids presenting a cleared v1 disposition as the
            # composer's own proceed.
            "composer": self.composer,
        }


def assess(judgments, *, stalled_rounds: int = 0,
           composer: Optional[Dict[str, Any]] = None) -> AssessmentVerdict:
    """Apply the v1 assessment gate to five aspect judgements.

    Every aspect must be judged -- scored or declared irrelevant with a
    reason -- because a missing aspect is a hole in the evidence, and the
    gate refuses to average over holes. The disposition ladder is the
    policy's, in order:

    1. three stalled rounds stop honestly, at any score;
    2. a named actual risk blocks, at any score;
    3. a majority ``<85`` blocks (below-bar: fix, then re-assess);
    4. a flex (85-89) or uncertain aspect investigates; it clears only with
       a recorded investigation and no named risk, and then passes with a
       note -- that is the flexibility and single-point rule;
    5. five green aspects pass.
    """
    if (isinstance(stalled_rounds, bool)
            or not isinstance(stalled_rounds, int)
            or stalled_rounds < 0):
        raise AssessmentError(
            "stalled_rounds must be a non-negative integer")
    by_name = {}
    for judgment in judgments or []:
        if not isinstance(judgment, AspectJudgment):
            raise AssessmentError(
                "judgements must be AspectJudgment instances; got "
                f"{type(judgment).__name__}")
        if judgment.name in by_name:
            raise AssessmentError(f"aspect {judgment.name!r} judged twice")
        by_name[judgment.name] = judgment
    missing = [name for name in ASPECTS if name not in by_name]
    if missing:
        raise AssessmentError(
            f"missing aspect judgement(s) {missing}; all five aspects must "
            f"be judged or declared irrelevant with a reason")
    aspects = [by_name[name] for name in ASPECTS]

    relevant = [aspect for aspect in aspects if aspect.relevant]
    if not relevant:
        raise AssessmentError(
            "all five aspects are irrelevant; there is no work product to "
            "assess, so there is no disposition to give")
    risks = [f"{aspect.name}: {aspect.risk}" for aspect in relevant
             if (aspect.risk or "").strip()]
    note = [f"{aspect.name} was declared irrelevant: {aspect.reason}"
            for aspect in aspects if not aspect.relevant]

    if stalled_rounds >= STALLED_ROUNDS:
        return AssessmentVerdict(
            decision=STOP, aspects=aspects, stalled=True, composer=composer,
            notes=note + [f"{stalled_rounds} stalled rounds with no progress; "
                          "the policy stops honestly and reports the scores"],
            risks=risks)

    if risks:
        return AssessmentVerdict(
            decision=BLOCK, aspects=aspects, risks=risks, notes=note,
            composer=composer)

    below = [aspect.name for aspect in relevant if aspect.band == BAND_BELOW]
    if below:
        return AssessmentVerdict(
            decision=BLOCK, aspects=aspects, composer=composer,
            notes=note + [f"below-bar aspect(s) {below}: fix and re-assess"])

    investigative = [aspect.name for aspect in relevant
                     if aspect.band in (BAND_FLEX, "UNCERTAIN")]
    unresolved = [aspect.name for aspect in relevant
                  if aspect.name in investigative
                  and not (aspect.investigation or "").strip()]
    if unresolved:
        return AssessmentVerdict(
            decision=INVESTIGATE, aspects=aspects, notes=note,
            investigate=unresolved, composer=composer)
    if investigative:
        # Cleared by investigation: the flexibility rule's "PASS with
        # documented note explaining the investigation".
        return AssessmentVerdict(
            decision=PASS_WITH_NOTE, aspects=aspects, composer=composer,
            notes=note + [
                f"{name} was investigated and no actual risk was found: "
                f"{by_name[name].investigation}" for name in investigative])
    return AssessmentVerdict(
        decision=PASS, aspects=aspects, notes=note, composer=composer)


def from_payload(payload: Dict[str, Any]) -> List[AspectJudgment]:
    """Build the five judgements from a CLI/JSON payload.

    The shape is deliberately flat and explicit -- one object per aspect --
    so a lane can paste the judgement it received without a translation
    layer, and so an unknown key is a refusal rather than a silently ignored
    field.
    """
    if not isinstance(payload, dict):
        raise AssessmentError("assessment payload must be an object")
    aspects = payload.get("aspects")
    if not isinstance(aspects, dict):
        raise AssessmentError(
            "assessment payload needs an 'aspects' object keyed by aspect")
    unknown = sorted(set(aspects) - set(ASPECTS))
    if unknown:
        raise AssessmentError(f"undeclared aspect(s) {unknown}")
    judgments = []
    for name in ASPECTS:
        if name not in aspects:
            raise AssessmentError(f"aspect {name!r} is missing from payload")
        entry = aspects[name]
        if not isinstance(entry, dict):
            raise AssessmentError(f"aspect {name!r} must be an object")
        allowed = {"probabilities", "relevant", "reason", "investigation",
                   "risk"}
        extra = sorted(set(entry) - allowed)
        if extra:
            raise AssessmentError(
                f"aspect {name!r} has unknown field(s) {extra}")
        judgments.append(AspectJudgment(
            name=name,
            probabilities=entry.get("probabilities"),
            relevant=entry.get("relevant", True),
            reason=entry.get("reason", ""),
            investigation=entry.get("investigation", ""),
            risk=entry.get("risk", ""),
        ))
    return judgments


def verdict_for_payload(payload: Dict[str, Any]) -> AssessmentVerdict:
    if not isinstance(payload, dict):
        raise AssessmentError("assessment payload must be an object")
    allowed = {"aspects", "stalled_rounds", "composer"}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise AssessmentError(f"unknown payload field(s) {extra}")
    stalled_rounds = payload.get("stalled_rounds", 0)
    if stalled_rounds is None:
        stalled_rounds = 0
    return assess(from_payload(payload), stalled_rounds=stalled_rounds,
                  composer=payload.get("composer"))


def run_cli(argv=None) -> int:
    """``harness assess --input FILE``: JSON in, verdict JSON out.

    Reads a file or stdin, prints the verdict, and exits 0 on a pass, 1 on
    anything else. The exit code is the gate: a script can branch on it
    without parsing prose, exactly like the rest of the CLI.
    """
    import argparse
    import json
    import sys

    from . import osal

    parser = argparse.ArgumentParser(
        prog="harness assess",
        description="Apply the v1 five-aspect assessment gate to a judgement")
    parser.add_argument("--input", default="-",
                        help="JSON file with the aspect judgements, or - for stdin")
    args = parser.parse_args(argv or [])
    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = osal.read_text(args.input)
        payload = json.loads(raw)
        verdict = verdict_for_payload(payload)
    except (AssessmentError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    return 0 if verdict.ok else 1
