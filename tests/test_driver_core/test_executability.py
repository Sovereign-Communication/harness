"""Declared is not executable: the vocabulary says so per action.

Hermetic: every collaborator is a fake, the input backend is a recording
double, and the real platform backend table is restored after each test.
"""
import unittest

from driver_core import osal
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeJev, action_answer
from driver_core.executor import Consent
from driver_core.executor_registry import (
    INPUT_BACKED, build_driver_registry, executability,
)
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import StructuredSource
from driver_core.server import Service

STATE = {"window_title": "report - editor", "foreground_app": "editor",
         "error_dialog_present": False}


def _driver(*, allow_write, sources=None, action="observe"):
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
                  jev=FakeJev(action_answer(action, confidence=0.95)))


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


if __name__ == "__main__":
    unittest.main()
