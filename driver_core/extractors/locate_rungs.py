"""The locate ladder's billed rungs: the dispatch half of target location.

The ladder itself -- presence, edge repair, rebox, and what each rung
concludes -- is :mod:`driver_core.locate`'s; what lives here is turning one
rung's questions into one billed dispatch against the same prepared
capture.

Mixed into :class:`~driver_core.extractors.vision.VisionExtractor`, which
owns the state these rungs read -- the slot, the budget, the audit log, the
transport, the capture seam -- because a rung dispatch is a vision call.
"""
import base64
from pathlib import Path
import tempfile
import uuid

from .. import osal
from ..audit import KIND_EXTRACTION
from ..budget import estimate_call_cost
from ..config import JEV_INPUT_PRICE_PER_MILLION, image_model
from ..consensus import AGREED, INSUFFICIENT, Vote, tally_boxes
from .. import jev_client
from ..locate import LocatedTarget, locate as locate_targets
from .sizing import IMAGE_REQUEST_TOKEN_BOUND
from .tiles import search_tiles


def _system_one_url():
    """Resolve the endpoint at call time so a host can point it at a local
    service without rebuilding the extractor."""
    import sys
    package = sys.modules.get(__package__)
    return getattr(package, "SYSTEM_ONE_URL", jev_client.SYSTEM_ONE_URL)


