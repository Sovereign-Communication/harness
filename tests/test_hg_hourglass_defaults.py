"""HG-decompose-default: resolve_hourglass owns the decompose switch."""
import unittest
from types import SimpleNamespace

from harness.cli import _resolve_hourglass
from harness.config import resolve_hourglass
from harness.cli_parser import build_parser


class HourglassDecomposeDefaultsTests(unittest.TestCase):
    def _settings(self, **kw):
        base = dict(
            hourglass_confirm=True, hourglass_parallel=True,
            hourglass_isolate=True, hourglass_require_attestation=True)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_decompose_defaults_true_when_hourglass_active(self):
        settings = self._settings()
        resolved = resolve_hourglass(settings)
        self.assertTrue(resolved["confirm"])
        self.assertTrue(resolved["parallel"])
        self.assertTrue(resolved["decompose"])

    def test_cli_and_config_share_one_mapping(self):
        settings = self._settings()
        opts = build_parser().parse_args(["plan", "--goal", "g"])
        self.assertIsNone(opts.decompose_llm)
        cli_resolved = _resolve_hourglass(opts, settings)
        self.assertEqual(cli_resolved, resolve_hourglass(settings))
        self.assertTrue(cli_resolved["decompose"])

    def test_explicit_flag_and_settings_win(self):
        settings = self._settings()
        off_flag = build_parser().parse_args(
            ["plan", "--goal", "g", "--no-decompose-llm"])
        self.assertFalse(_resolve_hourglass(off_flag, settings)["decompose"])
        on_flag = build_parser().parse_args(
            ["plan", "--goal", "g", "--decompose-llm"])
        self.assertTrue(_resolve_hourglass(on_flag, settings)["decompose"])
        settings_off = self._settings(hourglass_decompose=False)
        self.assertFalse(resolve_hourglass(settings_off)["decompose"])

    def test_inactive_hourglass_defaults_decompose_off(self):
        settings = self._settings(
            hourglass_confirm=False, hourglass_parallel=False)
        resolved = resolve_hourglass(settings)
        self.assertFalse(resolved["confirm"])
        self.assertFalse(resolved["parallel"])
        self.assertFalse(resolved["decompose"])

    def test_agent_opts_none_inherits_same_mapping(self):
        """The agent lane passes opts=None and must inherit the same
        decompose default CLI/MCP resolve from the same settings file."""
        settings = self._settings()
        self.assertEqual(resolve_hourglass(settings),
                         _resolve_hourglass(None, settings))
        self.assertTrue(resolve_hourglass(settings)["decompose"])


if __name__ == "__main__":
    unittest.main()
