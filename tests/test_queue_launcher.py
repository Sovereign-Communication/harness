"""The queue's launcher: a queued launch goes through the real launch gate.

The watcher is only useful if the item it claims can actually start something,
and only safe if starting it goes through the gates the driver already owns.
These tests drive the launcher the CLI supplies, so the composition is proven
here rather than at a call site: an item that asks for no launch starts
nothing, a closed gate refuses by name and leaves the item `blocked`, and an
open gate reaches the declared launcher.

The OS hand-off is substituted at ``subprocess.Popen`` so no window opens; the
substitution is the boundary #270's own acceptance names.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.queue_bus as qb
from driver_core.config import APP_ALLOWLIST, load_settings
from driver_core.osal import platform_name
from harness.queue_bus import QueueBus, Watcher, launch_item


class QueueWatchExitCodeTests(unittest.TestCase):
    """`harness queue watch` exit codes: a refused launch is not success."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = str(Path(self._tmp.name) / "queue")

    def _run(self, item_id, payload=None):
        bus = QueueBus(self.root)
        bus.enqueue("harness", item_id, "a lane item", payload=payload)
        closed = {"DRIVER_ALLOW_WRITE": "", "HARNESS_NL_CONTROL": "",
                  "DRIVER_LAUNCH_APP": ""}
        with mock.patch.dict("os.environ", closed, clear=False):
            return qb.run_cli(["watch", "--root", self.root, "--steps", "1",
                               "--authorized"])

    def test_a_refused_launch_exits_one(self):
        # Defaults: write authority off, so the launch cannot be performed.
        self.assertEqual(self._run("launch", {"launch_app": "notepad"}), 1)
        item = QueueBus(self.root).item("harness", "launch")
        self.assertEqual(item.state, qb.BLOCKED)

    def test_an_item_that_asks_for_no_launch_exits_zero(self):
        self.assertEqual(self._run("plain"), 0)
        self.assertEqual(QueueBus(self.root).item("harness", "plain").state,
                         qb.RUNNING)


class _Process:
    pid = 9001

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bus = QueueBus(str(self.root))
        self.spawns = []
        patcher = mock.patch.object(
            subprocess, "Popen",
            lambda argv, **kw: (self.spawns.append((argv, kw)), _Process())[1])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _settings(self, allow_write=True, launch=False, audit=True):
        overrides = {"allow_write": allow_write}
        if audit:
            overrides["audit_path"] = str(self.root / "audit.jsonl")
        return load_settings(env={"DRIVER_LAUNCH_APP": "1"} if launch else {},
                             **overrides)

    def _watcher(self, settings, environ):
        return Watcher(self.bus, launcher=launch_item(settings, environ))

    def test_an_item_that_asks_for_no_launch_starts_nothing(self):
        self.bus.enqueue("harness", "plain-item", "a lane task")
        outcome = self._watcher(self._settings(), {}).step(authorized=True)
        self.assertEqual(outcome.action, Watcher.CLAIMED)
        self.assertIn("no launch requested", outcome.detail)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.bus.item("harness", "plain-item").state, qb.RUNNING)

    def test_a_closed_launch_gate_blocks_the_item_in_the_gates_words(self):
        self.bus.enqueue("harness", "launch-item", "open notepad",
                         payload={"launch_app": "notepad"})
        outcome = self._watcher(self._settings(launch=False), {}).step(
            authorized=True)
        # A refused launch is its own outcome: the item is `blocked`, and a
        # caller must not read it as a claim that started something.
        self.assertEqual(outcome.action, Watcher.BLOCKED)
        self.assertIn("launch failed", outcome.detail)
        self.assertEqual(self.spawns, [])
        item = self.bus.item("harness", "launch-item")
        self.assertEqual(item.state, qb.BLOCKED)
        self.assertIn("not registered", item.reason)

    def test_write_authority_alone_does_not_open_the_gate(self):
        self.bus.enqueue("harness", "launch-item", "open notepad",
                         payload={"launch_app": "notepad"})
        outcome = self._watcher(
            self._settings(allow_write=False, launch=True), {}).step(
                authorized=True)
        self.assertEqual(self.bus.item("harness", "launch-item").state,
                         qb.BLOCKED)
        self.assertIn("launch failed", outcome.detail)
        self.assertEqual(self.spawns, [])

    def test_the_host_flag_opens_the_gate_and_the_declared_launcher_runs(self):
        self.bus.enqueue("harness", "launch-item", "open notepad",
                         payload={"launch_app": "notepad"})
        outcome = self._watcher(
            self._settings(), {"HARNESS_NL_CONTROL": "1"}).step(authorized=True)
        self.assertIn("launched notepad", outcome.detail)
        self.assertEqual(len(self.spawns), 1)
        argv, kwargs = self.spawns[0]
        self.assertIs(kwargs["shell"], False)
        # The declared launcher is per-platform (APP_ALLOWLIST); the gate
        # must hand off this platform's declared value, whatever it is.
        self.assertEqual(argv[-1],
                         APP_ALLOWLIST["notepad"][platform_name()])
        self.assertEqual(self.bus.item("harness", "launch-item").state,
                         qb.RUNNING)

    def test_an_unlisted_app_is_refused_by_the_allowlist_not_launched(self):
        self.bus.enqueue("harness", "launch-item", "open something",
                         payload={"launch_app": "definitely-not-declared"})
        self._watcher(self._settings(), {"HARNESS_NL_CONTROL": "1"}).step(
            authorized=True)
        item = self.bus.item("harness", "launch-item")
        self.assertEqual(item.state, qb.BLOCKED)
        self.assertIn("allowlist", item.reason)
        self.assertEqual(self.spawns, [])


if __name__ == "__main__":
    unittest.main()
