"""HUL-A mission pack gate tests (tests/test_hul_mission_record.py).

Covers: init layout, schema rejection, STATUS regeneration after simulated
attempts, append-only receipts across interrupt/reload, resume.json write +
validation, FINDINGS only via terminal helper, budget working_remaining, and
the CLI mission surface.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from harness import cli, mission_record as mr
from harness.errors import HarnessError


def _ok_spec(mid="m-hul-a-1", root="missions"):
    return mr.build_mission_spec(
        mission_id=mid,
        request="Implement HUL-A mission pack layout",
        success_definition="Pack inits, receipts append, STATUS regenerates",
        max_cost_usd=0.50,
        terminal_reserve_cost_usd=0.05,
        in_scope=["mission_record.py", "cli mission"],
        out_of_scope=["HUL-D driver live loop"],
        persistence_root=root,
        verifier_kind="hermetic-local",
    )


class MissionPackInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_full_pack_layout(self):
        pack = mr.init_mission_pack(self.root, _ok_spec())
        self.assertTrue(pack.dir.is_dir())
        for name in mr.PACK_FILES:
            path = pack.dir / name
            if name == "artifacts":
                self.assertTrue(path.is_dir(), name)
            else:
                self.assertTrue(path.is_file(), name)
        spec = pack.spec()
        self.assertEqual(spec["id"], "m-hul-a-1")
        self.assertEqual(spec["limits"]["max_cost_usd"], 0.50)
        self.assertEqual(spec["terminal_reserve"]["cost_usd"], 0.05)
        self.assertEqual(spec["verifier"]["kind"], "hermetic-local")
        self.assertIn("mission_record.py", spec["scope"]["in_scope"])

    def test_init_refuses_duplicate_pack(self):
        mr.init_mission_pack(self.root, _ok_spec())
        with self.assertRaises(HarnessError):
            mr.init_mission_pack(self.root, _ok_spec())

    def test_yaml_round_trip(self):
        spec = _ok_spec(mid="m-rt")
        text = mr.dump_mission_yaml(spec)
        loaded = mr.validate_mission_spec(mr.load_mission_yaml(text))
        self.assertEqual(loaded["id"], "m-rt")
        self.assertEqual(loaded["request"], spec["request"])
        self.assertEqual(loaded["scope"]["in_scope"], spec["scope"]["in_scope"])
        self.assertEqual(loaded["success_definition"], spec["success_definition"])
        self.assertEqual(loaded["limits"]["max_cost_usd"], 0.50)
        self.assertEqual(loaded["persistence"]["root"], "missions")
        self.assertEqual(loaded["verifier"]["kind"], "hermetic-local")

    def test_yaml_round_trip_empty_scope(self):
        spec = mr.build_mission_spec(
            mission_id="m-empty-scope",
            request="r",
            success_definition="s",
            max_cost_usd=0.1,
        )
        loaded = mr.validate_mission_spec(mr.load_mission_yaml(mr.dump_mission_yaml(spec)))
        self.assertEqual(loaded["scope"]["in_scope"], [])
        self.assertEqual(loaded["scope"]["out_of_scope"], [])

    def test_yaml_quoted_newlines_round_trip(self):
        spec = mr.build_mission_spec(
            mission_id="m-multiline",
            request="line one\nline two: with colon",
            success_definition="done",
            max_cost_usd=0.2,
            terminal_reserve_cost_usd=0.01,
        )
        loaded = mr.validate_mission_spec(mr.load_mission_yaml(mr.dump_mission_yaml(spec)))
        self.assertEqual(loaded["request"], "line one\nline two: with colon")


class MissionSchemaValidationTests(unittest.TestCase):
    def test_rejects_missing_success_definition(self):
        bad = _ok_spec()
        del bad["success_definition"]
        # dump-normalized specs always have it; build a raw dict instead
        raw = {
            "id": "m-bad-success",
            "request": "do work",
            "scope": {"in_scope": [], "out_of_scope": []},
            "limits": {"max_cost_usd": 0.2},
            "terminal_reserve": {"cost_usd": 0.0},
            "persistence": {"root": "missions"},
            "verifier": {"kind": "x"},
        }
        with self.assertRaises(HarnessError) as cm:
            mr.validate_mission_spec(raw)
        self.assertIn("success_definition", str(cm.exception))

    def test_rejects_missing_limits(self):
        raw = {
            "id": "m-bad-limits",
            "request": "do work",
            "scope": {"in_scope": [], "out_of_scope": []},
            "success_definition": "ok",
            "terminal_reserve": {"cost_usd": 0.0},
            "persistence": {"root": "missions"},
            "verifier": {"kind": "x"},
        }
        with self.assertRaises(HarnessError) as cm:
            mr.validate_mission_spec(raw)
        self.assertIn("limits", str(cm.exception))

    def test_rejects_missing_max_cost_usd(self):
        raw = _ok_spec()
        raw = dict(raw)
        raw["limits"] = {}
        with self.assertRaises(HarnessError) as cm:
            mr.validate_mission_spec(raw)
        self.assertIn("max_cost_usd", str(cm.exception))

    def test_rejects_reserve_exceeding_max(self):
        with self.assertRaises(HarnessError):
            mr.build_mission_spec(
                mission_id="m-reserve-bad",
                request="r",
                success_definition="s",
                max_cost_usd=0.01,
                terminal_reserve_cost_usd=0.5,
            )

    def test_rejects_unsafe_mission_id(self):
        for bad in ("../escape", "a/b", "", "..", "a\\b"):
            with self.assertRaises(HarnessError):
                mr.validate_mission_id(bad)

    def test_build_mission_spec_normalizes(self):
        spec = _ok_spec()
        self.assertIsInstance(spec["limits"]["max_cost_usd"], float)
        self.assertIsInstance(spec["terminal_reserve"]["cost_usd"], float)


class BudgetHelperTests(unittest.TestCase):
    def test_working_remaining_formula(self):
        self.assertAlmostEqual(mr.working_remaining(0.50, 0.10, 0.05), 0.35)
        self.assertAlmostEqual(mr.working_remaining(0.10, 0.20, 0.05), 0.0)

    def test_budget_file_after_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missions"
            root.mkdir()
            pack = mr.init_mission_pack(root, _ok_spec())
            budget = mr.load_budget(pack)
            self.assertEqual(budget["max_cost_usd"], 0.50)
            self.assertEqual(budget["terminal_reserve_cost_usd"], 0.05)
            self.assertEqual(budget["spent"], 0.0)
            self.assertAlmostEqual(budget["working_remaining"], 0.45)

    def test_record_spend_updates_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missions"
            root.mkdir()
            pack = mr.init_mission_pack(root, _ok_spec())
            updated = mr.record_spend(pack, 0.10)
            self.assertAlmostEqual(updated["spent"], 0.10)
            self.assertAlmostEqual(updated["working_remaining"], 0.35)


class ReceiptsAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()
        self.pack = mr.init_mission_pack(self.root, _ok_spec())

    def tearDown(self):
        self.tmp.cleanup()

    def _simulate_attempts(self, n=3):
        for i in range(n):
            mr.append_receipt(self.pack, {
                "kind": "attempt",
                "attempt": i + 1,
                "note": f"attempt {i + 1}",
            })
            mr.record_spend(self.pack, 0.01)
            (self.pack.artifacts_dir / f"attempt-{i + 1}.txt").write_text(
                f"artifact {i + 1}\n", encoding="utf-8")
        mr.write_resume(self.pack, mr.build_resume_state(self.pack))
        mr.write_status(self.pack)

    def test_status_regenerates_after_attempts(self):
        before = self.pack.status_md.read_text(encoding="utf-8")
        self.assertIn("**receipts:** 0", before)
        self.assertIn("**terminal:** no", before)
        self._simulate_attempts(3)
        after = self.pack.status_md.read_text(encoding="utf-8")
        self.assertIn("**receipts:** 3", after)
        self.assertIn("**budget.spent:** 0.03", after)
        self.assertIn("attempt-1.txt", after)
        self.assertIn("**terminal:** no", after)
        summary = mr.pack_summary(self.pack)
        self.assertEqual(summary["receipts_count"], 3)
        self.assertEqual(summary["budget"]["spent"], 0.03)

    def test_interrupt_loses_no_receipts(self):
        self._simulate_attempts(4)
        receipts_path = self.pack.receipts_path
        self.assertTrue(receipts_path.is_file())
        # Simulate interrupt: drop in-memory handle by reloading the pack.
        reloaded = mr.load_mission_pack(self.root, self.pack.id)
        receipts = mr.load_receipts(reloaded)
        self.assertEqual(len(receipts), 4)
        self.assertEqual([r["attempt"] for r in receipts], [1, 2, 3, 4])
        # A torn trailing line must not destroy the valid prefix (ledger style).
        with open(receipts_path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        again = mr.load_receipts(mr.load_mission_pack(self.root, self.pack.id))
        self.assertEqual(len(again), 4)

    def test_receipts_are_append_only(self):
        r1 = mr.append_receipt(self.pack, {"kind": "attempt", "attempt": 1})
        raw1 = self.pack.receipts_path.read_text(encoding="utf-8")
        r2 = mr.append_receipt(self.pack, {"kind": "attempt", "attempt": 2})
        raw2 = self.pack.receipts_path.read_text(encoding="utf-8")
        self.assertTrue(raw2.startswith(raw1))
        self.assertIn(r1["ts"], raw2)
        self.assertIn(r2["ts"], raw2)
        self.assertEqual(mr.load_receipts(self.pack)[0]["attempt"], 1)

    def test_jev_evals_append_and_load(self):
        mr.append_jev_eval(self.pack, {"verdict": "pass", "confidence": 0.9})
        mr.append_jev_eval(self.pack, {"verdict": "fail", "confidence": 0.2})
        evals = mr.load_jev_evals(self.pack)
        self.assertEqual(len(evals), 2)
        self.assertEqual(evals[0]["mission_id"], self.pack.id)
        self.assertEqual(evals[0]["verdict"], "pass")


class ResumeAndFindingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()
        self.pack = mr.init_mission_pack(self.root, _ok_spec())

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_json_written_on_init(self):
        self.assertTrue(self.pack.resume_path.is_file())
        state = mr.load_resume(self.pack)
        validated = mr.validate_resume(state, expected_id=self.pack.id)
        self.assertEqual(validated["schema_version"], 1)
        self.assertEqual(validated["mission_id"], self.pack.id)
        self.assertEqual(validated["status"], "in_progress")
        self.assertEqual(validated["receipts_seq"], 0)

    def test_resume_validation_rejects_bad_state(self):
        with self.assertRaises(HarnessError):
            mr.validate_resume({"schema_version": 99, "mission_id": self.pack.id})
        with self.assertRaises(HarnessError):
            mr.validate_resume({"schema_version": 1, "mission_id": ""})
        with self.assertRaises(HarnessError):
            mr.validate_resume(
                {"schema_version": 1, "mission_id": "other"},
                expected_id=self.pack.id)
        with self.assertRaises(HarnessError):
            mr.validate_resume({
                "schema_version": 1,
                "mission_id": self.pack.id,
                "status": "not-a-status",
            })
        with self.assertRaises(HarnessError):
            mr.validate_resume({
                "schema_version": 1,
                "mission_id": self.pack.id,
                "receipts_seq": -1,
            })

    def test_findings_placeholder_until_terminal(self):
        body = self.pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("Not terminal", body)
        self.assertFalse(mr.is_terminal(self.pack))
        # STATUS still says not terminal after simulated work
        mr.append_receipt(self.pack, {"kind": "attempt", "attempt": 1})
        mr.write_status(self.pack)
        self.assertFalse(mr.is_terminal(self.pack))
        status = self.pack.status_md.read_text(encoding="utf-8")
        self.assertIn("**terminal:** no", status)

    def test_findings_generated_only_via_terminal_helper(self):
        mr.append_receipt(self.pack, {"kind": "attempt", "attempt": 1})
        mr.write_resume(self.pack, mr.build_resume_state(self.pack))
        mr.write_status(self.pack)
        self.assertIn("Not terminal", self.pack.findings_md.read_text(encoding="utf-8"))
        resume = mr.mark_terminal(
            self.pack,
            outcome="complete",
            findings="# FINDINGS — m-hul-a-1\n\nDone. success_definition met.\n",
        )
        self.assertEqual(resume["status"], "complete")
        findings = self.pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("success_definition met", findings)
        self.assertNotIn("Not terminal", findings)
        self.assertTrue(mr.is_terminal(self.pack))
        status = self.pack.status_md.read_text(encoding="utf-8")
        self.assertIn("**terminal:** yes", status)
        self.assertIn("**phase:** complete", status)

    def test_mark_terminal_rejects_unknown_outcome(self):
        with self.assertRaises(HarnessError):
            mr.mark_terminal(self.pack, outcome="maybe")

    def test_mark_terminal_default_body(self):
        resume = mr.mark_terminal(self.pack, outcome="failed")
        self.assertEqual(resume["status"], "failed")
        findings = self.pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("Terminal outcome: `failed`", findings)

    def test_index_lists_pack_files(self):
        mr.write_index(self.pack)
        index = self.pack.index_md.read_text(encoding="utf-8")
        for name in mr.PACK_FILES:
            self.assertIn(name, index)


class MissionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.tmp.name) / "missions")
        os.makedirs(self.root, exist_ok=True)
        self.out = str(Path(self.tmp.name) / "out.json")

    def tearDown(self):
        # cli.main --quiet sets harness.output.QUIET globally; restore so
        # later hermetic tests that capture stderr still see warnings.
        from harness import output as _output
        _output.QUIET = False
        self.tmp.cleanup()

    def _read_out(self):
        with open(self.out, encoding="utf-8") as f:
            return json.load(f)

    def test_cli_init_status_resume_findings(self):
        cli.main([
            "mission", "init",
            "--id", "m-cli-1",
            "--request", "CLI mission pack smoke",
            "--success", "init+status+resume+findings green",
            "--max-cost", "0.25",
            "--terminal-reserve", "0.02",
            "--in-scope", "cli,mission_record",
            "--out-of-scope", "live-driver",
            "--root", self.root,
            "--verifier-kind", "hermetic-local",
            "--out", self.out,
            "--quiet",
        ])
        created = self._read_out()
        self.assertEqual(created["id"], "m-cli-1")
        self.assertEqual(created["budget"]["terminal_reserve_cost_usd"], 0.02)
        self.assertAlmostEqual(created["budget"]["working_remaining"], 0.23)

        # simulate attempts via library (driver is HUL-D)
        pack = mr.load_mission_pack(self.root, "m-cli-1")
        for i in range(2):
            mr.append_receipt(pack, {"kind": "attempt", "attempt": i + 1})
        mr.record_spend(pack, 0.02)
        mr.write_resume(pack, mr.build_resume_state(pack))

        cli.main([
            "mission", "status", "--id", "m-cli-1",
            "--root", self.root, "--out", self.out, "--quiet",
        ])
        status = self._read_out()
        self.assertEqual(status["receipts_count"], 2)
        self.assertAlmostEqual(status["budget"]["spent"], 0.02)
        self.assertFalse(status["terminal"])

        cli.main([
            "mission", "resume", "--id", "m-cli-1",
            "--root", self.root, "--out", self.out, "--quiet",
        ])
        resume = self._read_out()
        self.assertTrue(resume["resumable"])
        self.assertEqual(resume["resume"]["mission_id"], "m-cli-1")

        cli.main([
            "mission", "findings", "--id", "m-cli-1",
            "--root", self.root, "--out", self.out, "--quiet",
        ])
        findings = self._read_out()
        self.assertFalse(findings["terminal"])
        self.assertIn("Not terminal", findings["findings_md"])

        mr.mark_terminal(pack, outcome="complete",
                         findings="# FINDINGS\n\nCLI terminal ok.\n")
        cli.main([
            "mission", "findings", "--id", "m-cli-1",
            "--root", self.root, "--out", self.out, "--quiet",
        ])
        after = self._read_out()
        self.assertTrue(after["terminal"])
        self.assertIn("CLI terminal ok", after["findings_md"])

    def test_cli_init_rejects_missing_success(self):
        # cli.main maps HarnessError -> exit 1 (fail closed, no pack written).
        with self.assertRaises(SystemExit) as cm:
            cli.main([
                "mission", "init",
                "--id", "m-cli-bad",
                "--request", "x",
                "--success", " ",
                "--max-cost", "0.1",
                "--root", self.root,
                "--quiet",
            ])
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse((Path(self.root) / "m-cli-bad" / "mission.yaml").exists())

    def test_cli_run_is_stub_until_hul_d(self):
        cli.main([
            "mission", "init",
            "--id", "m-cli-run",
            "--request", "r",
            "--success", "s",
            "--max-cost", "0.1",
            "--root", self.root,
            "--out", self.out,
            "--quiet",
        ])
        with self.assertRaises(SystemExit) as cm:
            cli.main([
                "mission", "run", "--id", "m-cli-run",
                "--root", self.root, "--quiet",
            ])
        self.assertEqual(cm.exception.code, 1)
        # Direct handler call surfaces the HUL-D message (not only via exit).
        class _Opts:
            mission_cmd = "run"
            mission_id = "m-cli-run"
            root = self.root
            out = None
        with self.assertRaises(HarnessError) as cm2:
            cli._cmd_mission(_Opts(), None)
        self.assertIn("HUL-D", str(cm2.exception))
        self.assertTrue(mr.load_mission_pack(self.root, "m-cli-run").exists())

    def test_cli_status_missing_pack(self):
        with self.assertRaises(SystemExit) as cm:
            cli.main([
                "mission", "status", "--id", "m-nope",
                "--root", self.root, "--quiet",
            ])
        self.assertEqual(cm.exception.code, 1)

    def test_dispatch_includes_mission(self):
        self.assertIn("mission", cli._DISPATCH)
        self.assertIs(cli._DISPATCH["mission"], cli._cmd_mission)

    def test_cli_unknown_mission_cmd_rejected_by_parser(self):
        with self.assertRaises(SystemExit):
            cli.main(["mission", "--id", "x", "--quiet"])


class MissionErrorPathTests(unittest.TestCase):
    """Exercise fail-closed edges so changed harness lines stay suite-executed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()
        self.pack = mr.init_mission_pack(self.root, _ok_spec(mid="m-err"))

    def tearDown(self):
        from harness import output as _output
        _output.QUIET = False
        self.tmp.cleanup()

    def test_append_receipt_requires_existing_pack(self):
        ghost = mr.MissionPack(self.root, "m-ghost")
        with self.assertRaises(HarnessError):
            mr.append_receipt(ghost, {"kind": "attempt"})

    def test_append_jev_eval_requires_existing_pack(self):
        ghost = mr.MissionPack(self.root, "m-ghost")
        with self.assertRaises(HarnessError):
            mr.append_jev_eval(ghost, {"verdict": "pass"})

    def test_append_receipt_requires_mapping(self):
        with self.assertRaises(HarnessError):
            mr.append_receipt(self.pack, ["not-a-map"])  # type: ignore[list-item]

    def test_load_budget_missing_file(self):
        self.pack.budget_path.unlink()
        with self.assertRaises(HarnessError):
            mr.load_budget(self.pack)

    def test_load_resume_missing_file(self):
        self.pack.resume_path.unlink()
        with self.assertRaises(HarnessError):
            mr.load_resume(self.pack)
        self.assertFalse(mr.is_terminal(self.pack))

    def test_write_findings_rejects_empty(self):
        with self.assertRaises(HarnessError):
            mr.write_findings(self.pack, "   ")

    def test_mark_terminal_requires_existing_pack(self):
        ghost = mr.MissionPack(self.root, "m-ghost")
        with self.assertRaises(HarnessError):
            mr.mark_terminal(ghost, outcome="complete")

    def test_validate_mission_id_rejects_bad_types(self):
        with self.assertRaises(HarnessError):
            mr.validate_mission_id(None)  # type: ignore[arg-type]
        with self.assertRaises(HarnessError):
            mr.validate_mission_id("has space")
        with self.assertRaises(HarnessError):
            mr.pack_dir_for("", "m-x")
        with self.assertRaises(HarnessError):
            mr.pack_dir_for(None, "m-x")

    def test_validate_mission_spec_rejects_non_mapping(self):
        with self.assertRaises(HarnessError):
            mr.validate_mission_spec("not-a-map")
        with self.assertRaises(HarnessError):
            mr.validate_mission_spec({"id": "ok-id", "request": "r"})

    def test_require_number_and_list_helpers(self):
        with self.assertRaises(HarnessError):
            mr.validate_mission_spec({
                "id": "m-n",
                "request": "r",
                "scope": {"in_scope": "not-a-list", "out_of_scope": []},
                "success_definition": "s",
                "limits": {"max_cost_usd": 0.1},
                "terminal_reserve": {"cost_usd": 0.0},
            })
        with self.assertRaises(HarnessError):
            mr.validate_mission_spec({
                "id": "m-n",
                "request": "r",
                "scope": {"in_scope": [""], "out_of_scope": []},
                "success_definition": "s",
                "limits": {"max_cost_usd": 0.1},
                "terminal_reserve": {"cost_usd": 0.0},
            })
        with self.assertRaises(HarnessError):
            mr.working_remaining("bad", 0.0, 0.0)
        with self.assertRaises(HarnessError):
            mr.build_mission_spec(
                mission_id="m-neg",
                request="r",
                success_definition="s",
                max_cost_usd=-1.0,
            )

    def test_load_mission_yaml_errors(self):
        with self.assertRaises(HarnessError):
            mr.load_mission_yaml(None)  # type: ignore[arg-type]
        with self.assertRaises(HarnessError):
            mr.load_mission_yaml("not key value")
        with self.assertRaises(HarnessError):
            mr.load_mission_yaml("  - orphan\n")
        with self.assertRaises(HarnessError):
            mr.load_mission_yaml("key:\n  not-an-indent-key\n")
        # A bare list under a map key is representable in the subset; the
        # schema validator (not the loader) rejects non-map scope.
        loaded = mr.load_mission_yaml("scope:\n  - orphan-item\n")
        self.assertEqual(loaded.get("scope"), ["orphan-item"])
        with self.assertRaises(HarnessError):
            mr.validate_mission_spec({
                "id": "m-list-scope",
                "request": "r",
                "scope": ["orphan-item"],
                "success_definition": "s",
                "limits": {"max_cost_usd": 0.1},
                "terminal_reserve": {"cost_usd": 0.0},
            })

    def test_load_mission_yaml_file_missing(self):
        with self.assertRaises(HarnessError):
            mr.load_mission_yaml_file(self.root / "nope.yaml")

    def test_load_mission_pack_corrupt_yaml(self):
        self.pack.mission_yaml.write_text("id: only\n", encoding="utf-8")
        with self.assertRaises(HarnessError):
            mr.load_mission_pack(self.root, self.pack.id)

    def test_resume_bad_pack_dir_type(self):
        with self.assertRaises(HarnessError):
            mr.validate_resume({
                "schema_version": 1,
                "mission_id": self.pack.id,
                "pack_dir": 123,
            })

    def test_cli_findings_and_unknown_library_cmd(self):
        class _Opts:
            mission_cmd = "nope"
            mission_id = "m-err"
            root = str(self.root)
            out = None
        with self.assertRaises(HarnessError) as cm:
            cli._cmd_mission(_Opts(), None)
        self.assertIn("unknown mission command", str(cm.exception))

        class _Find:
            mission_cmd = "findings"
            mission_id = "m-err"
            root = str(self.root)
            out = None
        cli._cmd_mission(_Find(), None)

    def test_write_budget_normalizes_and_record_spend(self):
        out = mr.write_budget(self.pack, {
            "max_cost_usd": 0.5,
            "terminal_reserve_cost_usd": 0.1,
            "spent": 0.2,
        })
        self.assertAlmostEqual(out["working_remaining"], 0.2)
        again = mr.record_spend(self.pack, 0.05)
        self.assertAlmostEqual(again["spent"], 0.25)
        self.assertAlmostEqual(again["working_remaining"], 0.15)

    def test_ensure_findings_idempotent(self):
        path = mr.ensure_findings_placeholder(self.pack)
        body = path.read_text(encoding="utf-8")
        mr.ensure_findings_placeholder(self.pack)
        self.assertEqual(path.read_text(encoding="utf-8"), body)


if __name__ == "__main__":
    unittest.main()
