"""The structured tier: :class:`StructuredExtractor` and what one observed."""
from ..budget import UNAVAILABLE
from ..errors import SchemaError
from ..schema import validate_state


class Extraction:
    """What one extractor observed."""

    __slots__ = ("state", "reason", "cost", "usage_source", "model")

    def __init__(self, state=None, reason="", cost=0.0,
                 usage_source=UNAVAILABLE, model=None):
        self.state = state
        self.reason = reason
        self.cost = float(cost or 0.0)
        self.usage_source = usage_source
        self.model = model

    @property
    def ok(self):
        return self.state is not None

    def __repr__(self):
        return f"Extraction(ok={self.ok}, reason={self.reason!r})"


class StructuredExtractor:
    """Deterministic extraction from an already-structured capture.

    No model, no network, no cost, no variance. Given the same capture it
    always produces the same observation, which means a disagreement between
    two structured extractors is a bug in the reader rather than a genuine
    difference of opinion -- and is worth surfacing loudly.

    This is the class that makes the three structured target classes work at
    all. It is free, so a run that never leaves this tier never spends
    anything, which is the practical payoff of the CLI -> MCP -> DOM ordering
    rather than an aesthetic argument about it.
    """

    deterministic = True

    #: Takes the capture; paired with ``VisionExtractor.uses_capture``.
    uses_capture = True

    def __init__(self, slot, reader):
        self.slot = slot
        self._reader = reader

    def extract(self, capture, schema):
        try:
            raw = self._reader(capture, schema)
        except Exception as exc:
            return Extraction(reason=f"reader failed: {exc}")
        if not isinstance(raw, dict):
            return Extraction(reason="reader did not return a mapping")
        # Validate here rather than deferring: a structured reader that emits
        # an undeclared key is misconfigured, and letting that through would
        # turn a wiring mistake into a consensus disagreement.
        try:
            state = validate_state(raw, schema)
        except SchemaError as exc:
            return Extraction(reason=str(exc))
        return Extraction(state=state, usage_source="actual", cost=0.0)
