"""HV-3 acceptance: the token allowance and accounting owner.

Token ceilings and the existing dollar ceiling are *independent* limits. A
generous dollar ceiling does not buy extra tokens and a generous token
ceiling does not buy extra dollars, so these tests pin both directions of
that independence, the reservation arithmetic that makes concurrent
dispatch safe, and -- the point of the lane -- the honesty of the numbers
we report when a provider tells us nothing.

All hermetic: no network, no key. The dollar owner is the real
``SpendGovernor`` over a fake transport, so "composes without weakening"
is proved against the actual budget rather than a stand-in.
"""
import os
import threading
import unittest
from unittest import mock

from harness.config import MAX_HOURGLASS_TOKEN_ALLOWANCE, load_settings
from harness.errors import HarnessError
from harness.spend import SpendGovernor
from harness.token_budget import (
    ESTIMATED,
    INPUT,
    MEASURED,
    OUTPUT,
    UNAVAILABLE,
    TokenBudget,
    reserve_composed,
)
from harness.tokens import estimate_prompt_tokens

from tests._fake import FakeTransport

_HOURGLASS_ENV = [
    "HARNESS_HOURGLASS_MAX_INPUT_TOKENS",
    "HARNESS_HOURGLASS_MAX_OUTPUT_TOKENS",
    "HARNESS_HOURGLASS_RUN_MAX_INPUT_TOKENS",
    "HARNESS_HOURGLASS_RUN_MAX_OUTPUT_TOKENS",
    "HARNESS_HOURGLASS_STAGE_MAX_INPUT_TOKENS",
    "HARNESS_HOURGLASS_STAGE_MAX_OUTPUT_TOKENS",
]


class _HermeticEnv(unittest.TestCase):
    """No operator env pin may decide the outcome of a config assertion."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _HOURGLASS_ENV}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _gov(max_cost=0.05):
    return SpendGovernor(FakeTransport(), "sk-test", max_cost=max_cost)


def _budget(**kw):
    kw.setdefault("max_input_tokens", 1000)
    kw.setdefault("max_output_tokens", 500)
    return TokenBudget(**kw)


class PerCallCeilingTests(unittest.TestCase):
    """Each call carries input/output maxima; the ceiling is the owner's."""

    def test_request_above_issued_input_ceiling_is_refused(self):
        budget = _budget(max_input_tokens=1000)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("context", input_tokens=1001, max_output_tokens=10)
        self.assertIn("max_input_tokens", str(ctx.exception))
        # Refused before dispatch: no claim was left behind.
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})
        self.assertEqual(budget.used(), {INPUT: 0, OUTPUT: 0})

    def test_request_above_issued_output_ceiling_is_refused(self):
        budget = _budget(max_output_tokens=500)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("context", input_tokens=10, max_output_tokens=501)
        self.assertIn("max_output_tokens", str(ctx.exception))
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})

    def test_caller_cannot_opt_out_of_an_issued_output_ceiling(self):
        """Omitting the bound must not become a way to raise it."""
        budget = _budget(max_output_tokens=500)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("context", input_tokens=10)
        self.assertIn("max_output_tokens is required", str(ctx.exception))

    def test_call_with_neither_prompt_nor_count_is_refused(self):
        budget = _budget()
        with self.assertRaises(HarnessError):
            budget.reserve("context", max_output_tokens=10)

    def test_prompt_text_is_estimated_with_the_shared_estimator(self):
        budget = _budget()
        allowance = budget.preflight_call("context", prompt_text="a b c d e f",
                                          max_output_tokens=10)
        self.assertEqual(allowance.max_input_tokens,
                         estimate_prompt_tokens("a b c d e f"))

    def test_preflight_claims_nothing_so_only_one_of_two_callers_fits(self):
        """The documented reason reserve() exists: a check is not a claim."""
        budget = _budget(run_max_input_tokens=100)
        for _ in range(2):
            budget.preflight_call("context", input_tokens=60,
                                  max_output_tokens=10)
        budget.reserve("context", input_tokens=60, max_output_tokens=10)
        with self.assertRaises(HarnessError):
            budget.reserve("context", input_tokens=60, max_output_tokens=10)


