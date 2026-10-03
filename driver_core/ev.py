"""Hermetic test doubles at the model seams.

Kept in the package rather than in ``tests/`` on purpose: the extractors and
the decision client take their transport and their model by injection, and a
double that lives outside the package cannot be used to exercise the shipped
call sites without importing from a test directory. This one is available to
any caller, and is what makes "the whole pipeline, no network" a property of
the system rather than an accident of how the tests are written.

Nothing here reaches the network. :class:`FakeJev` returns a decision
envelope directly rather than a response body, because the interesting
subjects in this project are the *gates*, and a gate cannot be exercised
through a JSON body any more clearly than it can through the envelope the
body would have produced.
"""
from .consensus import Vote
from .jev_client import NATIVE, UNAVAILABLE, Decision
from .transport import Response


DECLARED_GUARDS = ("state_is_stable", "a_blocking_choice_is_required")


def action_answer(action, *, confidence=0.9, guards=None):
    """A well-formed native decision naming ``action``."""
    guards = guards if guards is not None else {
        "state_is_stable": 0.95,
        "a_blocking_choice_is_required": 0.2,
    }
    return Decision(
        NATIVE, recommended_action=action, confidence=confidence,
        probabilities={action: confidence}, guards=dict(guards),
        expected_guards=DECLARED_GUARDS,
        model="fake-jev", native=True, cost=0.0001, input_tokens=100,
        output_tokens=10, usage_source="actual")


def unavailable(reason="no key"):
    return Decision(UNAVAILABLE, stop_reason=reason)


class FakeJev:
    """A decision client stand-in.

    Records every call, which is how the tests assert the negative cases:
    the real safety property of this system is that the decision tier is
    *not called* on an unverified state, and that is only observable by
    checking what the double was asked.
    """

    def __init__(self, envelope=None, *, decision_fn=None, cost=0.0):
        self.envelope = envelope
        self._decision_fn = decision_fn
        self.calls = []
        self.cost = cost

    def decide(self, state, vocabulary, *, receipt=None, step_id="step"):
        self.calls.append({"state": dict(state) if state else state,
                           "receipt": receipt, "step_id": step_id})
        if self._decision_fn is not None:
            return self._decision_fn(state, vocabulary, receipt=receipt,
                                     step_id=step_id)
        if self.envelope is None:
            return unavailable("fake has no envelope configured")
        return self.envelope

    @property
    def called(self):
        return bool(self.calls)


class FakeTransport:
    """A transport stand-in returning queued responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"url": url, "questions": questions,
                              "body": body_extra})
        if not self.responses:
            return Response(0, "transport_error", detail="fake queue empty")
        response = self.responses.pop(0)
        return response if isinstance(response, Response) else Response(
            200, "ok", payload=response)


def ok_response(answers, *, model="fake-jev", input_tokens=100,
                output_tokens=10):
    return Response(200, "ok",
                    payload={"model": model, "answers": answers,
                             "usage": {"input_tokens": input_tokens,
                                       "output_tokens": output_tokens}})


def no_usage_response(answers, *, model="fake-jev"):
    """A response that reports no usage at all.

    The interesting case for spend honesty: the caller must charge its
    reservation rather than recording zero.
    """
    return Response(200, "ok", payload={"model": model, "answers": answers})


def votes(*states, slot_prefix="s"):
    """Build one ok Vote per state."""
    return [Vote.ok(f"{slot_prefix}{i}", state)
            for i, state in enumerate(states)]
