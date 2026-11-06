"""Asking a model well: how much context it needs, and what drove its answer.

A confidence figure says nothing about whether the model *had what it needed*.
A model answering from a one-line summary and a model answering from the code,
its tests and a live receipt both return a confident number, and only one of
them is worth acting on. And a wrong answer is unfixable without knowing which
dimension produced it -- "the model was 0.84 confident" is not a fact anybody
can iterate on.

So this module declares, as data, the kinds of context a caller can supply and
the dimensions a verdict can rest on, then runs one loop:

    ask -> read how much more context is needed -> supply the kind of context
    the model named -> ask again, until nothing further is needed

The endpoint has **no explanation channel**: a response carries ``answers``,
``model`` and ``usage`` and nothing else, which was verified by dumping a raw
reply. Both the driving dimension and the outstanding need therefore have to be
*asked for* as declared questions -- the same shape this project already uses
elsewhere, where an operator declares a bucket pack and the model picks the
bucket.

Three closed sets:

* :data:`CONTEXT_BUCKETS` -- the *kinds* of context that exist. A loop cannot
  act on "give it more context"; it can act on "the model asked for the tests".
  ``none`` is a member because "nothing further is needed" is a real answer and
  has to be nameable, or the model must invent a name for it.
* :data:`DRIVER_BUCKETS` -- the dimensions a verdict can rest on. Declared
  rather than free text is what makes "what is driving this?" answerable as a
  distribution instead of a paragraph.
* :data:`NEED_LEVELS` -- the scale the outstanding need is scored on.

The stopping rule is the useful part. Confidence alone is not enough to stop
on, because a model can be confident and under-informed at once, and that
combination is what produces a wrong answer with a good number beside it. The
loop stops when the caller's gate is met **and** the model reports it needs no
further context. When the model keeps asking for something no supplier can
provide, the loop stops and says so: the caller gets ``ungrounded`` rather than
a quiet success.

Nothing here decides anything. It is a protocol for asking, and it returns what
it heard, including the trail of what was supplied and when.
"""
from . import transport
from .jev_client import SYSTEM_ONE_URL

#: The kinds of context a caller may be asked to supply.
CONTEXT_BUCKETS = (
    ("facts", "Concrete values this build holds exactly: names, paths, "
              "settings, counts, identities."),
    ("code", "The source text of the thing being judged."),
    ("contract", "The docstrings or declarations that state what it promises."),
    ("tests", "The text of the tests that constrain it."),
    ("receipts", "Live observed output from a real run: results, refusals, "
                 "costs."),
    ("counterexample", "A concrete input that would falsify the answer."),
    ("constraints", "The invariants the answer must not violate."),
    ("alternatives", "The other implementations this could be compared "
                     "against."),
    ("none", "No further context is needed; the answer is as grounded as the "
             "evidence supplied allows."),
)

#: The dimensions a verdict may be resting on. Kept distinct so a distribution
#: over them means something.
DRIVER_BUCKETS = (
    ("never_offer_impossible",
     "A model must never be offered a choice that cannot be taken."),
    ("keep_contract_intact",
     "The declared contract and its extension point must stay intact."),
    ("break_nothing",
     "An existing test, guarantee or declaration must not be weakened."),
    ("diagnosable",
     "A refusal or failure must be diagnosable and auditable."),
    ("smallest_change",
     "Prefer the smallest change that removes the fault."),
    ("fail_closed",
     "Prefer the choice that refuses rather than the one that proceeds."),
    ("fidelity",
     "Code must do what its own documentation says it does."),
    ("single_owner",
     "One fact must have exactly one owner."),
    ("declaration_only",
     "Something is declared that nothing implements, or promised that "
     "nothing performs."),
)

#: The scale the outstanding need is scored on. Ten levels, as the API allows.
NEED_LEVELS = (
    "0 nothing further", "1", "2", "3", "4", "5", "6", "7", "8",
    "9 a great deal more",
)

