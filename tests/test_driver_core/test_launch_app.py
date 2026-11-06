"""launch_app: a declared application, a declared launcher, a bounded spawn.

The action exists so a computer-control scenario has one way to open a
program. The properties that matter are the ones a launch can get wrong: an
application the code did not declare must never start, the platform spelling
must be the declared one (never a shell string), and a launcher that does not
hand off inside three seconds must fail rather than occupy the step.

Every test here is hermetic: the launcher and the platform answer are patched,
so no process starts and no window appears.
"""
import subprocess
import unittest
from unittest import mock

import driver_core.osal as osal
from driver_core.actions import DEFAULT_VOCABULARY, MUTATING
from driver_core.audit import MemoryAuditLog
from driver_core.config import APP_ALLOWLIST, load_settings
from driver_core.driver import Driver
from driver_core.errors import ConsentError, ExecutorError, VocabularyError
from driver_core.executor import Executor
from driver_core.executor_registry import (
    LAUNCH_DISABLED_NOTE, _launch_app, build_driver_registry,
)


class _Process:
    def __init__(self, *, hang=False, pid=4242):
        self.pid = pid
        self.hang = hang
        self.killed = False

    def wait(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired(["launcher"], timeout)
        return 0

    def kill(self):
        self.killed = True


def _capture(process=None):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process or _Process()

    return calls, popen


class LaunchDeclarationTests(unittest.TestCase):
    def test_launch_app_is_one_mutating_declared_parameter(self):
        action = DEFAULT_VOCABULARY.resolve("launch_app")
        self.assertEqual(action.action_class, MUTATING)
        self.assertEqual(action.params, ("app_name",))
        self.assertTrue(action.requires_consent)
        self.assertFalse(action.requires_human_confirmation)
        with self.assertRaises(VocabularyError):
            action.check_params({"app_name": "notepad", "path": "/tmp/x"})

    def test_the_allowlist_is_code_owned_and_names_every_platform(self):
        self.assertEqual(sorted(APP_ALLOWLIST), ["freebuff", "notepad"])
        for name, platforms in APP_ALLOWLIST.items():
            self.assertEqual(sorted(platforms),
                             ["darwin", "linux", "windows"], name)


class LaunchDispatchTests(unittest.TestCase):
    def _launch(self, platform, app="notepad", process=None):
        calls, popen = _capture(process)
        action = DEFAULT_VOCABULARY.resolve("launch_app")
        with mock.patch.object(osal, "platform_name",
                               return_value=platform), \
                mock.patch.object(subprocess, "Popen", popen):
            output = _launch_app(action, {"app_name": app})
        return output, calls

    def test_windows_uses_start_with_an_empty_title_and_no_shell(self):
        output, calls = self._launch("windows")
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["cmd", "/c", "start", "", "notepad.exe"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["creationflags"], osal.CREATE_NO_WINDOW)
        self.assertEqual(output["app"], "notepad")
        self.assertEqual(output["pid"], 4242)

    def test_macos_and_linux_use_their_declared_launchers(self):
        _output, mac = self._launch("darwin")
        self.assertEqual(mac[0][0], ["open", "-a", "TextEdit"])
        _output, linux = self._launch("linux")
        self.assertEqual(linux[0][0], ["xdg-open", "org.gnome.TextEditor"])

    def test_the_declared_application_value_is_what_reaches_the_launcher(self):
        _output, calls = self._launch("windows", app="freebuff")
        self.assertEqual(calls[0][0], ["cmd", "/c", "start", "",
                                       "FreeBuff.exe"])

    def test_an_unlisted_app_is_refused_with_the_declared_set(self):
        action = DEFAULT_VOCABULARY.resolve("launch_app")
        with self.assertRaisesRegex(
                ExecutorError, "not in the declared launch allowlist") as raised:
            _launch_app(action, {"app_name": "definitely-not-declared"})
        self.assertIn("notepad", str(raised.exception))
        self.assertIn("freebuff", str(raised.exception))

    def test_an_app_without_a_declaration_for_this_platform_is_refused(self):
        action = DEFAULT_VOCABULARY.resolve("launch_app")
        with mock.patch.object(osal, "platform_name",
                               return_value="unknown:plan9"), \
                mock.patch.dict(APP_ALLOWLIST,
                                {"notepad": {"windows": "notepad.exe"}},
                                clear=True):
            with self.assertRaisesRegex(ExecutorError,
                                        "no launch declaration for unknown:plan9"):
                _launch_app(action, {"app_name": "notepad"})

    def test_a_launcher_that_does_not_hand_off_is_killed(self):
        process = _Process(hang=True)
        with self.assertRaisesRegex(ExecutorError, "did not hand off"):
            self._launch("windows", process=process)
        self.assertTrue(process.killed)

    def test_launching_is_unsupported_on_an_unknown_platform(self):
        with mock.patch.object(osal, "platform_name",
                               return_value="unknown:plan9"):
            output, detail = osal.launch_app("notepad", "notepad.exe")
        self.assertIsNone(output)
        self.assertIn("platform gap", detail)

    def test_an_empty_declaration_is_refused(self):
        output, detail = osal.launch_app("notepad", "")
        self.assertIsNone(output)
        self.assertIn("no launch declaration", detail)


class LaunchGateTests(unittest.TestCase):
    def test_off_by_default_and_the_second_gate_is_named(self):
        self.assertFalse(load_settings(env={}).launch_app_enabled)
        self.assertTrue(
            load_settings(env={"DRIVER_LAUNCH_APP": "1"}).launch_app_enabled)

        registry = build_driver_registry(allow_write=True)
        self.assertNotIn("launch_app", registry.names())
        self.assertEqual(registry.note_for("launch_app"), LAUNCH_DISABLED_NOTE)
        self.assertIn("DRIVER_LAUNCH_APP", LAUNCH_DISABLED_NOTE)

    def test_enabling_it_registers_the_executor(self):
        registry = build_driver_registry(allow_write=True, launch_app=True)
        self.assertIn("launch_app", registry.names())

    def test_write_authority_alone_does_not_register_it(self):
        registry = build_driver_registry(allow_write=False, launch_app=True)
        self.assertNotIn("launch_app", registry.names())
        self.assertIn("DRIVER_ALLOW_WRITE", registry.note_for("launch_app"))

    def test_launch_app_still_requires_consent(self):
        registry = build_driver_registry(allow_write=True, launch_app=True)
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=MemoryAuditLog())
        with self.assertRaises(ConsentError):
            executor.execute("launch_app", {"app_name": "notepad"})

    def test_driver_settings_wire_the_flag_into_the_registry(self):
        driver = Driver(
            settings=load_settings(env={"DRIVER_ALLOW_WRITE": "1",
                                        "DRIVER_LAUNCH_APP": "1"}),
            audit=MemoryAuditLog())
        self.assertIn("launch_app", driver.executor.registry.names())


if __name__ == "__main__":
    unittest.main()
