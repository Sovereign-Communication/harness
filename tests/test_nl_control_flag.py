"""The host gate #270 names: ``HARNESS_NL_CONTROL``, default off.

The driver keeps its own switch (``DRIVER_LAUNCH_APP`` / ``launch_app_enabled``)
and the daemon maps the host flag onto it. That mapping has no other test: the
driver-side gate is covered in ``tests/test_driver_core/test_launch_app.py``,
but nothing exercised the host flag itself, so an inverted or always-on
mapping would still have passed every suite.

The second property here is that the host flag is not a substitute for the
other gates: enabling it does not confer write authority, and write authority
does not confer launching.
"""
import unittest

from harness.server import nl_control_enabled
from driver_core.config import load_settings
from driver_core.executor_registry import LAUNCH_DISABLED_NOTE, build_driver_registry


class HostFlagTests(unittest.TestCase):
    def test_unset_means_off(self):
        self.assertFalse(nl_control_enabled({}))

    def test_explicit_off_values_mean_off(self):
        for value in ("", " ", "0", "false", "FALSE", "no", "off", " Off ",
                      "0\u00a0"):
            with self.subTest(value=value):
                self.assertFalse(
                    nl_control_enabled({"HARNESS_NL_CONTROL": value}))

    def test_an_affirmative_value_turns_it_on(self):
        for value in ("1", "true", "TRUE", "yes", "on", " enabled "):
            with self.subTest(value=value):
                self.assertTrue(
                    nl_control_enabled({"HARNESS_NL_CONTROL": value}))


class GateCompositionTests(unittest.TestCase):
    def test_the_host_flag_reaches_the_driver_switch(self):
        off = load_settings(env={})
        self.assertFalse(off.launch_app_enabled)
        on = off.with_overrides(
            launch_app_enabled=(off.launch_app_enabled
                                or nl_control_enabled({"HARNESS_NL_CONTROL": "1"})))
        self.assertTrue(on.launch_app_enabled)

    def test_the_host_flag_does_not_confer_write_authority(self):
        settings = load_settings(env={}).with_overrides(
            launch_app_enabled=nl_control_enabled({"HARNESS_NL_CONTROL": "1"}))
        self.assertFalse(settings.allow_write)
        registry = build_driver_registry(allow_write=settings.allow_write,
                                         launch_app=settings.launch_app_enabled)
        self.assertNotIn("launch_app", registry.names())

    def test_off_by_default_at_the_registry(self):
        settings = load_settings(env={}).with_overrides(
            launch_app_enabled=nl_control_enabled({}))
        registry = build_driver_registry(allow_write=True,
                                         launch_app=settings.launch_app_enabled)
        self.assertNotIn("launch_app", registry.names())
        self.assertEqual(registry.note_for("launch_app"), LAUNCH_DISABLED_NOTE)

    def test_on_with_write_authority_registers_it(self):
        settings = load_settings(env={}).with_overrides(
            allow_write=True,
            launch_app_enabled=nl_control_enabled({"HARNESS_NL_CONTROL": "1"}))
        registry = build_driver_registry(allow_write=settings.allow_write,
                                         launch_app=settings.launch_app_enabled)
        self.assertIn("launch_app", registry.names())


if __name__ == "__main__":
    unittest.main()
