"""harness dogfood: three fail-closed phases (ground -> verify -> apply)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import cli, session
from harness.claims import load_claims_manifest


class DogfoodPhaseGateTests(unittest.TestCase):
    """Hermetic tests pin the gates (ground -> verify -> apply) and the
    exit-code policy, with the verify and apply lanes mocked at their seams."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = os.path.join(self.dir.name, "target.py")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("HARD_MAX_COST = 0.10\nDEFAULT_MAX_COST = 0.02\n")
        self.claims = os.path.join(self.dir.name, "claims.json")

    def _write_claims(self, text):
        with open(self.claims, "w", encoding="utf-8") as f:
            json.dump({"claims": [{"id": "c1", "text": text}]}, f)

    def _argv(self):
        return ["dogfood", "--claims-file", self.claims,
                "--source-file", self.src, "--file", self.src,
                "--instruction", "fix the ceiling", "--verify", "python -m unittest"]

    def _run(self, argv, *, verify_result, apply_result=None, captured):
        """Patch the two seams dogfood consumes: the verify lane's verdict and
        the session that produces the apply engine. The real settings/ledger
        flow underneath, so the ground phase exercises the hermetic lint."""
        engine = mock.Mock()
        engine.apply_batch.return_value = apply_result or {"status": "ok", "cost": 0.0}
        with mock.patch.object(cli, "_run_claims_verify", return_value=verify_result), \
             mock.patch.object(cli, "_session", return_value=engine), \
             mock.patch.object(cli, "_emit", side_effect=lambda payload, out: captured.append(payload)):
            cli.main(argv)
        return engine

    def _phases(self, captured):
        return [p["dogfood_phase"] for p in captured if "dogfood_phase" in p]

    def test_ungrounded_claims_stop_before_any_network(self):
        # R1: a load-bearing absence phrase with no source_refs is rejected
        # hermetically, before the verify phase could ever be entered.
        self._write_claims("There is no cap on max_cost")
        captured = []
        with self.assertRaises(SystemExit) as cm:
            self._run(self._argv(), verify_result={}, captured=captured)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self._phases(captured), ["ground"])

    def test_unconfirmed_defect_stops_before_apply_exit_3(self):
        self._write_claims("The configured default task ceiling is 0.02")
        verify = {"convergence": {"tally": {"claims": {"c1": {
            "converged": True, "verdict": "not_real"}}}}}
        captured = []
        with self.assertRaises(SystemExit) as cm:
            self._run(self._argv(), verify_result=verify, captured=captured)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(self._phases(captured), ["ground", "verify"])

    def test_panel_shortfall_is_fail_closed_even_if_majority_says_real(self):
        self._write_claims("The configured default task ceiling is 0.02")
        verify = {"convergence": {"tally": {"claims": {"c1": {
            "converged": False, "verdict": "real"}}}}}
        captured = []
        with self.assertRaises(SystemExit) as cm:
            self._run(self._argv(), verify_result=verify, captured=captured)
        self.assertEqual(cm.exception.code, 3)  # shortfall never authorizes an edit

    def test_confirmed_defect_reaches_apply_with_gate_and_instruction(self):
        self._write_claims("The configured default task ceiling is 0.02")
        verify = {"convergence": {"tally": {"claims": {"c1": {
            "converged": True, "verdict": "real"}}}}}
        captured = []
        engine = self._run(self._argv(), verify_result=verify, captured=captured)
        kwargs = engine.apply_batch.call_args.kwargs
        self.assertEqual(kwargs["verify_cmd"], "python -m unittest")
        self.assertEqual(kwargs["instruction"], "fix the ceiling")
        self.assertEqual(self._phases(captured), ["ground", "verify", "apply"])

    def test_deferred_apply_exits_3_with_full_evidence(self):
        self._write_claims("The configured default task ceiling is 0.02")
        verify = {"convergence": {"tally": {"claims": {"c1": {
            "converged": True, "verdict": "real"}}}}}
        captured = []
        with self.assertRaises(SystemExit) as cm:
            self._run(self._argv(), verify_result=verify,
                      apply_result={"status": "deferred", "cost": 0.0},
                      captured=captured)
        self.assertEqual(cm.exception.code, 3)
        report = [p for p in captured if "phases" in p][-1]
        self.assertEqual(report["status"], "incomplete")


