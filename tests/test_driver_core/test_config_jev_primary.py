"""Tests for #453 S1: the ``jev_primary_mode`` settings flag (driver_core).

The flag declares that judgment surfaces route Jev-first (consumers land
in the harness #453 S3 re-pointing; S1 is the settings surface only). It
resolves from DRIVER_JEV_PRIMARY_MODE through the same _env_bool helper
as jev_ladder / jev_guardrails, defaults off, can be flipped through
with_overrides, and is carried by redacted() (it is not a secret).
"""
import unittest

from driver_core.config import Settings, load_settings


class JevPrimaryModeTests(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(Settings().jev_primary_mode)
        self.assertFalse(load_settings(env={}).jev_primary_mode)

    def test_environment_enables(self):
        settings = load_settings(env={"DRIVER_JEV_PRIMARY_MODE": "1"})
        self.assertTrue(settings.jev_primary_mode)

    def test_environment_explicit_off(self):
        settings = load_settings(env={"DRIVER_JEV_PRIMARY_MODE": "0"})
        self.assertFalse(settings.jev_primary_mode)

    def test_override_flips_via_load_settings(self):
        settings = load_settings(env={}, jev_primary_mode=True)
        self.assertTrue(settings.jev_primary_mode)

    def test_with_overrides_flips(self):
        settings = Settings().with_overrides(jev_primary_mode=True)
        self.assertTrue(settings.jev_primary_mode)

    def test_redacted_carries_the_flag(self):
        self.assertFalse(Settings().redacted()["jev_primary_mode"])
        settings = Settings().with_overrides(jev_primary_mode=True)
        self.assertTrue(settings.redacted()["jev_primary_mode"])


if __name__ == "__main__":
    unittest.main()
