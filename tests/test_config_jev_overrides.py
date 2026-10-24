"""Regression tests for #347: explicit Jev overrides must not resurrect keys.

The Settings contract is exact:

- an omitted ``jev_api_key`` override keeps normal configured/ambient
  resolution;
- an explicitly supplied ``None`` clears the key for that Settings instance;
- ``jev_disabled=True`` always yields no key and no native dispatch;
- explicit ``False`` permits normal resolution unless the key was cleared.

The ambient resolver is patched so these tests never read a real key file or
the process environment for Jev credentials.
"""
import os
import tempfile
import unittest
from unittest import mock

from harness import cli, config
from harness.cli_parser import build_parser


class JevSettingsOverrideTests(unittest.TestCase):
    def _load(self, overrides, *, env=None, ambient="ambient-key"):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "CONFIG_DIR", tmp), \
                 mock.patch.object(config, "resolve_api_key", return_value=None), \
                 mock.patch.object(config, "resolve_fireworks_key", return_value=None), \
                 mock.patch.object(config, "resolve_jev_key",
                                   return_value=ambient) as resolver:
                with mock.patch.dict(os.environ, env or {}, clear=True):
                    settings = config.load_settings(overrides)
        return settings, resolver

    def test_omitted_override_uses_normal_ambient_resolution(self):
        settings, resolver = self._load({})
        self.assertEqual(settings.jev_api_key, "ambient-key")
        self.assertFalse(settings.jev_disabled)
        resolver.assert_called_once_with()

    def test_configured_env_key_is_used_without_ambient_resolution(self):
        settings, resolver = self._load({}, env={"HARNESS_JEV_KEY": "env-key"})
        self.assertEqual(settings.jev_api_key, "env-key")
        resolver.assert_not_called()

    def test_explicit_none_clears_the_key(self):
        settings, resolver = self._load({"jev_api_key": None})
        self.assertIsNone(settings.jev_api_key)
        self.assertFalse(settings.jev_disabled)
        resolver.assert_not_called()

    def test_explicit_disable_wins_even_with_a_configured_key(self):
        settings, resolver = self._load({
            "jev_disabled": True,
            "jev_api_key": "configured-key",
        })
        self.assertTrue(settings.jev_disabled)
        self.assertIsNone(settings.jev_api_key)
        resolver.assert_not_called()

    def test_explicit_false_keeps_a_configured_key(self):
        settings, resolver = self._load({
            "jev_disabled": False,
            "jev_api_key": "configured-key",
        })
        self.assertFalse(settings.jev_disabled)
        self.assertEqual(settings.jev_api_key, "configured-key")
        resolver.assert_not_called()

    def test_explicit_false_does_not_restore_an_explicitly_cleared_key(self):
        settings, resolver = self._load({
            "jev_disabled": False,
            "jev_api_key": None,
        })
        self.assertFalse(settings.jev_disabled)
        self.assertIsNone(settings.jev_api_key)
        resolver.assert_not_called()

    def test_environment_disable_still_works_without_an_override(self):
        settings, resolver = self._load(
            {"jev_api_key": "configured-key"},
            env={"HARNESS_JEV_DISABLE": "1"},
        )
        self.assertTrue(settings.jev_disabled)
        self.assertIsNone(settings.jev_api_key)
        resolver.assert_not_called()

    def test_explicit_false_outranks_the_environment_switch(self):
        settings, resolver = self._load(
            {"jev_disabled": False, "jev_api_key": "configured-key"},
            env={"HARNESS_JEV_DISABLE": "1"},
        )
        self.assertFalse(settings.jev_disabled)
        self.assertEqual(settings.jev_api_key, "configured-key")
        resolver.assert_not_called()


class DisabledPlanPreviewTests(unittest.TestCase):
    """The actual CLI plan-preview path must not dispatch a Jev transport
    when the Settings object is explicitly disabled (#347 acceptance)."""

    def test_disabled_preview_makes_no_provider_transport_call(self):
        settings, resolver = JevSettingsOverrideTests()._load({
            "jev_disabled": True,
            "jev_api_key": "configured-key",
        })
        resolver.assert_not_called()
        opts = build_parser().parse_args([
            "plan", "--goal", "fix a deterministic bug",
            "--no-decompose-llm", "--no-confirm", "--no-plan-consensus",
            "--allow-heuristic-preview",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            settings.ledger_path = os.path.join(tmp, "ledger.jsonl")
            with mock.patch.object(cli, "_emit_by_status"), \
                 mock.patch("harness.jev.HttpTransport.post",
                            side_effect=AssertionError(
                                "disabled settings must not dispatch Jev")):
                cli._cmd_plan(opts, settings)


if __name__ == "__main__":
    unittest.main()
