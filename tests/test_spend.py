"""Spend-governor policy: per-token math, ceilings, key trust, payload guards."""
import unittest

from harness.errors import HarnessError
from harness.chat import chat, extract_content_and_cost
from harness.panel import panel_judge
from harness.tokens import estimate_prompt_tokens
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class CostMathTests(unittest.TestCase):
    def test_empty_preflight_is_zero_cost_and_skips_pricing(self):
        gov = _gov(FakeTransport(models=[m(P1)]), max_cost=0.05)
        gov.fetch_pricing = lambda _models: self.fail(
            "empty preflight must not fetch pricing")

        self.assertEqual(gov._preflight("prompt", []), (0.0, [], None))
        self.assertEqual(gov.spent, 0.0)
        self.assertEqual(gov.outstanding, 0.0)

    def test_snapshot_reports_public_spend_state(self):
        gov = _gov(FakeTransport(models=[m(P1)]), max_cost=0.05)
        token = gov.reserve(0.00234567, "task_1")
        gov.reconcile(token, 0.00123456)
        self.assertEqual(gov.snapshot(), {
            "spent": 0.001235,
            "outstanding": 0.0,
            "ceiling": 0.05,
        })

    def test_pricing_is_per_token_not_per_million(self):
        """Regression: OpenRouter pricing fields are per-token dollars. An
        earlier SCMessenger version divided by 1e6 a second time and
        undercounted worst-case cost ~1,000,000x. Costs here must land in the
        ~1e-5..1e-4 range, not ~1e-10."""
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)])
        gov = _gov(fake)
        prompt = " ".join(["word"] * 200)  # ~350 estimated tokens
        calls = [(P1, P1, 300, 0), (P2, P2, 300, 0), ("judge", JUDGE, 350, 700)]
        total, breakdown = gov.preflight(prompt, calls)
        pt = estimate_prompt_tokens(prompt)
        expected = (pt * 1e-8 + 300 * 2e-8) * 2 + (pt + 700) * 1e-8 + 350 * 2e-8
        self.assertAlmostEqual(total, expected, places=12)
        for _, _model, cost in breakdown:
            self.assertGreater(cost, 1e-9, "cost is mis-scaled by orders of magnitude")
        self.assertEqual(gov.spent, 0.0, "preflight must not spend anything")

    def test_preflight_refuses_when_over_ceiling(self):
        fake = FakeTransport(models=[m(P1, "0.0001", "0.0002")])
        gov = _gov(fake, max_cost=0.01)
        with self.assertRaises(HarnessError):
            gov.preflight(" ".join(["word"] * 5000), [(P1, P1, 300, 0)])

    def test_preflight_rechecks_latch_after_pricing_fetch(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake)
        original_fetch_pricing = gov.fetch_pricing

        def latch_during_fetch(model_ids):
            with self.assertRaisesRegex(HarnessError, "invalid reported cost"):
                gov.record_actual(None, "unaccounted provider")
            return original_fetch_pricing(model_ids)

        gov.fetch_pricing = latch_during_fetch
        with self.assertRaisesRegex(HarnessError, "prior billed response"):
            gov.preflight("hi", [(P1, P1, 10, 0)])

    def test_phase_change_during_pricing_fetch_invalidates_preflight(self):
        import threading

        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        started = threading.Event()
        resume = threading.Event()
        errors = []
        original_fetch = gov.fetch_pricing

        def blocking_fetch(model_ids):
            started.set()
            if not resume.wait(timeout=2):
                raise AssertionError("preflight pricing timed out")
            return original_fetch(model_ids)

        gov.fetch_pricing = blocking_fetch

        def preflight_attempt():
            try:
                gov.preflight("attempt", [("attempt", P1, 20, 0)])
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=preflight_attempt)
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        gov.set_phase("terminal")
        resume.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "phase changed during preflight")
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_chat_dispatch_pins_phase_until_actual_is_recorded(self):
        import threading

        entered = threading.Event()
        resume = threading.Event()

        class BlockingTransport(FakeTransport):
            def post(self, url, api_key, payload, timeout=45):
                entered.set()
                if not resume.wait(timeout=2):
                    raise RuntimeError("test dispatch timed out")
                return super().post(url, api_key, payload, timeout)

        fake = BlockingTransport(models=[m(P1)], posts=[comp("ok", cost=0.001)])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        gov.preflight("prompt", [("call", P1, 20, 0)])
        failures = []

        def dispatch_and_settle():
            try:
                status, response = chat(
                    fake, "k", P1, [{"role": "user", "content": "prompt"}],
                    20, governor=gov)
                self.assertEqual(status, 200)
                _content, _finish, cost, _byok = extract_content_and_cost(response)
                gov.record_actual(cost, P1)
            except Exception as exc:  # surfaced to this test thread below
                failures.append(exc)

        worker = threading.Thread(target=dispatch_and_settle)
        worker.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.assertRaisesRegex(HarnessError, "provider dispatches"):
            gov.set_phase("terminal")
        self.assertEqual(gov.outstanding, 0.0)  # phase guard is not a dollar reserve
        resume.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertAlmostEqual(gov.spent, 0.001)
        self.assertEqual(gov.set_phase("terminal"), "terminal")

    def test_stale_preflight_refuses_after_phase_change(self):
        fake = FakeTransport(models=[m(P1)], posts=[comp("must not run")])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        gov.preflight("prompt", [("call", P1, 20, 0)])
        gov.set_phase("terminal")
        with self.assertRaisesRegex(HarnessError, "phase changed after preflight"):
            chat(fake, "k", P1, [{"role": "user", "content": "prompt"}],
                 20, governor=gov)
        with self.assertRaisesRegex(HarnessError, "phase changed after preflight"):
            gov.begin_dispatch()
        self.assertEqual(fake.chat_posts(), [])

    def test_preflight_phase_is_isolated_between_threads(self):
        import threading

        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        attempt_preflight_done = threading.Event()
        allow_attempt_dispatch = threading.Event()
        terminal_dispatch_started = threading.Event()
        allow_terminal_settlement = threading.Event()
        attempt_errors = []
        terminal_errors = []

        def attempt_lane():
            gov.preflight("attempt", [("attempt", P1, 20, 0)])
            attempt_preflight_done.set()
            if not allow_attempt_dispatch.wait(timeout=2):
                attempt_errors.append(AssertionError("attempt dispatch timed out"))
                return
            try:
                token = gov.begin_dispatch()
            except HarnessError as exc:
                attempt_errors.append(exc)
            else:
                gov.fail_dispatch(token, unknown_cost=False)
                attempt_errors.append(AssertionError("stale attempt dispatch was allowed"))

        def terminal_lane():
            try:
                gov.preflight("terminal", [("terminal", P1, 20, 0)])
                token = gov.begin_dispatch()
                terminal_dispatch_started.set()
                if not allow_terminal_settlement.wait(timeout=2):
                    terminal_errors.append(AssertionError("terminal settlement timed out"))
                    gov.fail_dispatch(token, unknown_cost=False)
                    return
                gov.record_actual(0.0, "terminal")
            except Exception as exc:
                terminal_errors.append(exc)

        attempt = threading.Thread(target=attempt_lane)
        attempt.start()
        self.assertTrue(attempt_preflight_done.wait(timeout=2))
        self.assertEqual(gov.set_phase("terminal"), "terminal")

        terminal = threading.Thread(target=terminal_lane)
        terminal.start()
        self.assertTrue(terminal_dispatch_started.wait(timeout=2))
        allow_attempt_dispatch.set()
        attempt.join(timeout=2)
        self.assertFalse(attempt.is_alive())
        self.assertEqual(len(attempt_errors), 1)
        self.assertIsInstance(attempt_errors[0], HarnessError)
        self.assertIn("phase changed after preflight", str(attempt_errors[0]))
        self.assertEqual(gov._dispatches_in_progress, 1)

        allow_terminal_settlement.set()
        terminal.join(timeout=2)
        self.assertFalse(terminal.is_alive())
        self.assertEqual(terminal_errors, [])
        self.assertEqual(gov._dispatches_in_progress, 0)
        self.assertEqual(gov.outstanding, 0.0)

    def test_preflights_from_different_phases_cannot_overwrite_pending_context(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        gov.preflight("attempt", [("attempt", P1, 20, 0)])
        gov.set_phase("terminal")
        gov.preflight("terminal", [("terminal", P1, 20, 0)])
        with self.assertRaisesRegex(HarnessError, "multiple pending preflights"):
            gov.begin_dispatch()
        with self.assertRaisesRegex(HarnessError, "multiple pending preflights"):
            gov.begin_dispatch()
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_repeated_same_phase_implicit_preflights_are_ambiguous(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        gov.preflight("first", [("first", P1, 20, 0)])
        gov.preflight("second", [("second", P1, 20, 0)])
        with self.assertRaisesRegex(HarnessError, "multiple pending preflights"):
            gov.begin_dispatch()
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_failed_later_preflight_preserves_stale_phase_snapshot(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake, max_cost=0.10, terminal_reserve=0.05)
        gov.preflight("attempt", [("attempt", P1, 20, 0)])
        gov.set_phase("terminal")
        with self.assertRaises(HarnessError):
            gov.preflight("invalid model", [("bad", "missing/model", 20, 0)])
        with self.assertRaisesRegex(HarnessError, "phase changed after preflight"):
            gov.begin_dispatch()
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_explicit_phase_lease_covers_multiple_attempts_and_generation(self):
        gov = _gov(FakeTransport(models=[m(P1)]), max_cost=0.10,
                   terminal_reserve=0.05)
        _total, _breakdown, phase_lease = gov.preflight_with_lease(
            "two bounded attempts", [("attempt-1", P1, 20, 0),
                                     ("attempt-2", P1, 20, 0)])
        gov.begin_dispatch(phase_lease)
        gov.record_actual(0.0, "attempt-1")
        self.assertEqual(gov._dispatches_in_progress, 0)
        self.assertEqual(gov.set_phase("terminal"), "terminal")
        with self.assertRaisesRegex(HarnessError, "phase changed after preflight"):
            gov.begin_dispatch(phase_lease)
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_begin_dispatch_rejects_invalid_explicit_lease(self):
        gov = _gov(FakeTransport(models=[m(P1)]))
        with self.assertRaisesRegex(HarnessError,
                                    "invalid spend preflight phase lease"):
            gov.begin_dispatch(object())
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_finish_dispatch_preserves_count_when_token_index_is_inconsistent(self):
        import threading

        gov = _gov(FakeTransport(models=[m(P1)]))
        token = gov.begin_dispatch()
        thread_id = threading.get_ident()
        try:
            with gov._spend_lock:
                tokens = gov._dispatch_tokens_by_thread[thread_id]
                self.assertIs(tokens.pop(), token)
                self.assertFalse(gov._finish_dispatch_locked(token))
                self.assertEqual(gov._dispatch_thread_by_id[id(token)], thread_id)
                self.assertEqual(gov._dispatches_in_progress, 1)
        finally:
            with gov._spend_lock:
                tokens = gov._dispatch_tokens_by_thread.get(thread_id, [])
                if not any(current is token for current in tokens):
                    tokens.append(token)
            gov.fail_dispatch(token, unknown_cost=False)
        self.assertEqual(gov._dispatches_in_progress, 0)

    def test_unknown_model_refused(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            gov.preflight("hi", [("x", "nope/model", 10, 0)])

    def test_key_must_have_finite_limit(self):
        fake = FakeTransport(key={"label": "sk-test", "limit": None})
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            gov.verify_key()

    def test_expect_key_label_mismatch(self):
        fake = FakeTransport(key={"label": "sk-or-v1-aaaa", "limit": 1.0})
        gov = _gov(fake, expect_key_label="bbbb")
        with self.assertRaises(HarnessError):
            gov.verify_key()

    def test_expect_key_label_match(self):
        """Audit #9b: label expectation is an EXACT match now (substring let a
        similarly-named key through), and the error never echoes labels."""
        fake = FakeTransport(key={"label": "sk-or-v1-aaaa", "limit": 1.0,
                                  "limit_remaining": 0.5})
        gov = _gov(fake, expect_key_label="sk-or-v1-aaaa")
        info = gov.verify_key()
        self.assertEqual(info["label"], "sk-or-v1-aaaa")
        wrong = _gov(FakeTransport(key={"label": "sk-or-v1-bbbb", "limit": 1.0,
                                        "limit_remaining": 0.5}),
                     expect_key_label="sk-or-v1-aaaa")
        with self.assertRaises(HarnessError) as ctx:
            wrong.verify_key()
        self.assertNotIn("bbbb", str(ctx.exception))
        self.assertNotIn("aaaa", str(ctx.exception))


class GuardTests(unittest.TestCase):
    def test_no_tools_key_ever(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a"), comp("b"), comp("verdict")])
        gov = _gov(fake)
        panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                    panel=[P1, P2], judge=JUDGE)
        for payload in fake.payloads():
            self.assertNotIn("tools", payload)

    def test_byok_denied_before_any_post(self):
        fake = FakeTransport(models=[m(P1), m("anthropic/claude-3.5-sonnet")])
        gov = _gov(fake)
        with self.assertRaises(HarnessError) as ctx:
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, "anthropic/claude-3.5-sonnet"], judge=JUDGE)
        self.assertIn("BYOK", str(ctx.exception))
        self.assertEqual(fake.chat_posts(), [], "no chat call may go out")

    def test_mid_batch_fail_closed(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a", cost=0.0009), comp("b", cost=0.0009)])
        gov = _gov(fake, max_cost=0.001)
        with self.assertRaises(HarnessError):
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, P2], judge=JUDGE)
        # judge must never have been called
        self.assertEqual(len(fake.chat_posts()), 2)