#: The three questions every grounded request carries, on top of the caller's.
CONTEXT_QUESTION_NAMES = ("context_needed", "context_bucket", "driving_bucket")


def context_questions():
    """The three protocol questions, as a fresh mapping.

    Returned rather than shared so a caller can extend the pack without
    mutating a module-level dict -- the same reason every other declared set in
    this package is a tuple.
    """
    return {
        "context_needed": transport.score(
            "How much additional context would you need before this answer "
            "should be relied on? Answer 0 only when the answer is as grounded "
            "as the evidence supplied allows.", NEED_LEVELS),
        "context_bucket": transport.choice(
            "Which single declared kind of context would most improve this "
            "answer?",
            {name: text for name, text in CONTEXT_BUCKETS}),
        "driving_bucket": transport.choice(
            "Which single declared dimension most drove your answer?",
            {name: text for name, text in DRIVER_BUCKETS}),
    }


def _score_value(answer):
    """The numeric score out of a ``score`` answer, or ``None``."""
    if not isinstance(answer, dict):
        return None
    try:
        return float(answer.get("score"))
    except (TypeError, ValueError):
        return None


def _confidence(answer):
    """A confidence for any of the three primitives, or ``None``.

    A ``noul`` reports a probability, so its confidence is the distance from
    the fence; a ``choice`` and a ``score`` report their own. Reading a noul of
    0.05 as "confidence 0.05" would treat a decisive *no* as a coin toss, which
    is the same mistake the decision gate already refuses to make.

    A ``choice`` with no confidence of its own falls back to the probability
    it assigned the chosen option, which is the same rule the shipping reader
    uses in :func:`driver_core.jev_client.validate_answers`. Two readers of one
    answer shape disagreeing about what its confidence is would mean a verdict
    could pass the decision gate and fail this one, or the reverse.
    """
    if not isinstance(answer, dict):
        return None
    if answer.get("type") == "noul":
        try:
            value = float(answer.get("noul"))
        except (TypeError, ValueError):
            return None
        if not 0.0 <= value <= 1.0:
            return None
        return max(value, 1.0 - value)
    try:
        return float(answer.get("confidence"))
    except (TypeError, ValueError):
        pass
    if answer.get("type") == "choice":
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, dict):
            try:
                return float(probabilities.get(answer.get("choice")))
            except (TypeError, ValueError):
                return None
    return None


#: Why a loop ended without settling. One boolean cannot carry these apart,
#: and they call for entirely different responses.
#:
#: * ``transport`` -- the call itself failed. Retry, or fix the connection.
#: * ``unanswerable`` -- the model named a kind of context that no supplier
#:   has. Go and obtain that material; nothing else will move the answer.
#: * ``no_new_evidence`` -- it asked again for a bucket that already holds
#:   exactly the content supplied, so there is nothing new to give it. The
#:   material is not missing; the evidence this supplier has is exhausted.
#:   Counted apart from ``unanswerable`` because the two look identical in a
#:   summary count and ask for opposite things -- one says *go and get it*,
#:   the other says *there is nothing to get*.
#: * ``still_short`` -- every bucket it asked for was supplied, and it then
#:   said it needed nothing further, yet a declared answer is below the gate.
#:   There is no missing material: the question does not have a confident
#:   answer from this evidence, which is a result and not a failure.
#: * ``rounds_exhausted`` -- it was still asking for context when the round
#:   budget ran out. Nothing is missing and nothing was refused; there were
#:   simply not enough rounds to reach a settled answer, and the verdicts
#:   below the gate stand.
UNSETTLED_TRANSPORT = "transport"
UNSETTLED_UNANSWERABLE = "unanswerable"
UNSETTLED_NO_NEW_EVIDENCE = "no_new_evidence"
UNSETTLED_STILL_SHORT = "still_short"
UNSETTLED_ROUNDS_EXHAUSTED = "rounds_exhausted"