class CliCeilingWiringTests(unittest.TestCase):
    def _run(self, argv, captured):
        def fake_governor(settings, max_cost_override=None):
            captured["override"] = max_cost_override
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.max_cost = max_cost_override or settings.max_cost
            gov.spent = 0.0
            gov.preflight.return_value = (0.0, [])
            gov.check_byok.return_value = None
            gov.learned_blocked.return_value = False
            gov.record_actual.return_value = None
            gov.is_free.return_value = True
            gov.fetch_models.return_value = []
            return "key", gov

        # Both composition seams: capabilities reaches the governor through
        # cli's alias, bench through session.apply_session.
        with mock.patch.object(cli, "_governor", side_effect=fake_governor), \
             mock.patch.object(session, "governor_for", side_effect=fake_governor), \
             mock.patch.object(session, "HttpTransport"), \
             mock.patch.object(session, "AutonomyLedger", mock.MagicMock()):
            cli.main(argv)

    def test_bench_max_cost_reaches_governor(self):
        captured = {}
        with mock.patch.object(cli, "load_manifest", return_value=[]), \
             mock.patch.object(cli, "run_bench", return_value={"bench": {"statuses": {}}}):
            self._run(["bench", "tasks.json", "--max-cost", "0.004"], captured)
        self.assertEqual(captured["override"], 0.004)

    def test_capabilities_max_cost_reaches_governor(self):
        captured = {}
        # Patch where the name is USED: cli binds capability policy at module
        # level (architecture guard), so the patch target is the cli binding.
        with mock.patch.object(cli, "ensure_profiles",
                               return_value=({}, 0.0, False)):
            self._run(["capabilities", "--max-cost", "0.03"], captured)
        self.assertEqual(captured["override"], 0.03)

    def test_default_ceiling_when_flag_absent(self):
        captured = {}
        with mock.patch.object(cli, "ensure_profiles",
                               return_value=({}, 0.0, False)):
            self._run(["capabilities"], captured)
        self.assertIsNone(captured["override"])


class FromLedgerWiringTests(unittest.TestCase):
    """dogfood --from-ledger: curation is ledger-only (no key), the curated
    manifest persists via --claims-out, and the seeded chain reaches apply."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger_path = os.path.join(self.dir.name, "ledger.jsonl")
        from harness.ledger import AutonomyLedger
        led = AutonomyLedger(self.ledger_path)
        # Seed rule-1 evidence: one model fail-closed two runs.
        for t in ("t1", "t2"):
            led.append("dispatch_start", task_id=t, model="m/slow:free")
            led.append("abort", task_id=t, model="m/slow:free",
                       reason="verify rounds exhausted")
        self.target = os.path.join(self.dir.name, "target.py")
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        self.claims_out = os.path.join(self.dir.name, "curated.json")

    def _argv(self):
        return ["dogfood", "--from-ledger", "--claims-out", self.claims_out,
                "--file", self.target, "--instruction", "harden the gate",
                "--verify", "python -m unittest", "--task-id", "curate-1"]

    def _main(self, argv, captured):
        from harness.config import load_settings
        settings = load_settings()          # the REAL settings shape
        settings.ledger_path = self.ledger_path
        engine = mock.Mock()
        engine.apply_batch.return_value = {"status": "ok", "cost": 0.0}
        verify = {"convergence": {"tally": {"claims": {"c1": {
            "converged": True, "verdict": "real"}}}}, "panel_failures": []}
        with mock.patch.object(cli, "load_settings", return_value=settings), \
             mock.patch.object(cli, "_run_claims_verify", return_value=verify), \
             mock.patch.object(cli, "_session", return_value=engine), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda payload, out: captured.append(payload)):
            cli.main(argv)
        return engine

    def _phases(self, captured):
        return [p["dogfood_phase"] for p in captured if "dogfood_phase" in p]

    def test_curated_chain_reaches_apply_and_persists_manifest(self):
        captured = []
        engine = self._main(self._argv(), captured)
        self.assertEqual(self._phases(captured), ["ground", "verify", "apply"])
        report = [p for p in captured if "phases" in p][-1]
        self.assertEqual(report["status"], "ok")
        self.assertEqual(engine.apply_batch.call_args.kwargs["verify_cmd"],
                         "python -m unittest")
        # The curated manifest persisted and re-validates.
        ctx, claims = load_claims_manifest(self.claims_out)
        self.assertEqual(len(claims), 1)
        self.assertIn("m/slow:free", claims[0].text)
        # The curation event is ledgered (the loop's own audit trail).
        from harness.ledger import AutonomyLedger
        events = [e["event"] for e in AutonomyLedger(self.ledger_path).entries()]
        self.assertIn("dogfood_curate", events)

    def test_from_ledger_conflicts_with_claims_file(self):
        captured = []
        claims = os.path.join(self.dir.name, "claims.json")
        with open(claims, "w", encoding="utf-8") as f:
            json.dump({"claims": [{"id": "c1", "text": "x"}]}, f)
        argv = self._argv() + ["--claims-file", claims]
        with self.assertRaises(SystemExit) as cm:
            self._main(argv, captured)
        self.assertEqual(cm.exception.code, 1)

    def test_from_ledger_requires_claims_out(self):
        argv = [a for a in self._argv() if a != "--claims-out" and a != self.claims_out]
        with self.assertRaises(SystemExit) as cm:
            self._main(argv, [])
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
