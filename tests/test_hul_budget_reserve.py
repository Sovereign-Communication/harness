"""HUL-B dual-budget gate tests (tests/test_hul_budget_reserve.py).

Proves the dual envelope is real:

- attempt preflight / reserve / record refuse when worst-case would eat
  terminal_reserve
- terminal FINDINGS may spend up to the reserve only when the mission is
  terminal
- unkeyed / $0 paths stay honest (no fabricated spend, formula holds)
- mission budget.json numbers match the ONE formula
  ``working_remaining = max_cost_usd - spent - terminal_reserve.cost_usd``
"""
import tempfile
import unittest
from pathlib import Path

from harness import mission_record as mr
from harness.errors import HarnessError
from harness.jev import jev_cost
from harness.spend import (
    PHASE_ATTEMPT,
    PHASE_TERMINAL,
    SpendGovernor,
    assert_spend_allowed,
    dual_budget_envelope,
    working_remaining,
)
from tests._fake import FakeTransport, m, _gov, P1


def _spec(mid="m-hul-b-1", root="missions", max_cost=0.50, reserve=0.05):
    return mr.build_mission_spec(
        mission_id=mid,
        request="HUL-B dual budget reserve",
        success_definition="attempts never eat reserve; findings may",
        max_cost_usd=max_cost,
        terminal_reserve_cost_usd=reserve,
        in_scope=["spend.py", "mission_record.py"],
        out_of_scope=["HUL-D driver live loop"],
        persistence_root=root,
        verifier_kind="hermetic-local",
    )


class DualBudgetFormulaTests(unittest.TestCase):
    def test_working_remaining_is_the_one_formula(self):
        self.assertAlmostEqual(working_remaining(0.50, 0.10, 0.05), 0.35)
        self.assertAlmostEqual(working_remaining(0.50, 0.0, 0.05), 0.45)
        self.assertAlmostEqual(working_remaining(0.10, 0.20, 0.05), 0.0)
        # Unkeyed / $0 reserve: working remaining equals max - spent.
        self.assertAlmostEqual(working_remaining(0.25, 0.0, 0.0), 0.25)
        self.assertAlmostEqual(working_remaining(0.25, 0.10, 0.0), 0.15)

    def test_working_remaining_rejects_non_numbers(self):
        with self.assertRaises(HarnessError):
            working_remaining("bad", 0.0, 0.0)
        with self.assertRaises(HarnessError):
            working_remaining(0.1, None, 0.0)

    def test_mission_record_delegates_to_spend_owner(self):
        # Not a re-export facade: mission_record wraps the spend formula.
        self.assertIsNot(mr.working_remaining, working_remaining)
        self.assertAlmostEqual(
            mr.working_remaining(0.50, 0.10, 0.05),
            working_remaining(0.50, 0.10, 0.05))
        self.assertAlmostEqual(mr.working_remaining(0.50, 0.10, 0.05), 0.35)

    def test_dual_envelope_attempt_vs_terminal(self):
        env_a = dual_budget_envelope(0.50, 0.40, 0.05, phase=PHASE_ATTEMPT)
        self.assertAlmostEqual(env_a["working_remaining"], 0.05)
        self.assertAlmostEqual(env_a["phase_ceiling"], 0.45)
        self.assertAlmostEqual(env_a["phase_remaining"], 0.05)
        env_t = dual_budget_envelope(0.50, 0.40, 0.05, phase=PHASE_TERMINAL)
        self.assertAlmostEqual(env_t["working_remaining"], 0.05)
        self.assertAlmostEqual(env_t["phase_ceiling"], 0.50)
        self.assertAlmostEqual(env_t["phase_remaining"], 0.10)
        self.assertAlmostEqual(env_t["terminal_available"], 0.10)

    def test_assert_spend_allowed_refuses_attempt_into_reserve(self):
        # max 0.10, reserve 0.05, spent 0.04 → attempt may spend 0.01 only.
        assert_spend_allowed(0.10, 0.04, 0.01, 0.05, phase=PHASE_ATTEMPT)
        with self.assertRaises(HarnessError) as cm:
            assert_spend_allowed(0.10, 0.04, 0.02, 0.05, phase=PHASE_ATTEMPT)
        self.assertIn("terminal_reserve", str(cm.exception))

    def test_assert_spend_allowed_allows_terminal_into_reserve(self):
        assert_spend_allowed(0.10, 0.04, 0.06, 0.05, phase=PHASE_TERMINAL)
        with self.assertRaises(HarnessError):
            assert_spend_allowed(0.10, 0.04, 0.07, 0.05, phase=PHASE_TERMINAL)

    def test_assert_spend_allowed_zero_is_honest(self):
        self.assertEqual(
            assert_spend_allowed(0.10, 0.10, 0.0, 0.05, phase=PHASE_ATTEMPT),
            0.0)


