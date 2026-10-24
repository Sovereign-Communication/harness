"""GUI operator parity: the faces behind the new operator panes.

Hermetic: an ephemeral loopback server with stubbed runners, a fake ledger
for repair, and a patched mission writer. The gate runner is the one real
shell-free owner but its subprocess is mocked here, so no test executes a
gate. The static pins assert that every lane the server can drive has a
registered pane and that the shell exposes the script posture and workspace
recents.
"""
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import events as _events
from harness.errors import HarnessError, ToolCancelled
from harness.gate_runner import GATE_ORDER, STAGE_GATE_TIMEOUT, VERIFY_TIMEOUT
from harness.server import (
    GATE_OUTPUT_LIMIT,
    RUNNERS,
    _script_posture,
    run_gate_task,
    validate_dispatch,
)
from tests.test_server import ServerHarness, _request


class BenchManifestValidationTests(unittest.TestCase):
    def test_directory_manifest_is_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "efficiency"
            manifest.mkdir()
            args = validate_dispatch("bench", {
                "root_dir": root, "manifest": "efficiency"})
        self.assertEqual(Path(args["manifest"]).resolve(), manifest.resolve())

    def test_file_manifest_is_still_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "tasks.json"
            manifest.write_text("[]", encoding="utf-8")
            args = validate_dispatch("bench", {
                "root_dir": root, "manifest": "tasks.json"})
        self.assertEqual(Path(args["manifest"]).resolve(), manifest.resolve())

    def test_missing_manifest_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(HarnessError):
                validate_dispatch("bench", {
                    "root_dir": root, "manifest": "absent"})