class AggregateCeilingTests(unittest.TestCase):
    """Stage and run ceilings compose by reservation, in both directions."""

    def test_stage_ceiling_refuses_while_run_budget_would_still_fit(self):
        budget = _budget(run_max_input_tokens=1000,
                         stage_max_input_tokens=100)
        budget.reserve("planning", input_tokens=60, max_output_tokens=10)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("planning", input_tokens=60, max_output_tokens=10)
        self.assertIn("stage planning", str(ctx.exception))
        # The run scope was not drawn down by the refused call.
        self.assertEqual(budget.remaining()[INPUT], 940)

    def test_run_ceiling_refuses_while_each_stage_would_still_fit(self):
        budget = _budget(run_max_output_tokens=100,
                         stage_max_output_tokens=100)
        budget.reserve("context", input_tokens=10, max_output_tokens=60)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("execution", input_tokens=10, max_output_tokens=60)
        self.assertIn("run", str(ctx.exception))
        self.assertEqual(budget.remaining("execution")[OUTPUT], 100)

    def test_stage_ceilings_are_independent_of_each_other(self):
        budget = _budget(stage_max_output_tokens=100)
        budget.reserve("context", input_tokens=10, max_output_tokens=90)
        # Execution has its own full stage allowance; planning's draw did
        # not come out of it.
        self.assertEqual(budget.remaining("execution")[OUTPUT], 100)
        self.assertEqual(budget.remaining("context")[OUTPUT], 10)

    def test_input_and_output_dimensions_are_independent(self):
        """A broad prompt is not a licence to ask for a broad answer."""
        budget = _budget(run_max_input_tokens=100, run_max_output_tokens=100)
        budget.reserve("context", input_tokens=100, max_output_tokens=10)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("context", input_tokens=1, max_output_tokens=10)
        self.assertIn("input", str(ctx.exception))
        # Output headroom is untouched by an input-exhausted run, and the
        # mirror holds: input headroom survives an output-exhausted one.
        self.assertEqual(budget.remaining()[OUTPUT], 90)
        other = _budget(run_max_input_tokens=100, run_max_output_tokens=100)
        other.reserve("context", input_tokens=10, max_output_tokens=100)
        with self.assertRaises(HarnessError) as ctx:
            other.reserve("context", input_tokens=10, max_output_tokens=1)
        self.assertIn("output", str(ctx.exception))
        self.assertEqual(other.remaining()[INPUT], 90)

    def test_unbounded_dimension_reports_none_not_a_big_number(self):
        """``None`` still means 'no ceiling' -- but only when asked for.

        Leaving an aggregate alone now *bounds* the run, so the unbounded
        shape is only reachable by stating it, which is what this does.
        """
        budget = _budget(run_max_input_tokens=None, run_max_output_tokens=None,
                         stage_max_input_tokens=None, stage_max_output_tokens=None)
        remaining = budget.remaining()
        self.assertEqual(remaining, {INPUT: None, OUTPUT: None})
        snapshot = budget.snapshot()
        self.assertNotIn("run_ceiling", snapshot)
        self.assertNotIn("stage_ceiling", snapshot)
        self.assertIsNone(snapshot["remaining"][INPUT])

    def test_aggregates_default_to_bounded_not_unbounded(self):
        """The canon's 'enforce limits for each stage and call' must hold
        with nothing configured, so the aggregate default is the validated
        per-call allowance rather than no ceiling at all."""
        budget = _budget()
        self.assertEqual(budget.run_max_input_tokens,
                         MAX_HOURGLASS_TOKEN_ALLOWANCE)
        self.assertEqual(budget.run_max_output_tokens,
                         MAX_HOURGLASS_TOKEN_ALLOWANCE)
        self.assertEqual(budget.stage_max_input_tokens,
                         MAX_HOURGLASS_TOKEN_ALLOWANCE)
        self.assertEqual(budget.stage_max_output_tokens,
                         MAX_HOURGLASS_TOKEN_ALLOWANCE)
        remaining = budget.remaining()
        self.assertEqual(remaining[INPUT], MAX_HOURGLASS_TOKEN_ALLOWANCE)
        self.assertEqual(remaining[OUTPUT], MAX_HOURGLASS_TOKEN_ALLOWANCE)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["run_ceiling"],
                         {INPUT: MAX_HOURGLASS_TOKEN_ALLOWANCE,
                          OUTPUT: MAX_HOURGLASS_TOKEN_ALLOWANCE})
        self.assertEqual(snapshot["stage_ceiling"],
                         {INPUT: MAX_HOURGLASS_TOKEN_ALLOWANCE,
                          OUTPUT: MAX_HOURGLASS_TOKEN_ALLOWANCE})

    def test_a_default_run_stops_at_the_aggregate_not_just_per_call(self):
        """Each call is well under its per-call ceiling, so only an enforced
        aggregate can refuse the run: many legal calls must still add up."""
        cap = MAX_HOURGLASS_TOKEN_ALLOWANCE
        budget = TokenBudget(max_input_tokens=1000, max_output_tokens=cap)
        step = cap // 10
        for _ in range(9):
            call = budget.reserve("planning", input_tokens=1,
                                  max_output_tokens=step)
            budget.settle(call, input_tokens=1, output_tokens=step)
        self.assertEqual(budget.remaining()[OUTPUT], cap - 9 * step)
        call = budget.reserve("planning", input_tokens=1, max_output_tokens=step)
        budget.settle(call, input_tokens=1, output_tokens=step)
        self.assertEqual(budget.remaining()[OUTPUT], 0)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("planning", input_tokens=1, max_output_tokens=1)
        message = str(ctx.exception)
        self.assertIn("run", message)
        self.assertIn(str(MAX_HOURGLASS_TOKEN_ALLOWANCE), message)

    def test_zero_aggregate_ceiling_is_a_refusal_not_unboundedness(self):
        """0 is a real (exhausted) budget; only None means 'no ceiling'."""
        budget = _budget(run_max_output_tokens=0)
        self.assertEqual(budget.remaining()[OUTPUT], 0)
        with self.assertRaises(HarnessError):
            budget.reserve("context", input_tokens=10, max_output_tokens=10)

    def test_reserved_but_unsettled_calls_count_against_the_ceiling(self):
        budget = _budget(run_max_input_tokens=100)
        budget.reserve("context", input_tokens=100, max_output_tokens=10)
        self.assertEqual(budget.remaining()[INPUT], 0)
        with self.assertRaises(HarnessError):
            budget.reserve("context", input_tokens=1, max_output_tokens=10)

    def test_stage_identifier_must_be_a_real_stage(self):
        """A typo must not silently buy a second full stage allowance."""
        budget = _budget(stage_max_output_tokens=100)
        for bad in ("", "   ", "pl*ning", 5, "x" * 65):
            with self.assertRaises(HarnessError):
                budget.reserve(bad, input_tokens=1, max_output_tokens=1)
        with self.assertRaises(HarnessError):
            budget.preflight_call("  ", input_tokens=1, max_output_tokens=1)
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})


