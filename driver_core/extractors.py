"""The extraction tier: N independent extractors over one capture.

Independence is the only thing this module is for. The consensus tally can
only say "two or more independent observers saw the same thing" if the
observers really are independent, so the rules below are structural rather
than advisory:

* **An extractor never sees another's answer.** Each is handed the same
  capture and the same schema and nothing else. There is no shared
  conversation, no seed answer, no "the previous extractor said" hint. A
  second model told what the first one said is a rubber stamp, not a second
  observation.
* **Failures are isolated.** One extractor dying must not affect the others;
  a pool that stops at the first error is not a pool.
* **Cost is recorded per slot**, including for slots that failed after the
  provider billed them.

Two kinds of extractor exist, and the distinction is the single biggest
lever on cost and reliability:

:class:`StructuredExtractor` is deterministic and free. It handles CLI
output, DOM/accessibility trees and MCP results, where the "state" is
already structured. Three of the four target classes need no vision at all,
and this is the class that makes them work.

:class:`VisionExtractor` spends money and can be wrong. It exists for native
GUI pixels, where there is no structured source to read. It reaches the
model through the same client primitives the decision tier uses -- one
endpoint, one key, one budget, one price list -- and it is the only extractor
whose spend reaches the run ceiling.
"""
import json

from . import transport
from .audit import KIND_EXTRACTION
from .budget import UNAVAILABLE, estimate_call_cost
from .config import JEV_INPUT_PRICE_PER_MILLION
from .consensus import Vote
from .errors import SchemaError
from .jev_client import SYSTEM_ONE_URL
from .perception import VISION_CLASS
from .schema import validate_state

#: Instruction handed to a vision model. Deliberately instructs the model to
#: answer only with the declared fields and to say it cannot tell, because a
#: model that guesses produces a confident wrong value that -- absent the
#: consensus tier -- would be indistinguishable from a true observation.
EXTRACTION_INSTRUCTIONS = (
    "You are reading a screen. Report ONLY the fields named below, using "
    "exactly these field names. If a field is not visible or you are not "
    "certain of it, omit it rather than guessing. Do not add commentary.\n"
    "FIELDS:\n{fields}"
)


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


