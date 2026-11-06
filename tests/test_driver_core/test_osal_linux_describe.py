"""Linux screen description: the perception bridge's structured half.

Before this, driver_core.osal.describe_screen refused on Linux by
name ("a platform gap"), so every words-mode driver step on a Linux
desktop stopped no_capture. The reader is xdotool against the X
server, and these tests pin its contract hermetically (the run()
seam is faked; no X server is touched):

* a described window returns the Windows field shape, with the two
  fields that have no honest Linux source through the declared tool
  present and empty rather than invented;
* each absence -- Wayland-only session, no DISPLAY, xdotool missing,
  no active window -- refuses BY NAME, never as an empty success.
"""
import os
import unittest
from unittest import mock

from driver_core import osal


class _Result:
    def __init__(self, stdout="", ok=True, error="", stderr=""):
        self.stdout = stdout
        self.ok = ok
        self.error = error
        self.stderr = stderr


def _fake_run(title="Bridge Probe Window"):
    def run(argv, **kwargs):
        if argv[:2] == ["xdotool", "getactivewindow"] and len(argv) == 2:
            return _Result(stdout="4194308\n")
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return _Result(stdout=title + "\n")
        raise AssertionError(f"unexpected argv: {argv}")
    return run


class DescribeLinuxTests(unittest.TestCase):
    def _env(self, **overrides):
        env = {"DISPLAY": ":99", "WAYLAND_DISPLAY": ""}
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=False)

    def test_describes_the_active_window_in_the_windows_shape(self):
        with self._env(), \
                mock.patch.object(osal.shutil, "which",
                                  return_value="/usr/bin/xdotool"), \
                mock.patch.object(osal, "run", _fake_run()):
            fields, why = osal._describe_linux()
        self.assertEqual(why, "")
        self.assertEqual(fields["window_title"], "Bridge Probe Window")
        self.assertIn("foreground_app", fields)
        self.assertIn("window_class", fields)
        self.assertEqual(fields["foreground_app"], "")
        self.assertEqual(fields["window_class"], "")

    def test_wayland_only_session_refuses_by_name(self):
        with self._env(DISPLAY="", WAYLAND_DISPLAY="wayland-1"):
            fields, why = osal._describe_linux()
        self.assertIsNone(fields)
        self.assertIn("Wayland", why)

    def test_no_display_refuses_by_name(self):
        with self._env(DISPLAY="", WAYLAND_DISPLAY=""):
            fields, why = osal._describe_linux()
        self.assertIsNone(fields)
        self.assertIn("DISPLAY", why)

    def test_missing_xdotool_refuses_by_name(self):
        with self._env(), \
                mock.patch.object(osal.shutil, "which", return_value=None):
            fields, why = osal._describe_linux()
        self.assertIsNone(fields)
        self.assertIn("xdotool", why)

    def test_no_active_window_refuses_by_name(self):
        def run(argv, **kwargs):
            return _Result(ok=False, stderr="no window")
        with self._env(), \
                mock.patch.object(osal.shutil, "which",
                                  return_value="/usr/bin/xdotool"), \
                mock.patch.object(osal, "run", run):
            fields, why = osal._describe_linux()
        self.assertIsNone(fields)
        self.assertIn("no active window", why)

    def test_describe_screen_dispatches_to_linux(self):
        with mock.patch.object(osal, "platform_name",
                               return_value="linux"), \
                mock.patch.object(osal, "_describe_linux",
                                  return_value=({"window_title": "T"}, "")) \
                as describe:
            fields, why = osal.describe_screen()
        self.assertEqual(fields, {"window_title": "T"})
        describe.assert_called_once_with()


class CaptureLinuxToolChainTests(unittest.TestCase):
    def test_falls_back_to_scrot_when_gnome_screenshot_absent(self):
        def which(tool):
            return "/usr/bin/scrot" if tool == "scrot" else None

        def run(argv, **kwargs):
            return _Result(ok=True)
        with mock.patch.dict(os.environ,
                             {"DRIVER_SCREEN_OUT": "/tmp/x.png"}), \
                mock.patch.object(osal.shutil, "which", which), \
                mock.patch.object(osal, "run", run), \
                mock.patch.object(osal.os.path, "exists",
                                  return_value=True):
            path, detail = osal._capture_linux()
        self.assertEqual(path, "/tmp/x.png")

    def test_no_capture_tool_refuses_naming_both(self):
        with mock.patch.dict(os.environ,
                             {"DRIVER_SCREEN_OUT": "/tmp/x.png"}), \
                mock.patch.object(osal.shutil, "which",
                                  return_value=None):
            path, detail = osal._capture_linux()
        self.assertIsNone(path)
        self.assertIn("gnome-screenshot", detail)
        self.assertIn("scrot", detail)
