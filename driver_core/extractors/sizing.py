"""What one dispatch costs to reserve, before it is made.

Both evidence modes -- the recorded words and the pixels -- and every rung
of the locate ladder size their dispatch here. Everything errs high on
purpose: a reservation that is too large costs a refusal, while one that is
too small under-bills, and the refusal is the honest failure.
"""
import json


def _capture_state(described):
    """The payload the model is shown in the words mode: text, never a path.

    This is the mode that predates the endpoint's image surface, and the
    floor the pixels mode falls back to. The window manager holds the title
    exactly rather than as a transcription of a picture, which is why these
    words stand as evidence in their own right and not merely as a degraded
    substitute for pixels.

    **Whatever :func:`driver_core.osal.describe_screen` returns is what this
    carries.** There is deliberately no field list here. An earlier version
    named the keys it expected, making this and ``osal`` two owners of one
    fact: a key added to the reader alone was dropped, silently.
    """
    lines = [f"{key.replace('_', ' ')}: {value}"
             for key, value in (described or {}).items() if value]
    return {"description": "; ".join(lines) if lines else "no window described"}


def _estimate_capture_tokens(described, questions):
    """A pre-flight upper bound: the decision tier's approximation, over four."""
    material = len(json.dumps(_capture_state(described), default=str).encode(
        "utf-8"))
    material += len(json.dumps(questions, default=str).encode("utf-8"))
    return max(1, material // 4 + 1)


#: Reserve the entire ordinary request context, not compressed file bytes.
#: https://docs.typesafe.ai/models#context-length (checked 2026-10-14):
#: 64k per request includes state + all questions; image tokens count toward
#: it (https://docs.typesafe.ai/images#image-tokens). 65,536 conservatively
#: covers both decimal and binary "64k". No image-token formula is published.
#: Both image dispatch paths below send only the images opt-in header, never
#: TypeSafe-Danger-Zone-Long-Context, which would invalidate this bound.
#: This bounds the WHOLE request, including repeated assets and text; actual
#: provider charges still settle normally. Unknown usage settles this bound.
IMAGE_REQUEST_TOKEN_BOUND = 65_536
