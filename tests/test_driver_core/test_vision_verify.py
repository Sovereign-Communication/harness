"""Post-action vision verification and the versioned launch-verify pack.

The tests are hermetic: the screen source, clock, sleep, transport and image
preparation are injected. What they pin is the part the model must not own --
the prompt, the 0.85 threshold, the 2s/5s polls, the 2s/5s time budgets, the
95 floor and the fail-closed handling of every missing answer.
"""
import json
import struct
import unittest
import zlib

from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeJev, action_answer
from driver_core.executor import Consent, Executor
from driver_core.executor_registry import build_driver_registry
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.observation import Capture, StructuredSource, Target
from driver_core.states import SCREEN_SCHEMA
from driver_core.vision_verify import (
    APP_VISIBILITY_THRESHOLD, CAPTURE_BUDGET_SECONDS, PACK_FIELDS, PACK_FLOOR,
    PACK_ID, PACK_VERSION, TEXT_VISIBILITY_THRESHOLD,
    VERDICT_BUDGET_SECONDS, LaunchVerifyPack, VisionVerifier,
)

STATE = {
    "window_title": "Report - Editor",
    "foreground_app": "editor",
    "error_dialog_present": False,
}


def _png_bytes(text="", *, width=280, height=280):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    rows = (b"\0" + bytes(width)) * height
    marker = b"harness-test\0" + text.encode("ascii")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"tEXt", marker) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def _preparer(image):
    return image, ""


def _keyed(**over):
    env = {"DRIVER_JEV_API_KEY": "sk-test",
           "DRIVER_SCREEN": "true",
           "DRIVER_VISION_VERIFY": "true",
           "DRIVER_JEV_MODEL": "jev-latest"}
    env.update(over)
    return load_settings(env=env)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Source:
    def __init__(self, payload=None, *, delay=0.0, detail=""):
        self.payload = _png_bytes() if payload is None else payload
        self.delay = delay
        self.detail = detail
        self.calls = 0
        self.clock = None

    def capture(self, target=None):
        self.calls += 1
        if self.clock is not None:
            self.clock.now += self.delay
        return Capture("screen", target, self.payload,
                       detail=self.detail, fingerprint="fp")


class Service:
    def __init__(self, *answers, delay=0.0):
        self.answers = list(answers)
        self.delay = delay
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None, assets=None, images_enabled=None):
        self.requests.append({
            "url": url, "questions": questions, "headers": headers,
            "timeout": timeout, "body": body_extra, "assets": assets,
            "images_enabled": images_enabled,
        })
        from driver_core.transport import Response
        if not self.answers:
            return Response(0, "transport_error", detail="queue empty")
        answer = self.answers.pop(0)
        if isinstance(answer, Response):
            return answer
        return Response(200, "ok", payload={
            "model": "jev-latest", "answers": answer,
            "usage": {"input_tokens": 900, "output_tokens": 10},
        })


def _visible(value):
    return {"visible": {"type": "noul", "noul": value}}


def _verifier(service, *, source=None, clock=None, settings=None,
              capture_timeout=CAPTURE_BUDGET_SECONDS,
              verdict_timeout=VERDICT_BUDGET_SECONDS):
    clock = clock or Clock()
    source = source or Source()
    source.clock = clock
    return VisionVerifier(
        settings or _keyed(),
        budget=Budget(1.0, step_ceiling_usd=1.0),
        audit=MemoryAuditLog(), source=source,
        transport_module=service, clock=clock, sleep=clock.sleep,
        image_preparer=_preparer, capture_timeout=capture_timeout,
        verdict_timeout=verdict_timeout)


