"""Bipolar trust (-11..+11): cold-start unknown, slow earn, fast distrust.

All hermetic: no network, no key. Pure scoring works on plain report
dicts; gate tests use a stub ledger or temp real ledgers.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness import trust
from harness.continuation import gate_id
from harness.errors import HarnessError


def _report(calibration=None, completions=0, trust_gates=0):
    return {"calibration": calibration or {},
            "completions": completions,
            "trust_gates": trust_gates}


def _entry(**kw):
    base = {"confident_passed": 0, "success_pass": 0, "success_fail": 0,
            "structured_pass": 0, "structured_fail": 0,
            "unusable_outputs": 0, "consent_unusable": 0,
            "trust_denials": 0, "trust_hostile": 0}
    base.update(kw)
    return base


def _host_report(completions=0, gates=0, hostile=0):
    return {"calibration": {}, "completions": completions,
            "trust_gates": gates, "trust_hostile": hostile}


class StubLedger:
    """Minimal ledger double: records appends, never touches disk."""

    def __init__(self):
        self.events = []

    def append(self, event, task_id=None, **fields):
        self.events.append({"event": event, "task_id": task_id, **fields})
        return {"event": event}

    def participation_report(self):
        return _report()


class ScaleTests(unittest.TestCase):
    def test_cold_start_is_unknown(self):
        report = _report()
        self.assertEqual(trust.model_trust("ghost/model", report)[0], 0)
        self.assertEqual(trust.host_trust(report)[0], 0)
        self.assertEqual(trust.author_trust()[0], 0)
        self.assertEqual(trust.correctness_level("ghost/model", report), 0)
        self.assertEqual(trust.ceiling_fraction(0), 0.2)

    def test_unknown_reasons_say_unknown(self):
        _, reasons = trust.model_trust("ghost/model", _report())
        self.assertIn("unknown", ";".join(reasons).lower())


class EarnSlowlyTests(unittest.TestCase):
    def test_three_clean_runs_earn_one_level(self):
        report = _report({"m": _entry(success_pass=3)})
        self.assertEqual(trust.model_trust("m", report)[0], 1)

    def test_ramp_caps_at_eleven(self):
        report = _report({"m": _entry(success_pass=300, structured_pass=300)})
        score, _ = trust.model_trust("m", report)
        self.assertEqual(score, 11)

    def test_host_completions_earn_slowly(self):
        self.assertEqual(trust.host_trust(_report(completions=6))[0], 2)
        self.assertEqual(trust.host_trust(_report(completions=300))[0], 11)


class DistrustFastTests(unittest.TestCase):
    def test_ordinary_misses_do_not_move_trust(self):
        # A caught miss is the gate working, and round-level misses are
        # normal iteration: they ration correctness (ceilings), never trust
        # (mutation privilege).
        report = _report({"m": _entry(success_pass=9, success_fail=2)})
        self.assertEqual(trust.model_trust("m", report)[0], 3)
        self.assertEqual(trust.correctness_level("m", report), 2)

    def test_unusable_hygiene_is_bounded(self):
        # Protocol sloppiness costs at most HYGIENE_CAP: rotation already
        # handles it, and uncapped counts let sheer volume swamp history.
        report = _report({"m": _entry(success_pass=12, unusable_outputs=2)})
        self.assertEqual(trust.model_trust("m", report)[0], 2)
        report = _report({"m": _entry(success_pass=12, unusable_outputs=99)})
        self.assertEqual(trust.model_trust("m", report)[0], 1)

    def test_soft_denial_costs_one(self):
        score, _ = trust.host_trust(_host_report(completions=9, gates=1))
        # 3 levels - 1 = 2: guidance friction, not malice
        self.assertEqual(score, 2)

    def test_hostile_denial_costs_four(self):
        score, _ = trust.host_trust(_host_report(completions=9, gates=1,
                                                 hostile=1))
        # 3 levels - 4 = -1 (preview-only)
        self.assertEqual(score, -1)

    def test_two_hostile_denials_refuse(self):
        score, _ = trust.host_trust(_host_report(completions=6, gates=2,
                                                 hostile=2))
        self.assertLessEqual(score, trust.REFUSE_AT_OR_BELOW)

    def test_all_fail_rations_but_does_not_refuse_trust(self):
        # Incompetence is not malice: trust stays unknown (mutation still
        # needs its gate), while correctness carries -11 and rations the
        # budget to the floor. Refuse is reserved for hostile denials.
        report = _report({"m": _entry(success_fail=99)})
        self.assertEqual(trust.model_trust("m", report)[0], 0)
        self.assertEqual(trust.correctness_level("m", report), -11)
        led = StubLedger()
        # ...so the default 5c task ceiling is denied, but a floor-budget
        # gated run is still allowed: the joint policy in one assertion.
        with self.assertRaises(HarnessError):
            trust.check_apply(
                ledger=led, report=report, model="m", resumed=False,
                verify_only=False, verify_cmd="check", task_max_cost=0.05,
                task_id="t", hard_task_cap=0.25)
        out = trust.check_apply(
            ledger=led, report=report, model="m", resumed=False,
            verify_only=False, verify_cmd="check", task_max_cost=0.025,
            task_id="t", hard_task_cap=0.25)
        self.assertEqual(out["combined"], 0)

    def test_hostile_model_denials_refuse(self):
        report = _report({"m": _entry(success_pass=3, trust_denials=3,
                                       trust_hostile=3)})
        self.assertLessEqual(trust.model_trust("m", report)[0],
                             trust.REFUSE_AT_OR_BELOW)


class CombinedTests(unittest.TestCase):
    def test_weakest_link_wins(self):
        self.assertEqual(trust.combined_trust(5, -3), -3)
        self.assertEqual(trust.combined_trust(-8, 11), -8)

    def test_author_ignored_for_fresh_applies(self):
        # A fresh apply has no state author; the standing author==0 must
        # not cap a proven host+model pair.
        self.assertEqual(trust.combined_trust(7, 9), 7)
        self.assertEqual(trust.combined_trust(7, 9, 0), 0)

    def test_gate_bands(self):
        self.assertEqual(trust.gate_for_write_exec(-11), "refuse")
        self.assertEqual(trust.gate_for_write_exec(-6), "refuse")
        self.assertEqual(trust.gate_for_write_exec(-5), "preview-only")
        self.assertEqual(trust.gate_for_write_exec(-1), "preview-only")
        self.assertEqual(trust.gate_for_write_exec(0), "allow")
        self.assertEqual(trust.gate_for_write_exec(11), "allow")


class CorrectnessRationsCeilingTests(unittest.TestCase):
    def test_fractions(self):
        self.assertEqual(trust.ceiling_fraction(-4), 0.1)
        self.assertEqual(trust.ceiling_fraction(0), 0.2)
        self.assertEqual(trust.ceiling_fraction(3), 0.5)
        self.assertEqual(trust.ceiling_fraction(8), 1.0)
        self.assertEqual(trust.ceiling_fraction(11), 1.0)

    def test_unknown_unlocks_exactly_todays_defaults(self):
        # 0.2 of the 10c session cap == DEFAULT_MAX_COST (2c);
        # 0.2 of the 25c task cap == DEFAULT_TASK_MAX_COST (5c).
        self.assertAlmostEqual(
            trust.rationed_session_ceiling(0.10, 0, 0.10), 0.02)
        self.assertAlmostEqual(
            trust.rationed_task_ceiling(0.25, 0, 0.25), 0.05)

    def test_fails_push_correctness_negative(self):
        report = _report({"m": _entry(success_fail=9)})
        self.assertLess(trust.correctness_level("m", report), 0)

    def test_correctness_ignores_safety_strikes(self):
        # Unusable outputs move trust but not correctness: the answers
        # that did arrive were right.
        report = _report({"m": _entry(success_pass=9, unusable_outputs=9)})
        self.assertGreater(trust.correctness_level("m", report), 0)
        self.assertLessEqual(trust.model_trust("m", report)[0], 0)


class CheckApplyTests(unittest.TestCase):
    def test_fresh_preview_allowed(self):
        led = StubLedger()
        out = trust.check_apply(
            ledger=led, report=_report(), model="m", resumed=False,
            verify_only=True, verify_cmd=None, task_max_cost=0.05,
            task_id="t", hard_task_cap=0.25)
        self.assertEqual(out["combined"], 0)
        self.assertEqual(led.events, [])

    def test_fresh_gated_write_allowed_at_default_ceiling(self):
        led = StubLedger()
        out = trust.check_apply(
            ledger=led, report=_report(), model="m", resumed=False,
            verify_only=False, verify_cmd="check", task_max_cost=0.05,
            task_id="t", hard_task_cap=0.25)
        self.assertEqual(out["combined"], 0)
        self.assertEqual(led.events, [])

    def test_dispatch_does_not_demand_gate_yet(self):
        # Consent/readiness/deferral paths run gateless; only the write
        # is gated (check_mutation). A fresh gateless dispatch passes.
        led = StubLedger()
        out = trust.check_apply(
            ledger=led, report=_report(), model="m", resumed=False,
            verify_only=False, verify_cmd=None, task_max_cost=0.01,
            task_id="t", hard_task_cap=0.25)
        self.assertEqual(out["combined"], 0)

    def test_preview_band_denies_write_with_guidance(self):
        led = StubLedger()
        report = _report({"m": _entry(success_pass=6, unusable_outputs=6,
                                       trust_denials=1)})  # 2-3-1 = -2
        with self.assertRaisesRegex(HarnessError, "verify_only=true"):
            trust.check_apply(
                ledger=led, report=report, model="m", resumed=False,
                verify_only=False, verify_cmd="check", task_max_cost=0.01,
                task_id="t", hard_task_cap=0.25)
        self.assertEqual(led.events[0]["event"], "trust_gate")

    def test_refuse_band_denies_writes_but_allows_preview(self):
        led = StubLedger()
        report = _report({"m": _entry(success_pass=3, trust_denials=3,
                                       trust_hostile=3)})
        # A pure proposal writes nothing and runs nothing: allowed even
        # at refuse-level trust (spend governor still bounds the call).
        out = trust.check_apply(
            ledger=led, report=report, model="m", resumed=True,
            verify_only=True, verify_cmd=None, task_max_cost=0.01,
            task_id="t", hard_task_cap=0.25)
        self.assertEqual(out["combined"], -11)
        # ...while any mutation or gate execution is refused.
        with self.assertRaisesRegex(HarnessError, "refuse"):
            trust.check_apply(
                ledger=led, report=report, model="m", resumed=True,
                verify_only=False, verify_cmd="check",
                task_max_cost=0.01, task_id="t", hard_task_cap=0.25)

    def test_over_allowance_ceiling_denied_with_number(self):
        led = StubLedger()
        with self.assertRaisesRegex(HarnessError, r"\$0\.05"):
            trust.check_apply(
                ledger=led, report=_report(), model="m", resumed=False,
                verify_only=False, verify_cmd="check", task_max_cost=0.25,
                task_id="t", hard_task_cap=0.25)

    def test_proven_correctness_unlocks_full_cap(self):
        led = StubLedger()
        report = _report({"m": _entry(success_pass=60)})
        out = trust.check_apply(
            ledger=led, report=report, model="m", resumed=False,
            verify_only=False, verify_cmd="check", task_max_cost=0.25,
            task_id="t", hard_task_cap=0.25)
        self.assertEqual(out["allowed_task_ceiling"], 0.25)


class CheckMutationTests(unittest.TestCase):
    def test_unknown_gateless_write_denied(self):
        led = StubLedger()
        with self.assertRaisesRegex(HarnessError, "verify_cmd"):
            trust.check_mutation(ledger=led, combined=0, verify_cmd=None,
                                 task_id="t", model="m")
        self.assertEqual(led.events[0]["event"], "trust_gate")

    def test_unknown_gated_write_allowed(self):
        led = StubLedger()
        trust.check_mutation(ledger=led, combined=0, verify_cmd="check",
                             task_id="t", model="m")
        self.assertEqual(led.events, [])

    def test_refuse_level_write_denied(self):
        led = StubLedger()
        with self.assertRaisesRegex(HarnessError, "refuse"):
            trust.check_mutation(ledger=led, combined=-7,
                                 verify_cmd="check", task_id="t", model="m")

    def test_trusted_write_allowed(self):
        led = StubLedger()
        trust.check_mutation(ledger=led, combined=4, verify_cmd=None,
                             task_id="t", model="m")
        self.assertEqual(led.events, [])


class TrustStatusTests(unittest.TestCase):
    def test_host_only_shape(self):
        out = trust.trust_status(_report())
        self.assertEqual(out["host"]["score"], 0)
        self.assertEqual(out["scale"]["max"], 11)
        self.assertNotIn("model", out)

    def test_model_shape(self):
        out = trust.trust_status(_report({"m": _entry(success_pass=6)}),
                                 model="m")
        self.assertEqual(out["model"]["score"], 2)
        self.assertEqual(out["combined"], 0)  # host unknown caps it
        self.assertIn("session_fraction", out["correctness"])


class SessionCapTests(unittest.TestCase):
    def test_max_cost_override_capped_at_hard(self):
        """C2: an explicit --max-cost past HARD_MAX_COST must refuse,
        not silently raise the ceiling."""
        from harness import session
        from harness.config import HARD_MAX_COST
        settings = SimpleNamespace(max_cost=0.02, expect_key_label=None)
        with mock.patch.object(session, "resolve_api_key",
                               return_value="k"), \
             mock.patch.object(session, "HttpTransport"), \
             mock.patch("harness.spend.SpendGovernor.verify_key",
                        return_value=None):
            with self.assertRaises(HarnessError):
                session.governor_for(settings, HARD_MAX_COST + 1.0)
            _, gov = session.governor_for(settings, 0.05)
        self.assertEqual(gov.max_cost, 0.05)


class RetargetTests(unittest.TestCase):
    def test_continuation_file_retarget_refused(self):
        """C4: a saved gate+hash for file A must not authorize file B."""
        from tests._applyfixture import ApplyFixture
        from harness.continuation import validate_continuation
        fix = ApplyFixture()
        fix.setUp()
        try:
            target_a = fix.make_file()
            with open(os.path.join(fix.dir.name, "b.py"), "w",
                      encoding="utf-8") as f:
                f.write("other = 1\n")
            target_b = os.path.join(fix.dir.name, "b.py")
            cmd = "check"
            cont = {"schema_version": 1, "file_path": target_a,
                    "task_id": "t", "backend": "harness",
                    "verify_only": False, "max_lines": 500,
                    "verify_cmd": cmd, "verify_gate_id": gate_id(cmd),
                    "verification_required": True}
            validate_continuation(cont)  # sane fixture, then retarget it
            _, _, ledger, engine = fix.make_env()
            with self.assertRaisesRegex(HarnessError, "does not match"):
                engine.apply_edit(task_id="t", file_path=target_b,
                                  instruction="change", continuation=cont,
                                  require_consent=False)
            events = [e["event"] for e in ledger.entries()]
            self.assertIn("trust_gate", events)
        finally:
            fix.tearDown()


class EngineWriteGateTests(unittest.TestCase):
    def test_gateless_write_refused_at_unknown_trust(self):
        """Unknown trust may run consent/model lanes but may not land
        unreviewed bytes: the write-time gate refuses."""
        from tests._applyfixture import ApplyFixture, ORIGINAL, CHANGED
        from tests._fake import comp
        fix = ApplyFixture()
        fix.setUp()
        try:
            from tests._fake import FakeTransport, m
            from harness.spend import SpendGovernor
            from harness.ledger import AutonomyLedger
            from harness.router import Router
            from harness.apply import ApplyEngine
            p = fix.make_file()
            fake = FakeTransport(
                models=[m("m/apply"), m("m/judge")],
                posts=[comp("HARNESS_READY: confident\n" + CHANGED)])
            gov = SpendGovernor(fake, "sk-test")
            ledger = AutonomyLedger(fix.ledger_path)
            engine = ApplyEngine(fake, "k", gov, ledger,
                                 Router(["a"], "m/judge", "m/apply"),
                                 default_require_consent=False,
                                 default_renew_consent=False)
            with self.assertRaisesRegex(HarnessError, "verify_cmd"):
                engine.apply_edit(task_id="t", file_path=p,
                                  instruction="change")
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), ORIGINAL)
        finally:
            fix.tearDown()


class McpTrustTests(unittest.TestCase):
    def _server(self):
        from harness.mcp import McpServer
        from harness.spend import SpendGovernor
        from harness.ledger import AutonomyLedger
        from harness.router import Router
        from harness.apply import ApplyEngine
        from tests._fake import FakeTransport, m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        transport = FakeTransport(models=[m("m/j")], posts=[])
        governor = SpendGovernor(transport, "sk-test")
        ledger = AutonomyLedger(os.path.join(tmp.name, "l.jsonl"))
        router = Router(["m/j"], "m/j", "m/j")
        engine = ApplyEngine(transport, "sk-test", governor, ledger,
                             router)
        server = McpServer(transport=transport, api_key="k",
                           governor=governor, ledger=ledger, router=router,
                           engine=engine, allowed_roots=[tmp.name])
        return server, ledger, tmp

    def test_refusals_ledger_trust_gate(self):
        server, ledger, _ = self._server()
        with self.assertRaises(HarnessError):
            server._invoke("apply_edit", {"file": "/etc/passwd",
                                          "instruction": "x",
                                          "verify_only": True})
        events = [e["event"] for e in ledger.entries()]
        self.assertIn("trust_gate", events)

    def test_trust_status_tool(self):
        server, _, _ = self._server()
        out = server._invoke("trust_status", {})
        self.assertEqual(out["host"]["score"], 0)
        out = server._invoke("trust_status", {"model": "m/j"})
        self.assertEqual(out["model"]["id"], "m/j")

    def test_tools_list_includes_trust_status(self):
        server, _, _ = self._server()
        names = {t["name"] for t in server._tools()}
        self.assertIn("trust_status", names)


class CliTrustTests(unittest.TestCase):
    def test_trust_command_is_read_only(self):
        """harness trust needs no key and no network: ledger only."""
        from harness import cli
        from harness.ledger import AutonomyLedger
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            ledger.append("complete", task_id="t", model="m")
            with mock.patch.object(cli, "_ledger", return_value=ledger), \
                 mock.patch.object(cli, "_emit") as emit:
                cli.main(["trust"])
        out = emit.call_args[0][0]
        self.assertEqual(out["host"]["score"], 0)

    def test_ledger_report_carries_trust(self):
        from harness import cli
        from harness.ledger import AutonomyLedger
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            with mock.patch.object(cli, "_ledger", return_value=ledger), \
                 mock.patch.object(cli, "_emit") as emit:
                cli.main(["ledger", "report"])
        out = emit.call_args[0][0]
        self.assertIn("trust", out)
        self.assertEqual(out["trust"]["host"]["score"], 0)


if __name__ == "__main__":
    unittest.main()
