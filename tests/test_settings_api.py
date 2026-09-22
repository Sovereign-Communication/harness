"""Hermetic tests for the UI's runtime settings seam (POST /api/settings).

The endpoint persists routing posture (use_free), escalation arming
(allow_escalation), and the run cost ceiling (max_cost) into config.json via
``update_config`` -- the ONE config owner. These tests pin the contract the
GUI depends on: updates are validated fail-closed before any write, unknown
or non-runtime-updatable keys are refused, env precedence is preserved, the
write is atomic, and the response carries the post-write truth the UI
re-renders from.

CONFIG_DIR is patched to a temp dir per test: the operator's real
~/.config/harness/config.json is never touched, and env pins for the touched
keys are cleared so the tests stay hermetic on configured machines.
"""
import json
import os
from unittest import mock

from tests.test_server import ServerHarness, _request

_TOUCHED_ENV = ["HARNESS_USE_FREE", "HARNESS_ALLOW_ESCALATION",
                "HARNESS_MAX_COST", "HARNESS_TASK_MAX_COST"]


class SettingsApiHarness(ServerHarness):
    """ServerHarness with an isolated CONFIG_DIR and no env pins."""

    def setUp(self):
        tmp = _tmp_dir()
        self._tmp_patch = mock.patch("harness.config.CONFIG_DIR", tmp)
        self._tmp_patch.start()
        self.addCleanup(self._tmp_patch.stop)
        # Save/pop/restore: the operator env may pin these (HARNESS_USE_FREE
        # etc.); the settings seam must be tested with no env pins present.
        self._saved_env = {k: os.environ.get(k) for k in _TOUCHED_ENV}
        self.addCleanup(self._restore_env)
        for k in _TOUCHED_ENV:
            os.environ.pop(k, None)
        super().setUp()

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _tmp_dir():
    import tempfile
    global _last_tmp
    _last_tmp = tempfile.mkdtemp(prefix="harness-settings-")
    return _last_tmp


_last_tmp = None


def _read_cfg(tmp):
    path = os.path.join(tmp, "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class SettingsUpdateTests(SettingsApiHarness):
    def test_post_updates_max_cost_and_persists(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"max_cost": 0.08})
            self.assertEqual(status, 200)
            self.assertEqual(data["settings"]["max_cost"], 0.08)
        finally:
            conn.close()
        cfg = _read_cfg(_last_tmp)
        self.assertEqual(cfg["max_cost"], 0.08)
        # A fresh load (what the next task run does) sees the new ceiling.
        from harness.config import load_settings
        self.assertEqual(load_settings().max_cost, 0.08)

    def test_post_toggles_use_free(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"use_free": False})
            self.assertEqual(status, 200)
            self.assertFalse(data["settings"]["use_free"])
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"use_free": True})
            self.assertEqual(status, 200)
            self.assertTrue(data["settings"]["use_free"])
        finally:
            conn.close()

    def test_post_rejects_out_of_range_cap_fail_closed(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"max_cost": 5.0})
            self.assertEqual(status, 400)
            self.assertIn("max_cost", data["error"])
        finally:
            conn.close()
        path = os.path.join(_last_tmp, "config.json")
        if os.path.exists(path):
            self.assertNotIn("max_cost", _read_cfg(_last_tmp))

    def test_post_rejects_unknown_key(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"definitely_not_a_setting": 1})
            self.assertEqual(status, 400)
            self.assertIn("unknown setting", data["error"])
        finally:
            conn.close()

    def test_post_rejects_non_runtime_updatable_key(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"apply_model": "some/model"})
            self.assertEqual(status, 400)
            self.assertIn("not runtime-updatable", data["error"])
        finally:
            conn.close()

    def test_post_rejects_empty_and_non_object_bodies(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings", body={})
            self.assertEqual(status, 400)
            status, data = _request(conn, "POST", "/api/settings", body=[1])
            self.assertEqual(status, 400)
        finally:
            conn.close()

    def test_update_preserves_unrelated_keys_in_config(self):
        path = os.path.join(_last_tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"allow_escalation": False, "use_free": True}, f)
        conn = self._conn()
        try:
            status, _ = _request(conn, "POST", "/api/settings",
                                 body={"max_cost": 0.02})
            self.assertEqual(status, 200)
        finally:
            conn.close()
        cfg = _read_cfg(_last_tmp)
        self.assertEqual(cfg["allow_escalation"], False)
        self.assertEqual(cfg["max_cost"], 0.02)

    def test_env_pin_still_wins_over_config_write(self):
        # Precedence contract: env > config.json. The write succeeds but the
        # reported/persisted settings keep honoring the env pin, so the UI
        # never shows a value the next load_settings() would not use.
        os.environ["HARNESS_MAX_COST"] = "0.03"
        self.addCleanup(os.environ.pop, "HARNESS_MAX_COST", None)
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/settings",
                                    body={"max_cost": 0.08})
            self.assertEqual(status, 200)
            self.assertEqual(data["settings"]["max_cost"], 0.03)
        finally:
            conn.close()

    def test_update_config_atomic_failure_leaves_no_litter(self):
        # D12: the atomic-write failure path must clean its temp file and
        # propagate -- a failed write must never corrupt an existing
        # config.json.
        from harness.config import update_config
        tmp = _tmp_dir()
        with mock.patch("harness.config.CONFIG_DIR", tmp):
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"max_cost": 0.05}, f)
            # Branch 1: replace fails, unlink succeeds -> no litter.
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    update_config({"max_cost": 0.07})
            self.assertEqual(os.listdir(tmp), ["config.json"])
            # Branch 2: even when unlink itself fails, the error propagates.
            with mock.patch("os.replace", side_effect=OSError("disk full")), \
                    mock.patch("os.unlink", side_effect=OSError("gone")):
                with self.assertRaises(OSError):
                    update_config({"max_cost": 0.07})
            # Original config untouched through both failures.
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["max_cost"], 0.05)

    def test_post_requires_auth_when_token_configured(self):
        self.httpd.ui.auth_token = "tok-1"
        self.addCleanup(setattr, self.httpd.ui, "auth_token", None)
        conn = self._conn()
        try:
            status, _ = _request(conn, "POST", "/api/settings",
                                 body={"max_cost": 0.08},
                                 headers={"X-Harness-Auth": "wrong"})
            self.assertEqual(status, 401)
        finally:
            conn.close()
