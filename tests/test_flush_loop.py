"""FLUSH-0 and FLUSH-1: the contracts, and the loop that must honour them.

The commission names five acceptance tests; all five are here verbatim --
``test_pass_refused_without_delta``, ``test_whitespace_delta_is_not_a_delta``,
``test_register_has_no_noted_state``, ``test_stop_requires_two_clean_expansion_passes``
and ``test_info_request_names_missing_record`` -- plus the adjacent properties
these layers claim: a real delta is admitted, a close needs a cure and its
read, tail findings promote at the threshold, the ledger is append-only and
fails closed on a corrupt line, a refused pass never reaches the judge, and
``harness flush`` replays a campaign file end to end.

No live call is exercised: FLUSH-1 speaks to an injected judge, and the CLI
replays the verdicts a campaign file carries.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from harness.flush_loop import (
    CLOSED,
    DECISION_READY,
    AspectVerdict,
    EventLedger,
    EvidenceRecord,
    Finding,
    FlushError,
    FlushLoop,
    GapRegister,
    GapRegisterError,
    PassRefused,
    begin_pass,
    pass_identity,
    record_info_request,
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
class LoopStopRuleTests(unittest.TestCase):
    """The stop rule, which is the only thing that may say "saturated"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = EventLedger(Path(self._tmp.name) / "events.jsonl")
        self.records = [EvidenceRecord("ev-1", "the audit chain verifies")]

    def _loop(self, judge, campaign="c1"):
        return FlushLoop(ledger=self.ledger, judge=judge, campaign=campaign)

    def _clean_judge(self):
        seen = []

        def judge(*, aspect, angle, target, records):
            seen.append((aspect, angle))
            return AspectVerdict(aspect=aspect, basis="dossier:ev-1@v1")

        judge.seen = seen
        return judge

    def test_stop_requires_two_clean_expansion_passes(self):
        judge = self._clean_judge()
        loop = self._loop(judge)
        dossier = {"axes": {"completeness": 96, "grounding": 96,
                            "risk_honesty": 96},
                   "basis": "dossier:ev-1@v1"}

        loop.run_pass(target="t1", records=self.records,
                      angles=["flaw-hunt"], aspects=["scale"])
        loop.mark_cold_run(basis="cold reader B, dossier:ev-1@v1")
        ready, reasons = loop.stop_ready(dossier)
        self.assertFalse(ready, "one clean pass cannot be saturation")
        self.assertTrue(any("2 are required" in reason for reason in reasons),
                        reasons)
        with self.assertRaises(FlushError) as raised:
            loop.mark_stopped(dossier)
        self.assertIn("not saturated", str(raised.exception))

        loop.run_pass(target="t2", records=self.records,
                      angles=["flaw-hunt", "red-team"], aspects=["scale"])
        self.assertEqual(loop.clean_expansion_streak, 2)
        # The cold run belonged to pass 1; the final pass is pass 2.
        ready, reasons = loop.stop_ready(dossier)
        self.assertFalse(ready)
        self.assertTrue(any("cold run" in reason for reason in reasons),
                        reasons)

        loop.mark_cold_run(basis="cold reader C, dossier:ev-1@v1")
        ready, reasons = loop.stop_ready(dossier)
        self.assertTrue(ready, reasons)
        event = loop.mark_stopped(dossier)
        self.assertEqual(event["event"], "stop")
        self.assertEqual(event["streak"], 2)
        self.assertEqual(self.ledger.read()[-1]["event"], "stop")

    def test_a_material_finding_breaks_the_streak_and_a_tail_finding_does_not(self):
        verdicts = {"material": AspectVerdict(
            aspect="scale", basis="dossier:ev-1@v1",
            findings=({"id": "gap-1", "summary": "no read on the claim",
                       "materiality": "violates_invariant"},))}
        loop = self._loop(
            lambda *, aspect, angle, target, records: verdicts["material"])
        loop.run_pass(target="t1", records=self.records,
                      angles=["flaw-hunt"], aspects=["scale"])
        self.assertEqual(loop.clean_expansion_streak, 0)
        self.assertEqual([f.id for f in loop.register.open_findings()],
                         ["gap-1"])
        dossier = {"axes": {"completeness": 96, "grounding": 96},
                   "basis": "dossier:ev-1@v1"}
        loop.mark_cold_run(basis="cold reader")
        ready, reasons = loop.stop_ready(dossier)
        self.assertFalse(ready)
        self.assertTrue(any("still open" in reason for reason in reasons),
                        reasons)

    def test_an_expansion_pass_is_one_that_brings_a_new_angle(self):
        judge = self._clean_judge()
        loop = self._loop(judge)
        loop.run_pass(target="t1", records=self.records,
                      angles=["flaw-hunt"], aspects=["scale"])
        later = loop.run_pass(
            target="t1",
            records=self.records + [EvidenceRecord("ev-2", "more")],
            angles=["flaw-hunt"], aspects=["scale"])
        self.assertFalse(later.expansion,
                         "reusing one angle over new evidence is not an "
                         "expansion pass")
        self.assertEqual(loop.clean_expansion_streak, 0)

    def test_the_bar_names_the_axis_it_fails(self):
        loop = self._loop(self._clean_judge())
        ok, reasons = loop.bar_met({"axes": {"completeness": 96,
                                             "grounding": 70},
                                    "basis": "dossier:ev-1@v1"})
        self.assertFalse(ok)
        self.assertTrue(any("grounding" in reason for reason in reasons),
                        reasons)
        ok, reasons = loop.bar_met({"axes": {"completeness": 96,
                                             "grounding": 96}})
        self.assertFalse(ok, "a dossier with no basis cannot satisfy the bar")
        self.assertTrue(any("basis" in reason for reason in reasons), reasons)

    def test_a_verdict_without_a_basis_is_inadmissible(self):
        with self.assertRaises(FlushError) as raised:
            AspectVerdict(aspect="scale", scores={"completeness": 99})
        self.assertIn("information basis", str(raised.exception))


