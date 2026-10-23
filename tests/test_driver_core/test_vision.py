"""The vision tier: one client path, one budget, and nothing echoed back.

The load-bearing property in this module is the last one: **what came off a
screen does not come back out.** The image goes in, validated fields come
out, and nothing in between is written into an envelope, a result or the
audit chain.

That is easy to state and easy to break by accident, so it is enforced by a
test with a sentinel rather than by care. A capture is loaded with a unique
marker string in its payload, a fake provider is made to *echo that marker
back* in its response, and then the whole result -- the extraction, the vote,
the audit chain, the step payload -- is searched for the marker. Echoing it
back is the hostile case: it models a provider that includes the request
echo in its reply, which is exactly how a screen capture would end up in an
audit record without anyone adding it deliberately.

The other two properties are consolidation, not novelty: the vision call
uses the same endpoint, key, budget and price list as the decision tier,
because an extractor that could spend through a budget sized for something
else is not a bounded system.
"""
import base64
import json
import os
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest import mock

from driver_core.audit import AuditLog, MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.consensus import tally
from driver_core.driver import Driver
from driver_core.ev import FakeJev, action_answer
from driver_core.extractors import (
    IMAGE_REQUEST_TOKEN_BOUND, VisionExtractor, build_vision_pool,
)
from driver_core import osal
from driver_core.png import (
    MAX_DECODED_RASTER_BYTES, MAX_SCANLINE_BYTES,
    dimensions as png_dimensions,
)
from driver_core.perception import GUI, Capture, ScreenSource, Target
from driver_core.states import SCREEN_SCHEMA

#: Unique per run, and never a substring of anything in this repository.
SENTINEL = "SCREENPIXELS-SENTINEL-4f2a9c1e-must-not-escape"


def _capture(payload=None):
    """A screen capture carrying the sentinel, as pixels would."""
    return Capture(GUI, "my-app",
                   payload if payload is not None
                   else _png_bytes(),
                   fingerprint="abc123")


def _png_bytes(text=SENTINEL, *, width=280, height=280):
    """A valid grayscale PNG fixture with a private marker text chunk."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    rows = (b"\0" + bytes(width)) * height
    marker = b"harness-test\0" + text.encode("ascii")
    return (signature + chunk(b"IHDR", header) + chunk(b"tEXt", marker)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _zero_grayscale_png(width, height):
    """Build a small PNG file without holding its decoded raster in memory."""
    compressor = zlib.compressobj()
    compressed = []
    remaining = (width + 1) * height
    while remaining:
        count = min(65536, remaining)
        compressed.append(compressor.compress(bytes(count)))
        remaining -= count
    compressed.append(compressor.flush())

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                           8, 0, 0, 0, 0))
            + chunk(b"IDAT", b"".join(compressed))
            + chunk(b"IEND", b""))


def _keyed(*, screen=True, model="jev-latest"):
    return load_settings(env={"DRIVER_JEV_API_KEY": "sk-test",
                              "DRIVER_SCREEN": "true" if screen else "false",
                              "DRIVER_JEV_MODEL": model})


def _identity_preparer(image):
    """Use the production PNG checks without requiring a host image binary."""
    return osal.prepare_image_bytes(image, scaler=lambda path, *sizes: (path, ""))


def _resize_preparer(image):
    """Exercise production fit math with a deterministic local compositor."""
    def compose(path, width, height, draw_width, draw_height):
        with open(path, "wb") as handle:
            handle.write(_png_bytes("", width=width, height=height))
        return path, ""
    return osal.prepare_image_bytes(image, scaler=compose)


def _extractor(*args, **kwargs):
    kwargs.setdefault("image_preparer", _identity_preparer)
    return VisionExtractor(*args, **kwargs)


def _vision_pool(*args, **kwargs):
    kwargs.setdefault("image_preparer", _identity_preparer)
    return build_vision_pool(*args, **kwargs)


def _ok_response(state=None, *, echo=None):
    """A provider response that also echoes the request back at us."""
    payload = {
        "model": "jev-latest",
        "answers": {"observation": {"choice": "observed"}},
        "state": state or {"window_title": "report - editor",
                           "foreground_app": "editor",
                           "error_dialog_present": False},
        "usage": {"input_tokens": 900, "output_tokens": 40},
    }
    if echo:
        payload["echo"] = echo
    return payload


class FakeService:
    """A transport stand-in. Records what was sent; replies as configured."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []
        self.responses = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"url": url, "questions": questions,
                              "headers": headers, "body": body_extra})
        from driver_core.transport import Response
        if not self.payloads:
            return Response(0, "transport_error", detail="fake queue empty")
        payload = self.payloads.pop(0)
        self.responses.append(payload)
        return Response(200, "ok", payload=payload)


