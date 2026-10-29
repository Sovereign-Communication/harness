"""Budget and audit-log tests.

Both modules exist to make two promises the rest of the system relies on:
spend is bounded *before* it happens, and history is verifiable *after* it
happens. The concurrency tests are real threads rather than simulated
interleavings, because the failure being defended against -- several workers
each preflighting against the full remainder and collectively overspending
it -- is precisely a check-then-act race and cannot be observed sequentially.
"""
import multiprocessing
import os
import tempfile
import threading
import unittest

from driver_core.audit import (
    AuditError, AuditLog, GENESIS, MemoryAuditLog, _canonical, digest,
)
from driver_core.budget import Budget
from driver_core.errors import BudgetRefused


def _append_audit_record_in_process(path, start, results, kind):
    try:
        start.wait(timeout=10)
        record = AuditLog(path).append("capture", step_id=kind)
        results.put(("ok", record["seq"]))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))
        raise


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

    @staticmethod
    def _records(count):
        log = MemoryAuditLog()
        for index in range(count):
            log.append("capture", step_id=f"s{index}", source="test")
        return log.read_all()

    @staticmethod
    def _encoded(records, *, final_newline=True):
        text = "\n".join(_canonical(record) for record in records)
        if final_newline and records:
            text += "\n"
        return text.encode("utf-8")

    @staticmethod
    def _rehash(record, previous):
        record["hash"] = digest(
            {key: value for key, value in record.items() if key != "hash"},
            previous)

    def _write_bytes(self, value):
        with open(self.path, "wb") as handle:
            handle.write(value)

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
        verifier = AuditLog(self.path)
        with open(self.path, "r+", encoding="utf-8") as handle:
            body = handle.read().replace('"action":"observe"',
                                         '"action":"delete_file"')
            handle.seek(0)
            handle.write(body)
            handle.truncate()
        verdict = verifier.verify()
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

    def test_every_invalid_chain_shape_refuses_reopen_without_changing_bytes(self):
        wrong_genesis = self._records(1)
        wrong_genesis[0]["previous"] = "not-the-genesis-hash"

        sequence_gap = self._records(2)
        sequence_gap[1]["seq"] = 3
        self._rehash(sequence_gap[1], sequence_gap[0]["hash"])

        changed_payload = self._records(1)
        changed_payload[0]["source"] = "tampered"

        altered_hash = self._records(1)
        altered_hash[0]["hash"] = "0" * 64

        broken_middle_link = self._records(3)
        broken_middle_link[1]["previous"] = "broken-link"
        self._rehash(broken_middle_link[1], "broken-link")

        cases = (
            ("wrong genesis", self._encoded(wrong_genesis),
             "record 0 does not point to the genesis hash"),
            ("sequence gap", self._encoded(sequence_gap), "record 1 has seq 3"),
            ("changed payload", self._encoded(changed_payload),
             "record 0 has been altered"),
            ("altered hash", self._encoded(altered_hash),
             "record 0 has been altered"),
            ("broken middle link", self._encoded(broken_middle_link),
             "record 1 does not chain"),
            ("malformed JSON", b"{not-json}\n", "is not valid JSON"),
            ("truncated final row", self._encoded(self._records(1),
                                                   final_newline=False),
             "is truncated"),
        )
        for label, content, message in cases:
            with self.subTest(shape=label):
                self._write_bytes(content)
                with open(self.path, "rb") as handle:
                    before = handle.read()
                with self.assertRaises(AuditError) as raised:
                    AuditLog(self.path)
                self.assertIn(message, str(raised.exception))
                with open(self.path, "rb") as handle:
                    self.assertEqual(handle.read(), before)

    def test_append_revalidates_after_open_and_preserves_tampered_bytes(self):
        log = AuditLog(self.path)
        log.append("capture", step_id="s1")
        tampered = self._records(1)
        tampered[0]["source"] = "changed after the writer opened"
        self._write_bytes(self._encoded(tampered))
        with open(self.path, "rb") as handle:
            before = handle.read()

        with self.assertRaisesRegex(AuditError, "record 0 has been altered"):
            log.append("decision", step_id="s2")

        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_verify_reads_and_reports_one_locked_snapshot(self):
        class CountingAuditLog(AuditLog):
            def __init__(self, path):
                self.read_count = 0
                super().__init__(path)

            def _read_all_unlocked(self):
                self.read_count += 1
                return super()._read_all_unlocked()

        log = CountingAuditLog(self.path)
        log.append("capture", step_id="s1")
        log.read_count = 0
        verdict = log.verify()

        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.records, 1)
        self.assertEqual(log.read_count, 1)

    def test_separate_instances_serialize_concurrent_appends(self):
        first = AuditLog(self.path)
        second = AuditLog(self.path)
        start = threading.Barrier(3)
        appended = []
        errors = []
        result_lock = threading.Lock()

        def append(log, kind):
            try:
                start.wait()
                record = log.append(kind, step_id=kind)
                with result_lock:
                    appended.append(record)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=append, args=(first, "first")),
            threading.Thread(target=append, args=(second, "second")),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertCountEqual([record["seq"] for record in appended], [0, 1])
        reopened = AuditLog(self.path)
        self.assertTrue(reopened.verify().ok)
        self.assertEqual(len(reopened.read_all()), 2)

    def test_separate_processes_serialize_concurrent_appends(self):
        context = multiprocessing.get_context("spawn")
        start = context.Barrier(4)
        results = context.Queue()
        processes = [
            context.Process(
                target=_append_audit_record_in_process,
                args=(self.path, start, results, f"process-{index}"))
            for index in range(3)
        ]
        for process in processes:
            process.start()
        start.wait(timeout=10)
        for process in processes:
            process.join(timeout=10)

        self.assertFalse(any(process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0, 0, 0])
        outcomes = [results.get(timeout=3) for _ in processes]
        self.assertEqual([outcome[0] for outcome in outcomes], ["ok"] * 3)
        self.assertCountEqual([outcome[1] for outcome in outcomes], [0, 1, 2])
        reopened = AuditLog(self.path)
        self.assertTrue(reopened.verify().ok)
        self.assertEqual(len(reopened.read_all()), 3)

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