class LaunchVisibilityTests(unittest.TestCase):
    def test_the_prompt_is_the_declared_question_and_passes_at_the_bar(self):
        service = Service(_visible(0.95))
        verifier = _verifier(service)
        result = verifier.verify_application("FreeBuff")

        self.assertTrue(result.passed)
        self.assertEqual(result.kind, "application")
        self.assertEqual(result.threshold, APP_VISIBILITY_THRESHOLD)
        request = service.requests[0]
        question = request["questions"]["visible"]
        self.assertEqual(question["instructions"],
                         "Is the application 'FreeBuff' visible and focused?")
        self.assertIn("freebuff", json.dumps(request).lower())
        self.assertTrue(request["images_enabled"])
        self.assertTrue(request["assets"])

    def test_a_below_threshold_probability_does_not_pass(self):
        service = Service(_visible(0.84), _visible(0.84))
        result = _verifier(service).verify_application("FreeBuff")

        self.assertFalse(result.passed)
        self.assertEqual(result.attempts, 2)
        self.assertIn("0.840", result.evidence[-1]["reason"])
        self.assertFalse(result.timed_out)

    def test_the_second_poll_happens_at_five_seconds_from_the_action(self):
        clock = Clock()
        service = Service(_visible(0.2), _visible(0.9))
        result = _verifier(service, clock=clock).verify_application("FreeBuff")

        self.assertTrue(result.passed)
        self.assertEqual(result.attempts, 2)
        # The polls are measured from action completion: sleep 2s, ask,
        # then sleep the remaining 3s to reach the 5s poll.
        self.assertEqual(clock.sleeps, [2.0, 3.0])
        self.assertEqual(len(service.requests), 2)

    def test_a_passing_check_writes_the_declared_verification_kind(self):
        audit = MemoryAuditLog()
        source = Source()
        clock = Clock()
        source.clock = clock
        verifier = VisionVerifier(
            _keyed(), budget=Budget(1.0, step_ceiling_usd=1.0), audit=audit,
            source=source, transport_module=Service(_visible(0.99)),
            clock=clock, sleep=clock.sleep, image_preparer=_preparer)
        result = verifier.verify_application("FreeBuff")

        self.assertTrue(result.passed)
        kinds = [record["kind"] for record in audit.read_all()]
        self.assertIn("verification", kinds)

    def test_two_false_answers_escalate_and_record_the_reason(self):
        audit = MemoryAuditLog()
        service = Service(_visible(0.1), _visible(0.2))
        clock = Clock()
        source = Source()
        source.clock = clock
        verifier = VisionVerifier(
            _keyed(), budget=Budget(1.0, step_ceiling_usd=1.0), audit=audit,
            source=source, transport_module=service, clock=clock,
            sleep=clock.sleep, image_preparer=_preparer)
        result = verifier.verify_application("FreeBuff")

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "application not visible")
        escalations = [r for r in audit.read_all()
                       if r["kind"] == "escalation"]
        self.assertEqual(escalations[0]["reason"], "verification_failed")

    def test_a_slow_capture_times_out_without_calling_jev(self):
        clock = Clock()
        source = Source(delay=CAPTURE_BUDGET_SECONDS + 0.1)
        service = Service(_visible(0.99))
        result = _verifier(service, source=source, clock=clock).verify_application(
            "FreeBuff")

        self.assertFalse(result.passed)
        self.assertTrue(result.timed_out)
        self.assertEqual(service.requests, [])
        self.assertIn("capture exceeded", result.evidence[0]["reason"])

    def test_a_slow_verdict_times_out_before_a_second_poll(self):
        clock = Clock()
        service = Service(_visible(0.99))
        original = service.call_service

        def slow(*args, **kwargs):
            clock.now += VERDICT_BUDGET_SECONDS + 0.1
            return original(*args, **kwargs)

        service.call_service = slow
        result = _verifier(service, clock=clock).verify_application("FreeBuff")

        self.assertFalse(result.passed)
        self.assertTrue(result.timed_out)
        self.assertEqual(len(service.requests), 1)
        self.assertIn("verdict exceeded", result.evidence[0]["reason"])

    def test_a_missing_or_malformed_answer_fails_closed(self):
        for answers in ({}, {"visible": {"type": "noul", "noul": "yes"}},
                        {"visible": {"type": "noul", "noul": 1.2}}):
            with self.subTest(answers=answers):
                result = _verifier(Service(answers, answers)).verify_application(
                    "FreeBuff")
                self.assertFalse(result.passed)

    def test_the_screenshot_never_reaches_the_audit_chain(self):
        audit = MemoryAuditLog()
        marker = "SCREEN-SENTINEL-272"
        source = Source(_png_bytes(marker))
        clock = Clock()
        source.clock = clock
        verifier = VisionVerifier(
            _keyed(), budget=Budget(1.0, step_ceiling_usd=1.0), audit=audit,
            source=source, transport_module=Service(_visible(0.99)),
            clock=clock, sleep=clock.sleep, image_preparer=_preparer)
        verifier.verify_application("FreeBuff")
        rendered = json.dumps(audit.read_all(), default=str)
        self.assertNotIn(marker, rendered)
        self.assertNotIn("data_base64", rendered)