class OneClientPathTests(unittest.TestCase):
    """One provider, one key, one budget -- the same as the decision tier."""

    def test_the_vision_call_uses_the_decision_tier_endpoint(self):
        from driver_core.jev_client import SYSTEM_ONE_URL
        service = FakeService(_ok_response())
        extractor = _extractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertEqual(service.requests[0]["url"], SYSTEM_ONE_URL)

    def test_the_vision_call_uses_the_configured_key(self):
        service = FakeService(_ok_response())
        extractor = _extractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertEqual(service.requests[0]["headers"]["Authorization"],
                         "Bearer sk-test")

    def test_the_capture_is_sent_as_an_opted_in_image_asset(self):
        capture = _capture()
        service = FakeService(_ok_response())
        extractor = _extractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        result = extractor.extract(capture, SCREEN_SCHEMA)

        self.assertTrue(result.ok)
        sent = service.requests[0]
        self.assertEqual(sent["headers"]["TypeSafe-Danger-Zone-Images"],
                         "true")
        body = sent["body"]
        reference = body["state"]["screen"]
        self.assertIn(reference, body["assets"])
        asset = body["assets"][reference]
        self.assertEqual(asset, {
            "type": "image",
            "format": "png",
            "data_base64": base64.b64encode(capture.payload).decode("ascii"),
        })
        self.assertIn("`screen`", sent["questions"]["observation"]
                      ["instructions"])

    def test_image_request_reserves_the_full_context_bound(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(_capture(), SCREEN_SCHEMA)

        sent = service.requests[0]
        body = {"questions": sent["questions"], **sent["body"]}
        self.assertIn("assets", body)
        expected_usd = (IMAGE_REQUEST_TOKEN_BOUND
                        * JEV_INPUT_PRICE_PER_MILLION / 1_000_000)
        self.assertAlmostEqual(budget.snapshot()["entries"][0]["estimated_usd"],
                               expected_usd, places=9)

    def test_image_bound_and_price_match_the_harness_jev_catalog(self):
        from harness.dynamic_allocation import KNOWN_MODEL_SPECS

        for model in ("jev-latest", "jev-1.13.0"):
            with self.subTest(model=model):
                spec = KNOWN_MODEL_SPECS[model]
                self.assertEqual(spec.context_window,
                                 IMAGE_REQUEST_TOKEN_BOUND)
                self.assertEqual(spec.input_cost_per_million,
                                 JEV_INPUT_PRICE_PER_MILLION)

    def test_prepared_image_size_does_not_become_a_token_estimate(self):
        """No image-token estimator is published; reserve the sourced cap."""
        expected_usd = (IMAGE_REQUEST_TOKEN_BOUND
                        * JEV_INPUT_PRICE_PER_MILLION / 1_000_000)
        estimates = []
        cases = (
            (_png_bytes("", width=280, height=280), _identity_preparer),
            (_png_bytes("", width=1920, height=1080), _resize_preparer),
        )

        for image, prepare_image in cases:
            budget = Budget(1.0, step_ceiling_usd=1.0)
            extractor = _extractor(
                "s", _keyed(), budget=budget, audit=MemoryAuditLog(),
                transport_module=FakeService(_ok_response()),
                image_preparer=prepare_image)

            result = extractor.extract(_capture(image), SCREEN_SCHEMA)

            self.assertTrue(result.ok, result.reason)
            estimates.append(
                budget.snapshot()["entries"][0]["estimated_usd"])

        self.assertEqual(len(estimates), 2)
        for estimate in estimates:
            self.assertAlmostEqual(estimate, expected_usd, places=9)

    def test_unlisted_model_has_no_image_budget_bound_and_refuses_locally(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        preparer = mock.Mock(side_effect=_identity_preparer)
        extractor = _extractor(
            "s", _keyed(model="unlisted-model"), budget=budget,
            audit=MemoryAuditLog(), transport_module=service,
            image_preparer=preparer)

        result = extractor.extract(_capture(), SCREEN_SCHEMA)

        self.assertFalse(result.ok)
        self.assertIn("no verified image budget bound", result.reason)
        preparer.assert_not_called()
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_unprepared_out_of_bounds_image_is_refused_before_dispatch(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        source = _png_bytes("", width=1920, height=1080)
        extractor = _extractor(
            "s", _keyed(), budget=budget, audit=MemoryAuditLog(),
            transport_module=service,
            image_preparer=lambda image: (image, ""))

        result = extractor.extract(_capture(source), SCREEN_SCHEMA)

        self.assertFalse(result.ok)
        self.assertIn("outside the image bounds", result.reason)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_vision_is_not_admitted_without_the_screen_opt_in(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor("s", _keyed(screen=False), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=service)

        result = extractor.extract(_capture(), SCREEN_SCHEMA)

        self.assertFalse(result.ok)
        self.assertIn("not enabled", result.reason)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)

    def test_malformed_image_is_refused_before_reservation_or_dispatch(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=service)

        result = extractor.extract(_capture(b"not an image"), SCREEN_SCHEMA)

        self.assertFalse(result.ok)
        self.assertIn("image admission refused", result.reason)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_empty_capture_is_refused_before_reservation_or_dispatch(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor(
            "s", _keyed(), budget=budget, audit=MemoryAuditLog(),
            transport_module=service)

        result = extractor.extract(_capture(b""), SCREEN_SCHEMA)

        self.assertFalse(result.ok)
        self.assertIn("image admission refused", result.reason)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_there_is_no_way_to_declare_a_second_endpoint_or_key(self):
        """Not a test of behaviour so much as of shape: the constructor has
        no parameter through which a caller could introduce a second
        provider, which is the failure this consolidation prevents."""
        import inspect
        parameters = set(inspect.signature(
            VisionExtractor.__init__).parameters)
        self.assertNotIn("endpoint", parameters)
        self.assertNotIn("api_key", parameters)
        self.assertNotIn("url", parameters)

    def test_vision_spend_is_charged_to_the_shared_budget(self):
        """It used not to be. A vision pool could spend past a run ceiling
        that had been sized for the decision tier alone."""
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=FakeService(
                                        _ok_response()))
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertTrue(result.ok)
        self.assertGreater(budget.spent, 0.0)
        self.assertEqual(budget.snapshot()["entries"][0]["source"], "actual")
        self.assertEqual(budget.open_reservations(), [])

    def test_a_cost_read_from_usage_matches_the_shared_price_list(self):
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=FakeService(
                                        _ok_response()))
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertAlmostEqual(
            result.cost, 900 * JEV_INPUT_PRICE_PER_MILLION / 1_000_000,
            places=9)

    def test_a_failed_call_is_still_charged_rather_than_reported_free(self):
        from driver_core.transport import Response
        budget = Budget(1.0, step_ceiling_usd=1.0)

        class Failing:
            def call_service(self, *a, **k):
                return Response(500, "http_error", detail="boom")

        extractor = _extractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=Failing())
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertGreater(budget.spent, 0.0)
        self.assertEqual(budget.snapshot()["entries"][0]["source"],
                         "unavailable")

    def test_an_unkeyed_slot_makes_no_call_and_spends_nothing(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = _extractor(
            "s", load_settings(env={}), budget=budget, audit=MemoryAuditLog(),
            transport_module=service)
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_a_budget_refusal_dispatches_nothing(self):
        service = FakeService(_ok_response())
        budget = Budget(0.0000001, step_ceiling_usd=0.0000001)
        extractor = _extractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertIn("budget refused", result.reason)
        self.assertEqual(service.requests, [])

    def test_prepared_image_context_bound_refuses_before_dispatch(self):
        service = FakeService(_ok_response())
        bound_cost = (IMAGE_REQUEST_TOKEN_BOUND
                      * JEV_INPUT_PRICE_PER_MILLION / 1_000_000)
        budget = Budget(bound_cost / 2, step_ceiling_usd=bound_cost / 2)
        extractor = _extractor("s", _keyed(), budget=budget,
                               audit=MemoryAuditLog(),
                               transport_module=service,
                               image_preparer=_resize_preparer)

        large_capture = _capture(
            _png_bytes("", width=1920, height=1080))
        result = extractor.extract(large_capture, SCREEN_SCHEMA)

        self.assertFalse(result.ok)
        self.assertIn("budget refused", result.reason)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.open_reservations(), [])

    def test_one_slot_failing_does_not_silence_the_others(self):
        """A pool is only a pool if a member can fail."""
        from driver_core.transport import Response
        budget = Budget(1.0, step_ceiling_usd=1.0)

        class OneGood:
            def __init__(self):
                self.calls = 0

            def call_service(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    return Response(500, "http_error", detail="boom")
                return Response(200, "ok", payload=_ok_response())

        pool = _vision_pool(_keyed(), budget=budget,
                            audit=MemoryAuditLog(), slots=2,
                            transport_module=OneGood())
        votes = pool.run(_capture(), SCREEN_SCHEMA)
        self.assertEqual(len(votes), 2)
        self.assertEqual(sum(1 for v in votes if v.status == "ok"), 1)


class ImagePreparationTests(unittest.TestCase):
    """Measured fit policy plus fail-closed parser and compositor seams."""

    def test_rejects_oversized_scanline_before_inflation(self):
        width = 8 * 1024 * 1024
        image = _zero_grayscale_png(width, 1)
        self.assertLess(len(image), 16 * 1024)

        scaler = mock.Mock()
        with mock.patch("driver_core.png.zlib.decompressobj",
                        wraps=zlib.decompressobj) as inflater:
            prepared, why = osal.prepare_image_bytes(image, scaler=scaler)

        self.assertIsNone(prepared)
        inflater.assert_not_called()
        scaler.assert_not_called()
        self.assertIn("decodable PNG", why)

    def test_rejects_oversized_total_raster_before_inflation(self):
        width = MAX_SCANLINE_BYTES - 1
        height = MAX_DECODED_RASTER_BYTES // MAX_SCANLINE_BYTES + 1
        image = _zero_grayscale_png(width, height)
        self.assertLess(len(image), 128 * 1024)
        self.assertEqual(width + 1, MAX_SCANLINE_BYTES)
        self.assertGreater((width + 1) * height,
                           MAX_DECODED_RASTER_BYTES)

        scaler = mock.Mock()
        with mock.patch("driver_core.png.zlib.decompressobj",
                        wraps=zlib.decompressobj) as inflater:
            prepared, why = osal.prepare_image_bytes(image, scaler=scaler)

        self.assertIsNone(prepared)
        inflater.assert_not_called()
        scaler.assert_not_called()
        self.assertIn("decodable PNG", why)

    def test_oversized_desktop_frame_is_shrunk_without_distorting_aspect(self):
        source = _png_bytes("", width=1920, height=1080)
        calls = []

        def compose(path, width, height, draw_width, draw_height):
            calls.append((width, height, draw_width, draw_height))
            with open(path, "wb") as handle:
                handle.write(_png_bytes("", width=width, height=height))
            return path, ""

        prepared, why = osal.prepare_image_bytes(source, scaler=compose)

        self.assertIsNotNone(prepared, why)
        self.assertEqual(calls, [(1568, 882, 1568, 882)])
        self.assertEqual(png_dimensions(prepared), (1568, 882))
        self.assertNotEqual(prepared, source)

    def test_small_capture_is_center_padded_without_upscaling_content(self):
        source = _png_bytes("", width=200, height=100)
        calls = []

        def compose(path, width, height, draw_width, draw_height):
            calls.append((width, height, draw_width, draw_height))
            with open(path, "wb") as handle:
                handle.write(_png_bytes("", width=width, height=height))
            return path, ""

        prepared, why = osal.prepare_image_bytes(source, scaler=compose)

        self.assertIsNotNone(prepared, why)
        self.assertEqual(calls, [(280, 280, 200, 100)])
        self.assertEqual(png_dimensions(prepared), (280, 280))

    def test_darwin_scaler_pads_a_small_capture_on_the_platform_path(self):
        source = _png_bytes("", width=200, height=100)
        calls = []

        def fake_sips(argv, **kwargs):
            calls.append(tuple(argv[1:-1]))
            height, width = int(argv[2]), int(argv[3])
            with open(argv[-1], "wb") as handle:
                handle.write(_png_bytes("", width=width, height=height))
            return SimpleNamespace(ok=True, error="", stderr="")

        with mock.patch.object(osal, "platform_name", return_value="darwin"), \
                mock.patch.object(osal, "run", side_effect=fake_sips):
            prepared, why = osal.prepare_image_bytes(source)

        self.assertIsNotNone(prepared, why)
        self.assertEqual(png_dimensions(prepared), (280, 280))
        self.assertEqual(calls, [
            ("-z", "100", "200"),
            ("--padToHeightWidth", "280", "280", "--padColor", "000000"),
        ])

    def test_invalid_raster_and_failed_scaler_refuse_locally(self):
        source = _png_bytes("", width=1920, height=1080)
        scaler = mock.Mock()
        prepared, why = osal.prepare_image_bytes(source[:-12], scaler=scaler)
        self.assertIsNone(prepared)
        self.assertIn("decodable PNG", why)
        scaler.assert_not_called()

        prepared, why = osal.prepare_image_bytes(
            source, scaler=lambda *args: (None, "scaler unavailable"))
        self.assertIsNone(prepared)
        self.assertEqual(why, "scaler unavailable")

        paths = []

        def broken_scaler(path, *args):
            paths.append(path)
            raise RuntimeError("decoder refused the raster")

        prepared, why = osal.prepare_image_bytes(source, scaler=broken_scaler)
        self.assertIsNone(prepared)
        self.assertIn("decoder refused", why)
        self.assertEqual(len(paths), 1)
        self.assertFalse(os.path.exists(os.path.dirname(paths[0])))

class NothingEchoesBackTests(unittest.TestCase):
    """The load-bearing property, enforced by a sentinel rather than by care."""

    def _run(self, *, echo=True):
        audit = MemoryAuditLog()
        budget = Budget(1.0, step_ceiling_usd=1.0)
        capture = _capture()
        encoded = base64.b64encode(capture.payload).decode("ascii")
        service = FakeService(_ok_response(echo=encoded if echo else None))
        pool = _vision_pool(_keyed(), budget=budget, audit=audit,
                                 slots=1, transport_module=service)
        votes = pool.run(capture, SCREEN_SCHEMA)
        return capture, service, audit, budget, votes

    def test_the_provider_really_did_echo_the_pixels_back(self):
        """If this stops being true the test below proves nothing."""
        capture, service, _, _, _ = self._run()
        self.assertEqual(service.responses[0]["echo"],
                         base64.b64encode(capture.payload).decode("ascii"))

    def test_the_sentinel_never_reaches_the_audit_chain(self):
        capture, _, audit, _, _ = self._run()
        self.assertIn(SENTINEL.encode("ascii"), capture.payload, "precondition")
        rendered = json.dumps(audit.read_all(), default=str)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn(base64.b64encode(capture.payload).decode("ascii"),
                         rendered)
        self.assertNotIn(capture.fingerprint + "-leak", rendered)

    def test_the_sentinel_never_reaches_a_vote(self):
        _, _, _, _, votes = self._run()
        for vote in votes:
            rendered = json.dumps(vote.to_dict(), default=str)
            self.assertNotIn(SENTINEL, rendered)
            # The vote carries no state at all -- only the verdict.
            self.assertEqual(set(vote.to_dict()),
                             {"slot", "status", "reason", "cost",
                              "usage_source"})

    def test_the_sentinel_never_reaches_the_consensus_receipt(self):
        """The receipt is what travels with an agreed state into the audit."""
        _, _, _, _, votes = self._run()
        agreement = tally(votes, SCREEN_SCHEMA, quorum=1, min_agreement=1.0)
        rendered = json.dumps(agreement.receipt(SCREEN_SCHEMA.identity(), "s"),
                              default=str)
        self.assertNotIn(SENTINEL, rendered)

    def test_the_vision_audit_record_is_metadata_only(self):
        _, _, audit, _, _ = self._run()
        records = [r for r in audit.read_all() if r.get("tier") == "vision"]
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]),
                         {"seq", "kind", "at", "previous", "hash", "step_id",
                          "tier", "ok", "reason", "model", "cost_usd",
                          "usage_source"})

    def test_a_capture_summary_still_carries_no_payload(self):
        """The screen tier's own summary, which is what the driver audits."""
        summary = _capture().summary()
        self.assertNotIn(SENTINEL, json.dumps(summary, default=str))
        self.assertEqual(set(summary),
                         {"source", "target", "ok", "detail", "fingerprint"})

    def test_an_agreeing_pair_of_vision_slots_reaches_no_state_either(self):
        """Two independent slots agreeing is the strongest case: the state is
        real and agreed, and it still must not be logged."""
        audit = MemoryAuditLog()
        budget = Budget(1.0, step_ceiling_usd=1.0)
        service = FakeService(_ok_response(), _ok_response())
        pool = _vision_pool(_keyed(), budget=budget, audit=audit,
                                 slots=2, transport_module=service)
        votes = pool.run(_capture(), SCREEN_SCHEMA)
        agreement = tally(votes, SCREEN_SCHEMA, quorum=2, min_agreement=1.0)
        self.assertTrue(agreement.is_usable)
        rendered = json.dumps(agreement.to_dict(), default=str)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn("report - editor", rendered,
                         "an agreed value must be opt-in, not the default")
        self.assertNotIn(SENTINEL, json.dumps(audit.read_all(), default=str))


