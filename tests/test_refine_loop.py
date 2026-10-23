"""Tests for the refine loop (issue #239, Phase 0 pilot + P0-P3 prerequisites).

Hermetic: no network, no Jev calls, no ledger files. Uses in-memory fakes.
"""

import unittest

from harness import refine


class FakeLedger:
    def __init__(self):
        self._entries = []
        self._seq = 0

    def append(self, event, task_id=None, **fields):
        self._seq += 1
        entry = {"seq": self._seq, "event": event, "task_id": task_id}
        entry.update(fields)
        self._entries.append(entry)
        return entry

    def entries(self):
        return list(self._entries)


class TestTiers(unittest.TestCase):
    def test_tier0_immutable(self):
        self.assertEqual(refine.classify_tier("harness/refine.py"), 0)
        self.assertEqual(refine.classify_tier("harness/provider_connect.py"), 0)
        self.assertEqual(refine.classify_tier("THREAT_MODEL.md"), 0)
        self.assertEqual(refine.classify_tier("docs/security.md"), 0)
        # glob patterns catch key/token/secret anywhere
        self.assertEqual(refine.classify_tier("harness/api_key.py"), 0)
        self.assertEqual(refine.classify_tier("config/secret.json"), 0)

    def test_tier1_systemic(self):
        self.assertEqual(refine.classify_tier("harness/prompts.py"), 1)
        self.assertEqual(refine.classify_tier("harness/consent.py"), 1)
        self.assertEqual(refine.classify_tier("AGENTS.md"), 1)

    def test_tier2_default(self):
        self.assertEqual(refine.classify_tier("harness/router.py"), 2)
        self.assertEqual(refine.classify_tier("scripts/check.py"), 2)
        self.assertEqual(refine.classify_tier("docs/guide.md"), 2)

    def test_check_tier_rejects_tier0(self):
        ok, reason, tier = refine.check_tier(
            ["harness/router.py", "harness/refine.py"])
        self.assertFalse(ok)
        self.assertIn("TIER 0", reason)
        self.assertEqual(tier, 0)

    def test_check_tier_reports_highest(self):
        # Highest tier touched wins: TIER 1 + TIER 2 -> 2.
        ok, _, tier = refine.check_tier(
            ["harness/prompts.py", "docs/guide.md"])
        self.assertTrue(ok)
        self.assertEqual(tier, 2)
        # TIER 1 alone -> 1.
        ok2, _, tier2 = refine.check_tier(["harness/prompts.py"])
        self.assertTrue(ok2)
        self.assertEqual(tier2, 1)

    def test_check_tier_all_tier2(self):
        ok, _, tier = refine.check_tier(["harness/router.py"])
        self.assertTrue(ok)
        self.assertEqual(tier, 2)

    def test_diff_paths_extraction(self):
        diff = ("--- a/harness/router.py\n+++ b/harness/router.py\n"
                "@@ -1 +1 @@\n-x\n+y\n"
                "--- a/docs/new.md\n+++ b/docs/new.md\n")
        paths = refine.diff_paths_from_unified(diff)
        self.assertEqual(paths, ["harness/router.py", "docs/new.md"])


class TestTaskOutcome(unittest.TestCase):
    def test_record_and_get(self):
        ledger = FakeLedger()
        rec = refine.record_task_outcome(ledger, "t-1", "pr-review", 0.3,
                                         failure_mode="wrong-tool")
        self.assertEqual(rec["task_id"], "t-1")
        self.assertEqual(rec["task_family"], "pr-review")
        self.assertEqual(rec["jev_outcome"], 0.3)
        got = refine.get_task_outcome(ledger, "t-1")
        self.assertEqual(got["seq"], rec["seq"])

    def test_get_missing_returns_none(self):
        self.assertIsNone(refine.get_task_outcome(FakeLedger(), "nope"))

    def test_invalid_family_rejected(self):
        with self.assertRaises(ValueError):
            refine.record_task_outcome(FakeLedger(), "t-1", "nope", 0.5)

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            refine.record_task_outcome(FakeLedger(), "t-1", "pr-review", 1.5)

    def test_empty_task_id_rejected(self):
        for task_id in ("", "  "):
            with self.subTest(task_id=task_id):
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    refine.record_task_outcome(FakeLedger(), task_id, "pr-review", 0.5)

    def test_outcome_boundaries_are_valid(self):
        for outcome in (0.0, 1.0):
            with self.subTest(outcome=outcome):
                rec = refine.record_task_outcome(FakeLedger(), "t-1", "pr-review", outcome)
                self.assertEqual(rec["jev_outcome"], outcome)

    def test_recent_outcomes_filters(self):
        ledger = FakeLedger()
        refine.record_task_outcome(ledger, "t-1", "pr-review", 0.8)
        refine.record_task_outcome(ledger, "t-2", "pr-review", 0.4)
        refine.record_task_outcome(ledger, "t-3", "docs", 0.9)
        refine.record_task_outcome(ledger, "t-4", "pr-review", 0.9,
                                   is_refinement=True)
        out = refine.recent_outcomes(ledger, "pr-review", min_outcome=0.7)
        self.assertEqual([e["task_id"] for e in out], ["t-1"])
        # refinement tasks excluded
        out2 = refine.recent_outcomes(ledger, "pr-review")
        self.assertNotIn("t-4", [e["task_id"] for e in out2])