class VisionExtractor:
    """Vision extraction: one model, one opinion, charged honestly.

    The last resort, and the only extractor here that costs money or can be
    wrong in a way code cannot check. Three of the four target classes never
    reach it, and that is enforced by which pool a target selects rather than
    by this class refusing to help -- see :meth:`ExtractorPool.for_target`.

    **It goes through the same client primitives as the decision tier.** The
    same :class:`~driver_core.config.Settings`, the same
    :data:`~driver_core.jev_client.SYSTEM_ONE_URL`, the same
    :class:`~driver_core.budget.Budget`, the same
    :class:`~driver_core.audit.AuditLog`, the same
    :mod:`driver_core.transport`, and the same
    :data:`~driver_core.config.JEV_INPUT_PRICE_PER_MILLION`. There is no
    second provider, no second key, and no second price list.

    That consolidation is the point, not tidiness. The previous version took
    its own ``endpoint`` and ``api_key``, which meant extraction could be
    pointed at a different model than the decision tier, and it charged
    nothing at all -- so a vision pool could spend straight past a run
    ceiling that had been sized for the decision tier alone. Extraction is a
    pool, so that was N unaccounted calls per step.

    Runs in its own try/except so a provider that hangs, rate-limits, or
    returns a refusal becomes one failed slot rather than a failed round --
    and still settles its reservation, because a failed call is possibly a
    billed one.
    """

    deterministic = False

    #: The target class this extractor can answer. Declared rather than
    #: inferred, so a pool can be asked which class it serves without
    #: inspecting the objects in it.
    serves = (VISION_CLASS,)

    def __init__(self, slot, settings, *, budget, audit,
                 transport_module=None, timeout=90):
        self.slot = slot
        self.settings = settings
        self.budget = budget
        self.audit = audit
        self._transport = transport_module or transport
        self.timeout = timeout

    def extract(self, capture, schema):
        if not self.settings.keyed:
            # No call, no reservation, no charge. An unkeyed vision slot is
            # "unavailable", never "answered nothing".
            return self._record(Extraction(reason="no key for vision "
                                                   "extraction"))

        questions = _extraction_questions(schema)
        estimate = _estimate_capture_tokens(capture, questions)
        reserve_usd = estimate_call_cost(
            estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        try:
            reservation = self.budget.reserve(reserve_usd, label=self.slot)
        except Exception as exc:
            # Refused before dispatch, so nothing was spent.
            return self._record(Extraction(reason=f"budget refused: {exc}"))

        try:
            response = self._transport.call_service(
                SYSTEM_ONE_URL, questions,
                headers={"Authorization":
                         f"Bearer {self.settings.jev_api_key}"},
                timeout=self.timeout,
                body_extra={"state": _capture_state(capture),
                            "model": self.settings.jev_model})
        except Exception as exc:
            charged = reservation.settle(None)
            return self._record(Extraction(
                reason=f"transport raised: {exc}", cost=charged,
                model=self.settings.jev_model))

        usage = response.usage() or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        # A failed call is still possibly a billed call. Settling at zero here
        # is how a vision loop quietly runs up a bill nobody can account for.
        if response.ok and usage and input_tokens:
            charged = reservation.settle(
                estimate_call_cost(
                    input_tokens, price_per_million=JEV_INPUT_PRICE_PER_MILLION),
                source="actual")
            usage_source = "actual"
        elif response.ok and usage:
            charged = reservation.settle(
                reserve_usd, source="estimated")
            usage_source = "estimated"
        else:
            charged = reservation.settle(None)
            usage_source = UNAVAILABLE

        if not response.ok:
            return self._record(Extraction(
                reason=f"{response.outcome}: {response.detail}", cost=charged,
                usage_source=usage_source, model=self.settings.jev_model))

        state = _state_from_payload(response.payload, schema)
        if state is None:
            return self._record(Extraction(
                reason="model returned no parseable state", cost=charged,
                usage_source=usage_source, model=self.settings.jev_model))
        return self._record(Extraction(
            state=state, cost=charged, usage_source=usage_source,
            model=self.settings.jev_model))

    def _record(self, extraction):
        """Write one metadata-only audit record. Never the state, never the
        image.

        The fields below are an explicit allowlist rather than a filtered
        copy of something else, so a field added to :class:`Extraction`
        later cannot leak into the log by default. The pixels went in; the
        validated fields came out; nothing in between is written down.
        """
        if self.audit is not None:
            self.audit.append(
                KIND_EXTRACTION,
                step_id=self.slot,
                tier="vision",
                ok=extraction.ok,
                reason=extraction.reason,
                model=extraction.model,
                cost_usd=round(extraction.cost, 9),
                usage_source=extraction.usage_source,
            )
        return extraction


def _extraction_questions(schema):
    fields = "\n".join(
        f"- {f.name} ({f.type}"
        + (f"/{f.presence}" if f.presence != "required" else "")
        + f"): {f.description}" for f in schema.fields)
    return {
        "observation": transport.choice(
            EXTRACTION_INSTRUCTIONS.format(fields=fields),
            {"observed": "The screen was read and the fields are "
                         "reported in the structured reply.",
             "unreadable": "The screen could not be read; no fields are "
                           "reported."}),
    }


def _estimate_capture_tokens(capture, questions):
    """A pre-flight upper bound, using the decision tier's own approximation.

    The capture is opaque here -- a path, a base64 blob, a handle. Its
    serialised size is a safe over-estimate of what the provider will read,
    and it errs high on purpose: a reservation that is too large costs a
    refusal, while one that is too small silently under-bills.
    """
    material = len(json.dumps(_capture_state(capture), default=str).encode(
        "utf-8"))
    material += len(json.dumps(questions, default=str).encode("utf-8"))
    return max(1, material // 4 + 1)


def build_vision_pool(settings, *, budget, audit, slots=1,
                      transport_module=None, timeout=90):
    """A pool of independent vision slots, for the ``gui`` target class.

    Built here rather than by the caller so that the vision pool is
    constructed the same way every time: same endpoint, same budget, same
    audit log as the decision tier. A caller who wants two independent
    opinions asks for two slots; they do not assemble their own clients.
    """
    names = (f"vision-{i}" for i in range(max(1, int(slots))))
    return ExtractorPool(
        [VisionExtractor(name, settings, budget=budget, audit=audit,
                         transport_module=transport_module, timeout=timeout)
         for name in names],
        serves=(VISION_CLASS,))


def _state_from_payload(payload, schema):
    """Read a state out of a model response, or ``None``.

    A model that reports "unreadable" is believed. A model whose structured
    reply does not validate is not repaired -- the slot reports malformed and
    the tally excludes it, because a repaired extraction is an extraction
    nobody observed.
    """
    if not isinstance(payload, dict):
        return None
    choice = (payload.get("answers") or {}).get("observation") or {}
    if choice.get("choice") != "observed":
        return None
    fields = payload.get("state") or payload.get("fields")
    if not isinstance(fields, dict):
        return None
    try:
        return validate_state(fields, schema)
    except SchemaError:
        return None


def _capture_state(capture):
    """The payload a vision model is shown.

    The capture is opaque to this module: a path, a base64 payload or a
    handle, depending on the perception tier. Whatever it is, the two things
    that must never happen are (a) logging it and (b) letting it reach the
    audit log, both of which are enforced by the callers.
    """
    if isinstance(capture, dict):
        return capture
    return {"image_ref": str(capture)}


def _json_from_text(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class ExtractorPool:
    """Runs every extractor against one capture and collects votes.

    Sequential rather than parallel on purpose for the first version: the
    number of extractors is small, the cost is already the dominant cost, and
    a serial pool produces a deterministic ordering of records for the audit
    log, which is worth more here than the wall-clock saving.

    A pool declares the target classes it serves, and
    :meth:`refuses_class` is the structural half of "three of the four target
    classes never reach the vision tier": a vision pool handed a ``dom``
    capture returns no votes rather than billing for an opinion nobody asked
    for.
    """

    def __init__(self, extractors, *, serves=()):
        self.extractors = list(extractors)
        self.serves = tuple(serves)

    @property
    def size(self):
        return len(self.extractors)

    def refuses_class(self, target_class):
        """Whether this pool declines to answer ``target_class``.

        ``None`` -- an undeclared target -- is always answered. A pool that
        declares nothing is a pool that serves everything, which is the
        historical behaviour and the reason the declaration is optional.
        """
        if not self.serves:
            return False
        return target_class is not None and target_class not in self.serves

    def for_target(self, target):
        """This pool, or ``None`` if it does not serve the target's class."""
        target_class = getattr(target, "target_class", None)
        return None if self.refuses_class(target_class) else self

    def run(self, capture, schema):
        """Return one Vote per extractor. Never raises for a slot failure."""
        votes = []
        for extractor in self.extractors:
            slot = getattr(extractor, "slot", f"slot-{len(votes)}")
            try:
                result = extractor.extract(capture, schema)
            except Exception as exc:  # isolation is the point
                votes.append(Vote.error(slot, f"extractor raised: {exc}"))
                continue
            if result.ok:
                votes.append(Vote.ok(slot, result.state, cost=result.cost,
                                     usage_source=result.usage_source))
            else:
                # A model that answered but produced nothing usable is
                # malformed, not merely unavailable: it occupied a slot and
                # spent money, and the tally must reflect that.
                votes.append(Vote.malformed(slot, result.reason or "no state",
                                            cost=result.cost,
                                            usage_source=result.usage_source))
        return votes
