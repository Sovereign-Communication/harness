"""The decision tier: Jev, through one client, fail-closed.

This is the module that answers "what should happen next", and it is
deliberately the *weakest* component in the pipeline. It gets a verified
state and a closed vocabulary, and it returns the name of one action plus a
calibrated confidence. That is the whole contract. It does not write
anything, execute anything, or widen its own options.

Three properties carry most of the weight here.

**All-or-nothing signals.** Every declared question either has a validated
answer or has ``None``. There is no partial promotion, because a driver that
acted on two good signals and ignored a third bad one is how a confident wrong
answer gets executed. A malformed, partial, unkeyed or failed call yields an
envelope with every signal ``None`` and ``native=False``.

**The model recommends, the code decides.** ``recommended_action`` is only
ever a name that already exists in the vocabulary. A response naming
something undeclared is not "close enough" -- it is discarded, because an
action the code did not declare is one it cannot bound, consent to, or log.

**One reservation, one dispatch, one settlement, one audit record.** Every
path through this module, including the failure paths, costs exactly one
pre-flight reservation and writes exactly one metadata-only decision record.
The four legs are one owner -- :meth:`driver_core.budget.Budget.exchange` --
shared with every other billed call in the package, so the discipline cannot
drift between tiers; this module's own part is the record and its shape. The
record carries *metadata* -- the chosen action, the distribution, the
confidence -- and never the state that produced it, so the audit log stays
small enough to read and cannot become a second copy of sensitive screen
content.
"""
from . import guardrails, transport
from .audit import KIND_DECISION
from .budget import UNAVAILABLE as USAGE_UNAVAILABLE
from .budget import estimate_call_cost
from .config import JEV_INPUT_PRICE_PER_MILLION
from .errors import VocabularyError

SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"

#: Envelope states. ``unavailable`` covers every reason the model produced no
#: usable answer, and keeps the reason string so the two -- a refused budget
#: and a malformed body -- stay distinguishable in the record.
#:
#: The string values coincide with the budget's usage-source vocabulary, but
#: they mean different things: one is "the model gave no usable answer", the
#: other is "the provider reported no token counts". They are named
#: separately on purpose so the two can never be conflated by an import.
NATIVE = "native"
UNKEYED = "unkeyed"
UNAVAILABLE = "unavailable"
MALFORMED = "malformed"

#: A score/choice answer may land slightly outside its declared range when a
#: provider rounds. Beyond this it is a different answer, not a rounding.
RANGE_TOLERANCE = 1e-6

#: The narrowest grid this package will infer from a distribution's own
#: entries, in decimal places. A block of probabilities emitted at full
#: precision carries no quantisation to infer, and inferring one from float
#: noise would be a grid that exists nowhere but in this file. One decimal
#: place is the coarsest grid this will credit: coarser than that and the
#: values no longer describe a probability at all.
COARSEST_INFERRED_DECIMAL_PLACE = 1

#: Float noise, in absolute terms. Two hundred and fifty-five entries of at
#: most 1.0 cannot accumulate more than a few parts in 10^-14 of error from
#: double arithmetic, so this is a wide margin -- and still far below any
#: quantisation a provider could actually emit.
FLOAT_NOISE_TOLERANCE = 1e-9

#: A ceiling on the deviation this package will tolerate whatever grid it
#: inferred. Beyond one part in ten, a "distribution" no longer describes the
#: probability of one outcome; it describes a different question. Declared so
#: the rule holds even if a future option set is emitted on a very coarse
#: grid.
MAX_ACCEPTED_DEVIATION = 0.1


