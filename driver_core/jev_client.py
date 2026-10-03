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
The record carries *metadata* -- the chosen action, the distribution, the
confidence -- and never the state that produced it, so the audit log stays
small enough to read and cannot become a second copy of sensitive screen
content.
"""
from . import transport
from .audit import KIND_DECISION
from .budget import UNAVAILABLE as USAGE_UNAVAILABLE
from .budget import estimate_call_cost
from .config import JEV_INPUT_PRICE_PER_MILLION

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


class Decision:
    """One decision envelope. Signals are all present or all ``None``."""

    __slots__ = ("status", "recommended_action", "confidence",
                 "probabilities", "guards", "expected_guards", "reasons",
                 "model", "native", "cost", "input_tokens", "output_tokens",
                 "usage_source", "stop_reason")

    def __init__(self, status, *, recommended_action=None, confidence=None,
                 probabilities=None, guards=None, expected_guards=None,
                 reasons=(), model=None, native=False, cost=0.0,
                 input_tokens=0, output_tokens=0,
                 usage_source=USAGE_UNAVAILABLE, stop_reason=""):
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
        }

    def __repr__(self):
        return (f"Decision({self.status!r}, action="
                f"{self.recommended_action!r}, conf={self.confidence})")


def build_questions(state, vocabulary, *, include_guards=True):
    """The declared question set for one decision.

    The action question's options are generated *from the vocabulary*, so the
    model is choosing among declared actions and cannot name one that does not
    exist. Guards are nouls on properties of the state rather than on the
    proposed action, because whether a state is trustworthy is a fact, not a
    matter of opinion.
    """
    options = {name: action.description for name, action in
               ((n, vocabulary.resolve(n)) for n in vocabulary.names())}
    questions = {
        "action": transport.choice(
            "Which single declared action best advances the goal, given the "
            "current verified state? Choose 'no_action' if the correct "
            "response is to change nothing.",
            options),
    }
    if include_guards:
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
    if abs(total - 1.0) > 1e-3:
        reasons.append(f"probabilities sum to {total:.4f}, not 1")

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

    def decide(self, state, vocabulary, *, receipt=None, step_id="step"):
        """Recommend one declared action for a verified state.

        Every exit path -- keyed, unkeyed, budget-refused, transport-failed,
        malformed -- settles exactly one reservation and writes exactly one
        audit record, so the log has no silent holes.
        """
        questions = build_questions(state, vocabulary)
        estimate = self.estimate_tokens(state, questions)
        reserve_usd = estimate_call_cost(
            estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        if not self.settings.keyed:
            decision = Decision(UNKEYED, stop_reason="no decision key configured")
            return self._record(decision, step_id, receipt)

        try:
            reservation = self.budget.reserve(reserve_usd, label=step_id)
        except Exception as exc:
            decision = Decision(UNAVAILABLE, stop_reason=f"budget refused: {exc}")
            return self._record(decision, step_id, receipt)

        response = self._transport.call_service(
            self.url, questions,
            headers={"Authorization": f"Bearer {self.settings.jev_api_key}"},
            timeout=60,
            body_extra={"state": state, "model": self.settings.jev_model})

        if not response.ok:
            # Billed or not, unmeasurable either way: settle the full
            # reservation rather than reporting a cost we do not know.
            charged = reservation.settle(None)
            decision = Decision(UNAVAILABLE, cost=charged,
                                stop_reason=f"{response.outcome}: "
                                            f"{response.detail}")
            return self._record(decision, step_id, receipt)

        payload = response.payload or {}
        usage = response.usage() or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if usage and input_tokens:
            charged = reservation.settle(
                estimate_call_cost(input_tokens,
                                   price_per_million=JEV_INPUT_PRICE_PER_MILLION),
                source="actual")
            usage_source = "actual"
        elif usage:
            charged = reservation.settle(estimate_call_cost(
                estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION),
                source="estimated")
            usage_source = "estimated"
        else:
            charged = reservation.settle(None)
            usage_source = UNAVAILABLE

        action, confidence, probabilities, guards, reasons = validate_answers(
            payload.get("answers"), questions, vocabulary)
        if reasons:
            decision = Decision(
                MALFORMED, cost=charged, input_tokens=input_tokens,
                output_tokens=output_tokens, usage_source=usage_source,
                model=payload.get("model"), reasons=reasons,
                stop_reason="response did not satisfy the declared contract")
            return self._record(decision, step_id, receipt)

        decision = Decision(
            NATIVE, recommended_action=action, confidence=confidence,
            probabilities=probabilities, guards=guards,
            expected_guards=tuple(n for n, q in questions.items()
                                  if q["type"] == "noul"),
            model=payload.get("model"), native=True, cost=charged,
            input_tokens=input_tokens, output_tokens=output_tokens,
            usage_source=usage_source)
        return self._record(decision, step_id, receipt)

    def _record(self, decision, step_id, receipt):
        fields = decision.audit_fields()
        fields["step_id"] = step_id
        if receipt is not None:
            fields["extraction"] = receipt
        self.audit.append(KIND_DECISION, **fields)
        return decision