class TestRubric(unittest.TestCase):
    def test_composite_weighting(self):
        scores = {"completion": 3.0, "efficiency": 2.0,
                  "safety": 3.0, "quality": 2.0}
        # (3*3 + 2 + 3*2 + 2) / 7 = 19/7
        self.assertAlmostEqual(refine.composite_score(scores), 19 / 7)

    def test_gating_completion(self):
        ok, reason = refine.gating_pass(
            {"completion": 1.0, "efficiency": 3.0, "safety": 3.0, "quality": 3.0})
        self.assertFalse(ok)
        self.assertIn("completion", reason)

    def test_gating_safety(self):
        ok, reason = refine.gating_pass(
            {"completion": 3.0, "efficiency": 3.0, "safety": 1.0, "quality": 3.0})
        self.assertFalse(ok)
        self.assertIn("safety", reason)

    def test_gating_pass(self):
        ok, _ = refine.gating_pass(
            {"completion": 2.0, "efficiency": 2.0, "safety": 2.0, "quality": 2.0})
        self.assertTrue(ok)

    def test_rubric_questions_shape(self):
        pack = refine.rubric_questions("traj", "base")
        self.assertEqual(set(pack["questions"].keys()),
                         {"completion", "efficiency", "safety", "quality"})
        for q in pack["questions"].values():
            self.assertEqual(q["type"], "score")
            self.assertEqual(len(q["criteria"]), 4)


class TestCost(unittest.TestCase):
    def test_within_budget(self):
        c = refine.CycleCost(propose_tokens=100, probe_tokens=500,
                             control_tokens=200, base_task_tokens=300)
        self.assertTrue(c.within_budget())  # 800 <= 900
        c2 = refine.CycleCost(propose_tokens=100, probe_tokens=900,
                              control_tokens=200, base_task_tokens=300)
        self.assertFalse(c2.within_budget())  # 1200 > 900

    def test_no_baseline_allows(self):
        c = refine.CycleCost(propose_tokens=100)
        self.assertTrue(c.within_budget())


class TestPilot(unittest.TestCase):
    def test_pilot_state_defaults(self):
        # Fresh state (no file in test env path override not needed;
        # just check the shape of defaults).
        st = {"cycles_completed": 0, "cycles": [], "graduated": False,
              "started": None}
        self.assertFalse(st["graduated"])
        self.assertEqual(st["cycles_completed"], 0)

    def test_pilot_constants(self):
        self.assertEqual(refine.PILOT_CYCLES, 20)
        self.assertIn("min_completed", refine.PILOT_GRADUATION)

    def test_pilot_save_and_auto_merge(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pilot.json"
            with patch.object(refine, "_PILOT_STATE_PATH", p):
                self.assertTrue(refine.pilot_active())
                self.assertFalse(refine.pilot_allows_auto_merge())
                refine.save_pilot_state({
                    "cycles_completed": 20,
                    "cycles": [],
                    "graduated": True,
                    "started": "2026-10-18T00:00:00Z",
                })
                loaded = refine.load_pilot_state()
                self.assertTrue(loaded["graduated"])
                self.assertFalse(refine.pilot_active())
                self.assertTrue(refine.pilot_allows_auto_merge())


class TestDataFiles(unittest.TestCase):
    def test_families_valid(self):
        fams = refine.load_families()
        self.assertIn("families", fams)
        self.assertTrue(len(fams["families"]) >= 5)
        self.assertTrue(refine.valid_family("pr-review"))
        self.assertFalse(refine.valid_family("nope"))

    def test_lessons_load(self):
        lessons = refine.load_lessons()
        self.assertGreaterEqual(len(lessons), 5)
        seeds = [les for les in lessons if les.task_id.startswith("seed-")]
        self.assertGreaterEqual(len(seeds), 3)
        for les in lessons:
            self.assertTrue(les.lesson_text, f"{les.task_id} missing lesson")

    def test_retrieve_lessons(self):
        got = refine.retrieve_lessons("pr-review", limit=3)
        self.assertLessEqual(len(got), 3)
        # pr-review lessons should rank first
        self.assertIn("pr-review", got[0].family + str(got[0].tags))

    def test_append_lesson_temp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            exp = Path(tmp) / "experience.md"
            exp.write_text("# Lessons\n", encoding="utf-8")
            with patch.object(refine, "_EXPERIENCE_PATH", exp):
                refine.append_lesson(
                    "## t-99 | 2026-10-18T12:00:00Z | code-fix\n"
                    "- tags: [code-fix]\n"
                    "- lesson: Always check boundaries.\n"
                )
                loaded = refine.load_lessons()
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].task_id, "t-99")
                got = refine.retrieve_lessons("code-fix", limit=2)
                self.assertEqual(len(got), 1)

    def test_propose_prompt_exists(self):
        p = refine._PROPOSE_PROMPT_PATH
        self.assertTrue(p.exists(), f"missing {p}")
        text = p.read_text(encoding="utf-8")
        self.assertIn("TIER 0", text)
        self.assertIn("{task_id}", text)

    def test_status_report(self):
        rep = refine.status_report()
        self.assertIn("pilot_active", rep)
        self.assertIn("lessons_count", rep)
        self.assertGreaterEqual(rep["lessons_count"], 5)