class TextVisibilityTests(unittest.TestCase):
    def test_typed_text_uses_the_text_prompt_and_no_wait(self):
        service = Service(_visible(0.9))
        clock = Clock()
        result = _verifier(service, clock=clock).verify_text("hello world")

        self.assertTrue(result.passed)
        self.assertEqual(result.threshold, TEXT_VISIBILITY_THRESHOLD)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(
            service.requests[0]["questions"]["visible"]["instructions"],
            "Is the text 'hello world' visible in the screenshot?")

    def test_missing_typed_text_fails(self):
        result = _verifier(Service(_visible(0.2))).verify_text("hello")
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "typed text not visible")
        self.assertEqual(result.attempts, 1)

    def test_flag_off_does_no_work_at_all(self):
        service = Service(_visible(0.99))
        source = Source()
        clock = Clock()
        source.clock = clock
        verifier = VisionVerifier(
            load_settings(env={"DRIVER_JEV_API_KEY": "sk-test"}),
            budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(), source=source,
            transport_module=service, clock=clock, sleep=clock.sleep,
            image_preparer=_preparer)
        result = verifier.verify_application("FreeBuff")

        self.assertFalse(result.passed)
        self.assertEqual(source.calls, 0)
        self.assertEqual(service.requests, [])
        self.assertIn("not enabled", result.reason)