class Decision:
    """One decision envelope. Signals are all present or all ``None``."""

    __slots__ = ("status", "recommended_action", "confidence",
                 "probabilities", "guards", "expected_guards", "reasons",
                 "model", "native", "cost", "input_tokens", "output_tokens",
                 "usage_source", "stop_reason", "probability_deviation")

    def __init__(self, status, *, recommended_action=None, confidence=None,
                 probabilities=None, guards=None, expected_guards=None,
                 reasons=(), model=None, native=False, cost=0.0,
                 input_tokens=0, output_tokens=0,
                 usage_source=USAGE_UNAVAILABLE, stop_reason="",
                 probability_deviation=0.0):
        self.status = status
        self.recommended_action = recommended_action
        self.confidence = confidence
        self.probabilities = probabilities or {}
        self.guards = guards if guards is not None else {}
        # Which guards this decision was *supposed* to carry. Checking only
        # the guards that happen to be present would let a truncated envelope
        # -- one missing a guard entirely -- read as complete, which is the
        # exact shape of "a driver that ignored the check it did not have".
        self.expected_guards = tuple(expected_guards or ())
        self.reasons = list(reasons)
        self.model = model
        self.native = native
        self.cost = cost
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.usage_source = usage_source
        self.stop_reason = stop_reason
        #: How far the returned distribution landed from 1. Recorded rather
        #: than silently absorbed: a decision acted on after a tolerated
        #: deviation should still say so in the log. Zero when it was exact.
        self.probability_deviation = float(probability_deviation or 0.0)

    @property
    def missing_guards(self):
        return tuple(name for name in self.expected_guards
                     if self.guards.get(name) is None)

    @property
    def usable(self):
        """The only sanctioned signal that a recommendation may be acted on.

        Requires native, a named action, a real confidence, and *every*
        declared guard answered. A decision missing one guard is not a
        decision with one fewer guard; it is not a decision.
        """
        return (self.native
                and self.recommended_action is not None
                and self.confidence is not None
                and not self.missing_guards)

    def guard_true(self, name):
        """A guard's value, or ``None``. Never coerces absence to False."""
        return self.guards.get(name)

    def to_dict(self):
        return {
            "status": self.status,
            "recommended_action": self.recommended_action,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
            "guards": self.guards,
            "reasons": self.reasons,
            "model": self.model,
            "native": self.native,
            "cost": round(self.cost, 9),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_source": self.usage_source,
            "stop_reason": self.stop_reason,
            "probability_deviation": round(self.probability_deviation, 9),
            "usable": self.usable,
        }

    def audit_fields(self):
        """The metadata-only projection written to the audit log.

        Deliberately excludes ``reasons`` and anything state-shaped. A
        decision record answers "what did it choose and how sure was it",
        which is what an operator needs, without turning the audit log into a
        transcript of whatever was on screen.
        """
        return {
            "status": self.status,
            "recommended_action": self.recommended_action,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
            "guards": self.guards,
            "model": self.model,
            "native": self.native,
            "cost_usd": round(self.cost, 9),
            "usage_source": self.usage_source,
            "probability_deviation": round(self.probability_deviation, 9),
        }

    def __repr__(self):
        return (f"Decision({self.status!r}, action="
                f"{self.recommended_action!r}, conf={self.confidence})")


def offered_actions(vocabulary, performable=None):
    """The declared actions that may actually be offered to the model.

    Two sets are in play and only one is safe to show. The vocabulary declares
    what the system can *name*; the registry holds what this build can *do*.
    Offering the vocabulary alone is how a real model came to choose read_dom
    at 0.98 confidence and run_probe at 1.00, only for the step to end in a
    refusal naming an executor that nothing registered: three of the fourteen
    declared names have no implementation in any build, at any
    ``DRIVER_ALLOW_WRITE``.

    ``performable=None`` means "do not narrow", which is what a caller holding
    no registry gets, so nothing that exists today changes. The declaration is
    untouched either way -- this filters what is *offered*, and a host that
    backs one of the three through ``Driver.register_executor`` puts it
    straight back on the menu, which is the extension point the refusal
    message already promises.
    """
    names = vocabulary.names()
    if performable is None:
        return names
    allowed = set(performable)
    offered = tuple(name for name in names if name in allowed)
    if not offered:
        # A registry with nothing in it cannot offer a choice, and an empty
        # option set is not a question. Refused by name rather than left to
        # surface as a bare ValueError from the question builder.
        raise VocabularyError(
            f"no declared action is performable; the registry named "
            f"{sorted(allowed)}, none of which is in {list(names)}")
    return offered


