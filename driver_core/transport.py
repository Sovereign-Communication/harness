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

#: The far end's image opt-in header. Images are off by default at the
#: endpoint, and a request that carries one without this header is rejected
#: outright -- so it is not a preference but the admission ticket. It is
#: attached only when a caller both opts in and actually sends assets: an
#: ordinary request stays on the plain text surface, which is what
#: "never sent by default" has to mean.
IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"


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


def box(instructions):
    """Where something is in an image: four edges as image fractions.

    The answer is the smallest box containing the target -- ``top``,
    ``bottom``, ``left``, ``right``, each a fraction of the image -- and it
    is *always* a box: there is no "not found" answer, so pair one with a
    :func:`noul` asking whether the target is in the image at all and
    ignore the box when that answer is low. Takes no ``criteria``; the
    target is named in the instructions ("the coffee mug in `photo`").
    Needs an image in the request and a model that supports boxes (Jev
    1.14).
    """
    return {"type": "box", "instructions": instructions}


PRIMITIVES = ("noul", "choice", "score", "box")


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
        if kind == "box" and "criteria" in question:
            # The endpoint's shape, fail-closed here: a box question with
            # criteria is not a question this system builds and not one the
            # far end accepts.
            raise ValueError(
                f"question {key!r} is a box, which takes no criteria; "
                f"name the target in its instructions instead")
    return questions


def _validate_assets(assets):
    """Fail closed on an assets map this system would not have built.

    The keys are *references*: short strings the caller writes into
    ``state``, ``instructions`` or ``criteria`` wherever the image belongs.
    The bytes live in the map and nowhere else, so a request carries each
    image once and every other mention of it is a word -- which is the
    whole reason the endpoint takes a map instead of letting callers
    inline base64 wherever they like.
    """
    if not isinstance(assets, dict) or not assets:
        raise ValueError(
            "assets must be a non-empty mapping of reference to asset")
    for ref, entry in assets.items():
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(
                f"asset reference {ref!r} must be a non-empty string")
        if not isinstance(entry, dict):
            raise ValueError(
                f"asset {ref!r} must be a mapping, got "
                f"{type(entry).__name__}")
    return assets


def call_service(url, questions, *, headers=None, timeout=60, body_extra=None,
                 assets=None, images_enabled=False):
    """POST a typed question set to a System One compatible endpoint.

    ``assets`` is the request's image map: reference -> the endpoint's
    asset entry (``data_base64`` + ``format``, or ``part``). Sending one
    requires ``images_enabled``, the operator opt-in -- the far end
    rejects image requests that lack the danger-zone header, so an asset
    with no opt-in is refused here rather than at the wire. A ``box``
    question is image traffic by the same rule and follows it: refused
    without the opt-in, marked with the header with it. With the opt-in
    but neither assets nor a box question the request stays plain and the
    header is not attached; that asymmetry is deliberate, because the
    header marks *this request* as image traffic, not the process as
    image-capable.
    """
    validate_questions(questions)
    asks_box = any(isinstance(question, dict)
                   and question.get("type") == "box"
                   for question in questions.values())
    if asks_box and not images_enabled:
        raise ValueError(
            "a box question cannot be sent without the images opt-in; "
            "the endpoint rejects it without the "
            f"{IMAGES_HEADER} header, so this fails here rather "
            "than at the far end")
    body = {"questions": questions}
    if body_extra:
        body.update(body_extra)
    if assets is not None:
        _validate_assets(assets)
        if not images_enabled:
            raise ValueError(
                "assets cannot be sent without the images opt-in; the "
                "endpoint rejects image requests that lack the "
                f"{IMAGES_HEADER} header, so this fails here rather than "
                "at the far end")
        body["assets"] = assets
    if images_enabled and (assets or asks_box):
        headers = dict(headers or {})
        headers.setdefault(IMAGES_HEADER, "true")
    return request(url, body=body, headers=headers, timeout=timeout)


def require(response, what="service"):
    """Turn a failed response into a named, catchable failure."""
    if not response.ok:
        raise PerceptionUnavailable(f"{what} {response.outcome}: {response.detail}")
    return response.payload