class FailClosedConstructionTests(unittest.TestCase):
    """A directly-constructed owner is as strict as a settings-built one.

    The gap this closes: ``TokenBudget()`` used to default to *no* per-call
    output ceiling, so a consumer that skipped ``from_settings`` silently
    lost output enforcement and only found out when a call blew through.
    """

    def test_direct_construction_issues_the_same_output_ceiling(self):
        from harness.token_budget import TokenBudget as TB
        direct = TB()
        configured = TB.from_settings(load_settings())
        self.assertEqual(direct.max_output_tokens, MAX_HOURGLASS_TOKEN_ALLOWANCE)
        self.assertEqual(direct.max_output_tokens, configured.max_output_tokens)
        self.assertEqual(direct.per_call_allowance(),
                         configured.per_call_allowance())

    def test_direct_construction_refuses_an_unbounded_output_call(self):
        """The real call: a consumer that omits its output bound is
        refused, before dispatch, instead of dispatching unpoliced."""
        budget = TokenBudget()
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("execution", input_tokens=10)
        self.assertIn("max_output_tokens is required", str(ctx.exception))
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})
        self.assertEqual(budget.used(), {INPUT: 0, OUTPUT: 0})

    def test_an_explicit_narrowing_ceiling_still_applies_directly(self):
        budget = TokenBudget(max_input_tokens=1000, max_output_tokens=64)
        with self.assertRaises(HarnessError):
            budget.reserve("execution", input_tokens=10, max_output_tokens=65)
        self.assertIsNotNone(budget.reserve("execution", input_tokens=10,
                                            max_output_tokens=64))

    def test_declining_to_bound_output_is_an_explicit_opt_in(self):
        budget = TokenBudget(max_input_tokens=100, max_output_tokens=None)
        self.assertIsNone(budget.per_call_allowance().max_output_tokens)
        self.assertIsNotNone(budget.reserve("context", input_tokens=10))

    def test_unset_limits_are_annotated_honestly(self):
        from typing import Optional, get_type_hints
        from harness.token_budget import TokenAllowance, TokenReservation
        allowance = get_type_hints(TokenAllowance)
        reservation = get_type_hints(TokenReservation)
        self.assertEqual(allowance["max_input_tokens"], int)
        self.assertEqual(allowance["max_output_tokens"], Optional[int])
        self.assertEqual(reservation["output_tokens"], Optional[int])
        self.assertEqual(reservation["input_tokens"], int)


class ReadSymmetryTests(unittest.TestCase):
    """Reading the budget never makes a stage exist.

    A stage enters the snapshot when a claim is taken, not when someone
    asks whether a call would fit. Otherwise a surface renders a stage the
    run never entered, with confident all-zero numbers.
    """

    def test_preflight_of_a_fresh_stage_leaves_no_trace(self):
        budget = _budget(stage_max_output_tokens=100)
        budget.preflight_call("planning", input_tokens=10, max_output_tokens=10)
        snapshot = budget.snapshot()
        self.assertNotIn("stages", snapshot)
        self.assertEqual(snapshot["calls_reserved"], 0)
        self.assertEqual(budget.outstanding("planning"), {INPUT: 0, OUTPUT: 0})

    def test_remaining_of_a_fresh_stage_leaves_no_trace(self):
        budget = _budget(stage_max_output_tokens=100)
        self.assertEqual(budget.remaining("planning")[OUTPUT], 100)
        self.assertNotIn("stages", budget.snapshot())

    def test_every_read_accessor_agrees_on_creating_nothing(self):
        budget = _budget(stage_max_input_tokens=50, stage_max_output_tokens=100)
        budget.used("planning")
        budget.outstanding("planning")
        budget.remaining("planning")
        budget.usage_source(INPUT)
        budget.usage_counts()
        budget.snapshot()
        self.assertNotIn("stages", budget.snapshot())

    def test_a_fresh_stage_is_still_held_to_its_ceiling(self):
        """Non-mutating must not mean unchecked: a stage with no entry yet
        is still bounded by the stage ceiling."""
        budget = _budget(stage_max_output_tokens=100)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("planning", input_tokens=10, max_output_tokens=101)
        self.assertIn("stage planning", str(ctx.exception))
        self.assertNotIn("stages", budget.snapshot())

    def test_a_claim_is_what_creates_a_stage(self):
        budget = _budget(run_max_output_tokens=100, stage_max_output_tokens=60)
        budget.reserve("planning", input_tokens=10, max_output_tokens=20)
        stages = budget.snapshot()["stages"]
        self.assertEqual(list(stages), ["planning"])
        self.assertEqual(stages["planning"]["outstanding"][OUTPUT], 20)
        self.assertEqual(stages["planning"]["remaining"][OUTPUT], 40)

    def test_a_refused_reservation_leaves_no_stage_behind(self):
        budget = _budget(run_max_output_tokens=100, stage_max_output_tokens=10)
        with self.assertRaises(HarnessError):
            budget.reserve("planning", input_tokens=10, max_output_tokens=50)
        self.assertNotIn("stages", budget.snapshot())
        # A later call that does fit is still the one that creates it.
        budget.reserve("planning", input_tokens=10, max_output_tokens=10)
        self.assertEqual(
            budget.snapshot()["stages"]["planning"]["outstanding"][OUTPUT], 10)


