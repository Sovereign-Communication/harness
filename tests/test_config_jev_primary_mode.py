"""Tests for #453 S1: the ``jev_primary_mode`` settings flag (harness side).

The flag declares that judgment surfaces route Jev-first (the mode's
consumers land in S3; S1 is the settings surface only). Resolution follows
the standard load_settings contract: explicit override wins, then the
HARNESS_JEV_PRIMARY_MODE environment switch, then the config file, then
the default False. The flag is load-time only: it is not in the
update_config runtime allow-list and not in JEV_FREEZE_KEYS.

The ambient key resolvers are patched so these tests never read a real
key file or the process environment for credentials.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import config


class JevPrimaryModeResolutionTests(unittest.TestCase):
    def _load(self, overrides=None, *, env=None, cfg=None):
        with tempfile.TemporaryDirectory() as tmp:
            if cfg is not None:
                with open(os.path.join(tmp, "config.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(cfg, f)
            with mock.patch.object(config, "CONFIG_DIR", tmp), \
                 mock.patch.object(config, "resolve_api_key",
                                   return_value=None), \
                 mock.patch.object(config, "resolve_fireworks_key",
                                   return_value=None), \
                 mock.patch.object(config, "resolve_jev_key",
                                   return_value="ambient-key"):
                with mock.patch.dict(os.environ, env or {}, clear=True):
                    return config.load_settings(overrides)

    def test_default_is_off(self):
        settings = self._load({})
        self.assertFalse(settings.jev_primary_mode)

    def test_explicit_override_true(self):
        settings = self._load({"jev_primary_mode": True})
        self.assertTrue(settings.jev_primary_mode)

    def test_explicit_override_false(self):
        settings = self._load({"jev_primary_mode": False})
        self.assertFalse(settings.jev_primary_mode)

    def test_environment_switch_enables(self):
        settings = self._load({}, env={"HARNESS_JEV_PRIMARY_MODE": "1"})
        self.assertTrue(settings.jev_primary_mode)

    def test_environment_zero_keeps_off(self):
        settings = self._load({}, env={"HARNESS_JEV_PRIMARY_MODE": "0"})
        self.assertFalse(settings.jev_primary_mode)

    def test_config_file_value_is_honored(self):
        settings = self._load({}, cfg={"jev_primary_mode": True})
        self.assertTrue(settings.jev_primary_mode)

    def test_explicit_override_outranks_environment(self):
        settings = self._load(
            {"jev_primary_mode": False},
            env={"HARNESS_JEV_PRIMARY_MODE": "1"})
        self.assertFalse(settings.jev_primary_mode)

    def test_flag_does_not_touch_key_resolution(self):
        settings = self._load({"jev_primary_mode": True})
        self.assertEqual(settings.jev_api_key, "ambient-key")
        self.assertFalse(settings.jev_disabled)

    def test_to_dict_carries_the_flag(self):
        settings = self._load({"jev_primary_mode": True})
        self.assertTrue(settings.to_dict()["jev_primary_mode"])

    def test_flag_is_not_runtime_updatable_or_frozen(self):
        self.assertNotIn("jev_primary_mode", config.JEV_FREEZE_KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "CONFIG_DIR", tmp):
                with self.assertRaises(config.HarnessError):
                    config.update_config({"jev_primary_mode": True})


if __name__ == "__main__":
    unittest.main()