class LoopInfoRequestTests(unittest.TestCase):
    """A stall names the record it needs, and parks only that aspect."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = EventLedger(Path(self._tmp.name) / "events.jsonl")
        self.records = [EvidenceRecord("ev-1", "the gate pack")]
        self.calls = []

    def _judge(self, *, aspect, angle, target, records):
        self.calls.append(aspect)
        if aspect == "calibration":
            return AspectVerdict(
                aspect=aspect, basis="dossier:ev-1@v1",
                missing_record="shot:launch-gate-2026-11-04.png@v3")
        return AspectVerdict(aspect=aspect, basis="dossier:ev-1@v1")

    def test_info_request_names_missing_record(self):
        loop = FlushLoop(ledger=self.ledger, judge=self._judge, campaign="c1")
        outcome = loop.run_pass(
            target="the gate pack", records=self.records,
            angles=["missing-evidence"],
            aspects=["calibration", "scale"])
        self.assertEqual(self.calls, ["calibration", "scale"],
                         "the stalled aspect parks; the others still run")
        self.assertEqual(len(outcome.info_requests), 1)
        request = outcome.info_requests[0]
        self.assertEqual(request.aspect, "calibration")
        self.assertEqual(request.missing_record,
                         "shot:launch-gate-2026-11-04.png@v3")
        self.assertEqual(
            request.describe(),
            {"aspect": "calibration",
             "missing_record": "shot:launch-gate-2026-11-04.png@v3"})
        events = [event for event in self.ledger.read()
                  if event["event"] == "info_request"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["missing_record"],
                         "shot:launch-gate-2026-11-04.png@v3")
        self.assertFalse(outcome.clean,
                         "a pass that stalled on information has not covered "
                         "its aspects")

    def test_a_request_for_vague_information_is_refused(self):
        with self.assertRaises(FlushError) as raised:
            record_info_request(self.ledger, aspect="calibration",
                                missing_record="  ")
        self.assertIn("exact missing record", str(raised.exception))
        self.assertEqual(self.ledger.read(), [])

    def test_a_refused_pass_never_reaches_the_judge(self):
        loop = FlushLoop(ledger=self.ledger, judge=self._judge, campaign="c1")
        loop.run_pass(target="t", records=self.records,
                      angles=["flaw-hunt"], aspects=["scale"])
        with self.assertRaises(PassRefused):
            loop.run_pass(target="t", records=self.records,
                          angles=["flaw-hunt"], aspects=["scale"])
        self.assertEqual(self.calls, ["scale"],
                         "Rule 0 refusal must not spend a judge call")
        self.assertEqual([event["event"] for event in self.ledger.read()],
                         ["pass_admitted", "pass_refused"])

    def test_a_duplicate_aspect_is_a_repeated_call_not_new_work(self):
        loop = FlushLoop(ledger=self.ledger, judge=self._judge, campaign="c1")
        outcome = loop.run_pass(target="t", records=self.records,
                                angles=["flaw-hunt"],
                                aspects=["scale", "scale"])
        self.assertEqual(outcome.repeated_calls, ("scale/flaw-hunt",))
        self.assertFalse(outcome.clean,
                         "asking one instrument the same question twice is "
                         "not two pieces of evidence")

    def test_new_evidence_alone_is_not_an_expansion_pass(self):
        loop = FlushLoop(ledger=self.ledger, judge=self._judge, campaign="c1")
        loop.run_pass(target="t", records=self.records,
                      angles=["flaw-hunt"], aspects=["scale"])
        again = loop.run_pass(
            target="t",
            records=self.records + [EvidenceRecord("ev-2", "new")],
            angles=["flaw-hunt"], aspects=["scale"])
        self.assertFalse(again.expansion,
                         "an expansion pass brings a new angle or instrument")
        self.assertIn("evidence", again.delta)
        self.assertEqual(loop.clean_expansion_streak, 0)
class FlushCliTests(unittest.TestCase):
    """`harness flush` over a real campaign file: the executable path."""

    def _campaign(self, tmp, *, stop=False, verdicts=None):
        campaign = Path(tmp) / "campaign.json"
        spec = {
            "campaign": "c-cli",
            "target": "the gate pack",
            "passes": [
                {"angles": ["flaw-hunt"], "aspects": ["scale"],
                 "records": [{"id": "ev-1", "content": "the pack"}],
                 "verdicts": verdicts or {"scale": {
                     "basis": "dossier:ev-1@v1",
                     "scores": {"completeness": 96}}},
                 "cold_run": "reader B, dossier:ev-1@v1"},
                {"angles": ["flaw-hunt", "red-team"], "aspects": ["scale"],
                 "records": [{"id": "ev-1", "content": "the pack"}],
                 "verdicts": verdicts or {"scale": {
                     "basis": "dossier:ev-1@v1"}},
                 "cold_run": "reader C, dossier:ev-1@v1"},
            ],
            "dossier": {"axes": {"completeness": 96, "grounding": 96},
                        "basis": "dossier:ev-1@v1"},
            "stop": stop,
        }
        campaign.write_text(json.dumps(spec), encoding="utf-8")
        return campaign

    def test_flush_cli_replays_a_campaign_and_reports_the_rule(self):
        from harness.cli import _cmd_flush
        from harness.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign(tmp)
            opts = build_parser().parse_args(
                ["flush", "--campaign", str(campaign), "--json"])
            with redirect_stdout(io.StringIO()):
                result = _cmd_flush(opts, mock.Mock())
            self.assertTrue(result["stop_ready"], result["stop_reasons"])
            self.assertEqual(result["clean_expansion_streak"], 2)
            self.assertFalse(result["stopped"])
            ledger = Path(str(campaign) + ".events.jsonl")
            self.assertTrue(ledger.is_file())
            events = [json.loads(line) for line in
                      ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["event"] for e in events],
                             ["pass_admitted", "pass_admitted"])

    def test_flush_cli_claims_the_stop_only_when_the_rule_is_met(self):
        from harness.cli import _cmd_flush
        from harness.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign(tmp, stop=True)
            opts = build_parser().parse_args(
                ["flush", "--campaign", str(campaign), "--json"])
            with redirect_stdout(io.StringIO()):
                result = _cmd_flush(opts, mock.Mock())
            self.assertTrue(result["stopped"])
            events = [json.loads(line) for line in
                      Path(str(campaign) + ".events.jsonl")
                      .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event"], "stop")
            self.assertEqual(events[-1]["streak"], 2)

    def test_flush_cli_refuses_an_aspect_the_campaign_does_not_score(self):
        from harness.cli import _cmd_flush
        from harness.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign(tmp, verdicts={
                "calibration": {"basis": "dossier:ev-1@v1"}})
            opts = build_parser().parse_args(
                ["flush", "--campaign", str(campaign), "--json"])
            with self.assertRaises(FlushError) as raised:
                with redirect_stdout(io.StringIO()):
                    _cmd_flush(opts, mock.Mock())
            self.assertIn("does not invent scores", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