class GovernorAttemptPreflightTests(unittest.TestCase):
    def test_attempt_preflight_refused_when_worst_case_eats_reserve(self):
        # Ceiling 0.10, reserve 0.05 → attempt working remaining 0.05.
        # A priced call whose worst-case is above 0.05 must refuse.
        fake = FakeTransport(models=[m(P1, prompt="0.000001", completion="0.000002")])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        self.assertAlmostEqual(gov.working_remaining(), 0.05)
        self.assertAlmostEqual(gov.remaining(), 0.05)
        # Large max_tokens → worst-case clearly over working remaining.
        with self.assertRaises(HarnessError) as cm:
            gov.preflight("word " * 200, [(P1, P1, 80000, 0)])
        msg = str(cm.exception)
        self.assertTrue(
            "terminal_reserve" in msg or "eat" in msg or "ceiling" in msg,
            msg)
        self.assertEqual(gov.spent, 0.0, "preflight must not spend")

    def test_attempt_preflight_allows_within_working_remaining(self):
        fake = FakeTransport(models=[m(P1, prompt="0", completion="0")])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        total, breakdown = gov.preflight("hi", [(P1, P1, 10, 0)])
        self.assertEqual(total, 0.0)
        self.assertEqual(len(breakdown), 1)

    def test_attempt_reserve_refused_when_would_eat_reserve(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        token = gov.reserve(0.04, "attempt-ok")
        self.assertIsNotNone(token)
        with self.assertRaises(HarnessError) as cm:
            gov.reserve(0.02, "attempt-eats-reserve")
        self.assertIn("terminal_reserve", str(cm.exception))
        self.assertAlmostEqual(gov.outstanding, 0.04)

    def test_attempt_record_actual_refused_when_eats_reserve(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        gov.record_actual(0.04, "apply")
        with self.assertRaises(HarnessError) as cm:
            gov.record_actual(0.02, "apply-over")
        self.assertIn("terminal_reserve", str(cm.exception))
        self.assertAlmostEqual(gov.spent, 0.04)

    def test_attempt_preflight_jev_refused_when_eats_reserve(self):
        gov = _gov(FakeTransport(), max_cost=0.000001,
                   terminal_reserve=0.0000005)
        # jev_cost(200) = $0.00000084 > working remaining $0.0000005.
        worst = jev_cost(200)
        self.assertGreater(worst, gov.working_remaining())
        with self.assertRaises(HarnessError):
            gov.preflight_jev(200, label="jev:apply")

    def test_snapshot_exposes_dual_envelope_when_reserve_armed(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        snap = gov.snapshot()
        self.assertEqual(snap["ceiling"], 0.10)
        self.assertEqual(snap["terminal_reserve"], 0.05)
        self.assertEqual(snap["working_remaining"], 0.05)
        self.assertEqual(snap["phase"], PHASE_ATTEMPT)
        self.assertEqual(snap["phase_remaining"], 0.05)
        # Historical shape for non-mission runs stays three keys.
        plain = _gov(FakeTransport(), max_cost=0.10).snapshot()
        self.assertEqual(set(plain), {"spent", "outstanding", "ceiling"})


class GovernorTerminalReserveTests(unittest.TestCase):
    def test_terminal_findings_may_spend_up_to_reserve(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        gov.record_actual(0.04, "apply-attempt")
        self.assertAlmostEqual(gov.working_remaining(), 0.01)
        gov.set_phase(PHASE_TERMINAL)
        self.assertAlmostEqual(gov.remaining(), 0.06)
        # Terminal may spend into the reserve (up to max_cost).
        gov.record_actual(0.05, "findings")
        self.assertAlmostEqual(gov.spent, 0.09)
        self.assertAlmostEqual(gov.remaining(), 0.01)
        # Still cannot overspend max_cost.
        with self.assertRaises(HarnessError):
            gov.record_actual(0.02, "findings-over")

    def test_terminal_preflight_may_use_reserve(self):
        fake = FakeTransport(models=[m(P1, prompt="0", completion="0")])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        gov.record_actual(0.05, "apply")
        # Attempt phase: remaining is 0 — refuse any positive worst-case.
        paid = FakeTransport(models=[m(P1, prompt="0.000001", completion="0.000002")])
        gov_paid = _gov(paid, max_cost=0.10, terminal_reserve=0.05)
        gov_paid.record_actual(0.05, "apply")
        with self.assertRaises(HarnessError):
            gov_paid.preflight("word " * 200, [(P1, P1, 80000, 0)])
        gov.set_phase(PHASE_TERMINAL)
        # Terminal remaining includes unused reserve — a $0 call fits.
        total, _ = gov.preflight("hi", [(P1, P1, 10, 0)])
        self.assertEqual(total, 0.0)
        # Terminal paid preflight can also use the unlocked reserve headroom.
        gov_paid.set_phase(PHASE_TERMINAL)
        small, _ = gov_paid.preflight("hi", [(P1, P1, 10, 0)])
        self.assertGreaterEqual(small, 0.0)
        self.assertLessEqual(small, gov_paid.remaining())

    def test_terminal_reserve_unlock_via_reserve_token(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        gov.record_actual(0.05, "apply")
        with self.assertRaises(HarnessError):
            gov.reserve(0.03, "findings-while-attempt")
        gov.set_phase(PHASE_TERMINAL)
        token = gov.reserve(0.03, "findings-terminal")
        gov.reconcile(token, 0.03)
        self.assertAlmostEqual(gov.spent, 0.08)

    def test_zero_reserve_governor_behaves_like_plain_ceiling(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.0)
        self.assertAlmostEqual(gov.working_remaining(), 0.10)
        self.assertAlmostEqual(gov.remaining(), 0.10)
        gov.record_actual(0.10, "full")
        with self.assertRaises(HarnessError):
            gov.record_actual(0.001, "over")
        # set_phase(terminal) does not invent headroom when reserve is $0.
        gov.set_phase(PHASE_TERMINAL)
        self.assertAlmostEqual(gov.remaining(), 0.0)


class MissionBudgetJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "missions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.pack = mr.init_mission_pack(self.root, _spec())

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_formula(self, budget):
        max_cost = budget["max_cost_usd"]
        spent = budget["spent"]
        reserve = budget["terminal_reserve_cost_usd"]
        work = budget["working_remaining"]
        self.assertAlmostEqual(work, working_remaining(max_cost, spent, reserve))
        # Honest identity while spent has not yet overshot the working envelope.
        if spent + reserve <= max_cost + 1e-12:
            self.assertAlmostEqual(spent + work + reserve, max_cost)

    def test_budget_json_matches_formula_after_init(self):
        budget = mr.load_budget(self.pack)
        self.assertEqual(budget["max_cost_usd"], 0.50)
        self.assertEqual(budget["terminal_reserve_cost_usd"], 0.05)
        self.assertEqual(budget["spent"], 0.0)
        self.assertAlmostEqual(budget["working_remaining"], 0.45)
        self.assertAlmostEqual(budget["terminal_available"], 0.50)
        self._assert_formula(budget)

    def test_budget_json_matches_formula_after_attempt_spend(self):
        updated = mr.record_spend(self.pack, 0.10)
        self.assertAlmostEqual(updated["spent"], 0.10)
        self.assertAlmostEqual(updated["working_remaining"], 0.35)
        self._assert_formula(updated)
        on_disk = mr.load_budget(self.pack)
        self._assert_formula(on_disk)
        self.assertAlmostEqual(on_disk["working_remaining"], updated["working_remaining"])

    def test_attempt_record_spend_refused_when_eats_reserve(self):
        # working remaining = 0.45; spending 0.46 would eat into reserve.
        with self.assertRaises(HarnessError) as cm:
            mr.record_spend(self.pack, 0.46)
        self.assertIn("terminal_reserve", str(cm.exception))
        budget = mr.load_budget(self.pack)
        self.assertEqual(budget["spent"], 0.0)
        self.assertAlmostEqual(budget["working_remaining"], 0.45)
        self._assert_formula(budget)

    def test_preflight_mission_spend_refuses_worst_case_into_reserve(self):
        mr.record_spend(self.pack, 0.40)
        # working remaining = 0.05
        self.assertAlmostEqual(mr.load_budget(self.pack)["working_remaining"], 0.05)
        mr.preflight_mission_spend(self.pack, 0.05)
        with self.assertRaises(HarnessError) as cm:
            mr.preflight_mission_spend(self.pack, 0.06)
        self.assertIn("terminal_reserve", str(cm.exception))

    def test_terminal_findings_spend_allowed_up_to_reserve(self):
        # Drive spent to the attempt ceiling: max - reserve = 0.45.
        mr.write_budget(self.pack, {
            "max_cost_usd": 0.50,
            "terminal_reserve_cost_usd": 0.05,
            "spent": 0.45,
        })
        budget = mr.load_budget(self.pack)
        self.assertAlmostEqual(budget["working_remaining"], 0.0)
        self._assert_formula(budget)
        # Non-terminal attempt cannot spend the reserve.
        with self.assertRaises(HarnessError):
            mr.record_spend(self.pack, 0.01)
        # Explicit terminal phase before mark_terminal is refused.
        with self.assertRaises(HarnessError) as cm:
            mr.record_spend(self.pack, 0.01, phase=PHASE_TERMINAL)
        self.assertIn("terminal", str(cm.exception))
        # mark_terminal unlocks findings spend up to the reserve.
        mr.mark_terminal(self.pack, outcome="complete",
                         findings="# FINDINGS\n\nDone.\n")
        updated = mr.record_spend(self.pack, 0.04)
        self.assertAlmostEqual(updated["spent"], 0.49)
        self.assertAlmostEqual(updated["working_remaining"], 0.0)
        self.assertAlmostEqual(updated["phase_remaining"], 0.01)
        self._assert_formula(updated)
        with self.assertRaises(HarnessError):
            mr.record_spend(self.pack, 0.02)

    def test_status_and_summary_expose_working_remaining_and_reserve(self):
        mr.record_spend(self.pack, 0.10)
        mr.write_status(self.pack)
        status = self.pack.status_md.read_text(encoding="utf-8")
        self.assertIn("**budget.working_remaining:**", status)
        self.assertIn("**budget.terminal_reserve_cost_usd:**", status)
        self.assertIn("**budget.terminal_available:**", status)
        summary = mr.pack_summary(self.pack)
        self.assertAlmostEqual(summary["budget"]["working_remaining"], 0.35)
        self.assertEqual(
            summary["dual_budget"]["terminal_reserve_cost_usd"], 0.05)
        self.assertAlmostEqual(summary["dual_budget"]["working_remaining"], 0.35)
        self.assertAlmostEqual(
            summary["budget"]["working_remaining"],
            working_remaining(0.50, 0.10, 0.05))


class UnkeyedZeroPathTests(unittest.TestCase):
    def test_unkeyed_policy_never_moves_mission_budget(self):
        from harness.config import load_settings
        from harness.jev_policy import policy_for
        settings = load_settings()
        settings.jev_api_key = None
        gov = _gov(FakeTransport(), max_cost=0.20, terminal_reserve=0.05)
        result, envelope = policy_for(settings, governor=gov).evaluate_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "change x", "x.py", site="apply")
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(gov.spent, 0.0)
        self.assertAlmostEqual(gov.working_remaining(), 0.15)
        env = gov.dual_envelope()
        self.assertAlmostEqual(env["working_remaining"], 0.15)
        self.assertEqual(env["phase"], PHASE_ATTEMPT)

    def test_zero_dollar_preflight_and_budget_stay_honest(self):
        fake = FakeTransport(models=[m(P1, prompt="0", completion="0")])
        gov = SpendGovernor(fake, "sk-test", max_cost=0.08,
                            terminal_reserve=0.03)
        total, _ = gov.preflight("free path", [(P1, P1, 50, 0)])
        self.assertEqual(total, 0.0)
        gov.record_actual(0.0, "free-model")
        self.assertEqual(gov.spent, 0.0)
        self.assertAlmostEqual(gov.working_remaining(), 0.05)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missions"
            root.mkdir()
            pack = mr.init_mission_pack(
                root, _spec(mid="m-hul-b-zero", root=str(root),
                            max_cost=0.08, reserve=0.03))
            # $0 attempt spend is allowed and does not invent numbers.
            updated = mr.record_spend(pack, 0.0)
            self.assertEqual(updated["spent"], 0.0)
            self.assertAlmostEqual(updated["working_remaining"], 0.05)
            self.assertAlmostEqual(updated["terminal_available"], 0.08)
            budget = mr.load_budget(pack)
            self.assertAlmostEqual(
                budget["working_remaining"],
                working_remaining(0.08, 0.0, 0.03))

    def test_zero_reserve_mission_matches_plain_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missions"
            root.mkdir()
            pack = mr.init_mission_pack(
                root, _spec(mid="m-hul-b-nores", root=str(root),
                            max_cost=0.20, reserve=0.0))
            budget = mr.load_budget(pack)
            self.assertEqual(budget["terminal_reserve_cost_usd"], 0.0)
            self.assertAlmostEqual(budget["working_remaining"], 0.20)
            mr.record_spend(pack, 0.20)
            budget = mr.load_budget(pack)
            self.assertAlmostEqual(budget["working_remaining"], 0.0)
            # Attempt may still spend $0; positive amount over ceiling refuses.
            mr.record_spend(pack, 0.0)
            with self.assertRaises(HarnessError):
                mr.record_spend(pack, 0.01)


class InvalidPhaseAndOwnerTests(unittest.TestCase):
    def test_normalize_phase_rejects_unknown(self):
        from harness.spend import normalize_phase
        with self.assertRaises(HarnessError):
            normalize_phase("maybe")
        self.assertEqual(normalize_phase(None), PHASE_ATTEMPT)
        self.assertEqual(normalize_phase("TERMINAL"), PHASE_TERMINAL)

    def test_governor_rejects_reserve_over_max(self):
        with self.assertRaises(HarnessError):
            SpendGovernor(FakeTransport(), "sk-test", max_cost=0.01,
                          terminal_reserve=0.5)

    def test_dual_budget_envelope_rejects_reserve_over_max(self):
        with self.assertRaises(HarnessError) as cm:
            dual_budget_envelope(0.01, 0.0, 0.5)
        self.assertIn("terminal_reserve", str(cm.exception))

    def test_governor_phase_property_reads_set_phase(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        self.assertEqual(gov.phase, PHASE_ATTEMPT)
        self.assertEqual(gov.set_phase(PHASE_TERMINAL), PHASE_TERMINAL)
        self.assertEqual(gov.phase, PHASE_TERMINAL)

    def test_reconcile_actual_eats_reserve_refused(self):
        gov = _gov(FakeTransport(), max_cost=0.10, terminal_reserve=0.05)
        token = gov.reserve(0.04, "apply")
        with self.assertRaises(HarnessError) as cm:
            gov.reconcile(token, 0.06)
        self.assertIn("ceiling", str(cm.exception))
        # Liability is still released after the hard raise path settles.
        # (reconcile releases reservation before the ceiling re-check.)
        self.assertAlmostEqual(gov.spent, 0.0)

    def test_mission_preflight_and_record_ghost_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            ghost = mr.MissionPack(Path(tmp) / "missions", "m-ghost-hul-b")
            with self.assertRaises(HarnessError) as cm:
                mr.preflight_mission_spend(ghost, 0.01)
            self.assertIn("not found", str(cm.exception))
            with self.assertRaises(HarnessError) as cm:
                mr.record_spend(ghost, 0.01)
            self.assertIn("not found", str(cm.exception))

    def test_mission_preflight_terminal_phase_requires_terminal_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missions"
            root.mkdir()
            pack = mr.init_mission_pack(
                root, _spec(mid="m-hul-b-preterm", root=str(root)))
            with self.assertRaises(HarnessError) as cm:
                mr.preflight_mission_spend(pack, 0.01, phase=PHASE_TERMINAL)
            self.assertIn("terminal", str(cm.exception))

    def test_no_second_governor_class(self):
        import harness.spend as spend_mod
        names = [n for n in dir(spend_mod) if "Governor" in n]
        self.assertEqual(names, ["SpendGovernor"],
                         "dual budget must extend the one governor, not fork it")


if __name__ == "__main__":
    unittest.main()
