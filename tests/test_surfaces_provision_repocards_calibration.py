"""Hermetic unit tests for PR-04 surfaces:
1. Host provisioning (PROVISION-CORE) CLI, MCP, Server, and Agent faces.
2. Repo philosophy cards (REPO-CARDS) CLI, MCP, and Server faces.
3. Jev calibration analysis (JEV-P3-calibration-analysis) CLI, MCP, and Server faces.
"""
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import cli
from harness.agent import AutonomousAgent, classify_prompt_intent
from harness.cli_parser import build_parser
from harness.errors import HarnessError
from harness.mcp_lanes import lane_for
from harness.mcp_schemas import TOOL_SCHEMAS
from tests.test_mcp import make_server as make_mcp_server
from tests.test_server import ServerHarness, _request


class TestCliParserSurfaces(unittest.TestCase):
    def test_provision_probe_parsed(self):
        p = build_parser()
        args = p.parse_args(["provision", "probe", "--json"])
        self.assertEqual(args.command, "provision")
        self.assertEqual(args.provision_cmd, "probe")
        self.assertTrue(args.json)

    def test_provision_plan_parsed(self):
        p = build_parser()
        args = p.parse_args(["provision", "plan", "--goal", "install ruff", "--root", "/tmp"])
        self.assertEqual(args.command, "provision")
        self.assertEqual(args.provision_cmd, "plan")
        self.assertEqual(args.goal, "install ruff")
        self.assertEqual(args.root, "/tmp")

    def test_provision_apply_parsed(self):
        p = build_parser()
        args = p.parse_args(["provision", "apply", "--plan-file", "plan.json", "--execute"])
        self.assertEqual(args.command, "provision")
        self.assertEqual(args.provision_cmd, "apply")
        self.assertEqual(args.plan_file, "plan.json")
        self.assertFalse(args.dry_run)

    def test_repo_cards_parsed(self):
        p = build_parser()
        args = p.parse_args(["repo-cards", "--check", "--repo", "Harness"])
        self.assertEqual(args.command, "repo-cards")
        self.assertTrue(args.check)
        self.assertEqual(args.repo, "Harness")

    def test_jev_calibration_parsed(self):
        p = build_parser()
        args = p.parse_args(["jev-calibration", "--recommend", "--json"])
        self.assertEqual(args.command, "jev-calibration")
        self.assertTrue(args.recommend)
        self.assertTrue(args.json)

    def test_jev_freeze_calibrate_parsed(self):
        p = build_parser()
        args = p.parse_args(["jev-freeze", "--calibrate"])
        self.assertEqual(args.command, "jev-freeze")
        self.assertTrue(args.calibrate)