def build_questions(state, vocabulary, *, include_guards=True,
                    performable=None, stability=None):
    """The declared question set for one decision.

    The action question's options are generated from the declared vocabulary
    *and* narrowed to what this build can perform, so the model chooses among
    actions that exist and that something here can actually carry out. Guards
    are nouls on properties of the state rather than on the proposed action,
    because whether a state is trustworthy is a fact, not an opinion.
    """
    options = {name: action.description for name, action in
               ((n, vocabulary.resolve(n))
                for n in offered_actions(vocabulary, performable))}
    questions = {
        "action": transport.choice(
            "Which single declared action best advances the goal, given the "
            "current verified state? Choose 'no_action' if the correct "
            "response is to change nothing.",
            options),
    }
    if include_guards:
        # The stability guard is asked of the model only when no
        # frame-history judgment exists. When one does, compared captures
        # answer it (see FrameHistory): a static screen is not re-judged and
        # re-billed every step. The guard's name, threshold and fail-closed
        # missing-guard rule are unchanged -- only its source moves, from a
        # single-frame guess to observed history.
        if stability is None:
            questions["state_is_stable"] = transport.noul(
            "Is the state quiescent -- no progress indicator, spinner, or "
            "in-flight animation that would mean acting now would act on a "
            "transient state?",
            true="The application is idle and its state will not change on "
                 "its own in the next moment.",
            false="Something is still in flight; acting now would act on a "
                  "state that is about to change.")
        questions["a_blocking_choice_is_required"] = transport.noul(
            "Does the current state require the operator to choose between "
            "options before any progress can be made?",
            true="A modal, picker, or confirmation is blocking progress and "
                 "the next step must resolve it.",
            false="No blocking choice is pending.")
    return questions


def decimal_places(value):
    """How many decimal places a number appears to have been emitted with."""
    try:
        text = repr(float(value))
    except (TypeError, ValueError):
        return COARSEST_INFERRED_DECIMAL_PLACE
    if "e" in text or "E" in text:
        return 18
    _whole, _, fraction = text.partition(".")
    return len(fraction.rstrip("0"))


def inferred_grid(probabilities):
    """The last decimal place the endpoint emitted at, as a power of ten.

    Read off the values themselves rather than declared, because the grid is
    a property of what the endpoint *did* on this call. Floored at
    :data:`COARSEST_INFERRED_DECIMAL_PLACE` so a set of whole numbers cannot
    imply that the units place is a hundredth.
    """
    places = COARSEST_INFERRED_DECIMAL_PLACE
    for value in (probabilities or {}).values():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        places = max(places, decimal_places(number))
    return places


def accepted_deviation(probabilities):
    """The largest distance from 1 this distribution may land and still count.

    Derived, not chosen. The endpoint documents that the distribution sums to
    1, and every worked example it publishes is quantised to two decimal
    places. At eleven options those two documented facts stop being
    simultaneously satisfiable: a live run measured nine of twenty, then
    seven of sixteen, distributions landing exactly 0.01 short -- every entry
    on a hundredth, with the residue simply lost.

    A fixed ``1e-3`` bound therefore refused roughly half of all live
    eleven-option calls, and a refused distribution makes the whole envelope
    unusable, so the effect was a step that acted on nothing while holding a
    perfectly good ``no_action`` at 0.79 confidence. The confidence that
    actually decides whether an action runs is the chosen option's own entry,
    and the sum plays no part in it -- so the check was buying no safety at
    that price.

    The rule is therefore the provider's own precision: each entry may lose
    half a unit in the last decimal place the endpoint emitted, and no more.
    A distribution emitted at full precision gets essentially no tolerance,
    because full precision means the sum is meant to be exactly 1; a
    distribution on a hundredth gets enough to cover the rounding it
    demonstrably performs.
    """
    probabilities = probabilities or {}
    unit = 10.0 ** -inferred_grid(probabilities)
    return min(max(FLOAT_NOISE_TOLERANCE, len(probabilities) * unit / 2.0),
               MAX_ACCEPTED_DEVIATION)