class ConcurrentReservationTests(unittest.TestCase):
    """The MR-6 proof obligation, in tokens: parallel calls cannot overcommit."""

    def test_concurrent_reservations_cannot_overcommit_the_ceiling(self):
        budget = _budget(run_max_input_tokens=100)
        barrier = threading.Barrier(2)
        refusals = []

        def worker():
            barrier.wait()
            try:
                budget.reserve("context", input_tokens=60, max_output_tokens=10)
            except HarnessError as exc:
                refusals.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(refusals), 1)
        used = budget.used()[INPUT] + budget.outstanding()[INPUT]
        self.assertLessEqual(used, 100)

    def test_concurrent_reservations_cannot_overcommit_a_stage_ceiling(self):
        budget = _budget(stage_max_output_tokens=100)
        barrier = threading.Barrier(3)
        refusals = []

        def worker():
            barrier.wait()
            try:
                budget.reserve("planning", input_tokens=10, max_output_tokens=60)
            except HarnessError:
                refusals.append("refused")

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(refusals), 2)
        claimed = budget.outstanding("planning")[OUTPUT]
        self.assertLessEqual(claimed, 100)

    def test_settling_frees_the_claim_for_the_next_caller(self):
        budget = _budget(run_max_input_tokens=100)
        first = budget.reserve("context", input_tokens=100, max_output_tokens=10)
        with self.assertRaises(HarnessError):
            budget.reserve("context", input_tokens=1, max_output_tokens=10)
        budget.settle(first, input_tokens=40)
        # 100 ceiling - 40 settled: the rest is claimable again.
        self.assertEqual(budget.remaining()[INPUT], 60)


class UsageReconciliationTests(unittest.TestCase):
    """Reconcile what the provider said, and label how we know it."""

    def test_reported_usage_settles_as_measured(self):
        budget = _budget()
        call = budget.reserve("context", input_tokens=500, max_output_tokens=200)
        usage = budget.settle(call, input_tokens=410, output_tokens=130)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (410, 130))
        self.assertEqual(usage.input_source, MEASURED)
        self.assertEqual(usage.output_source, MEASURED)
        self.assertTrue(usage.measured)
        self.assertFalse(usage.estimated)
        self.assertEqual(budget.used(), {INPUT: 410, OUTPUT: 130})

    def test_partial_report_estimates_only_the_silent_dimension(self):
        """Providers routinely report completion tokens but not prompt ones."""
        budget = _budget()
        call = budget.reserve("context", input_tokens=500, max_output_tokens=200)
        usage = budget.settle(call, output_tokens=130)
        self.assertEqual(usage.input_source, ESTIMATED)
        self.assertEqual(usage.output_source, MEASURED)
        # The unreported input settles the preflight estimate, not zero.
        self.assertEqual(usage.input_tokens, 500)
        self.assertEqual(budget.used()[INPUT], 500)

    def test_silence_settles_the_preflight_estimate_labelled_estimated(self):
        budget = _budget()
        call = budget.reserve("context", input_tokens=500, max_output_tokens=200)
        usage = budget.settle(call)
        self.assertEqual(usage.input_tokens, 500)
        self.assertEqual(usage.output_tokens, 200)
        self.assertEqual(usage.input_source, ESTIMATED)
        self.assertEqual(usage.output_source, ESTIMATED)
        # Our own measurement of our own payload is still not "measured".
        self.assertEqual(budget.usage_source(INPUT), ESTIMATED)
        self.assertEqual(budget.usage_source(OUTPUT), ESTIMATED)

    def test_unbounded_dimension_settles_unavailable_not_zero_measured(self):
        # No per-call output ceiling is a deliberate opt-out, never a default.
        budget = TokenBudget(max_input_tokens=100, max_output_tokens=None)
        call = budget.reserve("context", input_tokens=40)
        usage = budget.settle(call)
        self.assertEqual(usage.output_source, UNAVAILABLE)
        self.assertEqual(usage.output_tokens, 0)
        self.assertFalse(usage.measured)
        self.assertEqual(budget.usage_source(OUTPUT), UNAVAILABLE)

    def test_a_later_measured_call_does_not_launder_an_unavailable_one(self):
        """Worst-wins: one unknown contribution means the total is partial."""
        budget = TokenBudget(max_input_tokens=100, max_output_tokens=None)
        budget.settle(budget.reserve("context", input_tokens=10))
        measured = budget.settle(
            budget.reserve("context", input_tokens=10, max_output_tokens=10),
            input_tokens=9, output_tokens=8)
        self.assertEqual(measured.output_source, MEASURED)
        self.assertEqual(budget.usage_source(OUTPUT), UNAVAILABLE)
        self.assertEqual(budget.usage_counts()[OUTPUT][MEASURED], 1)
        self.assertEqual(budget.usage_counts()[OUTPUT][UNAVAILABLE], 1)

    def test_usage_counts_are_tallied_per_call_not_per_scope(self):
        budget = _budget(run_max_input_tokens=100, stage_max_input_tokens=100)
        budget.settle(budget.reserve("context", input_tokens=10,
                                     max_output_tokens=10))
        self.assertEqual(budget.usage_counts()[INPUT][ESTIMATED], 1)
        self.assertEqual(budget.snapshot()["calls_settled"], 1)

    def test_usage_source_rejects_a_dimension_it_does_not_account(self):
        budget = _budget()
        with self.assertRaises(HarnessError) as ctx:
            budget.usage_source("latency")
        self.assertIn("latency", str(ctx.exception))

    def test_token_usage_serialises_with_its_honest_labels(self):
        """HV-6 surfaces read this dict, so the labels must survive the trip."""
        import json
        budget = _budget()
        call = budget.reserve("context", input_tokens=10, max_output_tokens=10)
        payload = budget.settle(call, output_tokens=7).as_dict()
        self.assertEqual(payload["input_tokens"], 10)
        self.assertEqual(payload["output_tokens"], 7)
        self.assertEqual(payload["input_source"], ESTIMATED)
        self.assertEqual(payload["output_source"], MEASURED)
        self.assertTrue(payload["settled"])
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_settling_an_unknown_reservation_is_refused(self):
        budget = _budget()
        call = budget.reserve("context", input_tokens=10, max_output_tokens=10)
        budget.settle(call, input_tokens=10)
        with self.assertRaises(HarnessError):
            budget.settle(call, input_tokens=10)
        with self.assertRaises(HarnessError):
            budget.cancel(call)

    def test_reported_counts_must_be_non_negative_integers(self):
        budget = _budget()
        call = budget.reserve("context", input_tokens=10, max_output_tokens=10)
        with self.assertRaises(HarnessError):
            budget.settle(call, input_tokens=-1)
        with self.assertRaises(HarnessError):
            budget.settle(call, input_tokens=1.5)


