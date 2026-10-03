"""Budget and audit-log tests.

Both modules exist to make two promises the rest of the system relies on:
spend is bounded *before* it happens, and history is verifiable *after* it
happens. The concurrency tests are real threads rather than simulated
interleavings, because the failure being defended against -- several workers
each preflighting against the full remainder and collectively overspending
it -- is precisely a check-then-act race and cannot be observed sequentially.
"""
import os
import tempfile
import threading
import unittest

from driver_core.audit import AuditError, AuditLog, GENESIS, MemoryAuditLog
from driver_core.budget import Budget
from driver_core.errors import BudgetRefused


class ReservationTests(unittest.TestCase):

    def test_a_reservation_holds_its_amount_against_the_ceiling(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.4, label="a")
        self.assertEqual(budget.reserved, 0.4)
        self.assertEqual(budget.remaining, 0.6)
        reservation.settle(0.3)
        self.assertEqual(budget.spent, 0.3)
        self.assertEqual(budget.reserved, 0.0)

    def test_settling_releases_the_hold_and_charges_the_actual(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.settle(0.1)
        self.assertAlmostEqual(budget.spent, 0.1)
        self.assertAlmostEqual(budget.remaining, 0.9)

    def test_an_unmeasurable_call_is_charged_the_full_reservation(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        charged = reservation.settle(None)
        self.assertAlmostEqual(charged, 0.5)
        self.assertEqual(reservation.usage_source, "unavailable")

    def test_cancelling_charges_nothing(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.cancel()
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.remaining, 1.0)

    def test_a_reservation_settles_exactly_once(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.settle(0.1)
        with self.assertRaises(RuntimeError):
            reservation.settle(0.1)
        self.assertAlmostEqual(budget.spent, 0.1)

    def test_a_cancelled_reservation_cannot_then_settle(self):
        budget = Budget(1.0)
        reservation = budget.reserve(0.5)
        reservation.cancel()
        with self.assertRaises(RuntimeError):
            reservation.settle(0.1)
        self.assertEqual(budget.spent, 0.0)


class CeilingTests(unittest.TestCase):

    def test_an_over_ceiling_reservation_is_refused_before_dispatch(self):
        budget = Budget(0.10, step_ceiling_usd=0.10)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.50, label="expensive")
        self.assertEqual(ctx.exception.estimated, 0.50)
        self.assertEqual(ctx.exception.ceiling, 0.10)
        self.assertEqual(budget.spent, 0.0)

    def test_a_refusal_carries_the_math(self):
        budget = Budget(0.10, step_ceiling_usd=0.10)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.50, label="clip")
        self.assertIn("0.500000", str(ctx.exception))
        self.assertIn("0.100000", str(ctx.exception))
        self.assertIn("clip", str(ctx.exception))

    def test_the_step_ceiling_bites_before_the_run_ceiling(self):
        budget = Budget(10.0, step_ceiling_usd=0.05)
        with self.assertRaises(BudgetRefused) as ctx:
            budget.reserve(0.5)
        self.assertEqual(ctx.exception.ceiling, 0.05)

    def test_a_leaked_reservation_is_visible_rather_than_silent(self):
        budget = Budget(1.0)
        budget.reserve(0.5, label="leaked")
        self.assertEqual(len(budget.open_reservations()), 1)
        budget.reserve(0.5, label="ok").cancel()
        self.assertEqual(len(budget.open_reservations()), 1)


class ConcurrencyTests(unittest.TestCase):

    def test_racing_workers_cannot_collectively_overspend(self):
        """40 threads race to reserve 0.1 against a 1.0 ceiling. Exactly 10
        may win. A check-then-act budget would admit more."""
        budget = Budget(1.0, step_ceiling_usd=1.0)
        admitted = []
        lock = threading.Lock()
        start = threading.Barrier(40)

        def attempt():
            start.wait()
            try:
                reservation = budget.reserve(0.1, label="race")
            except BudgetRefused:
                return
            with lock:
                admitted.append(reservation)

        threads = [threading.Thread(target=attempt) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(admitted), 10)
        self.assertLessEqual(budget.reserved, 1.0)

    def test_racing_settlements_never_double_charge(self):
        budget = Budget(10.0, step_ceiling_usd=10.0)
        reservations = [budget.reserve(0.1) for _ in range(20)]
        errors = []

        def settle(reservation):
            try:
                reservation.settle(0.05)
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=settle, args=(r,))
                   for r in reservations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertAlmostEqual(budget.spent, 20 * 0.05)


class AuditChainTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "audit.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_chain_verifies_after_appending(self):
        log = AuditLog(self.path)
        log.append("capture", step_id="s1", source="cli")
        log.append("decision", step_id="s1", action="observe")
        verdict = log.verify()
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.records, 2)

    def test_the_first_record_links_to_genesis(self):
        log = AuditLog(self.path)
        record = log.append("capture", step_id="s1")
        self.assertEqual(record["previous"], GENESIS)
        self.assertEqual(record["seq"], 0)

    def test_tampering_with_a_record_breaks_verification(self):
        log = AuditLog(self.path)
        log.append("capture", step_id="s1", source="cli")
        log.append("decision", step_id="s1", action="observe")
        with open(self.path, "r+", encoding="utf-8") as handle:
            body = handle.read().replace('"action":"observe"',
                                         '"action":"delete_file"')
            handle.seek(0)
            handle.write(body)
        verdict = AuditLog(self.path).verify()
        self.assertFalse(verdict.ok)
        self.assertIn("altered", verdict.detail)

    def test_a_deleted_record_stops_the_chain_being_extended(self):
        """Deleting a record is caught the moment the log is reopened, and
        the log refuses to continue rather than writing past the hole."""
        log = AuditLog(self.path)
        log.append("capture", step_id="s1")
        log.append("decision", step_id="s1", action="observe")
        log.append("action", step_id="s1", action="click")
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.readlines()
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines([lines[0], lines[2]])
        with self.assertRaises(AuditError):
            AuditLog(self.path)

    def test_a_restarted_run_extends_the_existing_chain(self):
        first = AuditLog(self.path)
        first.append("capture", step_id="s1")
        second = AuditLog(self.path)
        record = second.append("decision", step_id="s2")
        self.assertEqual(record["seq"], 1)
        self.assertEqual(record["previous"], first.head())
        self.assertTrue(AuditLog(self.path).verify().ok)

    def test_a_corrupt_line_stops_the_chain_from_being_extended(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        with self.assertRaises(AuditError):
            AuditLog(self.path)

    def test_an_unaltered_record_is_byte_stable(self):
        """Given the same facts, the serialised bytes and therefore the hash
        are identical. Without this a chain verifies once and then fails for
        no reason anyone can find."""
        from driver_core.audit import _canonical, digest
        record = {"seq": 0, "kind": "capture", "at": "2026-01-01T00:00:00",
                  "previous": GENESIS, "step_id": "s1", "source": "cli"}
        first = digest(record, GENESIS)
        second = digest(dict(reversed(list(record.items()))), GENESIS)
        self.assertEqual(first, second)
        self.assertEqual(_canonical(record), _canonical(
            dict(reversed(list(record.items())))))

    def test_two_independent_runs_record_the_same_facts(self):
        one = MemoryAuditLog()
        one.append("capture", step_id="s1", source="cli")
        two = MemoryAuditLog()
        two.append("capture", step_id="s1", source="cli")
        a, b = one.read_all()[0], two.read_all()[0]
        self.assertEqual(a["kind"], b["kind"])
        self.assertEqual(a["source"], b["source"])
        self.assertEqual(a["seq"], b["seq"])


if __name__ == "__main__":
    unittest.main()