def probability_deviation(probabilities):
    """How far a distribution lands from 1, or ``None`` if it is not numeric."""
    try:
        return sum(float(value) for value in (probabilities or {}).values()) - 1.0
    except (TypeError, ValueError):
        return None


def validate_answers(answers, questions, vocabulary):
    """Read a response through the declared answer shapes.

    Returns ``(action, confidence, probabilities, guards, reasons)``. Any
    problem is a *reason*, and the caller turns a non-empty reason list into
    a non-native envelope. Nothing partial is returned.
    """
    reasons = []
    if not isinstance(answers, dict):
        return None, None, {}, {}, ["answers were not an object"]

    expected = set(questions)
    got = set(answers)
    missing = sorted(expected - got)
    if missing:
        reasons.append(f"missing answer(s) {missing}")
    extra = sorted(got - expected)
    if extra:
        reasons.append(f"undeclared answer(s) {extra}")
    if reasons:
        return None, None, {}, {}, reasons

    guards = {}
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            reasons.append(f"answer {name!r} does not match its declared type")
    if reasons:
        return None, None, {}, {}, reasons

    action_answer = answers["action"]
    chosen = action_answer.get("choice")
    probabilities = action_answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        reasons.append("choice answer carried no probability distribution")
        return None, None, {}, {}, reasons
    try:
        probabilities = {str(k): float(v) for k, v in probabilities.items()}
    except (TypeError, ValueError):
        reasons.append("probability distribution was not numeric")
        return None, None, {}, {}, reasons
    total = sum(probabilities.values())
    tolerance = accepted_deviation(probabilities)
    if abs(total - 1.0) > tolerance:
        reasons.append(
            f"probabilities sum to {total:.4f}, not 1: off by "
            f"{total - 1.0:+.4f}, more than the {tolerance:.6g} that the "
            f"precision of this distribution allows")

    try:
        vocabulary.resolve(chosen)
    except Exception:
        reasons.append(
            f"model named {chosen!r}, which is not a declared action")
        return None, None, {}, {}, reasons

    # The distribution must cover the option that was actually returned. A
    # response whose `choice` has no probability of its own is internally
    # inconsistent, and its confidence cannot be trusted.
    if chosen not in probabilities:
        reasons.append(f"chosen option {chosen!r} is absent from the "
                       f"distribution")
        return None, None, {}, {}, reasons

    confidence = action_answer.get("confidence")
    if confidence is None:
        confidence = probabilities[chosen]
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        reasons.append("confidence was not numeric")
        return None, None, {}, {}, reasons
    if not 0.0 - RANGE_TOLERANCE <= confidence <= 1.0 + RANGE_TOLERANCE:
        reasons.append(f"confidence {confidence} is outside [0, 1]")
        return None, None, {}, {}, reasons
    confidence = min(1.0, max(0.0, confidence))

    for name, question in questions.items():
        if question["type"] != "noul":
            continue
        value = answers[name].get("noul")
        try:
            value = float(value)
        except (TypeError, ValueError):
            reasons.append(f"guard {name!r} was not numeric")
            continue
        if not 0.0 - RANGE_TOLERANCE <= value <= 1.0 + RANGE_TOLERANCE:
            reasons.append(f"guard {name!r} returned {value}, outside [0, 1]")
            continue
        guards[name] = min(1.0, max(0.0, value))

    if reasons:
        return None, None, {}, {}, reasons
    return chosen, confidence, probabilities, guards, []