class Grounding:
    """One answer, with the evidence of how far it is grounded.

    ``ungrounded`` is the field that matters, and ``reason_kind`` says which
    of the conditions above produced it. It is not a synonym for "the answer
    was low confidence" -- two loops can both end unsettled and want opposite
    things next. Each kind therefore names its own response: retry the call,
    go and obtain the missing material, accept that the question has no
    confident answer from this evidence, or spend more rounds on it.
    """

    __slots__ = ("answers", "confidences", "needed", "bucket", "drivers",
                 "rounds", "supplied", "ungrounded", "reason", "reason_kind",
                 "input_tokens", "cost", "model")

    def __init__(self, answers, confidences, needed, bucket, drivers, rounds,
                 supplied, ungrounded, reason, input_tokens, cost, model,
                 reason_kind=""):
        self.answers = answers
        self.confidences = confidences
        self.needed = needed
        self.bucket = bucket
        self.drivers = drivers
        self.rounds = rounds
        self.supplied = supplied
        self.ungrounded = ungrounded
        self.reason = reason
        self.reason_kind = reason_kind
        self.input_tokens = input_tokens
        self.cost = cost
        self.model = model

    def to_dict(self):
        return {
            "answers": self.answers,
            "confidences": self.confidences,
            "context_needed": self.needed,
            "context_bucket": self.bucket,
            "driving_bucket": self.drivers,
            "rounds": self.rounds,
            "supplied": list(self.supplied),
            "ungrounded": self.ungrounded,
            "reason": self.reason,
            "reason_kind": self.reason_kind,
            "input_tokens": self.input_tokens,
            "cost_usd": round(self.cost, 9),
            "model": self.model,
        }

    def __repr__(self):
        return (f"Grounding(rounds={self.rounds}, needed={self.needed}, "
                f"ungrounded={self.ungrounded})")


