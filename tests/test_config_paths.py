import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.paths import get_config_dir


class HarnessConfigPathTests(unittest.TestCase):
    def test_default_and_override_paths_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            expected = os.path.abspath(os.path.join(temp, ".config", "harness"))
            with patch.dict(os.environ, {"HOME": temp, "USERPROFILE": temp}, clear=True):
                self.assertEqual(get_config_dir(), expected)

            with patch.dict(os.environ, {
                "HOME": temp,
                "USERPROFILE": temp,
                "HARNESS_CONFIG_DIR": "~/harness-state",
            }, clear=True):
                self.assertEqual(get_config_dir(), os.path.abspath(os.path.join(temp, "harness-state")))

    def test_config_credentials_history_ui_and_adapters_share_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "isolated-state"
            home = Path(temp) / "isolated-home"
            root.mkdir()
            home.mkdir()
            (root / "openrouter.env").write_text(
                "OPENROUTER_API_KEY=fixture-openrouter-key\n", encoding="utf-8")
            (root / "jev.env").write_text(
                "HARNESS_JEV_KEY=fixture-jev-key\n", encoding="utf-8")
            (root / "config.json").write_text(
                '{"use_free": false}\n', encoding="utf-8")

            env = os.environ.copy()
            for key in (
                "OPENROUTER_API_KEY", "HARNESS_JEV_KEY", "TYPESAFE_API_KEY",
                "JEV_API_KEY", "MEDIA_CONFIG_PATH", "DRIVER_CONFIG_PATH",
            ):
                env.pop(key, None)
            env.update({
                "HARNESS_CONFIG_DIR": str(root),
                "HOME": str(home),
                "USERPROFILE": str(home),
            })

            script = r"""
import os
from harness.config import (CONFIG_DIR, load_settings, resolve_api_key,
                            resolve_api_key_with_source, resolve_jev_key)
from harness.history import get_default_history_dir
from harness.media_client import CONFIG_PATH as MEDIA_CONFIG_PATH
from harness.perception_client import _config_path as driver_config_path
from harness.ui_auth import load_or_create_ui_token

root = os.path.abspath(os.environ["HARNESS_CONFIG_DIR"])
def same(path, expected):
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(expected)

assert same(CONFIG_DIR, root)
assert load_settings().use_free is False
assert resolve_api_key() == "fixture-openrouter-key"
key, source = resolve_api_key_with_source()
assert key == "fixture-openrouter-key"
assert source == {"kind": "file", "path": os.path.join(root, "openrouter.env")}
assert resolve_jev_key() == "fixture-jev-key"
assert same(get_default_history_dir(), os.path.join(root, "chat_history"))
assert same(MEDIA_CONFIG_PATH, os.path.join(root, "media.json"))
assert same(driver_config_path(), os.path.join(root, "driver.json"))
token = load_or_create_ui_token()
assert token
"""
            project_root = Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue((root / "desktop_token").is_file())


if __name__ == "__main__":
    unittest.main()
