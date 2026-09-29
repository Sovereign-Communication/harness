"""GAP-freeze-face hermetic gates: the operator face over `freeze_jev_settings`.

`freeze_jev_settings` shipped in JEV-P4 and, until this slice, had **no
CLI/MCP face and had never been applied** -- so the P4 checklist item "model
pin + threshold freeze after first calibration" was not actually closed. This
suite pins the properties that make the new face trustworthy:

- a *preview* (the default) writes nothing, and says so in the envelope;
- a *persist* writes both keys through `update_config`, the ONE config-I/O
  owner, and reports the `effective` block read back with `load_settings()`
  (what the next run uses, not what the call hoped it wrote);
- an env-pinned key refuses the persist **before** any write, because a
  config.json the environment overrides is a file that only *looks* frozen;
- a malformed model id or an out-of-range threshold is refused fail-closed;
- the write allow-list cannot grow past `JEV_FREEZE_KEYS`;
- the CLI and MCP faces are thin -- same owner, same envelope -- and the MCP
  write is gated exactly like every other MCP write.

CONFIG_DIR is patched to a temp dir per test: the operator's real
~/.config/harness/config.json is never touched, and env pins for the touched
keys are cleared so the suite is hermetic on a configured machine.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness.config import (
    JEV_FREEZE_KEYS,
    freeze_jev_settings,
    load_settings,
    update_config,
    validate_jev_model_id,
)
from harness.errors import HarnessError
from harness.ledger import AutonomyLedger
from harness.mcp import McpServer
from harness.mcp_lanes import lane_for
from harness.mcp_schemas import TOOL_SCHEMAS

_TOUCHED_ENV = ["HARNESS_JEV_MODEL", "HARNESS_MIN_CONFIDENCE"]


class FreezeFaceHarness(unittest.TestCase):
    """Isolated CONFIG_DIR, no env pins for the frozen keys."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-freeze-")
        self._patch = mock.patch("harness.config.CONFIG_DIR", self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._saved_env = {k: os.environ.get(k) for k in _TOUCHED_ENV}
        self.addCleanup(self._restore_env)
        for key in _TOUCHED_ENV:
            os.environ.pop(key, None)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @property
    def config_path(self):
        return os.path.join(self.tmp, "config.json")

    def _config_file(self):
        with open(self.config_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _write_existing_config(self, payload):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)


class FreezeOwnerTests(FreezeFaceHarness):
    """The owner's own contract, exercised through the face's call shape."""

    def test_preview_writes_nothing_and_reports_persisted_false(self):
        frozen = freeze_jev_settings(
            load_settings(), jev_model="jev-1.13.0", min_confidence=0.85)
        self.assertEqual(frozen["jev_model"], "jev-1.13.0")
        self.assertEqual(frozen["min_confidence"], 0.85)
        self.assertTrue(frozen["jev_model_is_pinned"])
        self.assertEqual(frozen["freeze_keys"], ["jev_model", "min_confidence"])
        self.assertFalse(frozen["persisted"])
        self.assertNotIn("config_path", frozen)
        self.assertFalse(os.path.exists(self.config_path))

    def test_preview_without_keys_reports_the_current_pin(self):
        frozen = freeze_jev_settings(load_settings())
        self.assertEqual(frozen["freeze_keys"], [])
        self.assertFalse(frozen["persisted"])
        self.assertEqual(frozen["jev_model"], "jev-latest")
        self.assertFalse(frozen["jev_model_is_pinned"])

    def test_persist_writes_both_keys_and_reports_effective(self):
        frozen = freeze_jev_settings(
            load_settings(), jev_model="jev-1.13.0", min_confidence=0.85,
            persist=True)
        self.assertTrue(frozen["persisted"])
        self.assertEqual(frozen["config_path"], self.config_path)
        self.assertEqual(frozen["effective"], {
            "jev_model": "jev-1.13.0", "min_confidence": 0.85})
        stored = self._config_file()
        self.assertEqual(stored["jev_model"], "jev-1.13.0")
        self.assertEqual(stored["min_confidence"], 0.85)
        # The point of the persistence half: the NEXT run sees the pin.
        fresh = load_settings()
        self.assertEqual(fresh.jev_model, "jev-1.13.0")
        self.assertEqual(fresh.min_confidence, 0.85)

    def test_persist_preserves_unrelated_existing_keys(self):
        self._write_existing_config({"max_cost": 0.08, "use_free": False})
        freeze_jev_settings(load_settings(), jev_model="jev-1.13.0",
                            persist=True)
        stored = self._config_file()
        self.assertEqual(stored["max_cost"], 0.08)
        self.assertIs(stored["use_free"], False)
        self.assertEqual(stored["jev_model"], "jev-1.13.0")

    def test_partial_persist_keeps_the_other_key_out_of_the_file(self):
        frozen = freeze_jev_settings(load_settings(), min_confidence=0.9,
                                     persist=True)
        self.assertEqual(frozen["freeze_keys"], ["min_confidence"])
        stored = self._config_file()
        self.assertNotIn("jev_model", stored)
        self.assertEqual(stored["min_confidence"], 0.9)

    def test_env_pinned_key_refuses_persist_and_writes_nothing(self):
        os.environ["HARNESS_JEV_MODEL"] = "jev-1.12.0"
        self.addCleanup(lambda: os.environ.pop("HARNESS_JEV_MODEL", None))
        settings = load_settings()
        with self.assertRaises(HarnessError) as ctx:
            freeze_jev_settings(settings, jev_model="jev-1.13.0",
                                persist=True)
        self.assertIn("HARNESS_JEV_MODEL", str(ctx.exception))
        self.assertFalse(os.path.exists(self.config_path))

    def test_env_pin_for_an_unnamed_key_does_not_block_persist(self):
        # Only the keys being written are checked: pinning min_confidence in
        # the environment must not stop a model-only freeze.
        os.environ["HARNESS_MIN_CONFIDENCE"] = "0.42"
        self.addCleanup(lambda: os.environ.pop("HARNESS_MIN_CONFIDENCE", None))
        frozen = freeze_jev_settings(load_settings(), jev_model="jev-1.13.0",
                                     persist=True)
        self.assertEqual(frozen["freeze_keys"], ["jev_model"])
        self.assertEqual(self._config_file()["jev_model"], "jev-1.13.0")

    def test_invalid_model_id_refused_before_write(self):
        for bad in ("", "has space", "jev\n1", "-leading-dash", "a" * 129,
                    "model\",\"injected\":true", "x" * 200):
            with self.assertRaises(HarnessError):
                freeze_jev_settings(load_settings(), jev_model=bad,
                                    persist=True)
            self.assertFalse(os.path.exists(self.config_path))

    def test_out_of_range_min_confidence_refused_before_write(self):
        for bad in (-0.01, 1.01, float("nan"), float("inf")):
            with self.assertRaises(HarnessError):
                freeze_jev_settings(load_settings(), min_confidence=bad,
                                    persist=True)
            self.assertFalse(os.path.exists(self.config_path))

    def test_persist_without_named_keys_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            freeze_jev_settings(load_settings(), persist=True)
        self.assertIn("nothing to write", str(ctx.exception))
        self.assertFalse(os.path.exists(self.config_path))

    def test_model_id_validation_accepts_real_ids(self):
        for good in ("jev-1.13.0", "jev-latest", "inclusionai/ling-3.0-flash",
                     "z-ai/glm-5.3-flash", "model:variant", "a"):
            self.assertEqual(validate_jev_model_id(good), good)


class WriteAllowListTests(FreezeFaceHarness):
    """The runtime write allow-list grew by exactly the freeze keys."""

    def test_runtime_keys_are_exactly_the_documented_set(self):
        self.assertEqual(set(JEV_FREEZE_KEYS),
                         {"jev_model", "min_confidence"})

    def test_update_config_still_refuses_a_non_runtime_key(self):
        with self.assertRaises(HarnessError) as ctx:
            update_config({"judge": "some/model"})
        self.assertIn("not runtime-updatable", str(ctx.exception))
        self.assertFalse(os.path.exists(self.config_path))

    def test_update_config_refuses_an_unknown_key(self):
        with self.assertRaises(HarnessError):
            update_config({"not_a_setting": 1})
        self.assertFalse(os.path.exists(self.config_path))

    def test_update_config_accepts_a_freeze_key(self):
        update_config({"min_confidence": 0.9})
        self.assertEqual(self._config_file()["min_confidence"], 0.9)

    def test_update_config_rejects_a_hostile_model_id(self):
        with self.assertRaises(HarnessError):
            update_config({"jev_model": "bad model id"})
        self.assertFalse(os.path.exists(self.config_path))


class CliFaceTests(FreezeFaceHarness):
    """`harness jev-freeze`: preview by default, write only on --persist."""

    def _run(self, argv):
        from harness import cli

        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(argv)
        return json.loads(out.getvalue())

    def test_cli_preview_emits_envelope_and_writes_nothing(self):
        result = self._run(["jev-freeze", "--model", "jev-1.13.0",
                            "--min-confidence", "0.85", "--json"])
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["command"], "jev-freeze")
        self.assertFalse(result["freeze"]["persisted"])
        self.assertEqual(result["freeze"]["freeze_keys"],
                         ["jev_model", "min_confidence"])
        self.assertFalse(os.path.exists(self.config_path))

    def test_cli_persist_writes_and_reports_effective(self):
        result = self._run(["jev-freeze", "--model", "jev-1.13.0",
                            "--min-confidence", "0.85", "--persist", "--json"])
        self.assertEqual(result["status"], "frozen")
        self.assertTrue(result["freeze"]["persisted"])
        self.assertEqual(result["freeze"]["effective"],
                         {"jev_model": "jev-1.13.0", "min_confidence": 0.85})
        stored = self._config_file()
        self.assertEqual(stored["jev_model"], "jev-1.13.0")
        self.assertEqual(stored["min_confidence"], 0.85)

    def test_cli_persist_refusal_exits_nonzero_and_writes_nothing(self):
        os.environ["HARNESS_JEV_MODEL"] = "jev-1.12.0"
        self.addCleanup(lambda: os.environ.pop("HARNESS_JEV_MODEL", None))
        with self.assertRaises(SystemExit) as ctx:
            self._run(["jev-freeze", "--model", "jev-1.13.0", "--persist",
                       "--json"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(os.path.exists(self.config_path))

    def test_cli_bad_model_id_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            self._run(["jev-freeze", "--model", "bad model id", "--persist"])
        self.assertFalse(os.path.exists(self.config_path))


class McpFaceTests(FreezeFaceHarness):
    """`jev_freeze`: preview is read-only; the write is allow_write-gated."""

    def _server(self, allow_write=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return McpServer(
            transport=None, api_key=None, governor=None,
            ledger=AutonomyLedger(os.path.join(tmp.name, "ledger.jsonl")),
            router=None, engine=None, allow_write=allow_write)

    def test_mcp_preview_is_read_only(self):
        result = self._server()._invoke("jev_freeze", {"model": "jev-1.13.0",
                                                      "min_confidence": 0.85})
        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["freeze"]["persisted"])
        self.assertFalse(os.path.exists(self.config_path))

    def test_mcp_persist_refused_without_allow_write(self):
        with self.assertRaises(HarnessError) as ctx:
            self._server()._invoke("jev_freeze", {"model": "jev-1.13.0",
                                                 "persist": True})
        self.assertIn("allow_write", str(ctx.exception))
        self.assertFalse(os.path.exists(self.config_path))

    def test_mcp_persist_writes_when_allow_write_is_granted(self):
        result = self._server(allow_write=True)._invoke(
            "jev_freeze", {"model": "jev-1.13.0", "min_confidence": 0.85,
                           "persist": True})
        self.assertEqual(result["status"], "frozen")
        stored = self._config_file()
        self.assertEqual(stored["jev_model"], "jev-1.13.0")
        self.assertEqual(stored["min_confidence"], 0.85)

    def test_mcp_per_request_allow_write_is_honored(self):
        result = self._server()._invoke(
            "jev_freeze", {"model": "jev-1.13.0", "persist": True,
                           "allow_write": True})
        self.assertEqual(result["status"], "frozen")
        self.assertTrue(os.path.exists(self.config_path))

    def test_mcp_refusal_is_ledgered_as_trust_evidence(self):
        server = self._server()
        with self.assertRaises(HarnessError):
            server._invoke("jev_freeze", {"model": "jev-1.13.0",
                                          "persist": True})
        refusals = [e for e in server.ledger.entries()
                    if e.get("event") == "trust_gate"]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["reason"],
                         "mcp config write without allow_write")

    def test_jev_freeze_rides_the_mutation_lane(self):
        # An operator-config write is a mutation: it must never race another
        # write on the spendy/observe lanes.
        self.assertEqual(lane_for("jev_freeze"), "mutation")

    def test_schema_declares_preview_default_and_the_write_gate(self):
        tool = next(t for t in TOOL_SCHEMAS if t["name"] == "jev_freeze")
        props = tool["inputSchema"]["properties"]
        self.assertIs(props["persist"]["default"], False)
        self.assertIn("allow_write", props)
        self.assertEqual(props["min_confidence"]["minimum"], 0)
        self.assertEqual(props["min_confidence"]["maximum"], 1)


class FaceParityTests(FreezeFaceHarness):
    """CLI and MCP are thin over ONE owner: same inputs, same envelope."""

    def test_cli_and_mcp_previews_agree_byte_for_byte(self):
        from harness import cli

        argv = ["jev-freeze", "--model", "jev-1.13.0",
                "--min-confidence", "0.85", "--json"]
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(argv)
        cli_freeze = json.loads(out.getvalue())["freeze"]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = McpServer(
            transport=None, api_key=None, governor=None,
            ledger=AutonomyLedger(os.path.join(tmp.name, "ledger.jsonl")),
            router=None, engine=None)
        mcp_freeze = server._invoke(
            "jev_freeze", {"model": "jev-1.13.0", "min_confidence": 0.85}
        )["freeze"]

        self.assertEqual(cli_freeze, mcp_freeze)
        self.assertEqual(json.dumps(cli_freeze, sort_keys=True),
                         json.dumps(mcp_freeze, sort_keys=True))
        self.assertFalse(os.path.exists(os.path.join(tmp.name, "config.json")))


class FaceOwnsTheSingleOwnerTests(unittest.TestCase):
    """No second freeze implementation: both faces call the one owner."""

    def test_faces_call_the_owner_and_never_write_config_themselves(self):
        import inspect

        from harness import cli, mcp

        for module in (cli, mcp):
            source = inspect.getsource(module)
            self.assertIn("freeze_jev_settings", source)
            # A face must not reach past the owner into the config writer:
            # `update_config()` is config.py's own I/O surface.
            self.assertNotIn("update_config(", source)

    def test_update_config_has_exactly_one_definition(self):
        import glob
        import re

        from harness import config

        package_dir = os.path.dirname(os.path.abspath(config.__file__))
        defining = []
        for path in sorted(glob.glob(os.path.join(package_dir, "*.py"))):
            with open(path, encoding="utf-8") as handle:
                if re.search(r"^def update_config\(", handle.read(), re.M):
                    defining.append(os.path.basename(path))
        self.assertEqual(defining, ["config.py"])


if __name__ == "__main__":
    unittest.main()