class PackTests(unittest.TestCase):
    def manifest(self):
        return {
            "app_identity": {"expected": "FreeBuff Desktop",
                             "options": ["FreeBuff Desktop", "Other"]},
            "backend": {"expected": "FreeBuff",
                        "options": ["FreeBuff", "Codex"]},
            "model": {"expected": "jev-latest",
                      "options": ["jev-latest", "jev-1.13.0"]},
            "thinking": {"expected": "high", "options": ["low", "high"]},
            "repo": {"expected": "Harness", "options": ["Harness", "Other"]},
            "build_mode": {"expected": "active",
                           "options": ["active", "inactive"]},
            "mission_queued": {"expected": "queued",
                               "options": ["queued", "empty"]},
            "start_state": {"expected": "ready", "options": ["ready", "running"]},
        }

    def answers(self, *, score=3, override=None):
        answers = {}
        for name in PACK_FIELDS:
            expected = self.manifest()[name]["expected"]
            answers[f"{name}.choice"] = {"type": "choice", "choice": expected}
            answers[f"{name}.score"] = {"type": "score", "score": score}
        answers.update(override or {})
        return answers

    def test_the_pack_is_versioned_and_has_sixteen_questions(self):
        pack = LaunchVerifyPack(self.manifest())
        questions = pack.questions()
        self.assertEqual(PACK_ID, "launch-verify")
        self.assertEqual(PACK_VERSION, "1.0.0")
        self.assertEqual(PACK_FLOOR, 95.0)
        self.assertEqual(len(questions), 16)
        self.assertEqual(set(questions),
                         {f"{name}.{kind}" for name in PACK_FIELDS
                          for kind in ("choice", "score")})
        for name in PACK_FIELDS:
            self.assertEqual(questions[f"{name}.choice"]["type"], "choice")
            self.assertEqual(questions[f"{name}.score"]["type"], "score")
            self.assertNotIn("manifest",
                             questions[f"{name}.choice"]["instructions"].lower())
            self.assertNotIn("expected",
                             questions[f"{name}.choice"]["instructions"].lower())

    def test_only_agreement_plus_a_score_at_the_floor_passes(self):
        pack = LaunchVerifyPack(self.manifest())
        result = pack.evaluate(self.answers(score=3))
        self.assertTrue(result.passed, result.to_dict())
        self.assertTrue(all(field.passed for field in result.fields))

    def test_a_score_two_is_below_the_95_floor(self):
        result = LaunchVerifyPack(self.manifest()).evaluate(
            self.answers(score=2))
        self.assertFalse(result.passed)
        self.assertTrue(all(not field.passed for field in result.fields))
        self.assertAlmostEqual(result.fields[0].percent, 200.0 / 3.0)
        self.assertIn("below the 95 floor", result.fields[0].reason)

    def test_a_matching_score_cannot_rescue_a_wrong_extraction(self):
        wrong = {"backend.choice": {"type": "choice", "choice": "Codex"}}
        result = LaunchVerifyPack(self.manifest()).evaluate(
            self.answers(score=3, override=wrong))
        self.assertFalse(result.passed)
        backend = next(f for f in result.fields if f.name == "backend")
        self.assertFalse(backend.agrees)
        self.assertFalse(backend.passed)
        self.assertIn("extraction read 'Codex'", backend.reason)

    def test_not_visible_never_passes(self):
        missing = {"model.choice": {"type": "choice", "choice": "not_visible"}}
        result = LaunchVerifyPack(self.manifest()).evaluate(
            self.answers(score=3, override=missing))
        self.assertFalse(result.passed)

    def test_missing_and_undeclared_answers_fail_closed(self):
        pack = LaunchVerifyPack(self.manifest())
        incomplete = self.answers(score=3)
        incomplete.pop("repo.score")
        self.assertFalse(pack.evaluate(incomplete).passed)
        extra = self.answers(score=3, override={
            "surprise": {"type": "noul", "noul": 0.99}})
        self.assertFalse(pack.evaluate(extra).passed)

    def test_a_manifest_that_invents_a_value_or_omits_a_field_is_refused(self):
        bad = self.manifest()
        bad["model"]["expected"] = "not-in-options"
        with self.assertRaises(ValueError):
            LaunchVerifyPack(bad)
        missing = self.manifest()
        missing.pop("repo")
        with self.assertRaises(ValueError):
            LaunchVerifyPack(missing)

    def test_the_manifest_gate_uses_the_same_transport_and_screenshot(self):
        service = Service(self.answers(score=3))
        source = Source()
        clock = Clock()
        source.clock = clock
        verifier = VisionVerifier(
            _keyed(), budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(), source=source, transport_module=service,
            clock=clock, sleep=clock.sleep, image_preparer=_preparer)
        result = verifier.verify_manifest(self.manifest())

        self.assertTrue(result.passed, result.to_dict())
        self.assertEqual(source.calls, 1)
        self.assertEqual(len(service.requests), 1)
        self.assertEqual(len(service.requests[0]["questions"]), 16)
        self.assertTrue(service.requests[0]["images_enabled"])
        self.assertEqual(service.requests[0]["body"]["model"], "jev-preview")