class TestProvisionCliExecution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="provision_test_"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.root = os.path.join(self.tmp_dir, "approved")
        os.makedirs(self.root, exist_ok=True)

    def test_provision_probe_cli(self):
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="probe",
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_provision(opts, settings)
        data = json.loads(out_buf.getvalue())
        self.assertIn("os", data)
        self.assertIn("arch", data)
        self.assertIn("python_version", data)

    def test_provision_plan_cli(self):
        root = self.root
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="plan",
            goal="provision ruff",
            root=root,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_provision(opts, settings)
        data = json.loads(out_buf.getvalue())
        self.assertEqual(data["goal"], "provision ruff")
        self.assertTrue(len(data["steps"]) > 0)
        self.assertIn("digest", data)

    def test_provision_apply_dry_run_cli(self):
        from harness import provision as pv
        root = self.root
        probe = pv.probe_host()
        policy = pv.policy_for_probe(probe, [root])
        plan = pv.plan_provision("provision ruff", probe, policy=policy)
        plan_path = os.path.join(self.root, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan.to_dict(), f)

        opts = SimpleNamespace(
            command="provision",
            provision_cmd="apply",
            plan_file=plan_path,
            dry_run=True,
            approve_all=False,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli._ledger") as mock_ledger:
            mock_ledger.return_value = None
            cli._cmd_provision(opts, settings)
        data = json.loads(out_buf.getvalue())
        self.assertEqual(data["outcome"], "dry_run")
        self.assertTrue(data["dry_run"])

    def test_provision_apply_missing_file_raises(self):
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="apply",
            plan_file="nonexistent.json",
            dry_run=True,
            approve_all=False,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        with self.assertRaises(HarnessError):
            cli._cmd_provision(opts, settings)

    def test_cli_main_provision_probe(self):
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli.load_settings", return_value=SimpleNamespace()):
            cli.main(["provision", "probe", "--json"])
        data = json.loads(out_buf.getvalue())
        self.assertIn("os", data)

    def test_cli_main_provision_plan(self):
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli.load_settings", return_value=SimpleNamespace()):
            cli.main(["provision", "plan", "--goal", "install ruff", "--json"])
        data = json.loads(out_buf.getvalue())
        self.assertEqual(data["goal"], "install ruff")

    def test_provision_apply_approve_all(self):
        from harness import provision as pv
        root = self.root
        probe = pv.probe_host()
        policy = pv.policy_for_probe(probe, [root])
        plan = pv.plan_provision("provision ruff", probe, policy=policy)
        plan_path = os.path.join(self.root, "plan.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan.to_dict(), f)

        opts = SimpleNamespace(
            command="provision",
            provision_cmd="apply",
            plan_file=plan_path,
            dry_run=True,
            approve_all=True,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli._ledger") as mock_ledger:
            mock_ledger.return_value = None
            cli._cmd_provision(opts, settings)
        data = json.loads(out_buf.getvalue())
        self.assertEqual(data["outcome"], "dry_run")

    def test_provision_apply_invalid_json_raises(self):
        bad_path = os.path.join(self.root, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="apply",
            plan_file=bad_path,
            dry_run=True,
            approve_all=False,
            json=True,
            out=None,
            quiet=True,
        )
        with self.assertRaises(HarnessError):
            cli._cmd_provision(opts, SimpleNamespace())

    def test_provision_apply_malformed_plan_raises(self):
        bad_path = os.path.join(self.root, "malformed.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({"not": "a plan"}, f)
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="apply",
            plan_file=bad_path,
            dry_run=True,
            approve_all=False,
            json=True,
            out=None,
            quiet=True,
        )
        with self.assertRaises(HarnessError):
            cli._cmd_provision(opts, SimpleNamespace())

    def test_provision_probe_cli_human_output(self):
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="probe",
            json=False,
            out=None,
            quiet=False,
        )
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_provision(opts, SimpleNamespace())
        text = out_buf.getvalue()
        self.assertIn("OS:", text)
        self.assertIn("Python:", text)

    def test_provision_plan_cli_human_output(self):
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="plan",
            goal="install ruff",
            root=self.root,
            json=False,
            out=None,
            quiet=False,
        )
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_provision(opts, SimpleNamespace())
        text = out_buf.getvalue()
        self.assertIn("Goal: install ruff", text)

    def test_provision_apply_cli_human_output(self):
        from harness import provision as pv
        root = self.root
        probe = pv.probe_host()
        policy = pv.policy_for_probe(probe, [root])
        plan = pv.plan_provision("install ruff", probe, policy=policy)
        plan_path = os.path.join(self.root, "plan_human.json")
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan.to_dict(), f)
        opts = SimpleNamespace(
            command="provision",
            provision_cmd="apply",
            plan_file=plan_path,
            dry_run=True,
            approve_all=False,
            json=False,
            out=None,
            quiet=False,
        )
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli._ledger", return_value=None):
            cli._cmd_provision(opts, SimpleNamespace())
        text = out_buf.getvalue()
        self.assertIn("Provision run:", text)


class TestRepoCardsCliExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repos_dir = Path(self.tmp.name) / "repos"
        self.cards_dir = Path(self.tmp.name) / "cards"
        self.repos_dir.mkdir()
        (self.repos_dir / "TestRepo" / ".git").mkdir(parents=True)
        (self.repos_dir / "TestRepo" / "AGENTS.md").write_text("# Test", encoding="utf-8")

    def test_repo_cards_write_and_check_cli(self):
        # Write cards
        opts_write = SimpleNamespace(
            root=str(self.repos_dir),
            cards=str(self.cards_dir),
            check=False,
            repo=None,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_repo_cards(opts_write, settings)
        write_res = json.loads(out_buf.getvalue())
        self.assertEqual(write_res["status"], "written")
        self.assertIn("TestRepo", write_res["changed"])

        # Check cards (should be up to date)
        opts_check = SimpleNamespace(
            root=str(self.repos_dir),
            cards=str(self.cards_dir),
            check=True,
            repo="TestRepo",
            json=True,
            out=None,
            quiet=True,
        )
        out_buf2 = io.StringIO()
        with mock.patch("sys.stdout", out_buf2):
            cli._cmd_repo_cards(opts_check, settings)
        check_res = json.loads(out_buf2.getvalue())
        self.assertEqual(check_res["status"], "ok")
        self.assertEqual(check_res["stale"], [])

    def test_cli_main_repo_cards(self):
        # Generate cards first
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli.load_settings", return_value=SimpleNamespace()):
            cli.main(["repo-cards", "--root", str(self.repos_dir), "--cards", str(self.cards_dir), "--json"])
        res = json.loads(out_buf.getvalue())
        self.assertEqual(res["status"], "written")

        # Now check cards via main
        out_buf2 = io.StringIO()
        with mock.patch("sys.stdout", out_buf2), mock.patch("harness.cli.load_settings", return_value=SimpleNamespace()):
            cli.main(["repo-cards", "--root", str(self.repos_dir), "--cards", str(self.cards_dir), "--check", "--json"])
        check_res = json.loads(out_buf2.getvalue())
        self.assertEqual(check_res["status"], "ok")

    def test_repo_cards_stale_raises(self):
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        target = self.cards_dir / "TestRepo.md"
        target.write_text("corrupted content", encoding="utf-8")
        opts_check = SimpleNamespace(
            root=str(self.repos_dir),
            cards=str(self.cards_dir),
            check=True,
            repo="TestRepo",
            json=True,
            out=None,
            quiet=True,
        )
        with self.assertRaises(HarnessError):
            cli._cmd_repo_cards(opts_check, SimpleNamespace())

    def test_repo_cards_cli_human_output(self):
        opts_write = SimpleNamespace(
            root=str(self.repos_dir),
            cards=str(self.cards_dir),
            check=False,
            repo="TestRepo",
            json=False,
            out=None,
            quiet=False,
        )
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_repo_cards(opts_write, SimpleNamespace())
        text = out_buf.getvalue()
        self.assertIn("wrote TestRepo", text)

        # check human output
        opts_check = SimpleNamespace(
            root=str(self.repos_dir),
            cards=str(self.cards_dir),
            check=True,
            repo="TestRepo",
            json=False,
            out=None,
            quiet=False,
        )
        out_buf2 = io.StringIO()
        with mock.patch("sys.stdout", out_buf2):
            cli._cmd_repo_cards(opts_check, SimpleNamespace())
        self.assertIn("cards up to date", out_buf2.getvalue())

    def test_repo_cards_standalone_functions(self):
        from harness import repo_cards as rc
        changed, unchanged = rc.generate(self.repos_dir, self.cards_dir, repo="TestRepo")
        self.assertEqual(len(changed) + len(unchanged), 1)

        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            code = rc.main(["--root", str(self.repos_dir), "--cards", str(self.cards_dir), "--repo", "TestRepo", "--check"])
            self.assertEqual(code, 0)


class TestJevCalibrationCliExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records_file = os.path.join(self.tmp.name, "judgments.json")
        sample_records = [
            {"confidence": 0.85, "verdict": "pass", "disposition": "pass", "destructive": 0.0},
            {"confidence": 0.35, "verdict": "fail", "disposition": "fail", "destructive": 0.0},
            {"confidence": 0.70, "verdict": "pass", "disposition": "pass", "destructive": 0.0},
        ]
        with open(self.records_file, "w", encoding="utf-8") as f:
            json.dump(sample_records, f)

    def test_jev_calibration_file_mode(self):
        opts = SimpleNamespace(
            file=self.records_file,
            recommend=True,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace()
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_jev_calibration(opts, settings)
        report = json.loads(out_buf.getvalue())
        self.assertEqual(report["n"], 3)
        self.assertIsNotNone(report["recommendation"])
        band = report["recommendation"]["discriminating_band"]
        self.assertEqual(len(band), 2)
        self.assertTrue(band[0] <= band[1])

    def test_cli_main_jev_calibration(self):
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("harness.cli.load_settings", return_value=SimpleNamespace()):
            cli.main(["jev-calibration", "--file", self.records_file, "--json"])
        report = json.loads(out_buf.getvalue())
        self.assertEqual(report["n"], 3)

    def test_jev_calibration_missing_file_raises(self):
        opts = SimpleNamespace(
            file="nonexistent_judgments.json",
            recommend=True,
            json=True,
            out=None,
            quiet=True,
        )
        with self.assertRaises(HarnessError):
            cli._cmd_jev_calibration(opts, SimpleNamespace())

    def test_jev_calibration_ledger_mode(self):
        mock_entries = [
            {"ev": "jev_eval", "confidence": 0.80, "verdict": "pass", "task_id": "t1"},
            {"ev": "jev_eval", "confidence": 0.90, "verdict": "pass", "task_id": "t2"},
        ]
        opts = SimpleNamespace(
            file=None,
            recommend=True,
            json=True,
            out=None,
            quiet=True,
        )
        with mock.patch("harness.cli._ledger") as mock_ledger:
            mock_ledger.return_value = SimpleNamespace(tail=lambda n: mock_entries)
            out_buf = io.StringIO()
            with mock.patch("sys.stdout", out_buf):
                cli._cmd_jev_calibration(opts, SimpleNamespace())
            report = json.loads(out_buf.getvalue())
            self.assertEqual(report["n"], 2)

    def test_jev_freeze_calibrate_flag(self):
        opts = SimpleNamespace(
            model=None,
            min_confidence=None,
            persist=False,
            calibrate=True,
            json=True,
            out=None,
            quiet=True,
        )
        settings = SimpleNamespace(jev_model="jev-latest", min_confidence=0.5)
        # Mock ledger tail returning records
        mock_entries = [
            {"ev": "jev_eval", "confidence": 0.80, "verdict": "pass"},
            {"ev": "jev_eval", "confidence": 0.90, "verdict": "pass"},
        ]
        with mock.patch("harness.cli._ledger") as mock_ledger:
            mock_ledger.return_value = SimpleNamespace(tail=lambda n: mock_entries)
            out_buf = io.StringIO()
            with mock.patch("sys.stdout", out_buf):
                cli._cmd_jev_freeze(opts, settings)
            res = json.loads(out_buf.getvalue())
            self.assertEqual(res["status"], "preview")
            self.assertIsNotNone(res["freeze"]["min_confidence"])

    def test_jev_calibration_human_output(self):
        opts = SimpleNamespace(
            file=self.records_file,
            recommend=True,
            json=False,
            out=None,
            quiet=False,
        )
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_jev_calibration(opts, SimpleNamespace())
        text = out_buf.getvalue()
        self.assertIn("confidence:", text)

    def test_jev_calibration_records_dict_format(self):
        dict_path = os.path.join(self.tmp.name, "records_dict.json")
        with open(dict_path, "w", encoding="utf-8") as f:
            json.dump({"records": [{"confidence": 0.8, "verdict": "pass"}]}, f)
        opts = SimpleNamespace(
            file=dict_path,
            recommend=False,
            json=True,
            out=None,
            quiet=True,
        )
        out_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf):
            cli._cmd_jev_calibration(opts, SimpleNamespace())
        res = json.loads(out_buf.getvalue())
        self.assertEqual(res["n"], 1)

    def test_extract_judgment_records_variations(self):
        from harness.jev_calibration import extract_judgment_records
        entries = [
            {"ev": "jev_eval", "supported": 0.85, "verdict": "pass"},
            {"ev": "judgment"},
            {"ev": "jev_decision", "confidence": "unparseable"},
            {"event": "jev_eval", "confidence": 0.75},
        ]
        records = extract_judgment_records(entries)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["confidence"], 0.85)
        self.assertEqual(records[1]["confidence"], 0.75)