class FailureHonestyTests(unittest.TestCase):
    """A dispatched call consumed tokens; a cancelled one did not."""

    def test_dispatched_failure_settles_and_never_releases(self):
        """The request left the process, so its tokens are real: a failure
        is recorded, not written off as a cancelled call."""
        budget = _budget()
        call = budget.reserve("context", input_tokens=500, max_output_tokens=200)
        usage = budget.settle(call, error="HTTP 500 after dispatch")
        self.assertTrue(usage.settled)
        self.assertEqual(budget.used(), {INPUT: 500, OUTPUT: 200})
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})
        # A failed call still draws down a *bounded* run budget.
        self.assertEqual(budget.remaining()[INPUT],
                         MAX_HOURGLASS_TOKEN_ALLOWANCE - 500)

    def test_dispatched_failure_past_a_ceiling_records_then_stops_the_run(self):
        """The only way to pass a ceiling is a provider-reported actual
        above it; the breach stops the run and names the failing call."""
        budget = _budget(run_max_output_tokens=100)
        call = budget.reserve("context", input_tokens=10, max_output_tokens=100,
                              label="synthesis")
        with self.assertRaises(HarnessError) as ctx:
            budget.settle(call, output_tokens=130,
                          error="HTTP 500 after dispatch")
        self.assertIn("Aborting", str(ctx.exception))
        self.assertIn("synthesis", str(ctx.exception))
        self.assertIn("HTTP 500 after dispatch", str(ctx.exception))
        # Recorded first, then the run stopped: the honest total survives.
        self.assertEqual(budget.used(), {INPUT: 10, OUTPUT: 130})
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})

    def test_dispatched_failure_with_no_usage_settles_the_estimate(self):
        budget = _budget()
        call = budget.reserve("context", input_tokens=500, max_output_tokens=200)
        usage = budget.settle(call, dispatched=True, error="timeout")
        self.assertTrue(usage.settled)
        self.assertEqual(usage.input_source, ESTIMATED)
        self.assertEqual(budget.used()[INPUT], 500)

    def test_pre_dispatch_cancellation_releases_and_records_nothing(self):
        budget = _budget(run_max_input_tokens=100)
        call = budget.reserve("context", input_tokens=100, max_output_tokens=10)
        usage = budget.settle(call, dispatched=False)
        self.assertFalse(usage.settled)
        self.assertEqual(budget.used(), {INPUT: 0, OUTPUT: 0})
        self.assertEqual(budget.remaining()[INPUT], 100)

    def test_cancel_does_not_strand_budget_for_the_rest_of_the_run(self):
        budget = _budget(run_max_input_tokens=100)
        budget.cancel(budget.reserve("context", input_tokens=100,
                                     max_output_tokens=10))
        self.assertIsNotNone(budget.reserve("context", input_tokens=100,
                                            max_output_tokens=10))

    def test_a_breach_at_settlement_is_recorded_then_reported(self):
        """Real tokens cannot be un-spent: record, then stop the run."""
        budget = _budget(run_max_output_tokens=100)
        call = budget.reserve("context", input_tokens=10, max_output_tokens=100)
        with self.assertRaises(HarnessError) as ctx:
            budget.settle(call, output_tokens=250)
        self.assertIn("exceeded", str(ctx.exception))
        self.assertEqual(budget.used()[OUTPUT], 250)
        self.assertEqual(budget.outstanding()[OUTPUT], 0)


