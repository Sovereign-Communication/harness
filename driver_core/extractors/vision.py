"""The billed words-and-pixels extractor: :class:`VisionExtractor`."""
import base64
import os
import tempfile
import uuid

from .. import osal
from .. import transport
from ..audit import KIND_EXTRACTION
from ..budget import estimate_call_cost
from ..config import JEV_INPUT_PRICE_PER_MILLION, image_model
from .. import jev_client
from ..perception import VISION_CLASS
from .frames import FrameHistory, STABILITY_QUESTION
from .locate_rungs import LocateRungs
from .questions import _extraction_questions, _observed_state
from .sizing import (IMAGE_REQUEST_TOKEN_BOUND, _capture_state,
                     _estimate_capture_tokens)
from .structured import Extraction


def _system_one_url():
    """Resolve the endpoint at call time so a host can point it at a local
    service without rebuilding the extractor."""
    import sys
    package = sys.modules.get(__package__)
    return getattr(package, "SYSTEM_ONE_URL", jev_client.SYSTEM_ONE_URL)


_VISION_MODELS_WITH_VERIFIED_BOUND = ("jev-latest", "jev-1.13.0")


class VisionExtractor(LocateRungs):
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

    #: Reads the window through ``osal`` itself: in pixels mode it captures
    #: and ships its own asset, and in the recorded fallback the window
    #: manager holds the words exactly. A pool capture -- the pixel state of
    #: another tier's input -- is evidence neither mode can use, so none is
    #: accepted.
    uses_capture = False

    #: The target class this extractor can answer. Declared rather than
    #: inferred, so a pool can be asked which class it serves without
    #: inspecting the objects in it.
    serves = (VISION_CLASS,)

    def __init__(self, slot, settings, *, budget, audit,
                 transport_module=None, timeout=90, describer=None,
                 captor=None, history=None, preparer=None,
                 image_preparer=None):
        self.slot = slot
        self.settings = settings
        self.budget = budget
        self.audit = audit
        self._transport = transport_module or transport
        self.timeout = timeout
        # The same injectable-seam rule the rest of the package uses: a test
        # supplies a description so the suite never reads a real foreground
        # window, and two slots stay independent of whatever is on screen.
        self._describer = describer or osal.describe_screen
        # Prepare raw captures here so geometry is retained for locate.
        # An injected captor likewise supplies the original image/crop.
        self._captor = captor or (lambda: osal.capture_screen(max_side=None))
        self._preparer = preparer or osal.prepare_image
        self._image_preparer = image_preparer
        # One history per slot: this extractor's own view of how the screen
        # has been moving, fed from the same prepared capture the pixels
        # mode ships. See :class:`FrameHistory` for why nothing else holds
        # a copy.
        self.history = history if history is not None else FrameHistory()

    def stability(self):
        """This slot's frame-history judgment, or ``None`` when there is none.

        ``None`` means the decision tier keeps asking its own stability
        question: too few frames, or the frame history not opted in.
        """
        return self.history.judgment()

    def extract(self, schema, capture=None):
        """Read the screen, judge it, and return the agreed-typed state.

        ``capture`` keeps the 3.4.0 calling convention alive for a caller
        that already holds the observation: the slot judges those pixels
        instead of reading the screen a second time. The pool passes none,
        so an ordinary extraction still captures inside the slot.
        """
        if hasattr(schema, "payload") and not hasattr(capture, "payload"):
            schema, capture = capture, schema
        if not self.settings.screen_enabled:
            return self._record(Extraction(
                reason="screen vision is not enabled"))
        if not self.settings.keyed:
            # No call, no reservation, no charge. An unkeyed vision slot is
            # "unavailable", never "answered nothing".
            return self._record(Extraction(reason="no key for vision "
                                                   "extraction"))

        # The evidence is gathered before anything is reserved, so a screen
        # that cannot be read is a refusal rather than a billed call.
        #
        # The describer runs in both modes: it reads the facts the platform
        # holds exactly -- the title is the title -- and the state merges
        # them whatever the model judged. The mode selects only the
        # evidence the model judges: pixels, the operator's opt-in; or the
        # words below, the mode that was here first and the floor any path
        # falls back to -- a fallback recorded in the reason, not hidden.
        described, why = self._describer()
        use_pixels = bool(self.settings.jev_images)
        if (use_pixels and self.settings.jev_model
                not in _VISION_MODELS_WITH_VERIFIED_BOUND):
            return self._record(Extraction(
                reason="no verified image budget bound for configured Jev "
                       "model; use jev-latest or jev-1.13.0"))
        assets = None
        note = ""
        if use_pixels:
            path = self._capture_path(capture) if capture is not None else None
            try:
                assets, _geometry, asset_why = self._image_assets(path)
            finally:
                if path:
                    os.unlink(path)
            if assets is None and capture is not None:
                return self._record(Extraction(
                    reason=f"image admission refused: {asset_why}"))
            if assets is None:
                use_pixels = False
                note = f"pixels unavailable ({asset_why}); "
        if not use_pixels:
            self.history.clear()
        if not use_pixels and not described:
            return self._record(Extraction(
                reason=f"screen description unavailable: {why}"))

        questions = _extraction_questions(
            schema, basis="image" if use_pixels else "description")
        if use_pixels:
            estimate = IMAGE_REQUEST_TOKEN_BOUND
            state_payload = {"screen": next(iter(assets))}
            if self.settings.jev_frames:
                self.history.record(next(iter(assets.values()))["data_base64"])
                if self.history.needs_judgment:
                    assets, frames = self.history.pack()
                    state_payload = {"screen": frames[-1]["frame"], "frames": frames}
                    questions[STABILITY_QUESTION] = self.history.question()
        else:
            estimate = _estimate_capture_tokens(described, questions)
            state_payload = _capture_state(described)
        # The image path is pinned (see ``config.JEV_IMAGE_MODEL``); the
        # words path keeps whatever the operator configured.
        model = (image_model(self.settings) if use_pixels
                 else self.settings.jev_model)
        reserve_usd = estimate_call_cost(
            estimate, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        def dispatch():
            return self._transport.call_service(
                _system_one_url(), questions,
                headers={"Authorization":
                         f"Bearer {self.settings.jev_api_key}",
                         "TypeSafe-Danger-Zone-Images":
                         "true" if use_pixels else "false"},
                timeout=self.timeout,
                body_extra={"state": state_payload, "model": model},
                assets=assets,
                images_enabled=use_pixels)

        def report(one):
            """One extraction record from one exchange."""
            if one.stage == "reserve":
                self.history.invalidate()
                # Refused before dispatch, so nothing was spent.
                return self._record(
                    Extraction(reason=f"budget refused: {one.error}"))
            if one.error is not None:
                self.history.invalidate()
                return self._record(Extraction(
                    reason=f"transport raised: {one.error}",
                    cost=one.charged, model=model))
            response = one.response
            if not response.ok:
                self.history.invalidate()
                return self._record(Extraction(
                    reason=f"{response.outcome}: {response.detail}",
                    cost=one.charged, usage_source=one.source, model=model))
            state = _observed_state(response.payload, schema, described)
            if state is None:
                self.history.invalidate()
                return self._record(Extraction(
                    reason="model returned no parseable state",
                    cost=one.charged,
                    # The model that was dispatched, not the configured one:
                    # the record an operator reads to ask which model ran.
                    usage_source=one.source, model=model))
            if STABILITY_QUESTION in questions:
                self.history.accept(response.payload)
            return self._record(Extraction(
                state=state,
                reason=(note + "judged the window-manager description")
                if note else "",
                cost=one.charged, usage_source=one.source, model=model))

        return self.budget.exchange(
            label=self.slot, reserve_usd=reserve_usd, dispatch=dispatch,
            report=report, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

    def _image_assets(self, path=None):
        """Capture the screen and package it as exactly one request asset.

        Returns ``(assets, geometry, why)``; assets is None on a refusal, with
        ``why`` naming it. The bytes go in the assets map and nowhere
        else: ``state`` and the questions carry the reference, so a record
        of the request says which screen was judged without holding the
        screen itself -- which is why the audit record never grows a
        payload field no matter how the evidence arrives.
        """
        if path is None:
            path, why = self._captor()
            if not path:
                return None, None, why or "no capture"
        if self._image_preparer is not None:
            with open(path, "rb") as handle:
                prepared, why = self._image_preparer(handle.read())
        else:
            prepared, why = self._preparer(path)
        if prepared is None:
            return None, None, why
        data = getattr(prepared, "data", prepared)
        dimensions, why = osal.validate_prepared_image(data)
        if dimensions is None:
            return None, None, why
        geometry = getattr(prepared, "geometry", dimensions)
        ref = f"screen-{uuid.uuid4().hex[:8]}"
        entry = {"type": "image", "format": "png",
                 "data_base64": base64.b64encode(data).decode("ascii")}
        return {ref: entry}, geometry, ""

    @staticmethod
    def _capture_path(capture):
        """Write one held observation to a temp file for the PNG checks."""
        payload = getattr(capture, "payload", None)
        if not payload:
            return None
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            handle.write(payload)
        finally:
            handle.close()
        return handle.name

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
