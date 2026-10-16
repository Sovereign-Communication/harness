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
import base64
import binascii
import json
import uuid

from . import osal, transport
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
    "You are reading the screen image in the `screen` state field. "
    "Report ONLY the fields named below, using "
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
                 transport_module=None, timeout=90, image_preparer=None):
        self.slot = slot
        self.settings = settings
        self.budget = budget
        self.audit = audit
        self._transport = transport_module or transport
        self.timeout = timeout
        self._image_preparer = image_preparer or osal.prepare_image_bytes

    def extract(self, capture, schema):
        if not self.settings.screen_enabled:
            return self._record(Extraction(
                reason="screen vision is not enabled"))
        if not self.settings.keyed:
            # No call, no reservation, no charge. An unkeyed vision slot is
            # "unavailable", never "answered nothing".
            return self._record(Extraction(reason="no key for vision "
                                                   "extraction"))

        questions = _extraction_questions(schema)
        try:
            body_extra, estimate = _vision_request(
                capture, questions, self.settings.jev_model,
                prepare_image=self._image_preparer)
        except (ValueError, binascii.Error) as exc:
            return self._record(Extraction(
                reason=f"image admission refused: {exc}"))
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
                         f"Bearer {self.settings.jev_api_key}",
                         "TypeSafe-Danger-Zone-Images": "true"},
                timeout=self.timeout,
                body_extra=body_extra)
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


# There is no published image-token estimator yet: the image docs say image
# tokens are included in usage.input_tokens and that estimation guidance is
# forthcoming. The default model context is 64k per request, and this path
# does not enable long context. Harness records that documented limit as
# 65,536 tokens in its Jev model catalog, so reserve the full request context
# as a conservative maximum for text plus the prepared image. This is a bound,
# not an estimate from image bytes. Sources: docs.typesafe.ai/images and
# docs.typesafe.ai/models.
IMAGE_REQUEST_TOKEN_BOUND = 65_536
# These are the Jev model IDs whose context and input price are present in
# Harness's model catalog. Refuse other IDs rather than reusing this bound
# where its model and pricing provenance is unknown.
_VISION_MODELS_WITH_VERIFIED_BOUND = frozenset({"jev-latest", "jev-1.13.0"})
# The provider image contract sets these limits; preparation may shrink a
# larger local capture before the prepared-asset limits are applied. Source:
# docs.typesafe.ai/images (Limits).
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _vision_request(capture, questions, model, *, prepare_image=None):
    """Build and admit one image request before it can reserve spend.

    Image bytes are prepared before admission and dispatch. The prepared
    raster, rather than the source capture or its base64 representation,
    becomes the sole request asset. The estimator reserves the full ordinary
    image-request context because compressed bytes are not a token estimate.
    """
    if model not in _VISION_MODELS_WITH_VERIFIED_BOUND:
        raise ValueError(
            "no verified image budget bound for configured Jev model; "
            "use jev-latest or jev-1.13.0")

    payload = getattr(capture, "payload", None)
    declared_format = None
    if isinstance(payload, dict):
        declared_format = payload.get("format")
        encoded = payload.get("data_base64")
        if not isinstance(encoded, str):
            raise ValueError("capture asset needs base64 image data")
        image = base64.b64decode(encoded, validate=True)
    elif isinstance(payload, bytes):
        image = payload
        encoded = base64.b64encode(image).decode("ascii")
    elif isinstance(payload, str):
        encoded = payload
        image = base64.b64decode(encoded, validate=True)
    else:
        raise ValueError("capture does not contain image bytes")

    if not image:
        raise ValueError("capture image is empty")
    if image.startswith(_PNG_SIGNATURE):
        image_format = "png"
    else:
        raise ValueError("only PNG captures can be safely prepared")
    if declared_format is not None:
        declared_format = str(declared_format).lower()
        if declared_format == "jpeg":
            declared_format = "jpg"
        if declared_format != image_format:
            raise ValueError("declared image format does not match its bytes")

    prepare_image = prepare_image or osal.prepare_image_bytes
    try:
        prepared, detail = prepare_image(image)
    except Exception as exc:
        raise ValueError(f"image preparation failed: {exc}") from exc
    if prepared is None:
        raise ValueError(detail or "image preparation refused the capture")
    dimensions, detail = osal.validate_prepared_image(prepared)
    if dimensions is None:
        raise ValueError(detail or "image preparation returned an invalid raster")
    if len(prepared) > MAX_IMAGE_BYTES:
        raise ValueError("prepared image exceeds the per-image byte limit")

    encoded = base64.b64encode(prepared).decode("ascii")
    reference = f"screen-{uuid.uuid4().hex}"
    body_extra = {
        "state": {"screen": reference},
        "assets": {reference: {"type": "image", "format": image_format,
                              "data_base64": encoded}},
        "model": model,
    }
    body = {"questions": questions, **body_extra}
    body_size = len(json.dumps(body).encode("utf-8"))
    if body_size > MAX_REQUEST_BYTES:
        raise ValueError("prepared image request exceeds the request-body limit")
    return body_extra, IMAGE_REQUEST_TOKEN_BOUND


def build_vision_pool(settings, *, budget, audit, slots=1,
                      transport_module=None, timeout=90, image_preparer=None):
    """A pool of independent vision slots, for the ``gui`` target class.

    Built here rather than by the caller so that the vision pool is
    constructed the same way every time: same endpoint, same budget, same
    audit log as the decision tier. A caller who wants two independent
    opinions asks for two slots; they do not assemble their own clients.
    """
    names = (f"vision-{i}" for i in range(max(1, int(slots))))
    return ExtractorPool(
        [VisionExtractor(name, settings, budget=budget, audit=audit,
                         transport_module=transport_module, timeout=timeout,
                         image_preparer=image_preparer)
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
