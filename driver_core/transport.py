"""One transport, stdlib only.

Deliberately minimal and deliberately singular. Every outbound HTTP call in
driver-core -- extraction, decision, anything added later -- goes through
:func:`request`, so there is exactly one place that knows how a timeout, a
non-200 body, or an unparseable response is turned into something a caller
can branch on.

The transport never raises on a remote failure. A provider that is down, slow,
rate-limited or returning nonsense is a *condition*, and every caller's next
move depends on which one it was. Swallowing it into an exception would make
"the model refused" and "the network died" the same event, and the honest
answer to a decision is different in each case.
"""
import json
import urllib.error
import urllib.request

from .errors import PerceptionUnavailable

USER_AGENT = "driver-core/1.0"

#: Outcomes of one transport attempt.
OK = "ok"
HTTP_ERROR = "http_error"
TRANSPORT_ERROR = "transport_error"
MALFORMED = "malformed"

OUTCOMES = (OK, HTTP_ERROR, TRANSPORT_ERROR, MALFORMED)


class Response:
    """A transport result that never raises on remote failure."""

    __slots__ = ("status", "outcome", "payload", "detail", "raw")

    def __init__(self, status, outcome, payload=None, detail="", raw=""):
        self.status = status
        self.outcome = outcome
        self.payload = payload if isinstance(payload, dict) else None
        self.detail = detail
        self.raw = raw

    @property
    def ok(self):
        return self.outcome == OK

    def usage(self):
        """Reported token usage, or ``None`` when the provider said nothing.

        ``None`` is load-bearing. Callers must treat it as *unmeasurable* and
        charge their reservation, never as zero.
        """
        if not self.payload:
            return None
        usage = self.payload.get("usage")
        return usage if isinstance(usage, dict) else None

    def to_dict(self):
        return {"status": self.status, "outcome": self.outcome,
                "detail": self.detail}

    def __repr__(self):
        return f"Response({self.status}, {self.outcome!r})"


def request(url, *, method="POST", headers=None, body=None, timeout=60):
    """Perform one HTTP request. Never raises on remote failure."""
    data = None
    request_headers = {"User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=request_headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as handle:
            raw = handle.read().decode("utf-8", "replace")
            status = handle.status
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            pass
        return Response(exc.code, HTTP_ERROR,
                        detail=f"HTTP {exc.code}: {_first_line(raw)}", raw=raw)
    except urllib.error.URLError as exc:
        return Response(0, TRANSPORT_ERROR, detail=f"unreachable: {exc.reason}")
    except (TimeoutError, OSError) as exc:
        return Response(0, TRANSPORT_ERROR, detail=f"transport failure: {exc}")

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return Response(status, MALFORMED, detail=f"unparseable body: {exc}",
                        raw=raw)
    if not isinstance(payload, dict):
        return Response(status, MALFORMED,
                        detail="response body is not an object", raw=raw)
    return Response(status, OK, payload=payload, raw=raw)


def _first_line(text):
    line = (text or "").strip().splitlines()
    return line[0][:200] if line else ""


# ---- typed question builders -------------------------------------------
# The three primitives. Anything else is not a question this system asks, and
# a caller that wants a fourth thing is describing a different subsystem.

def noul(instructions, *, true=None, false=None):
    """A yes/no question. Returns P(yes); there is no generated text."""
    question = {"type": "noul", "instructions": instructions}
    criteria = {}
    if true is not None:
        criteria["true"] = true
    if false is not None:
        criteria["false"] = false
    if criteria:
        question["criteria"] = criteria
    return question


def choice(instructions, options):
    """A pick from a declared option set.

    The option set is declared by code, never by the model, and an option
    whose value is ``None`` is legal for "needs no extra description".
    """
    if not options:
        raise ValueError("a choice needs at least one option")
    if len(options) > 255:
        raise ValueError("a choice supports at most 255 options")
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {str(k): v for k, v in options.items()},
    }


def score(instructions, levels):
    """A rating across declared levels. Two to ten, as the API requires."""
    levels = list(levels)
    if not 2 <= len(levels) <= 10:
        raise ValueError("a score needs between 2 and 10 levels")
    return {"type": "score", "instructions": instructions,
            "criteria": [str(level) for level in levels]}


PRIMITIVES = ("noul", "choice", "score")


def validate_questions(questions):
    """Fail closed on a question set this system would not have built."""
    if not questions or not isinstance(questions, dict):
        raise ValueError("a request needs a non-empty questions map")
    for key, question in questions.items():
        if not isinstance(question, dict):
            raise ValueError(f"question {key!r} is not an object")
        kind = question.get("type")
        if kind not in PRIMITIVES:
            raise ValueError(
                f"question {key!r} has type {kind!r}; only {PRIMITIVES} are "
                f"used by this system")
        if not question.get("instructions"):
            raise ValueError(f"question {key!r} has no instructions")
    return questions


def call_service(url, questions, *, headers=None, timeout=60, body_extra=None):
    """POST a typed question set to a System One compatible endpoint."""
    validate_questions(questions)
    body = {"questions": questions}
    if body_extra:
        body.update(body_extra)
    return request(url, body=body, headers=headers, timeout=timeout)


def require(response, what="service"):
    """Turn a failed response into a named, catchable failure."""
    if not response.ok:
        raise PerceptionUnavailable(f"{what} {response.outcome}: {response.detail}")
    return response.payload