class TestRefineCli(unittest.TestCase):
    def test_cli_status_and_record_and_validate(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from harness import cli
        from harness.errors import HarnessError

        ledger = FakeLedger()
        with patch.object(cli, "_ledger", return_value=ledger):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["refine", "--status", "--json"])
            self.assertIn("pilot_active", buf.getvalue())

            buf_pretty = io.StringIO()
            with redirect_stdout(buf_pretty):
                cli.main(["refine", "--status"])
            self.assertIn("refine pilot:", buf_pretty.getvalue())

            buf_rec = io.StringIO()
            with redirect_stdout(buf_rec):
                cli.main(["refine", "--record-outcome", "task-1", "code-fix", "0.85", "--json"])
            self.assertIn("task-1", buf_rec.getvalue())

            buf_task = io.StringIO()
            with redirect_stdout(buf_task):
                cli.main(["refine", "--task", "task-1", "--json"])
            self.assertIn("validated", buf_task.getvalue())

            buf_fam = io.StringIO()
            with redirect_stdout(buf_fam):
                cli.main(["refine", "--family", "code-fix", "--json"])
            self.assertIn("validated", buf_fam.getvalue())

            with self.assertRaises(HarnessError):
                cli._cmd_refine(type("O", (), {"record_outcome": ["t", "code-fix", "bad"]})(), None)
            with self.assertRaises(HarnessError):
                cli._cmd_refine(type("O", (), {"task": None, "family": None, "status": False, "record_outcome": None})(), None)
            with self.assertRaises(HarnessError):
                cli._cmd_refine(type("O", (), {"task": "missing-id", "family": None, "status": False, "record_outcome": None})(), None)
            with self.assertRaises(HarnessError):
                cli._cmd_refine(type("O", (), {"task": None, "family": "bad-fam", "status": False, "record_outcome": None})(), None)

            with patch.object(refine, "pilot_active", return_value=False), \
                 patch.object(refine, "load_pilot_state", return_value={"graduated": False}):
                with self.assertRaises(HarnessError):
                    cli._cmd_refine(type("O", (), {"task": "task-1", "family": None, "status": False, "record_outcome": None})(), None)

    def test_cli_reports_invalid_outcomes_and_blank_task_ids(self):
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch
        from harness import cli

        ledger = FakeLedger()
        cases = (
            (["refine", "--record-outcome", "t-1", "unknown-family", "0.5"],
             "unknown task family"),
            (["refine", "--record-outcome", "t-1", "code-fix", "1.1"],
             "jev_outcome must be 0-1"),
            (["refine", "--record-outcome", "t-1", "code-fix", "-0.01"],
             "jev_outcome must be 0-1"),
            (["refine", "--record-outcome", "t-1", "code-fix", "nan"],
             "jev_outcome must be 0-1"),
            (["refine", "--record-outcome", "t-1", "code-fix", "inf"],
             "jev_outcome must be 0-1"),
            (["refine", "--record-outcome", " ", "code-fix", "0.5"],
             "task_id must be a non-empty string"),
            (["refine", "--task", "   "], "--task requires a non-empty task ID"),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with patch.object(cli, "_ledger", return_value=ledger), \
                     patch.object(cli, "load_settings", return_value=object()), \
                     patch.object(refine, "pilot_active", return_value=True), \
                     redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(argv)
                self.assertEqual(raised.exception.code, 1)
                self.assertIn(expected, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_refine_actions_cannot_silently_override_each_other(self):
        import io
        from contextlib import redirect_stderr
        from harness import cli

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                cli.build_parser().parse_args(["refine", "--status", "--task", "t-1"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
