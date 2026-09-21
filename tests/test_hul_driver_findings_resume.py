"""HUL-D until-limits driver tests (tests/test_hul_driver_findings_resume.py).

Covers: fixture reaches stall/limit with FINDINGS.md; resume continues after
interrupt; false-done blocked (unkeyed/low-success cannot alone complete);
honest complete only via HUL-C determination; interrupt-safe resume after
every attempt; dual-budget note when HUL-B absent.
"""
import os
import tempfile
import unittest
from pathlib import Path

from harness import mission_record as mr
from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger
from harness.mission_driver import (
    DEFAULT_STALL_LIMIT,
    HUL_B_DUAL_BUDGET_NOTE,
    artifact_names,
    findings_md,
    normalize_attempt,
    pack_probe_attempt,
    run_mission,
)


class _CountingGovernor:
    def __init__(self):
        self.reserved = []
        self.spent = 0.0
        self.max_cost = 1.0

    def reserve(self, worst, label):
        self.reserved.append((worst, label))
        return {"label": label, "worst": worst}

    def reconcile(self, reservation, cost):
        self.spent += float(cost or 0.0)

    def record_actual(self, cost, model):
        self.spent += float(cost or 0.0)


class _FixedEvaluator:
    def __init__(self, answers=None, *, keyed=True, fallback=False):
        self.api_key = "jev-key" if keyed else None
        self.model = "jev-test"
        self.answers = answers or {}
        self.fallback = fallback

    def evaluate(self, state, questions=None):
        if self.fallback:
            return JevEvaluationResult(
                "fail", 0.0, 0.0, {}, ["fallback"],
                is_fallback=True, model=self.model)
        return JevEvaluationResult(
            "pass", 0.8, 0.9, dict(self.answers), ["ok"],
            is_fallback=False, model=self.model)


def _good_answers():
    return {
        "scope_coverage": {
            "type": "score", "score": 0.9,
            "legend": {"0": 0.1, "1": 0.9},
            "probabilities": {"0": 0.1, "1": 0.9},
            "confidence": 0.85,
        },
        "success_definition_met": {"type": "noul", "noul": 0.95},
        "claims_supported": {"type": "noul", "noul": 0.9},
        "needs_human": {"type": "noul", "noul": 0.1},
        "complexity_class": {
            "type": "choice", "choice": "bounded",
            "probabilities": {"bounded": 0.8, "iterative": 0.1,
                              "architectural": 0.1},
            "confidence": 0.8,
        },
    }


def _unkeyed_settings():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


def _ledger(tmpdir):
    return AutonomyLedger(os.path.join(tmpdir, "ledger.jsonl"))


def _init_pack(root, mid="m-driver-1", *, max_cost=0.50, reserve=0.05):
    spec = mr.build_mission_spec(
        mission_id=mid,
        request="Drive mission attempts until limits",
        success_definition="Driver reaches terminal with FINDINGS",
        max_cost_usd=max_cost,
        terminal_reserve_cost_usd=reserve,
        in_scope=["harness/mission_driver.py"],
        out_of_scope=["live network dogfood"],
        persistence_root=str(root),
        verifier_kind="hermetic-local",
    )
    return mr.init_mission_pack(root, spec)


def _empty_attempt(_context):
    return {"ok": False, "error": "no work produced",
            "evidence_summary": "empty attempt"}


def _false_done_attempt(_context):
    return {
        "ok": True,
        "verifier_holds": True,
        "evidence_summary": "claimed done without scope hold",
        "notes": "builder claims complete",
    }


def _costly_attempt(context):
    return {
        "ok": True,
        "cost_usd": 0.20,
        "tokens": 10,
        "verifier_holds": True,
        "evidence_summary": f"attempt {context['attempt']} spent",
        "artifacts": [f"note-{context['attempt']}.md"],
    }


class NormalizeAttemptTests(unittest.TestCase):
    def test_normalize_defaults_and_coercion(self):
        receipt = normalize_attempt(None, attempt=3)
        self.assertEqual(receipt["kind"], "attempt")
        self.assertEqual(receipt["attempt"], 3)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["cost_usd"], 0.0)
        with self.assertRaises(HarnessError):
            normalize_attempt({"cost_usd": -1}, attempt=1)
        with self.assertRaises(HarnessError):
            normalize_attempt({"tokens": True}, attempt=1)
        with self.assertRaises(HarnessError):
            normalize_attempt("not-a-map", attempt=1)

    def test_findings_md_includes_driver_and_scope_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missions"
            root.mkdir()
            pack = _init_pack(root)
            body = findings_md(
                pack, outcome="stalled", reason="no progress",
                attempts=5, stall_counter=5, spent=0.0,
                working_remaining=0.45,
                determination={"complete": False, "is_fallback": True,
                               "site": "hul_scope", "reasons": ["unkeyed"]})
            self.assertIn("HUL-D", body)
            self.assertIn("stalled", body)
            self.assertIn("HUL-C", body)
            self.assertIn(HUL_B_DUAL_BUDGET_NOTE, body)
            body2 = findings_md(
                pack, outcome="blocked", reason="budget",
                attempts=1, stall_counter=0, spent=0.5,
                working_remaining=0.0, determination=None)
            self.assertIn("no scope gate", body2)


class DriverStallAndLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()
        self.ledger = _ledger(self.tmp.name)

    def test_default_stall_limit_is_five(self):
        self.assertEqual(DEFAULT_STALL_LIMIT, 5)

    def test_fixture_reaches_stall_with_findings(self):
        pack = _init_pack(self.root)
        summary = run_mission(pack, attempt_fn=_empty_attempt, stall_limit=5)
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["driver"]["status"], "stalled")
        self.assertEqual(summary["driver"]["attempts"], 5)
        self.assertTrue(pack.findings_md.is_file())
        body = pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("stalled", body)
        self.assertIn("HUL-D", body)
        self.assertIn("no new artifact/evidence", body)
        self.assertTrue(mr.is_terminal(pack))
        resume = mr.load_resume(pack)
        self.assertEqual(resume["status"], "stalled")
        receipts = mr.load_receipts(pack)
        self.assertEqual(len(receipts), 5)
        self.assertIn("HUL-B dual-budget", body)

    def test_cost_limit_blocked_with_findings(self):
        pack = _init_pack(self.root, mid="m-cost", max_cost=0.50, reserve=0.0)
        summary = run_mission(pack, attempt_fn=_costly_attempt,
                              stall_limit=10, max_attempts=20)
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["driver"]["status"], "blocked")
        self.assertTrue(
            "cost limit" in summary["driver"]["reason"]
            or "working_remaining" in summary["driver"]["reason"])
        body = pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("blocked", body)
        budget = mr.load_budget(pack)
        self.assertGreaterEqual(budget["spent"], 0.40)

    def test_max_attempts_limit(self):
        pack = _init_pack(self.root, mid="m-maxatt")
        summary = run_mission(pack, attempt_fn=_empty_attempt,
                              stall_limit=10, max_attempts=2)
        self.assertEqual(summary["driver"]["status"], "blocked")
        self.assertIn("max attempts", summary["driver"]["reason"])
        self.assertTrue(pack.findings_md.is_file())

    def test_max_tokens_and_error_limits(self):
        pack = _init_pack(self.root, mid="m-tokens")
        summary = run_mission(
            pack,
            attempt_fn=lambda ctx: {"tokens": 50, "artifacts": [f"t{ctx['attempt']}"]},
            stall_limit=10, max_attempts=10, max_tokens=60)
        self.assertEqual(summary["driver"]["status"], "blocked")
        self.assertIn("token limit", summary["driver"]["reason"])

        pack2 = _init_pack(self.root, mid="m-errors")
        summary2 = run_mission(
            pack2,
            attempt_fn=lambda ctx: {"error": f"e{ctx['attempt']}",
                                    "artifacts": [f"e{ctx['attempt']}"]},
            stall_limit=10, max_attempts=10, max_errors=2)
        self.assertEqual(summary2["driver"]["status"], "failed")
        self.assertIn("error limit", summary2["driver"]["reason"])

    def test_attempt_exception_counted_as_error(self):
        pack = _init_pack(self.root, mid="m-boom")

        def boom(_ctx):
            raise RuntimeError("attempt seat exploded")

        summary = run_mission(pack, attempt_fn=boom, stall_limit=3,
                              max_attempts=10)
        self.assertTrue(summary["terminal"])
        receipts = mr.load_receipts(pack)
        self.assertTrue(any(r.get("error") for r in receipts))

    def test_requires_attempt_fn(self):
        pack = _init_pack(self.root, mid="m-nofn")
        with self.assertRaises(HarnessError) as cm:
            run_mission(pack, attempt_fn=None)
        self.assertIn("attempt_fn", str(cm.exception))

    def test_already_terminal_returns_summary(self):
        pack = _init_pack(self.root, mid="m-done")
        mr.mark_terminal(pack, outcome="stalled", findings="# FINDINGS\n\nx\n")
        summary = run_mission(pack, attempt_fn=_empty_attempt)
        self.assertTrue(summary["driver"]["terminal"])
        self.assertIn("already terminal", summary["driver"]["reason"])


class DriverFalseDoneAndCompleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()
        self.ledger = _ledger(self.tmp.name)

    def test_false_done_blocked_without_scope_policy(self):
        """Builder claims verifier_holds — driver must not mark complete."""
        pack = _init_pack(self.root, mid="m-false-done")
        summary = run_mission(
            pack, attempt_fn=_false_done_attempt,
            scope_policy=None, stall_limit=3, max_attempts=10)
        self.assertTrue(summary["terminal"])
        self.assertNotEqual(summary["driver"]["status"], "complete")
        self.assertEqual(summary["driver"]["status"], "stalled")
        self.assertFalse(summary["driver"]["determination"]["complete"])
        body = pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("cannot alone mark mission complete", body)

    def test_false_done_blocked_when_scope_unkeyed(self):
        pack = _init_pack(self.root, mid="m-false-unkeyed")
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        summary = run_mission(
            pack, attempt_fn=_false_done_attempt,
            scope_policy=policy, stall_limit=3)
        self.assertEqual(summary["driver"]["status"], "stalled")
        self.assertFalse(summary["driver"]["determination"]["complete"])
        evals = mr.load_jev_evals(pack)
        self.assertGreaterEqual(len(evals), 1)
        self.assertTrue(all(e["determination"]["complete"] is False
                            for e in evals))

    def test_false_done_blocked_when_success_definition_low(self):
        answers = _good_answers()
        answers["success_definition_met"] = {"type": "noul", "noul": 0.1}
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_FixedEvaluator(answers),
            governor=_CountingGovernor(), ledger=self.ledger)
        pack = _init_pack(self.root, mid="m-false-low")
        summary = run_mission(
            pack, attempt_fn=_false_done_attempt,
            scope_policy=policy, stall_limit=3)
        self.assertEqual(summary["driver"]["status"], "stalled")
        self.assertFalse(summary["driver"]["determination"]["complete"])

    def test_honest_success_via_scope_determination(self):
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_FixedEvaluator(_good_answers()),
            governor=_CountingGovernor(), ledger=self.ledger)
        pack = _init_pack(self.root, mid="m-complete")
        summary = run_mission(
            pack,
            attempt_fn=lambda ctx: {
                "ok": True,
                "verifier_holds": True,
                "evidence_summary": "tests green and STATUS honest",
                "artifacts": [f"proof-{ctx['attempt']}.md"],
            },
            scope_policy=policy,
            stall_limit=5,
        )
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["driver"]["status"], "complete")
        self.assertTrue(summary["driver"]["determination"]["complete"])
        body = pack.findings_md.read_text(encoding="utf-8")
        self.assertIn("complete", body)
        self.assertIn("HUL-C", body)
        self.assertTrue(mr.is_terminal(pack))
        evals = mr.load_jev_evals(pack)
        self.assertTrue(evals[-1]["determination"]["complete"])


class DriverResumeInterruptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()

    def test_resume_pack_written_after_every_attempt(self):
        pack = _init_pack(self.root, mid="m-resume-each")
        observed = []

        def watching_attempt(ctx):
            state = mr.load_resume(pack)
            observed.append({
                "ctx_attempt": ctx["attempt"],
                "status": state.get("status"),
                "stall_counter": state.get("stall_counter"),
            })
            return {"artifacts": [f"a-{ctx['attempt']}.md"],
                    "evidence_summary": f"step {ctx['attempt']}"}

        run_mission(pack, attempt_fn=watching_attempt,
                    stall_limit=3, max_attempts=2)
        self.assertEqual(len(observed), 2)
        # Resume was rewritten before each attempt (interrupt-safe).
        self.assertEqual(observed[0]["ctx_attempt"], 1)
        self.assertEqual(observed[1]["ctx_attempt"], 2)
        self.assertTrue(all(o["status"] == "in_progress" for o in observed))
        self.assertTrue(mr.is_terminal(pack))
        resume = mr.load_resume(pack)
        self.assertEqual(resume["attempts"], 2)

    def test_resume_continues_after_partial_interrupt(self):
        """Simulate an interrupt after 2 productive attempts, then resume."""
        pack = _init_pack(self.root, mid="m-resume-cont")

        def first_leg(ctx):
            return {
                "ok": True,
                "artifacts": [f"leg1-{ctx['attempt']}.md"],
                "evidence_summary": f"leg1 {ctx['attempt']}",
            }

        summary1 = run_mission(pack, attempt_fn=first_leg,
                               stall_limit=10, max_attempts=2)
        self.assertEqual(summary1["driver"]["status"], "blocked")
        self.assertFalse(summary1["driver"]["determination"]["complete"])
        resume = mr.load_resume(pack)
        self.assertEqual(resume["attempts"], 2)
        self.assertTrue(mr.is_terminal(pack))

        # Reset to non-terminal to simulate operator clearing the limit and
        # resuming continuation-style (validate_resume accepts open status).
        resume["status"] = "in_progress"
        mr.write_resume(pack, resume)
        self.assertFalse(mr.is_terminal(pack))

        calls2 = {"n": 0}

        def second_leg(ctx):
            calls2["n"] += 1
            return {
                "ok": True,
                "artifacts": [f"leg2-{ctx['attempt']}.md"],
                "evidence_summary": f"leg2 {ctx['attempt']}",
            }

        summary2 = run_mission(pack, attempt_fn=second_leg,
                               stall_limit=10, max_attempts=4)
        # Continues from prior receipts (attempts already 2, then more).
        self.assertGreaterEqual(summary2["driver"]["attempts"], 2)
        self.assertGreaterEqual(calls2["n"], 1)
        receipts = mr.load_receipts(pack)
        self.assertGreaterEqual(len(receipts), 3)
        self.assertTrue(any("leg2" in str(r) for r in receipts))
        self.assertTrue(pack.findings_md.is_file())

    def test_interrupt_safe_resume_survives_reload(self):
        pack = _init_pack(self.root, mid="m-interrupt-safe")
        run_mission(pack,
                    attempt_fn=lambda ctx: {"artifacts": [f"x{ctx['attempt']}"],
                                            "evidence_summary": "e"},
                    stall_limit=5, max_attempts=3)
        # Reload pack from disk — state must be consistent.
        reloaded = mr.load_mission_pack(self.root, "m-interrupt-safe")
        resume = mr.load_resume(reloaded)
        self.assertEqual(resume["mission_id"], "m-interrupt-safe")
        self.assertGreaterEqual(resume["attempts"], 3)
        self.assertTrue(mr.is_terminal(reloaded))
        budget = mr.load_budget(reloaded)
        self.assertIn("working_remaining", budget)

    def test_pack_probe_attempt_empty_and_with_artifacts(self):
        pack = _init_pack(self.root, mid="m-probe")
        out = pack_probe_attempt({"pack": pack})
        self.assertFalse(out["ok"])
        self.assertIn("no artifacts", out["error"])
        (pack.artifacts_dir / "proof.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(artifact_names(pack), {"proof.md"})
        out2 = pack_probe_attempt({"pack": pack})
        self.assertTrue(out2["ok"])
        self.assertIn("proof.md", out2["artifacts"])
        out3 = pack_probe_attempt({})
        self.assertFalse(out3["ok"])

    def test_cli_run_policy_for_failure_falls_back_to_none_scope(self):
        """CLI mission run: policy_for HarnessError → unkeyed/None scope seat."""
        from unittest import mock
        from harness import cli
        from harness.errors import HarnessError as HE
        pack = _init_pack(self.root, mid="m-cli-policy-fail")
        out_path = str(Path(self.tmp.name) / "out.json")

        class _Opts:
            mission_cmd = "run"
            mission_id = "m-cli-policy-fail"
            root = str(self.root)
            stall_limit = 2
            max_attempts = None
            max_tokens = None
            max_errors = None

        opts = _Opts()
        opts.out = out_path
        with mock.patch.object(cli, "policy_for", side_effect=HE("no settings")):
            cli._cmd_mission(opts, None)
        with open(out_path, encoding="utf-8") as f:
            import json as _json
            result = _json.load(f)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["driver"]["status"], "stalled")
        self.assertTrue(pack.findings_md.is_file())


class DriverValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir()

    def test_missing_pack_and_bad_stall_limit(self):
        ghost = mr.MissionPack(self.root, "m-ghost")
        with self.assertRaises(HarnessError):
            run_mission(ghost, attempt_fn=_empty_attempt)
        pack = _init_pack(self.root, mid="m-badstall")
        with self.assertRaises(HarnessError):
            run_mission(pack, attempt_fn=_empty_attempt, stall_limit=0)
        with self.assertRaises(HarnessError):
            run_mission(pack, attempt_fn=_empty_attempt, max_attempts=-1)
        with self.assertRaises(HarnessError):
            run_mission(pack, attempt_fn="not-callable")


if __name__ == "__main__":
    unittest.main()
