"""HV-3: the token allowance and accounting owner.

The contract: one policy owner for per-call input/output maxima and
composable stage/run reservations, reconcile reported usage, label estimates
and unavailable usage, and expose remaining/used allowances -- enforced
independently from SpendGovernor's dollar reservations, including concurrent
calls and failures.

Every assertion here is about the three ways a token budget quietly lies: a
reservation that is never settled (leaked forever), usage that is unknown
being recorded as zero, and a stage that widens its own limits by nesting.
"""
import threading
import unittest

from harness.errors import HarnessError
from harness.token_budget import (
    DEFAULT_RUN_INPUT_TOKENS,
    USAGE_ACTUAL,
    USAGE_ESTIMATED,
    USAGE_UNAVAILABLE,
    Allowance,
    TokenBudget,
    budget_from_settings,
)


class ReservationTests(unittest.TestCase):
    def test_a_reservation_is_held_then_released_against_the_allowance(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        call = run.allowance(600, max_output_tokens=50, label="plan")
        self.assertEqual(call.worst_case, 650)
        # While in flight the worst case is NOT spendable.
        self.assertEqual(run.remaining_input(), 400)
        self.assertEqual(run.reserved(), 650)
        self.assertEqual(run.open_allowances, 1)
        usage = run.settle(call, input_tokens=610, output_tokens=12)
        self.assertEqual(usage, (610, 12, USAGE_ACTUAL))
        self.assertEqual(run.remaining_input(), 390)
        self.assertEqual(run.reserved(), 0)
        self.assertEqual(run.open_allowances, 0)

    def test_a_call_that_does_not_fit_is_refused_before_dispatch(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        with self.assertRaises(HarnessError) as ctx:
            run.allowance(1001)
        self.assertIn("refusing", str(ctx.exception))
        # A refused call reserves nothing and leaves no open allowance.
        self.assertEqual(run.open_allowances, 0)
        self.assertEqual(run.remaining_input(), 1000)

    def test_output_is_bounded_by_the_per_call_maximum(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        with self.assertRaises(HarnessError) as ctx:
            run.allowance(10, max_output_tokens=101)
        self.assertIn("per-call output maximum", str(ctx.exception))
        self.assertEqual(run.remaining_output(), 100)

    def test_output_allowance_is_spent_like_input(self):
        # A caller that can afford the input can still be refused because the
        # answer it asked for does not fit the remaining output allowance.
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        first = run.allowance(10, max_output_tokens=80)
        with self.assertRaises(HarnessError) as ctx:
            run.allowance(10, max_output_tokens=30)
        self.assertIn("remaining output allowance", str(ctx.exception))
        self.assertEqual(run.remaining_output(), 20)
        run.settle(first, input_tokens=10, output_tokens=80)
        self.assertEqual(run.remaining_output(), 20)

    def test_two_calls_sharing_the_allowance_cannot_both_fit(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=0)
        first = run.allowance(600)
        with self.assertRaises(HarnessError):
            run.allowance(500)  # only 400 left, reservations included
        run.settle(first, input_tokens=600, output_tokens=0)
        second = run.allowance(400)
        self.assertEqual(second.worst_case, 400)
        self.assertEqual(run.remaining_input(), 0)

    def test_an_allowance_settles_or_cancels_exactly_once(self):
        run = TokenBudget(max_input_tokens=100, max_output_tokens=10)
        call = run.allowance(10)
        run.settle(call, input_tokens=10, output_tokens=1)
        with self.assertRaises(HarnessError) as ctx:
            run.settle(call)
        self.assertIn("already settled", str(ctx.exception))

        other = run.allowance(10, max_output_tokens=5, label="b")
        run.cancel(other)
        self.assertTrue(other.cancelled)
        with self.assertRaises(HarnessError) as ctx:
            run.cancel(other)
        self.assertIn("already cancelled", str(ctx.exception))

    def test_a_budget_refuses_an_allowance_it_does_not_own(self):
        a = TokenBudget("a", max_input_tokens=100, max_output_tokens=10)
        b = TokenBudget("b", max_input_tokens=100, max_output_tokens=10)
        call = a.allowance(10)
        with self.assertRaises(HarnessError) as ctx:
            b.settle(call)
        self.assertIn("does not own", str(ctx.exception))
        with self.assertRaises(HarnessError):
            b.settle("not an allowance")


class UsageHonestyTests(unittest.TestCase):
    def test_unknown_usage_is_charged_in_full_and_labeled(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        call = run.allowance(400, max_output_tokens=50, label="apply")
        usage = run.settle(call, source=USAGE_UNAVAILABLE)
        # The worst case is charged: a call whose usage we cannot read may
        # have billed all of it, and zero is the one answer we may not give.
        self.assertEqual(usage, (400, 50, USAGE_UNAVAILABLE))
        self.assertEqual(run.used_input(), 400)
        self.assertEqual(run.used_output(), 50)
        self.assertEqual(run.snapshot()["usage_sources"][USAGE_UNAVAILABLE], 1)

    def test_estimated_usage_is_recorded_as_estimated_not_actual(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        call = run.allowance(400, max_output_tokens=50)
        usage = run.settle(call, input_tokens=380, output_tokens=20,
                           source=USAGE_ESTIMATED)
        self.assertEqual(usage.source, USAGE_ESTIMATED)
        snap = run.snapshot()
        self.assertEqual(snap["usage_sources"][USAGE_ACTUAL], 0)
        self.assertEqual(snap["usage_sources"][USAGE_ESTIMATED], 1)
        # An estimate that came in under the reservation leaves the rest free.
        self.assertEqual(snap["remaining_input_tokens"], 620)
        self.assertEqual(snap["over_input_tokens"], 0)

    def test_usage_above_the_reservation_is_recorded_not_clamped(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        call = run.allowance(100, max_output_tokens=10)
        run.settle(call, input_tokens=180, output_tokens=30)
        snap = run.snapshot()
        # The real number is what was billed; the overrun is counted, not hidden.
        self.assertEqual(snap["used_input_tokens"], 180)
        self.assertEqual(snap["over_input_tokens"], 80)
        self.assertEqual(snap["over_output_tokens"], 20)
        # ...and the remaining allowance floors at zero instead of going negative.
        self.assertEqual(snap["remaining_input_tokens"], 820)

    def test_a_bogus_usage_label_or_count_is_refused(self):
        run = TokenBudget(max_input_tokens=100, max_output_tokens=10)
        call = run.allowance(10)
        with self.assertRaises(HarnessError) as ctx:
            run.settle(call, source="probably")
        self.assertIn("usage source", str(ctx.exception))
        for bad in (-1, 1.5, True, "12", []):
            with self.assertRaises(HarnessError):
                run.settle(call, input_tokens=bad)
        # A refused settle leaves the reservation open, not half-applied.
        self.assertEqual(run.open_allowances, 1)


class StageCompositionTests(unittest.TestCase):
    def test_a_stage_spends_its_parent_and_can_only_narrow(self):
        run = TokenBudget("run", max_input_tokens=1000, max_output_tokens=100)
        plan = run.stage("planning", max_input_tokens=600)
        self.assertEqual(plan.max_input_tokens, 600)
        call = plan.allowance(500, max_output_tokens=40)
        # The reservation is held in BOTH budgets, so the run cannot be spent
        # twice by the same tokens.
        self.assertEqual(run.reserved(), 540)
        self.assertEqual(plan.reserved(), 540)
        self.assertEqual(run.remaining_input(), 500)
        plan.settle(call, input_tokens=480, output_tokens=20)
        self.assertEqual(run.used_input(), 480)
        self.assertEqual(plan.used_input(), 480)
        self.assertEqual(run.remaining_input(), 520)
        self.assertEqual(plan.remaining_input(), 120)

    def test_a_stage_may_not_widen_its_parent(self):
        run = TokenBudget("run", max_input_tokens=1000, max_output_tokens=100)
        for kwargs in ({"max_input_tokens": 1001},
                       {"max_output_tokens": 101}):
            with self.assertRaises(HarnessError) as ctx:
                run.stage("greedy", **kwargs)
            self.assertIn("only narrow", str(ctx.exception))
        # An unspecified cap inherits the parent's, it does not invent one.
        self.assertEqual(run.stage("plain").max_input_tokens, 1000)

    def test_a_call_over_the_stage_cap_is_refused_even_if_the_run_can_pay(self):
        run = TokenBudget("run", max_input_tokens=1000, max_output_tokens=100)
        plan = run.stage("planning", max_input_tokens=200)
        with self.assertRaises(HarnessError) as ctx:
            plan.allowance(300)  # the run could afford it; the stage may not
        self.assertIn("planning", str(ctx.exception))
        self.assertEqual(run.remaining_input(), 1000)

    def test_successive_planning_stages_shrink_and_the_run_is_never_exceeded(self):
        run = TokenBudget("run", max_input_tokens=1000, max_output_tokens=0)
        for size, want in ((800, 800), (100, 900), (50, 950)):
            stage = run.stage(f"plan-{size}", max_input_tokens=size)
            stage.settle(stage.allowance(size), input_tokens=size, output_tokens=0)
            self.assertEqual(run.remaining_input(), 1000 - want)
        with self.assertRaises(HarnessError):
            run.stage("plan-too-big", max_input_tokens=1000).allowance(1000)


class SnapshotAndSettingsTests(unittest.TestCase):
    def test_snapshot_reports_per_label_usage_with_its_source(self):
        run = TokenBudget("run", max_input_tokens=1000, max_output_tokens=100)
        a = run.allowance(100, max_output_tokens=10, label="plan")
        b = run.allowance(200, max_output_tokens=20, label="apply")
        run.settle(a, input_tokens=90, output_tokens=5)
        run.settle(b, source=USAGE_UNAVAILABLE)
        snap = run.snapshot()
        self.assertEqual(snap["label"], "run")
        self.assertIsNone(snap["parent"])
        self.assertEqual(snap["calls"], 2)
        self.assertEqual(snap["cancelled"], 0)
        self.assertEqual(snap["open_allowances"], 0)
        self.assertEqual(snap["by_label"]["plan"],
                         {"calls": 1, "input_tokens": 90, "output_tokens": 5,
                          USAGE_ACTUAL: 1, USAGE_ESTIMATED: 0,
                          USAGE_UNAVAILABLE: 0})
        self.assertEqual(snap["by_label"]["apply"][USAGE_UNAVAILABLE], 1)
        self.assertEqual(snap["usage_sources"],
                         {USAGE_ACTUAL: 1, USAGE_ESTIMATED: 0,
                          USAGE_UNAVAILABLE: 1})
        child = run.stage("planning")
        self.assertEqual(child.snapshot()["parent"], "run")
        self.assertEqual(child.snapshot()["used_input_tokens"], 0)

    def test_a_cancelled_call_is_recorded_without_charging(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=100)
        call = run.allowance(300, label="refused-lane")
        run.cancel(call)
        snap = run.snapshot()
        self.assertEqual(snap["used_input_tokens"], 0)
        self.assertEqual(snap["cancelled"], 1)
        self.assertEqual(snap["reserved_input_tokens"], 0)
        self.assertEqual(snap["calls"], 0)

    def test_defaults_and_the_settings_seam(self):
        self.assertEqual(TokenBudget().max_input_tokens,
                         DEFAULT_RUN_INPUT_TOKENS)

        class FakeSettings:
            token_budget_input = 5000
            token_budget_output = 500

        run = budget_from_settings(FakeSettings())
        self.assertEqual(run.max_input_tokens, 5000)
        self.assertEqual(run.max_output_tokens, 500)
        # A Settings without the keys (older config object) still works.
        self.assertEqual(
            budget_from_settings(object()).max_input_tokens,
            DEFAULT_RUN_INPUT_TOKENS)

    def test_nonsense_limits_are_refused_at_construction(self):
        for kwargs in ({"max_input_tokens": -1},
                       {"max_input_tokens": 1.5},
                       {"max_output_tokens": "1000"},
                       {"max_output_tokens": True},
                       {"max_output_tokens": None}):
            with self.assertRaises(HarnessError, msg=kwargs):
                TokenBudget("run", **kwargs)

    def test_an_allowance_reads_as_an_open_reservation(self):
        run = TokenBudget(max_input_tokens=100, max_output_tokens=10)
        call = run.allowance(10, label="x")
        self.assertIsInstance(call, Allowance)
        self.assertIn("open", repr(call))
        run.settle(call, input_tokens=10, output_tokens=1)
        self.assertIn("settled", repr(call))


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_calls_cannot_reserve_past_the_allowance(self):
        # The same race SpendGovernor guards for dollars, in tokens: W workers
        # each preflighting against the full remaining allowance must not all
        # be admitted.
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=0)
        granted, refused = [], []
        lock = threading.Lock()

        def worker(n):
            try:
                call = run.allowance(100, label=f"w{n}")
            except HarnessError:
                with lock:
                    refused.append(n)
                return
            with lock:
                granted.append(call)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(granted), 10)
        self.assertEqual(len(refused), 20)
        self.assertEqual(run.reserved(), 1000)
        self.assertEqual(run.remaining_input(), 0)
        for call in granted:
            run.settle(call, input_tokens=100, output_tokens=0)
        self.assertEqual(run.open_allowances, 0)
        self.assertEqual(run.used_input(), 1000)

    def test_settling_concurrently_never_double_counts(self):
        run = TokenBudget(max_input_tokens=10_000, max_output_tokens=1000)
        calls = [run.allowance(10, max_output_tokens=5) for _ in range(40)]

        def settle(call):
            run.settle(call, input_tokens=10, output_tokens=5)

        threads = [threading.Thread(target=settle, args=(c,)) for c in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(run.used_input(), 400)
        self.assertEqual(run.reserved(), 0)
        self.assertEqual(run.open_allowances, 0)
        self.assertEqual(run.snapshot()["usage_sources"][USAGE_ACTUAL], 40)


class IndependenceFromDollarsTests(unittest.TestCase):
    def test_token_allowances_are_enforced_independently_of_spend(self):
        # A dollar governor with money to spare must not unlock a token call,
        # and a $0.00 ceiling must not either: the two limits answer different
        # questions, and this module answers only its own.
        from harness.spend import SpendGovernor

        from tests._fake import FakeTransport
        governor = SpendGovernor(FakeTransport(), "sk-test", max_cost=1.0)
        run = TokenBudget(max_input_tokens=100, max_output_tokens=10)
        self.assertEqual(governor.remaining(), 1.0)
        with self.assertRaises(HarnessError):
            run.allowance(101)
        self.assertEqual(governor.remaining(), 1.0)

        broke = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.0)
        self.assertEqual(broke.remaining(), 0.0)
        free = TokenBudget(max_input_tokens=10, max_output_tokens=1)
        with self.assertRaises(HarnessError):
            free.allowance(11)  # a free route still has a context window
        self.assertEqual(free.remaining_input(), 10)


class TokenKindsTests(unittest.TestCase):
    def test_usage_tuple_compatibility_and_attributes(self):
        from harness.token_budget import Usage
        u = Usage(100, 20, USAGE_ACTUAL, 15, 40)
        # 3-tuple backward-compatibility
        self.assertEqual(u, (100, 20, USAGE_ACTUAL))
        # 5-tuple equality
        self.assertEqual(u, (100, 20, USAGE_ACTUAL, 15, 40))
        # Field access
        self.assertEqual(u.input_tokens, 100)
        self.assertEqual(u.output_tokens, 20)
        self.assertEqual(u.source, USAGE_ACTUAL)
        self.assertEqual(u.reasoning_tokens, 15)
        self.assertEqual(u.cached_tokens, 40)

    def test_settle_tracks_reasoning_and_cached_tokens(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=200)
        call = run.allowance(200, max_output_tokens=50, label="reasoning_call")
        usage = run.settle(call, input_tokens=180, output_tokens=40,
                           reasoning_tokens=25, cached_tokens=50,
                           source=USAGE_ACTUAL)
        self.assertEqual(usage.reasoning_tokens, 25)
        self.assertEqual(usage.cached_tokens, 50)
        self.assertEqual(run.used_reasoning(), 25)
        self.assertEqual(run.used_cached(), 50)

        snap = run.snapshot()
        self.assertEqual(snap["used_reasoning_tokens"], 25)
        self.assertEqual(snap["used_cached_tokens"], 50)
        self.assertEqual(snap["token_kinds"], {
            "input": 180,
            "output": 40,
            "reasoning": 25,
            "cached": 50,
        })
        self.assertEqual(snap["token_kinds_by_label"]["reasoning_call"]["reasoning_tokens"], 25)
        self.assertEqual(snap["token_kinds_by_label"]["reasoning_call"]["cached_tokens"], 50)
        self.assertTrue(snap["estimation_markers"]["all_actual"])
        self.assertFalse(snap["estimation_markers"]["has_estimates"])
        self.assertFalse(snap["estimation_markers"]["has_unavailable"])

    def test_estimation_markers_honestly_reflect_sources(self):
        run = TokenBudget(max_input_tokens=1000, max_output_tokens=200)
        c1 = run.allowance(100, max_output_tokens=20, label="c1")
        c2 = run.allowance(100, max_output_tokens=20, label="c2")
        c3 = run.allowance(100, max_output_tokens=20, label="c3")

        run.settle(c1, input_tokens=80, output_tokens=10, source=USAGE_ACTUAL)
        run.settle(c2, input_tokens=90, output_tokens=15, source=USAGE_ESTIMATED)
        run.settle(c3, source=USAGE_UNAVAILABLE)

        snap = run.snapshot()
        self.assertFalse(snap["estimation_markers"]["all_actual"])
        self.assertTrue(snap["estimation_markers"]["has_estimates"])
        self.assertTrue(snap["estimation_markers"]["has_unavailable"])

    def test_refuse_invalid_token_kind_counts(self):
        run = TokenBudget(max_input_tokens=500, max_output_tokens=100)
        call = run.allowance(50, max_output_tokens=10)
        for bad in (-1, 1.5, "10", True):
            with self.assertRaises(HarnessError):
                run.settle(call, input_tokens=50, output_tokens=10,
                           reasoning_tokens=bad)
            with self.assertRaises(HarnessError):
                run.settle(call, input_tokens=50, output_tokens=10,
                           cached_tokens=bad)


if __name__ == "__main__":
    unittest.main()
