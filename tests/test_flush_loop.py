"""FLUSH-0 acceptance: the contracts a saturation pass cannot talk around.

The three tests the commission names are here verbatim
(``test_pass_refused_without_delta``, ``test_whitespace_delta_is_not_a_delta``,
``test_register_has_no_noted_state``), plus the adjacent properties this layer
claims: a real delta is admitted, a close needs a cure and its read, tail
findings promote at the threshold, and the ledger is append-only and fails
closed on a corrupt line.

No judge, panel or live call is exercised, because FLUSH-0 has none.
"""
import json
import tempfile
import unittest
from pathlib import Path

from harness.flush_loop import (
    CLOSED,
    DECISION_READY,
    EventLedger,
    EvidenceRecord,
    Finding,
    GapRegister,
    GapRegisterError,
    PassRefused,
    begin_pass,
    pass_identity,
    record_pass,
    record_refusal,
    semantic_delta,
)


class RuleZeroTests(unittest.TestCase):
    def test_pass_refused_without_delta(self):
        records = [EvidenceRecord("ev-1", "the audit chain verifies")]
        first, _delta = begin_pass(None, records=records)
        with self.assertRaises(PassRefused) as raised:
            begin_pass(first, records=records)
        self.assertIn("no semantic delta", str(raised.exception))
        self.assertIn("formatting churn is not a delta", str(raised.exception))

    def test_whitespace_delta_is_not_a_delta(self):
        original = "line one\nline two\nline three\n"
        reformatted = ("line one   \r\n\r\n\r\nline two\t\nline three\r\n\r\n")
        first, _delta = begin_pass(
            None, records=[EvidenceRecord("ev-1", original)])
        second = pass_identity(records=[EvidenceRecord("ev-1", reformatted)])
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(semantic_delta(first, second), ())
        with self.assertRaises(PassRefused):
            begin_pass(first, records=[EvidenceRecord("ev-1", reformatted)])

    def test_a_real_edit_is_a_delta(self):
        first, _delta = begin_pass(
            None, records=[EvidenceRecord("ev-1", "the chain verifies")])
        second, delta = begin_pass(
            first, records=[EvidenceRecord("ev-1", "the chain fails to verify")])
        self.assertIn("evidence", delta)
        self.assertNotEqual(first.digest, second.digest)

    def test_a_new_record_angle_or_instrument_is_a_delta(self):
        records = [EvidenceRecord("ev-1", "same")]
        first, _ = begin_pass(None, records=records)
        _current, delta = begin_pass(
            first, records=records + [EvidenceRecord("ev-2", "new")])
        self.assertIn("evidence", delta)
        _current, delta = begin_pass(first, records=records, angles=["flaw-hunt"])
        self.assertIn("angle", delta)
        _current, delta = begin_pass(first, records=records,
                                     instruments=["deterministic"])
        self.assertIn("instrument", delta)
        _current, delta = begin_pass(first, records=records,
                                     target="a different target")
        self.assertIn("target", delta)


class GapRegisterTests(unittest.TestCase):
    def test_register_has_no_noted_state(self):
        register = GapRegister()
        finding = register.add(Finding("gap-1", "the claim has no read"),
                               materiality="violates_invariant")
        with self.assertRaises(GapRegisterError) as raised:
            register.note("gap-1", "keep an eye on this")
        self.assertIn("no 'noted' state", str(raised.exception))
        with self.assertRaises(GapRegisterError):
            register.set_state("gap-1", "noted")
        self.assertEqual(finding.state, "open")
        self.assertEqual([f.id for f in register.open_findings()], ["gap-1"])

    def test_only_the_two_exit_states_are_reachable(self):
        register = GapRegister()
        register.add(Finding("gap-1", "no read"), materiality="axis_move")
        register.close("gap-1", cure="commit abc123", read="re-score 91")
        self.assertEqual(register.get("gap-1").state, CLOSED)
        register.add(Finding("gap-2", "needs a decision"),
                     materiality="changes_ship_decision")
        register.decide_ready("gap-2", packet="docs/packet.md",
                              owner_act="owner signs the bar change")
        self.assertEqual(register.get("gap-2").state, DECISION_READY)

    def test_a_close_without_a_cure_or_read_is_refused(self):
        register = GapRegister()
        register.add(Finding("gap-1", "no read"), materiality="axis_move")
        with self.assertRaises(GapRegisterError):
            register.close("gap-1", cure="", read="re-score 91")
        with self.assertRaises(GapRegisterError):
            register.close("gap-1", cure="commit abc123", read="")
        self.assertEqual(register.get("gap-1").state, "open")

    def test_tail_findings_promote_at_the_threshold(self):
        register = GapRegister()
        for index in range(4):
            register.add(Finding(f"tail-{index}", "small", bucket="style"))
        self.assertEqual(register.tail_count("style"), 4)
        self.assertEqual(register.findings, {})
        register.add(Finding("tail-4", "small", bucket="style"))
        self.assertEqual(register.tail_count("style"), 0)
        self.assertIn("tail-4", register.findings)

    def test_an_unknown_materiality_reason_is_refused(self):
        register = GapRegister()
        with self.assertRaises(GapRegisterError):
            register.add(Finding("gap-1", "x"), materiality="felt important")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "events.jsonl"

    def test_events_append_and_read_back_in_order(self):
        ledger = EventLedger(self.path)
        records = [EvidenceRecord("ev-1", "same")]
        identity, delta = begin_pass(None, records=records)
        record_pass(ledger, identity=identity, delta=delta, campaign="c1")
        record_refusal(ledger, reason="no semantic delta", campaign="c1")
        events = ledger.read()
        self.assertEqual([event["event"] for event in events],
                         ["pass_admitted", "pass_refused"])
        self.assertEqual(events[0]["delta"], ["first_pass"])

    def test_a_corrupt_line_fails_closed(self):
        self.path.write_text('{"event": "pass_admitted"}\nnot json\n',
                             encoding="utf-8")
        with self.assertRaises(GapRegisterError) as raised:
            EventLedger(self.path).read()
        self.assertIn("fails closed", str(raised.exception))

    def test_the_ledger_is_jsonl_on_disk(self):
        ledger = EventLedger(self.path)
        record_refusal(ledger, reason="no delta")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["event"], "pass_refused")


if __name__ == "__main__":
    unittest.main()