class TestMcpTools(unittest.TestCase):
    def setUp(self):
        _, self.mcp = make_mcp_server()

    def test_tool_schemas_contain_new_tools(self):
        names = {t["name"] for t in TOOL_SCHEMAS}
        self.assertIn("host_provision_probe", names)
        self.assertIn("host_provision_plan", names)
        self.assertIn("repo_cards", names)
        self.assertIn("jev_calibration", names)

    def test_tool_lanes(self):
        self.assertEqual(lane_for("host_provision_plan"), "spendy")
        self.assertEqual(lane_for("host_provision_probe"), "observe")
        self.assertEqual(lane_for("repo_cards"), "observe")
        self.assertEqual(lane_for("jev_calibration"), "observe")

    def _call(self, name, args):
        envelope = self.mcp._call_tool_impl({"params": {"name": name, "arguments": args}})
        result = envelope.get("result", {})
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_host_provision_probe_mcp(self):
        res = self._call("host_provision_probe", {})
        self.assertIn("os", res)
        self.assertIn("arch", res)
        self.assertIn("python_version", res)

    def test_host_provision_plan_mcp(self):
        res = self._call("host_provision_plan", {"goal": "install ruff"})
        self.assertEqual(res["goal"], "install ruff")
        self.assertTrue(len(res["steps"]) > 0)
        self.assertIn("rendered", res)

    def test_repo_cards_mcp(self):
        res = self._call("repo_cards", {"check": True, "repo": "Harness"})
        self.assertIn(res["status"], ("ok", "stale"))

    def test_mcp_repo_cards_write_and_stale(self):
        res = self._call("repo_cards", {"check": False, "repo": "Harness"})
        self.assertEqual(res["status"], "written")
        with mock.patch("harness.repo_cards.render_card", return_value="different card content"):
            res_stale = self._call("repo_cards", {"check": True, "repo": "Harness"})
            self.assertEqual(res_stale["status"], "stale")
            self.assertIn("Harness", res_stale["stale"])

    def test_jev_calibration_mcp(self):
        res = self._call("jev_calibration", {"window": 50})
        self.assertIn("n", res)
        self.assertIn("confidence", res)