class JevClient:
    """The one decision client.

    Takes its budget, audit log and settings by injection rather than
    building them, so it can be exercised with fakes and so no caller ends up
    with two budgets or two ledgers.
    """

    def __init__(self, settings, *, budget, audit, transport_module=None,
                 url=SYSTEM_ONE_URL):
        self.settings = settings
        self.budget = budget
        self.audit = audit
        self._transport = transport_module or transport
        self.url = url

    def estimate_tokens(self, state, questions):
        """A measured-size upper bound for the pre-flight reservation.

        Serialised bytes over four is the same crude, safe approximation used
        across this family of projects, and it errs high on purpose: a
        reservation that is too large costs nothing but a refusal, while one
        that is too small silently under-bills.
        """
        import json
        material = len(json.dumps(state, default=str).encode("utf-8"))
        material += len(json.dumps(questions, default=str).encode("utf-8"))
        return max(1, material // 4 + 1)

    def decide(self, state, vocabulary, *, receipt=None, step_id="step",
               performable=None, stability=None):
        """Recommend one declared action for a verified state.

        ``performable`` narrows the actions the model is *offered* to those
        this build can actually carry out. Left ``None`` the full declared
        vocabulary is offered, which is the behaviour every existing caller
        had; the driver passes its own registry names so a model is never
        invited to choose an action nothing here implements.

        ``stability`` is the frame-history judgment, when this build has
        one. It *replaces* the model's single-frame stability question
        rather than supplementing it: the guard is fed the judgment, and the
        question -- and its bill -- leave this call. ``None`` leaves the
        question in place, which is the behaviour every existing caller had.

        Every exit path -- keyed, unkeyed, budget-refused, transport-failed,
        malformed -- settles exactly one reservation and writes exactly one
        audit record, so the log has no silent holes.
        """
        questions = build_questions(state, vocabulary,
                                   performable=performable,
                                   stability=stability)
        estimate = self.estimate_tokens(state, questions)
        reserve_usd = estimate_call_cost(
            estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        if not self.settings.keyed:
            decision = Decision(UNKEYED, stop_reason="no decision key configured")
            return self._record(decision, step_id, receipt)

        def dispatch():
            return self._transport.call_service(
                self.url, questions,
                headers={"Authorization": f"Bearer {self.settings.jev_api_key}"},
                timeout=60,
                body_extra={"state": state, "model": self.settings.jev_model})

        def report(one):
            """Shape one decision out of one exchange, and record it once."""
            if one.stage == "reserve":
                decision = Decision(
                    UNAVAILABLE, stop_reason=f"budget refused: {one.error}")
                return self._record(decision, step_id, receipt)
            if one.error is not None:
                decision = Decision(
                    UNAVAILABLE, cost=one.charged,
                    stop_reason=f"transport raised: {one.error}")
                return self._record(decision, step_id, receipt)

            response = one.response
            if not response.ok:
                decision = Decision(
                    UNAVAILABLE, cost=one.charged,
                    stop_reason=f"{response.outcome}: "
                                f"{response.detail}")
                return self._record(decision, step_id, receipt)

            payload = response.payload or {}
            usage = response.usage() or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            charged, usage_source = one.charged, one.source

            action, confidence, probabilities, guards, reasons = \
                validate_answers(payload.get("answers"), questions, vocabulary)
            if reasons:
                decision = Decision(
                    MALFORMED, cost=charged, input_tokens=input_tokens,
                    output_tokens=output_tokens, usage_source=usage_source,
                    model=payload.get("model"), reasons=reasons,
                    stop_reason="response did not satisfy the declared contract")
                return self._record(decision, step_id, receipt)

            if stability is not None:
                # Fed, not asked. The guard keeps its name and its threshold;
                # its value is now the frame history's judgment rather than a
                # single-frame guess the model re-answers (and bills) every
                # step. It lands in ``guards`` -- exactly what the decision
                # record carries -- so the judgment is recorded where the
                # decision is.
                guards["state_is_stable"] = min(1.0, max(0.0, float(stability.p)))

            decision = Decision(
                NATIVE, recommended_action=action, confidence=confidence,
                probabilities=probabilities, guards=guards,
                expected_guards=tuple(n for n, q in questions.items()
                                      if q["type"] == "noul"),
                model=payload.get("model"), native=True, cost=charged,
                input_tokens=input_tokens, output_tokens=output_tokens,
                usage_source=usage_source,
                # How far the returned distribution landed from 1, so a decision
                # acted on after a tolerated deviation still says so in the log.
                probability_deviation=probability_deviation(probabilities) or 0.0)
            return self._record(decision, step_id, receipt)

        return self.budget.exchange(
            label=step_id, reserve_usd=reserve_usd, dispatch=dispatch,
            report=report, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

    def guard(self, action_name, state, *, step_id="step"):
        """One guardrail judgment of a proposed action against a state.

        The guardrails pack -- hazard Nouls plus a severity Score -- asked
        once about the action the decision tier just named, with the same
        verified state the decision saw, so the questions judge the proposed
        action against the observed screen. The verdict itself is computed
        in :mod:`driver_core.guardrails`; this method only moves the
        question and keeps the books.

        Fails closed at every exit: a call that produced no judgment --
        unkeyed, refused by the budget, failed on the wire -- is a ``block``,
        never a pass, because a guard that returned nothing is not a guard
        that passed. Every exit settles exactly one reservation and writes
        exactly one audit record (``mode: "guardrail"``), the same discipline
        as :meth:`decide`.
        """
        questions = guardrails.guardrail_questions(action_name)
        estimate = self.estimate_tokens(state, questions)
        reserve_usd = estimate_call_cost(
            estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        if not self.settings.keyed:
            return self._record_guard(
                guardrails.GuardrailVerdict(
                    guardrails.BLOCK,
                    "no decision key configured: the guardrail pack could "
                    "not judge this action, and an unjudged action does not "
                    "act"),
                action_name, step_id)

        def dispatch():
            return self._transport.call_service(
                self.url, questions,
                headers={"Authorization":
                         f"Bearer {self.settings.jev_api_key}"},
                timeout=60,
                body_extra={"state": state, "model": self.settings.jev_model})

        def report(one):
            """Shape one guardrail verdict out of one exchange, record it once."""
            if one.stage == "reserve":
                verdict = guardrails.GuardrailVerdict(
                    guardrails.BLOCK, f"budget refused: {one.error}")
            elif one.error is not None:
                verdict = guardrails.GuardrailVerdict(
                    guardrails.BLOCK, f"transport raised: {one.error}",
                    cost=one.charged)
            elif not one.response.ok:
                verdict = guardrails.GuardrailVerdict(
                    guardrails.BLOCK,
                    f"{one.response.outcome}: {one.response.detail}",
                    cost=one.charged, usage_source=one.source)
            else:
                payload = one.response.payload or {}
                verdict = guardrails.verdict_from(
                    payload.get("answers")).billed(
                        cost=one.charged, usage_source=one.source,
                        model=payload.get("model"))
            return self._record_guard(verdict, action_name, step_id)

        return self.budget.exchange(
            label=step_id, reserve_usd=reserve_usd, dispatch=dispatch,
            report=report, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

    def _record(self, decision, step_id, receipt):
        fields = decision.audit_fields()
        fields["step_id"] = step_id
        if receipt is not None:
            fields["extraction"] = receipt
        self.audit.append(KIND_DECISION, **fields)
        return decision

    def _record_guard(self, verdict, action_name, step_id):
        fields = verdict.audit_fields()
        fields["mode"] = "guardrail"
        fields["action"] = action_name
        fields["step_id"] = step_id
        self.audit.append(KIND_DECISION, **fields)
        return verdict