class SnapshotTests(unittest.TestCase):
    """Used/remaining allowances are exposed, honestly, for envelopes."""

    def test_snapshot_reports_used_outstanding_and_remaining(self):
        # Output is explicitly unbounded here, so this test still covers the
        # rule that a partially-configured aggregate reports only the
        # dimensions that actually have a ceiling.
        budget = _budget(run_max_input_tokens=100, run_max_output_tokens=None,
                         stage_max_input_tokens=60, stage_max_output_tokens=None)
        live = budget.reserve("planning", input_tokens=30, max_output_tokens=10)
        budget.settle(budget.reserve("context", input_tokens=20,
                                     max_output_tokens=10))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["used"][INPUT], 20)
        self.assertEqual(snapshot["outstanding"][INPUT], 30)
        self.assertEqual(snapshot["remaining"][INPUT], 50)
        self.assertEqual(snapshot["calls_reserved"], 1)
        self.assertEqual(snapshot["calls_settled"], 1)
        self.assertEqual(snapshot["per_call"], {"max_input_tokens": 1000,
                                                "max_output_tokens": 500})
        self.assertEqual(snapshot["stages"]["planning"]["outstanding"][INPUT], 30)
        self.assertEqual(snapshot["stages"]["context"]["used"][INPUT], 20)
        self.assertEqual(snapshot["run_ceiling"], {"input": 100})
        self.assertEqual(snapshot["stage_ceiling"], {"input": 60})
        # The claim is still held by `live`.
        self.assertFalse(live.released)

    def test_snapshot_is_json_serializable_for_ledgers_and_surfaces(self):
        import json
        budget = _budget(run_max_input_tokens=100)
        budget.settle(budget.reserve("context", input_tokens=10,
                                     max_output_tokens=10))
        self.assertIsInstance(json.dumps(budget.snapshot()), str)

    def test_accessors_agree_with_the_snapshot(self):
        budget = _budget(run_max_output_tokens=100, stage_max_output_tokens=50)
        budget.settle(budget.reserve("context", input_tokens=10,
                                     max_output_tokens=30),
                      output_tokens=20)
        snapshot = budget.snapshot()
        self.assertEqual(budget.used(), snapshot["used"])
        self.assertEqual(budget.remaining(), snapshot["remaining"])
        self.assertEqual(budget.used("context")[OUTPUT], 20)
        self.assertEqual(budget.remaining("context")[OUTPUT], 30)
        self.assertEqual(budget.used("never-ran"), {INPUT: 0, OUTPUT: 0})