class TestServerEndpoints(ServerHarness):
    def test_server_provision_probe(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/provision/probe")
            self.assertEqual(status, 200)
            self.assertIn("os", data)
            self.assertIn("python_version", data)
        finally:
            conn.close()

    def test_server_provision_plan(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/provision/plan", {"goal": "install ruff"})
            self.assertEqual(status, 200)
            self.assertEqual(data["goal"], "install ruff")
            self.assertIn("rendered", data)
        finally:
            conn.close()

    def test_server_cards(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/cards")
            self.assertEqual(status, 200)
            self.assertIn("cards", data)
            self.assertTrue(isinstance(data["cards"], list))
        finally:
            conn.close()

    def test_server_jev_calibration(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/jev-calibration")
            self.assertEqual(status, 200)
            self.assertIn("n", data)
        finally:
            conn.close()

    def test_server_provision_plan_missing_goal(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/provision/plan", {"goal": ""})
            self.assertEqual(status, 400)
        finally:
            conn.close()

    def _conn(self):
        import http.client
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)


class TestAgentProvisionIntent(unittest.TestCase):
    def test_intent_classification(self):
        self.assertEqual(classify_prompt_intent("probe host hardware"), "provision")
        self.assertEqual(classify_prompt_intent("plan provision for package ruff"), "provision")
        self.assertEqual(classify_prompt_intent("setup host environment"), "provision")

    def test_agent_run_prompt_provision(self):
        agent = AutonomousAgent()
        res = agent.run_prompt("plan provision for ruff")
        self.assertEqual(res["status"], "planned")
        self.assertEqual(res["intent"], "provision")
        self.assertIn("Goal: plan provision for ruff", res["response"])
        self.assertIn("plan", res)

    def test_agent_run_prompt_provision_force_conversation(self):
        agent = AutonomousAgent()
        res = agent.run_prompt("provision host probe", force_conversation=True)
        self.assertEqual(res["status"], "planned")
        self.assertEqual(res["intent"], "provision")
        self.assertIn("plan", res)

    def test_should_escalate_to_plan(self):
        agent = AutonomousAgent()
        self.assertTrue(agent._escalation_authorized("execute the plan", False, "plan_required"))


if __name__ == "__main__":
    unittest.main()