class DriverIntegrationTests(unittest.TestCase):
    class Vision:
        def __init__(self, passed=True):
            self.passed = passed
            self.calls = []

        def verify_application(self, app_name, *, step_id="verification"):
            self.calls.append(("application", app_name, step_id))
            return _verification("application", self.passed)

        def verify_text(self, text, *, step_id="verification"):
            self.calls.append(("text", text, step_id))
            return _verification("text", self.passed)

    def _step(self, action, params, *, vision, settings=None):
        audit = MemoryAuditLog()
        settings = settings or load_settings(env={
            "DRIVER_ALLOW_WRITE": "1", "DRIVER_LAUNCH_APP": "1",
            "DRIVER_SCREEN": "true", "DRIVER_VISION_VERIFY": "true",
        }, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
           dry_run=False)

        def backend(kind, value=None, target=None):
            return True, "sent"

        backend.supported_kinds = ("type",)
        registry = build_driver_registry(allow_write=True, launch_app=False,
                                         input_backend=backend)
        registry.register("launch_app", lambda action, params: {
            "app": params["app_name"], "pid": 7})
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=audit, dry_run=settings.dry_run)
        pool = ExtractorPool([
            StructuredExtractor(f"s{i}", lambda capture, schema: dict(STATE))
            for i in range(2)])
        source = StructuredSource("cli", lambda target: dict(STATE))
        driver = Driver(
            settings=settings, audit=audit,
            budget=Budget(1.0, step_ceiling_usd=1.0), pool=pool,
            sources=[source], jev=FakeJev(action_answer(
                action, confidence=0.99)),
            executor=executor, vision=vision)
        consent = Consent(True, action, params)
        return driver.step(Target("target", "cli"), schema=SCREEN_SCHEMA,
                           consent=consent, params=params,
                           step_id="verify-step")

    def test_a_successful_launch_is_followed_by_application_verification(self):
        vision = self.Vision(passed=True)
        result = self._step("launch_app", {"app_name": "freebuff"},
                            vision=vision)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(vision.calls[0][0], "application")
        self.assertEqual(vision.calls[0][1], "freebuff")
        self.assertEqual(result.verification.kind, "application")
        self.assertTrue(result.verification.passed)

    def test_a_failed_launch_verification_blocks_the_step(self):
        vision = self.Vision(passed=False)
        result = self._step("launch_app", {"app_name": "freebuff"},
                            vision=vision)
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_at, "verification")
        self.assertEqual(result.reason, "verification_failed")
        self.assertIn("application", result.detail)
        self.assertEqual(result.execution.params["app_name"], "freebuff")

    def test_typed_text_is_followed_by_text_verification(self):
        vision = self.Vision(passed=True)
        result = self._step("type_text", {"text": "hello"}, vision=vision)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(vision.calls[0][0], "text")
        self.assertEqual(vision.calls[0][1], "hello")

    def test_a_vision_free_driver_still_completes_the_action(self):
        vision = None
        settings = load_settings(env={
            "DRIVER_ALLOW_WRITE": "1", "DRIVER_LAUNCH_APP": "1",
        }, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
           dry_run=False)
        result = self._step("launch_app", {"app_name": "freebuff"},
                            vision=vision, settings=settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertIsNone(result.verification)

    def test_a_dry_run_is_not_verified(self):
        vision = self.Vision(passed=True)
        settings = load_settings(env={
            "DRIVER_ALLOW_WRITE": "1", "DRIVER_LAUNCH_APP": "1",
        }, quorum=2, min_agreement=1.0, confidence_threshold=0.7,
           dry_run=True)
        result = self._step("launch_app", {"app_name": "freebuff"},
                            vision=vision, settings=settings)
        self.assertTrue(result.ok)
        self.assertEqual(vision.calls, [])
        self.assertIsNone(result.verification)


def _verification(kind, passed):
    from driver_core.vision_verify import Verification
    return Verification(
        kind, passed=passed, attempts=1, threshold=0.85, timed_out=False,
        reason="" if passed else f"{kind} not visible")


if __name__ == "__main__":
    unittest.main()
