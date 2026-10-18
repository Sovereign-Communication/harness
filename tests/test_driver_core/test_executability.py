"""Declared is not executable: the vocabulary says so per action.

Hermetic: every collaborator is a fake, the input backend is a recording
double, and the real platform backend table is restored after each test.
"""
import unittest
from unittest import mock

from driver_core import osal
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.errors import VocabularyError
from driver_core.ev import FakeJev, action_answer
from driver_core.executor import Consent, Executor
from driver_core.executor_registry import (
    INPUT_BACKED, ExecutorRegistry, build_driver_registry, executability,
)
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import StructuredSource
from driver_core.server import Service

STATE = {"window_title": "report - editor", "foreground_app": "editor",
         "error_dialog_present": False}


def _driver(*, allow_write, sources=None, action="observe",
            input_backend=osal.USE_REGISTERED_INPUT_BACKEND):
    settings = load_settings(env={}, quorum=2, min_agreement=1.0,
                            confidence_threshold=0.7, run_ceiling_usd=1.0,
                            step_ceiling_usd=1.0, dry_run=False,
                            allow_write=allow_write)
    pool = ExtractorPool([StructuredExtractor(
        f"s{i}", lambda capture, schema: dict(STATE)) for i in range(2)])
    if sources is None:
        sources = [StructuredSource("screen", lambda t: dict(STATE),
                                    serves=("gui",))]
    return Driver(settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
                  audit=MemoryAuditLog(), pool=pool, sources=sources,
                  jev=FakeJev(action_answer(action, confidence=0.95)),
                  input_backend=input_backend)


class _NoBackend(unittest.TestCase):
    def setUp(self):
        saved = osal.input_backends()
        self.addCleanup(self._restore, saved)
        for name in list(saved):
            osal.disable_input_backend(name)

    @staticmethod
    def _restore(saved):
        for name in list(osal.input_backends()):
            osal.disable_input_backend(name)
        for name, backend in saved.items():
            osal.register_input_backend(name, backend)


class VocabularyExecutabilityTests(_NoBackend):

    def _by_name(self, driver):
        view = Service(driver, token="t").handle("vocabulary", {})["body"]
        return {a["name"]: a for a in view["vocabulary"]["actions"]}

    def test_read_only_actions_are_executable_and_inputs_are_not(self):
        actions = self._by_name(_driver(allow_write=False))
        for name in ("observe", "read_value", "no_action"):
            self.assertTrue(actions[name]["executable"], name)
            self.assertFalse(actions[name]["refused_until_backend"], name)
            self.assertEqual(actions[name]["needs"], [])
        for name in ("click", "type_text", "press_key", "focus", "scroll",
                     "submit_irreversible", "write_file", "delete_file"):
            self.assertFalse(actions[name]["executable"], name)
            self.assertTrue(actions[name]["refused_until_backend"], name)
            self.assertTrue(actions[name]["needs"], name)

    def test_write_switch_enables_files_but_not_input(self):
        actions = self._by_name(_driver(allow_write=True))
        self.assertTrue(actions["write_file"]["executable"])
        self.assertTrue(actions["delete_file"]["executable"])
        self.assertFalse(actions["click"]["executable"])
        self.assertIn("input backend", " ".join(actions["click"]["needs"]))

    def test_native_backend_is_an_explicit_driver_local_capability(self):
        def backend(kind, value=None, target=None):
            return True, "focused"

        backend.supported_kinds = ("focus",)
        settings = load_settings(env={}, allow_write=True,
                                 native_input_enabled=True)
        with mock.patch.object(osal, "native_input_backend",
                               return_value=backend) as make_backend:
            driver = Driver(settings=settings, audit=MemoryAuditLog())
        make_backend.assert_called_once_with()
        actions = self._by_name(driver)
        self.assertTrue(actions["focus"]["executable"])
        self.assertFalse(actions["click"]["executable"])
        self.assertIn("does not support", " ".join(actions["click"]["needs"]))
        self.assertEqual(osal.input_backends(), {})

    def test_native_backend_environment_setting_is_opt_in(self):
        self.assertFalse(load_settings(env={}).native_input_enabled)
        self.assertTrue(load_settings(
            env={"DRIVER_NATIVE_INPUT": "1"}).native_input_enabled)

    def test_registering_a_backend_makes_input_executable(self):
        osal.register_input_backend(osal.platform_name(),
                                    lambda kind, value=None, target=None:
                                    (True, "ok"))
        actions = self._by_name(_driver(allow_write=True))
        for name in INPUT_BACKED:
            self.assertTrue(actions[name]["executable"], name)

    def test_every_vocabulary_action_is_tagged_and_health_agrees(self):
        service = Service(_driver(allow_write=False), token="t")
        health = service.handle("health", {})["body"]["vocabulary"]["actions"]
        self.assertEqual(len(health), len(DEFAULT_VOCABULARY))
        for entry in health:
            self.assertIn("executable", entry)

    def test_executability_helper_names_the_missing_executor(self):
        registry = build_driver_registry(allow_write=False)
        click = DEFAULT_VOCABULARY.resolve("click")
        ok, needs = executability(click, registry)
        self.assertFalse(ok)
        self.assertIn("DRIVER_ALLOW_WRITE", needs[0])