class ReservationTests(unittest.TestCase):
    """MR-6 verdict pins: reservations are real liabilities; concurrent
    dispatch can never push spent + outstanding over the ceiling."""

    def setUp(self):
        self.fake = FakeTransport(models=[m("cheap/x")])
        self.gov = _gov(self.fake, max_cost=0.01)

    def test_reserve_and_reconcile(self):
        token = self.gov.reserve(0.004, "task_1")
        self.assertEqual(self.gov.outstanding, 0.004)
        self.gov.reconcile(token, 0.002)
        self.assertEqual(self.gov.outstanding, 0.0)
        self.assertEqual(self.gov.spent, 0.002)

    def test_reservation_refused_over_ceiling(self):
        with self.assertRaises(HarnessError):
            self.gov.reserve(0.02, "task_1")

    def test_reconcile_unknown_token_raises(self):
        with self.assertRaises(HarnessError):
            self.gov.reconcile(("ghost", 0.004), 0.0)

    def test_concurrent_duplicate_reconcile_refuses_while_first_settles(self):
        import threading
        from unittest import mock
        import harness.spend as spend_mod

        token = self.gov.reserve(0.004, "task_1")
        validating = threading.Event()
        resume = threading.Event()
        failures = []
        original_finite_number = spend_mod.finite_number

        def blocking_validation(value, label, minimum):
            validating.set()
            if not resume.wait(timeout=2):
                raise AssertionError("first settlement validation timed out")
            return original_finite_number(value, label, minimum)

        def settle_first():
            try:
                self.gov.reconcile(token, 0.001)
            except Exception as exc:
                failures.append(exc)

        with mock.patch.object(spend_mod, "finite_number",
                               side_effect=blocking_validation):
            worker = threading.Thread(target=settle_first)
            worker.start()
            try:
                self.assertTrue(validating.wait(timeout=2))
                with self.assertRaisesRegex(
                        HarnessError, "already being settled"):
                    self.gov.reconcile(token, 0.001)
            finally:
                resume.set()
                worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.gov.spent, 0.001)
        self.assertEqual(self.gov.outstanding, 0.0)
        self.assertEqual(self.gov._settlements_in_progress, 0)

    def test_reconcile_invalid_actual_raises(self):
        token = self.gov.reserve(0.004, "task_1")
        with self.assertRaises(HarnessError):
            self.gov.reconcile(token, "not-a-number")

    def test_reconcile_over_ceiling_actual_raises_and_records_billed_cost(self):
        token = self.gov.reserve(0.004, "task_1")
        with self.assertRaises(HarnessError):
            self.gov.reconcile(token, 0.02)
        # Release the estimate but preserve the provider-billed actual. The
        # public run total is capped at max_cost for envelope compatibility;
        # the per-model ledger retains the exact charge.
        self.assertEqual(self.gov.outstanding, 0.0)
        self.assertEqual(self.gov.spent, 0.01)
        self.assertEqual(self.gov.cost_by_model(), {"task_1": 0.02})
        self.assertIn("attempt", self.gov._budget_breached_phases)

    def test_reconcile_locked_recheck_raises_under_race(self):
        """The lock-guarded re-check is the last line of defense: another
        thread can land an actual between the unlocked check and the
        locked commit -- that interleaving must still fail closed."""
        gov = _gov(self.fake, max_cost=0.01)
        token = gov.reserve(0.004, "task_1")

        class RacyLock:
            enters = 0

            def __enter__(self):
                RacyLock.enters += 1
                if RacyLock.enters == 2:  # second entry: the commit block
                    gov.spent = 0.009     # a concurrent winner landed
            def __exit__(self, *exc):
                return False

        gov._spend_lock = RacyLock()
        with self.assertRaises(HarnessError):
            gov.reconcile(token, 0.005)
        # The reconciliation discovers the overrun under the commit lock;
        # retain the billed amount while respecting the public run-total cap.
        self.assertEqual(gov.spent, 0.01)
        self.assertEqual(gov.cost_by_model(), {"task_1": 0.005})
        self.assertIn("attempt", gov._budget_breached_phases)

    def test_node_reserver_uses_route_tier_ceiling(self):
        from harness.dag import DAGNode
        from harness.spend import NodeReserver

        gov = _gov(self.fake, max_cost=0.01)
        routes = {"task_1": {"task_max_cost": 0.004}, "task_2": None}
        reserver = NodeReserver(gov, routes, 0.006,
                                route_kwargs_fn=lambda d: dict(d or {}))
        n1 = DAGNode(node_id="task_1", instruction="x")
        n2 = DAGNode(node_id="task_2", instruction="y")
        n3 = DAGNode(node_id="task_9", instruction="z")  # absent from routes
        t1 = reserver.reserve(n1)
        self.assertEqual(gov.outstanding, 0.004)
        t2 = reserver.reserve(n2)
        self.assertEqual(gov.outstanding, 0.010)
        with self.assertRaises(HarnessError):
            reserver.reserve(n3)  # default worst case would breach
        reserver.reconcile(t1, 0.001)
        self.assertEqual(gov.spent, 0.001)
        self.assertEqual(gov.outstanding, 0.006)
        reserver.reconcile(t2, 0.002)
        self.assertEqual(gov.spent, 0.003)

    def test_node_reserver_free_route_reserves_zero_under_small_ceiling(self):
        """The GUI-lane defect: a FREE node declares a $0.00 route ceiling,
        but ``node_apply_kwargs`` deliberately drops a $0 ceiling from the
        request (a zero task budget would refuse the escalation ladder), so
        the reserver used to read "no ceiling" and reserve the engine's
        nominal $0.10 default. Against the default $0.05 run ceiling that
        refused EVERY node before any work, so the lane completed nothing.
        """
        from harness.dag import DAGNode, plan_task
        from harness.spend import NodeReserver

        gov = _gov(self.fake, max_cost=0.05)
        plan = plan_task(goal="Add a module docstring",
                         candidate_files=["util.py"], use_free=True)
        routes = {n["node_id"]: n for n in plan["nodes"]}
        node_id = list(routes)[0]
        reserver = NodeReserver(gov, routes, 0.10, run_ceiling=0.05)
        self.assertEqual(float(routes[node_id]["route"]["cost_ceiling"]), 0.0)
        # The nominal default is an unrelated number: capped by the run's own
        # ceiling instead of trusted as a cost.
        self.assertEqual(reserver.default_amount, 0.05)
        token = reserver.reserve(DAGNode(node_id=node_id, instruction="x"))
        self.assertEqual(token[1], 0.0)
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(gov.remaining(), 0.05)

    def test_node_reserver_refuses_a_worst_case_that_cannot_fit(self):
        """Fail-closed is preserved: an amount larger than what the run can
        still afford refuses rather than being trimmed to fit (a trimmed
        reservation would let a call that may bill its full ceiling dispatch
        and only surface at reconcile time, after the money was spent)."""
        from harness.dag import DAGNode
        from harness.spend import NodeReserver

        gov = _gov(self.fake, max_cost=0.05)
        routes = {"task_1": {"route": {"ladder": ["m/p"],
                                       "cost_ceiling": 0.10}}}
        reserver = NodeReserver(gov, routes, 0.10, run_ceiling=0.05)
        with self.assertRaises(HarnessError) as ctx:
            reserver.reserve(DAGNode(node_id="task_1", instruction="x"))
        self.assertIn("does not fit", str(ctx.exception))
        self.assertEqual(gov.outstanding, 0.0)

    def test_node_reserver_defaults_its_ceiling_to_the_governor(self):
        """No explicit run ceiling: the governor's own max_cost IS the run
        ceiling, so the fallback bound still cannot exceed it."""
        from harness.dag import DAGNode
        from harness.spend import NodeReserver

        gov = _gov(self.fake, max_cost=0.02)
        reserver = NodeReserver(gov, {}, 0.10)
        self.assertEqual(reserver.run_ceiling, 0.02)
        self.assertEqual(reserver.default_amount, 0.02)
        reserver.reserve(DAGNode(node_id="unknown", instruction="x"))
        self.assertEqual(gov.outstanding, 0.02)

    def test_node_reserver_paid_ceiling_is_still_used_verbatim(self):
        """A real (nonzero) tier ceiling is the node's worst case: reserving
        it is what keeps W concurrent workers from overcommitting."""
        from harness.dag import DAGNode
        from harness.spend import NodeReserver

        gov = _gov(self.fake, max_cost=0.05)
        routes = {"task_1": {"route": {"ladder": ["m/p"],
                                       "cost_ceiling": 0.02}}}
        reserver = NodeReserver(gov, routes, 0.10, run_ceiling=0.05)
        token = reserver.reserve(DAGNode(node_id="task_1", instruction="x"))
        self.assertEqual(token[1], 0.02)
        self.assertEqual(gov.outstanding, 0.02)

    def test_remaining_counts_reservations_as_committed(self):
        """``remaining`` is the one accessor for "what this run can still
        commit": outstanding reservations are real liability, so they count
        exactly like recorded spend."""
        gov = _gov(self.fake, max_cost=0.01)
        self.assertEqual(gov.remaining(), 0.01)
        gov.reserve(0.004, "task_1")
        self.assertAlmostEqual(gov.remaining(), 0.006, places=12)
        gov.record_actual(0.002, "label")
        self.assertAlmostEqual(gov.remaining(), 0.004, places=12)

    def test_outstanding_blocks_preflight(self):
        self.gov.reserve(0.0095, "task_1")
        # Remaining headroom is 0.0005 minus the outstanding liability: a
        # preflight worst case above that must refuse even though raw spent
        # is still ~0.
        with self.assertRaises(HarnessError):
            self.gov.preflight("p", [("ask", "cheap/x", 40000, 0)])

    def test_concurrent_dispatch_cannot_overcommit_ceiling(self):
        """The MR-6 proof obligation: W barrier-synced workers reserving
        per-call worst cases never overspend; without reservations the
        same interleaving would bill 2x the ceiling."""
        import threading

        gov = _gov(self.fake, max_cost=0.01)
        barrier = threading.Barrier(2)
        overspends = []

        def worker():
            barrier.wait()
            try:
                token = gov.reserve(0.006, "w")
                gov.reconcile(token, 0.006)
            except HarnessError as exc:
                overspends.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # spent + outstanding <= ceiling at every point: the second worker's
        # reserve is refused (0.006 outstanding + 0.006 > 0.01), so the
        # billed total can never reach 0.012.
        self.assertEqual(len(overspends), 1)
        self.assertLessEqual(gov.spent + gov.outstanding, 0.01 + 1e-9)

    def test_unreserved_inflight_overspend_documented(self):
        """Documents WHY per-call preflight alone is insufficient (MR-6):
        two check-only preflights against the full remaining ceiling both
        pass before either records; only the SECOND actual trips the guard
        -- by then both calls are already dispatched and billed."""
        gov = _gov(self.fake, max_cost=0.01)
        gov.preflight("p", [("a", "cheap/x", 100, 0)])
        gov.preflight("p", [("b", "cheap/x", 100, 0)])   # also passes
        gov.record_actual(0.009, "a")                    # first actual fits
        with self.assertRaises(HarnessError):
            gov.record_actual(0.009, "b")                # too late: both ran
        self.assertLessEqual(gov.spent, 0.01)


if __name__ == "__main__":
    unittest.main()