class ChainIntegrityTests(unittest.TestCase):
    """A real on-disk chain, because the in-memory one is not the artifact."""

    def test_the_vision_record_verifies_on_disk(self):
        import os
        import shutil
        import tempfile
        directory = tempfile.mkdtemp(prefix="driver-vision-audit-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "audit.jsonl")
        audit = AuditLog(path)
        extractor = _extractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=audit,
                                    transport_module=FakeService(
                                        _ok_response(echo=SENTINEL)))
        extractor.extract(_capture(), SCREEN_SCHEMA)
        verdict = audit.verify()
        self.assertTrue(verdict.ok, verdict.detail)
        with open(path, encoding="utf-8") as handle:
            on_disk = handle.read()
        self.assertNotIn(SENTINEL, on_disk)


class LocalHttpVisionSmokeTests(unittest.TestCase):
    """A real Driver step sends a screen image through local HTTP."""

    def test_driver_step_wires_screen_image_to_local_system_one_endpoint(self):
        image = _png_bytes(SENTINEL, width=1920, height=1080)
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.append({
                    "headers": dict(self.headers.items()),
                    "body": json.loads(self.rfile.read(length)),
                })
                body = json.dumps(_ok_response()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        with tempfile.TemporaryDirectory(prefix="driver-vision-smoke-") as d:
            path = os.path.join(d, "screen.png")
            with open(path, "wb") as handle:
                handle.write(image)
            server = HTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                settings = load_settings(
                    env={"DRIVER_JEV_API_KEY": "fake-only",
                         "DRIVER_SCREEN": "true"},
                    quorum=1, min_agreement=1.0,
                    confidence_threshold=0.7,
                    run_ceiling_usd=1.0, step_ceiling_usd=1.0,
                    dry_run=True)
                budget = Budget(1.0, step_ceiling_usd=1.0)
                audit = MemoryAuditLog()
                pool = _vision_pool(
                    settings, budget=budget, audit=audit, slots=1,
                    image_preparer=_resize_preparer)
                jev = FakeJev(action_answer("observe", confidence=0.95))
                driver = Driver(
                    settings=settings, budget=budget, audit=audit,
                    sources=[ScreenSource()], pools={GUI: pool}, jev=jev)

                endpoint = (
                    f"http://127.0.0.1:{server.server_port}/v1/systemone")
                with mock.patch("driver_core.adapters.osal.capture_screen",
                                return_value=(path, "")):
                    with mock.patch("driver_core.extractors.SYSTEM_ONE_URL",
                                    endpoint):
                        result = driver.step(Target("my-app", GUI),
                                             schema=SCREEN_SCHEMA)

                self.assertTrue(result.ok, (result.reason, result.detail))
                self.assertEqual(result.stopped_at, "executed")
                self.assertTrue(jev.called)
                self.assertEqual(len(jev.calls), 1)
                self.assertEqual(jev.calls[0]["state"]["window_title"],
                                 "report - editor")
                self.assertEqual(result.decision.recommended_action, "observe")
                self.assertEqual(len(received), 1)
                normalized_headers = {
                    key.lower(): value
                    for key, value in received[0]["headers"].items()
                }
                self.assertEqual(
                    normalized_headers.get("typesafe-danger-zone-images"),
                    "true")
                body = received[0]["body"]
                reference = body["state"]["screen"]
                prepared = base64.b64decode(
                    body["assets"][reference]["data_base64"], validate=True)
                self.assertEqual(png_dimensions(prepared), (1568, 882))
                self.assertNotEqual(prepared, image)
                self.assertEqual(body["model"], "jev-latest")
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), image)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