class SettingsIntegrationTests(_HermeticEnv):
    """The published config surface feeds the owner, validated, fail-closed."""

    def test_from_settings_uses_the_validated_settings(self):
        settings = load_settings(overrides={
            "hourglass_max_input_tokens": 4000,
            "hourglass_max_output_tokens": 2000,
            "hourglass_run_max_input_tokens": 9000,
            "hourglass_run_max_output_tokens": None,
            "hourglass_stage_max_input_tokens": 5000,
            "hourglass_stage_max_output_tokens": 1000,
        })
        budget = TokenBudget.from_settings(settings)
        self.assertEqual(budget.max_input_tokens, 4000)
        self.assertEqual(budget.max_output_tokens, 2000)
        self.assertEqual(budget.run_max_input_tokens, 9000)
        self.assertIsNone(budget.run_max_output_tokens)
        self.assertEqual(budget.stage_max_output_tokens, 1000)
        with self.assertRaises(HarnessError):
            budget.reserve("planning", input_tokens=4001, max_output_tokens=10)

    def test_settings_default_bounds_the_run_without_narrowing_lanes(self):
        """HV-3 must never *weaken* a lane, and must still enforce the canon's
        "limits for each stage and call". Both hold because the aggregate
        default is the repo's *existing* hard token maximum: no shipped
        ceiling sits below a number the repository already used.
        """
        settings = load_settings()
        self.assertEqual(settings.hourglass_max_input_tokens,
                         MAX_HOURGLASS_TOKEN_ALLOWANCE)
        for name in ("hourglass_run_max_input_tokens",
                     "hourglass_run_max_output_tokens",
                     "hourglass_stage_max_input_tokens",
                     "hourglass_stage_max_output_tokens"):
            with self.subTest(setting=name):
                self.assertEqual(getattr(settings, name),
                                 MAX_HOURGLASS_TOKEN_ALLOWANCE)

    def test_settings_still_allow_an_explicit_unbounded_opt_out(self):
        """Failing closed must not remove the owner's ability to state its
        own ceilings, including 'this dimension is not mine to bound'."""
        settings = load_settings(overrides={
            "hourglass_run_max_output_tokens": None,
            "hourglass_stage_max_output_tokens": None,
        })
        budget = TokenBudget.from_settings(settings)
        self.assertIsNone(budget.run_max_output_tokens)
        self.assertIsNone(budget.stage_max_output_tokens)
        self.assertIsNone(budget.remaining()[OUTPUT])
        # An owner that states only the output dimension unbounded still
        # gets a bounded input dimension -- the opt-out is per-dimension.
        self.assertEqual(budget.remaining()[INPUT],
                         MAX_HOURGLASS_TOKEN_ALLOWANCE)

    def test_a_default_configured_run_is_bounded_across_many_calls(self):
        """The shipped default must actually stop a run, not just one call."""
        settings = load_settings()
        budget = TokenBudget.from_settings(settings)
        cap = MAX_HOURGLASS_TOKEN_ALLOWANCE
        step = cap // 4
        for _ in range(4):
            call = budget.reserve("execution", prompt_text="ship it",
                                  max_output_tokens=step)
            budget.settle(call, output_tokens=step)
        self.assertEqual(budget.remaining()[OUTPUT], 0)
        with self.assertRaises(HarnessError) as ctx:
            budget.reserve("execution", prompt_text="one more",
                           max_output_tokens=1)
        self.assertIn("run", str(ctx.exception))

    def test_out_of_range_token_allowance_fails_closed(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_HOURGLASS_MAX_INPUT_TOKENS": "999999999"}):
            with self.assertRaises(HarnessError):
                load_settings()
        with mock.patch.dict(os.environ,
                             {"HARNESS_HOURGLASS_RUN_MAX_OUTPUT_TOKENS": "-5"}):
            with self.assertRaises(HarnessError):
                load_settings()
        with mock.patch.dict(os.environ,
                             {"HARNESS_HOURGLASS_STAGE_MAX_INPUT_TOKENS": "1e5"}):
            with self.assertRaises(HarnessError):
                load_settings()

    def test_configured_allowance_actually_bites(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_HOURGLASS_MAX_OUTPUT_TOKENS": "64"}):
            settings = load_settings()
        self.assertEqual(settings.hourglass_max_output_tokens, 64)
        with self.assertRaises(HarnessError):
            TokenBudget.from_settings(settings).reserve(
                "execution", input_tokens=10, max_output_tokens=65)

    def test_ceiling_constructor_rejects_nonsense(self):
        with self.assertRaises(HarnessError):
            TokenBudget(max_input_tokens=0)
        with self.assertRaises(HarnessError):
            TokenBudget(max_input_tokens=MAX_HOURGLASS_TOKEN_ALLOWANCE + 1)
        with self.assertRaises(HarnessError):
            TokenBudget(run_max_input_tokens=-1)
        with self.assertRaises(HarnessError):
            TokenBudget(stage_max_output_tokens="many")


class ComposedCeilingTests(unittest.TestCase):
    """Composition with SpendGovernor: both preflighted, neither weakened."""

    def test_token_exhaustion_blocks_while_dollars_remain(self):
        gov = _gov(max_cost=0.05)
        budget = _budget(run_max_input_tokens=100)
        budget.settle(budget.reserve("context", input_tokens=90,
                                     max_output_tokens=10))
        self.assertGreater(gov.remaining(), 0.0)
        with self.assertRaises(HarnessError):
            reserve_composed(budget, gov, "context", cost=0.001,
                             input_tokens=50, max_output_tokens=10)
        # Refused by the token owner, and the dollar owner was never asked
        # to move: no liability stranded on either side.
        self.assertEqual(gov.spent, 0.0)
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})

    def test_dollar_exhaustion_blocks_while_tokens_remain(self):
        gov = _gov(max_cost=0.001)
        budget = _budget(run_max_input_tokens=100000)
        with self.assertRaises(HarnessError):
            reserve_composed(budget, gov, "context", cost=0.05,
                             input_tokens=10, max_output_tokens=10)
        # The token claim taken first is unwound, so the token budget is
        # not quietly consumed by a call the dollar ceiling refused.
        self.assertEqual(budget.used(), {INPUT: 0, OUTPUT: 0})
        self.assertEqual(budget.remaining()[INPUT], 100000)
        self.assertIsNotNone(budget.reserve("context", input_tokens=10,
                                            max_output_tokens=10))

    def test_both_reservations_held_before_dispatch_and_settled_apart(self):
        gov = _gov(max_cost=0.05)
        budget = _budget(run_max_input_tokens=100, run_max_output_tokens=100)
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        # In flight: both budgets show the liability.
        self.assertEqual(budget.outstanding()[INPUT], 40)
        self.assertEqual(gov.outstanding, 0.001)
        usage = call.settle(input_tokens=25, output_tokens=12, cost=0.0004)
        self.assertEqual(usage.input_source, MEASURED)
        self.assertEqual(budget.used(), {INPUT: 25, OUTPUT: 12})
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})
        # Money is settled through the spend owner, at exactly what it was told.
        self.assertAlmostEqual(gov.spent, 0.0004)
        self.assertEqual(gov.outstanding, 0.0)

    def test_unknown_dollar_amount_is_refused_not_recorded_as_zero(self):
        """An unknown cost is not a $0.00 actual. Settling it as one
        fabricates a measurement and silently returns the reservation's
        headroom to the run; this lane must refuse instead."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        with self.assertRaises(HarnessError) as ctx:
            call.settle(cost=None, input_tokens=40)
        self.assertIn("unknown dollar", str(ctx.exception))
        # Nothing was written to the spend owner: no measured actual...
        self.assertEqual(gov.spent, 0.0)
        # ...and the reservation is still held, so the run cannot respend
        # the same money on a second call.
        self.assertAlmostEqual(gov.outstanding, 0.001)

    def test_unknown_dollar_amount_still_settles_the_real_tokens(self):
        """Tokens really were spent; refusing the dollar leg must not also
        write the token consumption off."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        with self.assertRaises(HarnessError):
            call.settle(cost=None, input_tokens=40, output_tokens=12)
        self.assertEqual(budget.used(), {INPUT: 40, OUTPUT: 12})
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})
        self.assertEqual(budget.usage_source(INPUT), MEASURED)
        self.assertEqual(budget.usage_source(OUTPUT), MEASURED)

    def test_a_composed_call_knows_its_own_stage(self):
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "planning", cost=0.001,
                                input_tokens=10, max_output_tokens=10)
        self.assertEqual(call.stage, "planning")
        self.assertEqual(call.label, "planning#1")
        call.settle(cost=0.0001, input_tokens=10)

    def test_the_caller_can_finish_the_dollar_leg_after_a_refusal(self):
        """The refusal is recoverable through the seam's own API, without
        the caller hand-reaching into the reservation."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        with self.assertRaises(HarnessError):
            call.settle(cost=None, input_tokens=40)
        self.assertTrue(call.dollar_outstanding)
        call.settle_dollar(0.0004)   # the provider's reported cost
        self.assertAlmostEqual(gov.spent, 0.0004)
        self.assertEqual(gov.outstanding, 0.0)
        self.assertFalse(call.dollar_outstanding)

    def test_a_refused_settlement_can_be_unwound_with_cancel(self):
        """Ordering 1: the refusal message offers cancel(), so cancel() must
        work -- it used to raise on the already-settled token leg and leave
        the dollar reservation stranded."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        with self.assertRaises(HarnessError):
            call.settle(cost=None, input_tokens=40, output_tokens=12)
        usage = call.cancel()          # must not raise
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(gov.spent, 0.0)
        self.assertFalse(call.dollar_outstanding)
        # The tokens that were really spent stay settled, and the usage
        # recorded at refusal time comes back.
        self.assertEqual(usage.input_tokens, 40)
        self.assertEqual(usage.output_tokens, 12)
        self.assertEqual(budget.used(), {INPUT: 40, OUTPUT: 12})

    def test_cancel_after_a_normal_settlement_is_a_no_op(self):
        """Ordering 2: cancel() after a completed settle closes nothing and
        records nothing."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        call.settle(cost=0.0004, input_tokens=40)
        call.cancel()
        self.assertAlmostEqual(gov.spent, 0.0004)
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(budget.used(), {INPUT: 40, OUTPUT: 30})

    def test_the_originating_token_error_survives_a_money_leg_failure(self):
        """A double settle must report why the call went wrong, not the
        follow-on 'unknown reservation' from the money leg."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        call.settle(cost=0.0004, input_tokens=40)
        with self.assertRaises(HarnessError) as ctx:
            call.settle(cost=0.0004, input_tokens=40)
        self.assertIn("unknown token reservation", str(ctx.exception))
        # The cost was not recorded a second time.
        self.assertAlmostEqual(gov.spent, 0.0004)

    def test_when_both_legs_fail_the_token_error_is_the_one_reported(self):
        """The masking case with both legs genuinely broken: the token leg
        breaches its own ceiling and the money leg breaches the dollar
        ceiling. The caller must be told the token failure, not the
        follow-on money one."""
        gov = _gov(max_cost=0.001)
        budget = _budget(run_max_output_tokens=100)
        call = reserve_composed(budget, gov, "context", cost=0.0001,
                                input_tokens=10, max_output_tokens=100)
        # Another call consumes the rest of the money budget, so the
        # eventual actual breaches the dollar ceiling too.
        gov.record_actual(0.0009, "other")
        with self.assertRaises(HarnessError) as ctx:
            call.settle(cost=0.05, output_tokens=130)
        message = str(ctx.exception)
        # The token breach is reported; the money breach is not.
        self.assertIn("output tokens exceeded the 100 run ceiling", message)
        self.assertNotIn("actual running cost", message)
        # Both legs were still closed, so neither budget is stranded.
        self.assertFalse(call.dollar_outstanding)
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(budget.outstanding()[OUTPUT], 0)

    def test_settle_dollar_refuses_when_nothing_is_outstanding(self):
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        call.settle(cost=0.0004, input_tokens=40)
        with self.assertRaises(HarnessError) as ctx:
            call.settle_dollar(0.0004)
        self.assertIn("no outstanding dollar reservation", str(ctx.exception))

    def test_composed_settle_passes_cost_through_untouched(self):
        """A real cost reaches the spend owner exactly as given -- this
        lane never re-derives, rounds, or estimates it."""
        gov = _gov(max_cost=0.05)
        budget = _budget()
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=30)
        usage = call.settle(cost=0.000123, input_tokens=40)
        self.assertEqual(usage.input_source, MEASURED)
        self.assertAlmostEqual(gov.spent, 0.000123)
        self.assertEqual(gov.outstanding, 0.0)

    def test_composed_cancel_records_a_true_zero_because_nothing_was_sent(self):
        """$0.00 is honest for a call that never dispatched, and is the
        opposite of the unknown-cost case above."""
        gov = _gov(max_cost=0.05)
        budget = _budget(run_max_input_tokens=100)
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=100, max_output_tokens=10)
        usage = call.cancel()
        self.assertFalse(usage.settled)
        self.assertEqual(budget.remaining()[INPUT], 100)
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(gov.spent, 0.0)

    def test_a_dollar_breach_at_settlement_still_releases_the_token_claim(self):
        gov = _gov(max_cost=0.001)
        budget = _budget(run_max_input_tokens=100)
        call = reserve_composed(budget, gov, "context", cost=0.001,
                                input_tokens=40, max_output_tokens=10)
        with self.assertRaises(HarnessError):
            call.settle(cost=0.05)  # provider billed past the dollar ceiling
        # The spend owner refused the actual but released the liability (its
        # own contract), and the token claim is settled, not stranded.
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})
        self.assertEqual(budget.used()[INPUT], 40)

    def test_reserved_cost_must_be_a_finite_non_negative_number(self):
        gov = _gov()
        budget = _budget()
        for bad in (-1, "free", None, float("inf")):
            with self.assertRaises(HarnessError):
                reserve_composed(budget, gov, "context", cost=bad,
                                 input_tokens=10, max_output_tokens=10)
        self.assertEqual(budget.outstanding(), {INPUT: 0, OUTPUT: 0})

    def test_concurrent_composed_calls_cannot_overcommit_either_budget(self):
        gov = _gov(max_cost=0.01)
        budget = _budget(run_max_input_tokens=100)
        barrier = threading.Barrier(2)
        refusals = []

        def worker():
            barrier.wait()
            try:
                call = reserve_composed(budget, gov, "context", cost=0.006,
                                        input_tokens=60, max_output_tokens=10)
                call.settle(cost=0.006)
            except HarnessError:
                refusals.append("refused")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(refusals), 1)
        self.assertLessEqual(gov.spent + gov.outstanding, 0.01 + 1e-9)
        self.assertLessEqual(budget.used()[INPUT]
                             + budget.outstanding()[INPUT], 100)


if __name__ == "__main__":
    unittest.main()