class GateDispatchValidationTests(unittest.TestCase):
    def test_gate_kind_is_registered(self):
        self.assertIn("gate", RUNNERS)
        self.assertEqual(RUNNERS["gate"], run_gate_task)

    def test_known_gate_and_default_phase(self):
        args = validate_dispatch("gate", {"gate": "jev-phase"})
        self.assertEqual(args["gate"], "jev-phase")
        self.assertEqual(args["phase"], "JEV-P0")

    def test_non_phase_gate_rejects_phase(self):
        with self.assertRaises(HarnessError) as ctx:
            validate_dispatch("gate", {"gate": "import", "phase": "JEV-P0"})
        self.assertIn("only applies", str(ctx.exception))

    def test_unknown_gate_is_refused_by_name(self):
        with self.assertRaises(HarnessError) as ctx:
            validate_dispatch("gate", {"gate": "rm-rf"})
        self.assertIn("unknown gate", str(ctx.exception))
        for name in GATE_ORDER:
            self.assertIn(name, str(ctx.exception))

    def test_phase_grammar_is_enforced(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("gate", {"gate": "jev-phase", "phase": "../x"})
        with self.assertRaises(HarnessError):
            validate_dispatch("gate", {"gate": "jev-phase", "phase": "x" * 65})

    def test_timeout_is_bounded(self):
        self.assertEqual(
            validate_dispatch("gate", {"gate": "import", "timeout": "12"})["timeout"],
            12.0)
        for bad in (0, 3601, "soon"):
            with self.assertRaises(HarnessError):
                validate_dispatch("gate", {"gate": "import", "timeout": bad})


class GateRunnerTests(unittest.TestCase):
    def _run(self, gate="import", **extra):
        args = {"gate": gate, "phase": None}
        args.update(extra)
        return run_gate_task("task-gate", args, lambda: False)

    def test_passing_gate_returns_evidence(self):
        with mock.patch("harness.gate_runner.run_gate",
                        return_value=(0, "all good\n")) as run:
            result = self._run()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["passed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "all good\n")
        self.assertFalse(result["output_truncated"])
        self.assertEqual(run.call_args.kwargs["timeout"], VERIFY_TIMEOUT * 2)
        self.assertTrue(run.call_args.kwargs["cwd"].endswith("Harness-gui-parity")
                        or os.path.isdir(run.call_args.kwargs["cwd"]))

    def test_failing_gate_keeps_exit_code_and_output(self):
        with mock.patch("harness.gate_runner.run_gate",
                        return_value=(7, "boom\n")):
            result = self._run()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("boom", result["output"])

    def test_long_gate_uses_stage_budget(self):
        with mock.patch("harness.gate_runner.run_gate",
                        return_value=(0, "ok")) as run:
            self._run("audit")
        self.assertEqual(run.call_args.kwargs["timeout"], STAGE_GATE_TIMEOUT)

    def test_explicit_timeout_wins(self):
        with mock.patch("harness.gate_runner.run_gate",
                        return_value=(0, "ok")) as run:
            self._run("unittest", timeout=11.0)
        self.assertEqual(run.call_args.kwargs["timeout"], 11)

    def test_output_is_tail_bounded(self):
        output = "x" * (GATE_OUTPUT_LIMIT + 10)
        with mock.patch("harness.gate_runner.run_gate",
                        return_value=(0, output)):
            result = self._run()
        self.assertTrue(result["output_truncated"])
        self.assertEqual(len(result["output"]), GATE_OUTPUT_LIMIT)
        self.assertTrue(result["output"].endswith("x"))

    def test_jev_phase_forwards_the_phase(self):
        with mock.patch("harness.gate_runner.run_gate",
                        return_value=(0, "ok")) as run:
            self._run("jev-phase", phase="JEV-P2")
        self.assertIn("JEV-P2", run.call_args.args[0])

    def test_cancel_before_start_does_not_run(self):
        with mock.patch("harness.gate_runner.run_gate") as run:
            with self.assertRaises(ToolCancelled):
                run_gate_task("task-gate", {"gate": "import", "phase": None},
                              lambda: True)
        run.assert_not_called()

    def test_gate_emits_start_and_end_events(self):
        seen = []
        sink = seen.append
        self.assertIsNotNone(_events.add_sink(sink))
        try:
            with mock.patch("harness.gate_runner.run_gate",
                            return_value=(1, "nope")):
                self._run()
        finally:
            _events.remove_sink(sink)
        kinds = [e.get("type") for e in seen]
        self.assertIn("gate_start", kinds)
        self.assertIn("gate_end", kinds)
        end = next(e for e in seen if e.get("type") == "gate_end")
        self.assertEqual(end["gate"], "import")
        self.assertFalse(end["passed"])


class GatesEndpointTests(ServerHarness):
    def test_registry_shape_and_phase_command(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/gates?phase=JEV-P1")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual([g["name"] for g in data["gates"]], list(GATE_ORDER))
        jev = next(g for g in data["gates"] if g["name"] == "jev-phase")
        self.assertIn("JEV-P1", jev["command"])
        self.assertEqual(jev["timeout_s"], VERIFY_TIMEOUT * 2)
        audit = next(g for g in data["gates"] if g["name"] == "audit")
        self.assertEqual(audit["timeout_s"], STAGE_GATE_TIMEOUT)

    def test_bad_phase_is_a_400(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/gates?phase=../x")
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("phase", data["error"])

    def test_unknown_gate_dispatch_is_a_400(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/runs",
                                    {"kind": "gate",
                                     "args": {"gate": "not-a-gate"}})
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("unknown gate", data["error"])

    def test_gate_run_is_pollable_and_terminal(self):
        def fake(task_id, args, cancel):
            return {"status": "passed", "gate": args["gate"],
                    "exit_code": 0, "passed": True, "duration_s": 0.01,
                    "output": "ok", "output_truncated": False}

        with mock.patch.dict(RUNNERS, {"gate": fake}):
            conn = self._conn()
            try:
                status, run = _request(
                    conn, "POST", "/api/runs",
                    {"kind": "gate", "args": {"gate": "import"}})
                self.assertEqual(status, 201)
                deadline = 5.0
                end = time.time() + deadline
                result = None
                while time.time() < end:
                    status, result = _request(
                        conn, "GET", f"/api/runs/{run['id']}/result")
                    if result["status"] != "running":
                        break
                    time.sleep(0.02)
            finally:
                conn.close()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["result"]["gate"], "import")


class _FakeLedger:
    def __init__(self, repair_result=(3, 2)):
        self.repair_result = repair_result
        self.repaired = 0
        self.calls = []

    def chain_status(self):
        self.calls.append("chain")
        return {"ok": self.repaired > 0, "first_bad_seq": None,
                "segmented": False, "segments": [], "pruned": False,
                "entries": 3, "quarantined": 0, "chain_broken_on_load": False}

    def repair(self):
        self.calls.append("repair")
        self.repaired += 1
        return self.repair_result


class LedgerRepairEndpointTests(ServerHarness):
    def test_repair_requires_the_exact_confirmation_word(self):
        ledger = _FakeLedger()
        with mock.patch("harness.server.ledger_for", return_value=ledger):
            conn = self._conn()
            try:
                status, data = _request(conn, "POST", "/api/ledger/repair",
                                        {"confirm": "yes"})
                self.assertEqual(status, 400)
                self.assertIn("confirm", data["error"])
                status, _ = _request(conn, "POST", "/api/ledger/repair", {})
                self.assertEqual(status, 400)
            finally:
                conn.close()
        self.assertEqual(ledger.calls, [])

    def test_repair_returns_before_and_after(self):
        ledger = _FakeLedger(repair_result=(7, 0))
        with mock.patch("harness.server.ledger_for", return_value=ledger):
            conn = self._conn()
            try:
                status, data = _request(conn, "POST", "/api/ledger/repair",
                                        {"confirm": "repair"})
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertEqual((data["kept"], data["dropped"]), (7, 0))
        self.assertEqual(data["before"]["entries"], 3)
        self.assertFalse(data["before"]["ok"])
        self.assertEqual(ledger.calls, ["chain", "repair", "chain"])


class MissionInitEndpointTests(ServerHarness):
    def test_non_object_body_is_a_400(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/missions", [1, 2])
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("JSON object", data["error"])

    def _payload(self, **extra):
        body = {
            "id": "gui-parity",
            "request": "Harden the ledger",
            "success_definition": "tests pass",
            "max_cost_usd": 1.5,
            "terminal_reserve_cost_usd": 0.25,
            "in_scope": ["ledger"],
            "out_of_scope": ["network"],
            "verifier_kind": "unittest",
            "root": "missions",
        }
        body.update(extra)
        return body

    def test_missing_cost_is_a_400(self):
        conn = self._conn()
        try:
            body = self._payload()
            body.pop("max_cost_usd")
            status, data = _request(conn, "POST", "/api/missions", body)
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("max_cost_usd", data["error"])

    def test_non_list_scope_is_refused(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/missions",
                                    self._payload(in_scope={"bad": True}))
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("in_scope", data["error"])

    def test_out_of_scope_must_be_a_list(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/missions",
                                    self._payload(out_of_scope=7))
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("out_of_scope", data["error"])

    def test_scope_entries_must_be_strings(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/missions",
                                    self._payload(in_scope=["ok", 3]))
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("in_scope", data["error"])

    def test_defaults_fill_root_reserve_and_verifier(self):
        pack = mock.Mock(id="gui-parity")
        with (mock.patch("harness.mission_record.init_mission_pack",
                         return_value=pack) as init,
              mock.patch("harness.mission_record.pack_summary",
                         return_value={"id": "gui-parity"})):
            body = self._payload()
            body.pop("root")
            body.pop("terminal_reserve_cost_usd")
            body.pop("verifier_kind")
            conn = self._conn()
            try:
                status, _ = _request(conn, "POST", "/api/missions", body)
            finally:
                conn.close()
        self.assertEqual(status, 201)
        spec = init.call_args.args[1]
        self.assertEqual(spec["persistence"]["root"], "missions")
        self.assertEqual(spec["terminal_reserve"]["cost_usd"], 0.0)
        self.assertEqual(spec["verifier"]["kind"], "unspecified")

    def test_string_scopes_are_split_into_lines(self):
        pack = mock.Mock(id="gui-parity")
        with (mock.patch("harness.mission_record.init_mission_pack",
                         return_value=pack) as init,
              mock.patch("harness.mission_record.pack_summary",
                         return_value={"id": "gui-parity"})):
            conn = self._conn()
            try:
                status, data = _request(
                    conn, "POST", "/api/missions",
                    self._payload(in_scope="a\nb\n",
                                  out_of_scope="c\n"))
            finally:
                conn.close()
        self.assertEqual(status, 201)
        self.assertEqual(data["id"], "gui-parity")
        spec = init.call_args.args[1]
        self.assertEqual(spec["scope"]["in_scope"], ["a", "b"])
        self.assertEqual(spec["scope"]["out_of_scope"], ["c"])


class ScriptPostureTests(ServerHarness):
    def test_status_reports_disabled_by_default(self):
        with mock.patch("harness.server._script.resolve_script_policy") as policy:
            policy.return_value = mock.Mock(
                enabled=False, timeout_s=30, max_output_bytes=65536,
                allow_network=False)
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/status")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data["scripting"], {
            "enabled": False, "timeout_s": 30,
            "max_output_bytes": 65536, "allow_network": False,
        })

    def test_posture_helper_maps_policy(self):
        policy = mock.Mock(enabled=True, timeout_s=9,
                           max_output_bytes=1234, allow_network=True)
        with mock.patch("harness.server._script.resolve_script_policy",
                        return_value=policy):
            self.assertEqual(_script_posture(), {
                "enabled": True, "timeout_s": 9,
                "max_output_bytes": 1234, "allow_network": True,
            })


class StaticPaneRegistrationTests(unittest.TestCase):
    UI = Path(__file__).resolve().parents[1] / "harness" / "ui"

    def test_every_operator_lane_has_a_pane_and_renderer(self):
        source = (self.UI / "panes.js").read_text(encoding="utf-8")
        for pane_id, label in (
                ("plan", "Plan"), ("bench", "Bench"), ("gates", "Gates"),
                ("ledger", "Ledger"), ("models", "Models & Cost"),
                ("missions", "Missions"), ("provision", "Provision")):
            self.assertIn(f'["{pane_id}", "{label}"', source)
        for renderer in (
                "renderPlan", "renderBench", "renderGates", "renderLedger",
                "renderModels", "renderMissions", "renderProvision"):
            self.assertIn(f"async function {renderer}", source)

    def test_pane_forms_use_existing_faces(self):
        source = (self.UI / "panes.js").read_text(encoding="utf-8")
        for needle in (
                'kind: "plan"', 'kind: "bench"', 'kind: "gate"',
                "/api/gates?phase=", '"/api/ledger/verify"',
                '"/api/ledger/repair"', '"/api/models?limit=40"',
                '"/api/capabilities"', "/api/missions?limit=",
                '"/api/provision/probe"', '"/api/provision/plan"'):
            self.assertIn(needle, source)

    def test_shell_exposes_script_posture_and_recent_workspaces(self):
        html = (self.UI / "index.html").read_text(encoding="utf-8")
        app = (self.UI / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="script-badge"', html)
        self.assertIn('id="workspace-recents"', html)
        self.assertIn("loadScriptPosture", app)
        self.assertIn("RECENT_WORKSPACES_KEY", app)


if __name__ == "__main__":
    unittest.main()