class InputStepTests(_NoBackend):
    """An input action runs only with a registered backend AND consent."""

    def _click(self, consent):
        return _driver(allow_write=True, action="click").step(
            "t", schema=None, consent=consent, params={"target": "OK"})

    def test_click_with_backend_and_consent_executes(self):
        seen = []

        def backend(kind, value=None, target=None):
            seen.append((kind, target))
            return True, "done"
        osal.register_input_backend(osal.platform_name(), backend)
        result = self._click(Consent(True, "click", {"target": "OK"}))
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.stopped_at, "executed")
        self.assertEqual(seen, [("click", "OK")])
        self.assertEqual(result.needs, [])

    def test_click_without_consent_refuses_and_never_reaches_the_backend(self):
        seen = []
        osal.register_input_backend(
            osal.platform_name(),
            lambda kind, value=None, target=None: seen.append(kind) or (True, ""))
        result = self._click(None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertEqual(seen, [])

    def test_click_without_backend_refuses_and_says_what_is_needed(self):
        result = self._click(Consent(True, "click", {"target": "OK"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("no registered backend", result.detail)
        self.assertIn("input backend", " ".join(result.needs))

    def test_no_capture_envelope_lists_what_would_be_needed(self):
        driver = _driver(allow_write=False, sources=[])
        result = driver.step("t", schema=None)
        self.assertEqual(result.reason, "no_capture")
        wire = result.to_dict()
        text = " ".join(wire["needs"])
        self.assertIn("configure a perception source", text)
        self.assertIn("DRIVER_CLI_COMMAND", text)
        self.assertIn("input backend", text)
        self.assertIn("click", text)

    def test_needs_present_and_empty_on_a_successful_step(self):
        result = _driver(allow_write=False).step("t")
        self.assertTrue(result.ok)
        self.assertEqual(result.to_dict()["needs"], [])


class InputCapabilityBoundaryTests(_NoBackend):

    def test_backend_capabilities_cannot_widen_out_of_sync(self):
        calls = []

        def backend(kind, value=None, target=None):
            calls.append((kind, value, target))
            return True, "sent"

        backend.supported_kinds = ("focus",)
        driver = _driver(allow_write=True, action="click",
                         input_backend=backend)

        def broader_backend(kind, value=None, target=None):
            calls.append(("broader", kind))
            return True, "sent"

        broader_backend.supported_kinds = ("focus", "click")
        registry = driver.executor.registry
        with self.assertRaises(AttributeError):
            registry.input_backend = broader_backend

        actions = Service(driver, token="t").handle(
            "vocabulary", {})["body"]["vocabulary"]["actions"]
        click = next(action for action in actions if action["name"] == "click")
        self.assertFalse(click["executable"])
        self.assertIn("does not support 'click'", " ".join(click["needs"]))

        # Mutating the original backend's declared kinds cannot widen dispatch.
        backend.supported_kinds = ("focus", "click")
        result = driver.step(
            "test", consent=Consent(True, "click", {"target": "OK"}),
            params={"target": "OK"})

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("does not support 'click'", result.detail)
        self.assertEqual(calls, [])
        with self.assertRaises(AttributeError):
            driver.executor.registry.input_backend._supported_kinds = (
                "focus", "click")

    def test_every_input_executor_obeys_the_backend_capability_set(self):
        calls = []

        def backend(kind, value=None, target=None):
            calls.append((kind, value, target))
            return True, "sent"

        backend.supported_kinds = ("focus",)
        executor = Executor(
            vocabulary=DEFAULT_VOCABULARY,
            registry=build_driver_registry(allow_write=True,
                                           input_backend=backend),
            audit=MemoryAuditLog())
        cases = (
            ("click", {"target": "OK"}, "click"),
            ("type_text", {"text": "hello"}, "type"),
            ("press_key", {"keys": "ENTER"}, "key"),
            ("focus", {"target": "window"}, "focus"),
            ("scroll", {"direction": "down"}, "scroll"),
            ("submit_irreversible", {"target": "Publish"}, "submit"),
        )

        for action, params, kind in cases:
            with self.subTest(action=action):
                consent = Consent(True, action, params)
                result = executor.execute(action, params, consent=consent)
                self.assertEqual(result.ok, action == "focus")
                if action == "focus":
                    self.assertEqual(result.output["kind"], "focus")
                else:
                    self.assertIn(f"does not support '{kind}'", result.detail)
                if action == "submit_irreversible":
                    self.assertEqual(consent.spent, frozenset())

        self.assertEqual(calls, [("focus", None, "window")])

    def test_executor_refuses_an_unsupported_action_before_a_custom_handler(self):
        calls = []

        def backend(kind, value=None, target=None):
            calls.append(("backend", kind))
            return True, "sent"

        backend.supported_kinds = ("focus",)
        registry = ExecutorRegistry(
            {"click": lambda action, params: calls.append(("handler", params))},
            input_backend=backend)
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=MemoryAuditLog())

        result = executor.execute(
            "click", {"target": "OK"},
            consent=Consent(True, "click", {"target": "OK"}))

        self.assertFalse(result.ok)
        self.assertIn("does not support 'click'", result.detail)
        self.assertEqual(calls, [])

    def test_missing_backend_refuses_even_in_dry_run(self):
        calls = []
        registry = ExecutorRegistry(
            {"click": lambda action, params: calls.append(params)},
            input_backend=None)
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            dry_run=True, audit=MemoryAuditLog())

        result = executor.execute(
            "click", {"target": "OK"},
            consent=Consent(True, "click", {"target": "OK"}))

        self.assertFalse(result.ok)
        self.assertIn("no registered backend", result.detail)
        self.assertEqual(calls, [])

    def test_empty_action_parameters_fail_before_backend_selection(self):
        calls = []

        def backend(kind, value=None, target=None):
            calls.append(kind)
            return True, "sent"

        registry = ExecutorRegistry({"click": lambda action, params: None},
                                    input_backend=backend)
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=MemoryAuditLog())

        with self.assertRaises(VocabularyError) as ctx:
            executor.execute("click", {}, consent=Consent(True, "click", {}))

        self.assertIn("missing required parameter", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_dispatch_rechecks_capability_after_backend_replacement(self):
        called = []

        def initially_supported(kind, value=None, target=None):
            called.append(("initial", kind))
            return True, "sent"

        initially_supported.supported_kinds = ("click",)

        def replacement(kind, value=None, target=None):
            called.append(("replacement", kind))
            return True, "sent"

        replacement.supported_kinds = ("focus",)
        platform = osal.platform_name()
        osal.register_input_backend(platform, initially_supported)
        original_send_input = osal.send_input

        def replace_before_dispatch(kind, value=None, *, target=None,
                                    backend=osal.USE_REGISTERED_INPUT_BACKEND):
            osal.register_input_backend(platform, replacement)
            return original_send_input(kind, value, target=target,
                                       backend=backend)

        driver = _driver(allow_write=True, action="click")
        with mock.patch.object(osal, "send_input", replace_before_dispatch):
            result = driver.step(
                "test", consent=Consent(True, "click", {"target": "OK"}),
                params={"target": "OK"})

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("does not support 'click'", result.detail)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