def ask_until_grounded(questions, *, state, settings, supplier=None,
                       gate=0.99, need_floor=3.0, max_rounds=24,
                       optional=(), transport_module=None, timeout=120,
                       url=None):
    """Ask, supply what was asked for, ask again, until nothing is needed.

    ``questions`` is the caller's own pack; the three protocol questions are
    added to it and read back out of the reply. ``supplier`` is called as
    ``supplier(bucket, round_number)`` and returns text to append under that
    bucket, or ``None`` when it has nothing for it -- which is how the loop
    learns it is ungrounded rather than merely unfinished.

    ``optional`` names questions whose answers are *recorded* but which do not
    *gate* the loop. A diagnostic question -- "which dimension would most
    improve with more context" -- is worth asking on every round and is not
    worth holding the loop open for: a model that is genuinely torn between
    two diagnostics would otherwise keep the loop running against a gate it
    was never meant to satisfy.

    Stops on the first round where every caller answer meets ``gate`` and the
    model scores its outstanding need at or below ``need_floor``. Runs to
    ``max_rounds`` otherwise, and never raises for a transport failure: a
    provider that is down is a condition, and the caller gets a Grounding whose
    ``reason`` says which one it was. The spend is measured on every round,
    including the ones that failed, because a call that returned nothing is
    possibly still a call that was billed.
    """
    from .budget import estimate_call_cost
    from .config import JEV_INPUT_PRICE_PER_MILLION

    transport_module = transport_module or transport
    url = url or SYSTEM_ONE_URL
    provided = dict(state or {})
    supplied = []
    answers, confidences = {}, {}
    needed, bucket, drivers, rounds = None, None, {}, 0
    ungrounded, reason, reason_kind = False, "", ""
    settled = False
    input_tokens, cost, model = 0, 0.0, None

    for round_number in range(1, max_rounds + 1):
        rounds = round_number
        pack = dict(questions)
        pack.update(context_questions())
        response = transport_module.call_service(
            url, pack,
            headers={"Authorization": f"Bearer {settings.jev_api_key}"},
            timeout=timeout,
            body_extra={"state": provided, "model": settings.jev_model})
        if not response.ok:
            reason = f"{response.outcome}: {response.detail}"
            ungrounded = True
            reason_kind = UNSETTLED_TRANSPORT
            break

        payload = response.payload or {}
        heard = payload.get("answers") or {}
        usage = response.usage() or {}
        tokens = int(usage.get("input_tokens") or 0)
        input_tokens += tokens
        if tokens:
            cost += estimate_call_cost(
                tokens, price_per_million=JEV_INPUT_PRICE_PER_MILLION)
        model = payload.get("model") or model

        for name in questions:
            answers[name] = heard.get(name)
            confidences[name] = _confidence(heard.get(name))
        drivers = dict((heard.get("driving_bucket") or {})
                       .get("probabilities") or {})
        needed = _score_value(heard.get("context_needed"))
        asked_for = (heard.get("context_bucket") or {}).get("choice")
        bucket = asked_for

        short = [name for name in questions
                 if name not in optional
                 and (confidences.get(name) or 0.0) < gate]
        settling = needed is None or needed <= need_floor
        if not short and settling and asked_for in (None, "none"):
            settled = True
            break
        if asked_for in (None, "none"):
            if not short:
                # Grounded as far as the protocol can measure, even though the
                # need score is still above the floor. Nothing left to supply.
                settled = True
                break
            reason = (f"the model asked for no further context, yet "
                      f"{sorted(short)} remain below the {gate} gate")
            ungrounded = True
            reason_kind = UNSETTLED_STILL_SHORT
            break

        text = supplier(asked_for, round_number) if supplier else None
        if not text:
            reason = (f"the model asked for {asked_for!r}, which no supplier "
                      f"can provide")
            ungrounded = True
            reason_kind = UNSETTLED_UNANSWERABLE
            break
        held = provided.setdefault("supplied_context", {})
        if held.get(asked_for) == text:
            # Already supplied, verbatim. Repeating it cannot change the
            # answer, and another identical round would only reproduce
            # the same number -- which is the spinning this loop exists
            # to avoid. A supplier with genuinely more to say for the same
            # bucket returns different text and is taken at its word.
            reason = (f"the model asked again for {asked_for!r}, but that "
                      f"bucket already holds exactly this content, so no "
                      f"new evidence is available")
            ungrounded = True
            reason_kind = UNSETTLED_NO_NEW_EVIDENCE
            break
        held[asked_for] = text
        supplied.append({"bucket": asked_for, "round": round_number})

    if not settled and not ungrounded:
        # The one exit with no branch of its own: the loop ran out of rounds
        # while still being asked for new context. Falling off the end of the
        # ``for`` used to return ``ungrounded=False`` -- a settled answer for
        # a question that never settled, counted by the report among the
        # grounded. A false success reads exactly like a true one, and a
        # summary count is the least likely place to notice it. Every other
        # exit records why it left; this net makes that true of every exit,
        # ``max_rounds=0`` included.
        reason = (f"the model was still asking for context when the round "
                  f"budget of {max_rounds} ran out, so its answers below the "
                  f"{gate} gate stand unimproved by anything further this "
                  f"loop can supply")
        ungrounded = True
        reason_kind = UNSETTLED_ROUNDS_EXHAUSTED

    return Grounding(answers, confidences, needed, bucket, drivers, rounds,
                     supplied, ungrounded, reason, input_tokens, cost, model,
                     reason_kind=reason_kind)


__all__ = ["CONTEXT_BUCKETS", "DRIVER_BUCKETS", "NEED_LEVELS",
           "CONTEXT_QUESTION_NAMES", "context_questions", "Grounding",
           "ask_until_grounded", "UNSETTLED_TRANSPORT",
           "UNSETTLED_UNANSWERABLE", "UNSETTLED_NO_NEW_EVIDENCE",
           "UNSETTLED_STILL_SHORT", "UNSETTLED_ROUNDS_EXHAUSTED"]