class LocateRungs:
    """The locate ladder's dispatch, as rungs on a vision slot."""

    def locate(self, targets, **thresholds):
        """Locate targets on the current screen. One result per target.

        Box questions are the operator's second opt-in (``DRIVER_JEV_BOX``):
        image traffic on the pinned model only, refused by name without it,
        before any capture or charge. With it on, this captures once through
        the pixels seam and runs the ladder in :mod:`driver_core.locate` --
        once per configured quorum opinion, every rung one billed dispatch
        against call-owned images. After three full-frame rungs, unresolved
        low confidence permits one four-tile search (up to three rungs per
        tile): at most fifteen dispatches per opinion, without recursion.
        Returned boxes are fractions of the original capture (or of an
        injected crop), with preparation's shrink and padding undone. A
        caller supplying a crop still owns its origin in a parent frame.
        The consensus tally compares source-space boxes before returning any
        location. Shortfalls yield insufficient_agreement; valid conflicting
        opinions yield extraction_disagreement. Agreement returns an actual
        observed box, never an average. Unanimous absence remains absent.
        """
        if not self.settings.jev_box:
            raise ValueError(
                "box questions are opt-in; set DRIVER_JEV_BOX to locate "
                "targets, because the endpoint needs its danger-zone "
                "header and the surface is not yet stable")
        if not self.settings.keyed:
            return [LocatedTarget(target,
                                  reason="no key for vision extraction")
                    for target in targets]
        targets = list(targets)
        if not targets:
            return []
        path, why = self._captor()
        if not path:
            return [LocatedTarget(t, reason=f"pixels unavailable: {why}")
                    for t in targets]
        try:
            # Own the original resolution for the entire call. Preparation
            # and every crop read this snapshot, never the mutable captor file.
            data = Path(path).read_bytes()
            with tempfile.TemporaryDirectory(prefix="driver-locate-") as directory:
                snapshot = Path(directory) / "source.png"
                snapshot.write_bytes(data)
                return self._locate_snapshot(targets, str(snapshot), thresholds)
        except OSError as exc:
            return [LocatedTarget(t, reason=f"pixels unavailable: {exc}")
                    for t in targets]

    def _locate_snapshot(self, targets, source, thresholds):
        assets, geometry, why = self._image_assets(path=source)
        if assets is None:
            return [LocatedTarget(target,
                                  reason=f"pixels unavailable: {why}")
                    for target in targets]
        ref = next(iter(assets))
        model = image_model(self.settings)

        def ask(questions):
            return self._ask_locate(questions, assets, ref, model)

        rounds = []
        crops = {}
        for _ in range(self.settings.quorum):
            results = locate_targets(targets, ask, **thresholds)
            for result in results:
                if result.box is not None:
                    result.box = geometry.source_box(result.box)
                    if result.box is None:
                        result.reason = "box lies outside the captured content"

            def prepare(region):
                if region not in crops:
                    crops[region] = osal.prepare_crop(source, region)
                return crops[region]

            def crop_ask(questions, prepared):
                crop_ref = f"crop-{uuid.uuid4().hex}"
                crop_assets = {crop_ref: {
                    "type": "image", "format": "png",
                    "data_base64": base64.b64encode(prepared.data).decode("ascii")}}
                return self._ask_locate(questions, crop_assets, crop_ref, model)

            search_tiles(results, (geometry.width, geometry.height),
                         prepare, crop_ask, thresholds)
            rounds.append(results)
        agreed = []
        for index, target in enumerate(targets):
            observations = [results[index] for results in rounds]
            if any(r.reason == "extraction_disagreement" for r in observations):
                agreed.append(LocatedTarget(target, reason="extraction_disagreement"))
                continue
            votes = []
            for slot, result in enumerate(observations):
                if result.located or result.reason == "absent":
                    votes.append(Vote.ok(str(slot), {
                        "present": result.located, "box": result.box,
                        "confidence": result.confidence}))
                else:
                    votes.append(Vote.error(str(slot), result.reason))
            verdict = tally_boxes(votes, quorum=self.settings.quorum,
                                  min_agreement=self.settings.min_agreement)
            if verdict.outcome == AGREED:
                agreed.append(next(result for result in observations
                                   if result.located == verdict.state["present"]
                                   and result.box == verdict.state["box"]))
            else:
                reason = ("insufficient_agreement" if verdict.outcome == INSUFFICIENT
                          else "extraction_disagreement")
                agreed.append(LocatedTarget(target, reason=reason))
        return agreed

    def _ask_locate(self, questions, assets, ref, model):
        """One billed dispatch on the locate ladder. Returns the answers.

        The bytes stay in ``assets`` while the questions carry the
        reference. Every exit that is not a usable answer re-raises after
        recording, because the ladder names a dispatch failure to the
        operator and must see it.
        """
        reserve_usd = estimate_call_cost(
            IMAGE_REQUEST_TOKEN_BOUND,
            price_per_million=JEV_INPUT_PRICE_PER_MILLION)

        def dispatch():
            return self._transport.call_service(
                _system_one_url(), questions,
                headers={"Authorization":
                         f"Bearer {self.settings.jev_api_key}"},
                timeout=self.timeout,
                body_extra={"state": {"screen": ref}, "model": model},
                assets=assets,
                images_enabled=True)

        def report(one):
            if one.stage == "reserve":
                self._record_locate(False, f"budget refused: {one.error}",
                                    one.charged, one.source, model)
                raise one.error
            if one.error is not None:
                self._record_locate(False, f"transport raised: {one.error}",
                                    one.charged, one.source, model)
                raise one.error
            response = one.response
            if not response.ok:
                self._record_locate(False,
                                    f"{response.outcome}: {response.detail}",
                                    one.charged, one.source, model)
                raise RuntimeError(f"{response.outcome}: {response.detail}")
            self._record_locate(True, "locate", one.charged, one.source, model)
            return (response.payload or {}).get("answers") or {}

        return self.budget.exchange(
            label=self.slot, reserve_usd=reserve_usd, dispatch=dispatch,
            report=report, price_per_million=JEV_INPUT_PRICE_PER_MILLION)

    def _record_locate(self, ok, reason, cost, usage_source, model):
        """One metadata-only record per locate dispatch.

        The same allowlist rule as :meth:`VisionExtractor._record`: named
        fields only. A box is an observation about screen content; the
        record carries that a call happened and what it cost, never what it
        saw or where it looked.
        """
        if self.audit is not None:
            self.audit.append(
                KIND_EXTRACTION,
                step_id=self.slot,
                tier="vision",
                ok=ok,
                reason=reason,
                model=model,
                cost_usd=round(cost, 9),
                usage_source=usage_source,
            )
