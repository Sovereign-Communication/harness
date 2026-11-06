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
    COLD_INSTRUMENT,
    DECISION_READY,
    DETERMINISTIC_INSTRUMENT,
    FLUSH_TWO_INSTRUMENTS,
    JEV_INSTRUMENT,
    OPEN,
    PANEL_INSTRUMENT,
    AspectVerdict,
    CanaryCase,
    run_campaign_file,
    run_canary_gate,
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

    def test_a_pass_without_an_aspect_is_refused(self):
        loop = FlushLoop(ledger=self.ledger, judge=self._judge, campaign="c1")
        with self.assertRaises(FlushError) as raised:
            loop.run_pass(target="t", records=self.records,
                          angles=["flaw-hunt"], aspects=[])
        self.assertIn("at least one aspect", str(raised.exception))
        self.assertEqual(self.calls, [],
                         "an empty aspect list must not reach the judge")
        self.assertEqual(self.ledger.read(), [])

    def test_flush_one_refuses_a_non_jev_instrument(self):
        loop = FlushLoop(ledger=self.ledger, judge=self._judge, campaign="c1")
        with self.assertRaises(FlushError) as raised:
            loop.run_pass(target="t", records=self.records,
                          angles=["flaw-hunt"], aspects=["scale"],
                          instruments=["panel"])
        self.assertIn("FLUSH-1", str(raised.exception))
        self.assertEqual(self.calls, [],
                         "FLUSH-1 cannot spend a Jev call as a panel call")
        self.assertEqual(self.ledger.read(), [])

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

    def test_flush_cli_resolves_repo_relative_records(self):
        """A campaign in a scratch folder may name repo files."""
        from harness.cli import _cmd_flush
        from harness.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            campaign = Path(tmp) / "repo-relative.json"
            campaign.write_text(json.dumps({
                "campaign": "c-repo",
                "target": "harness/flush_loop.py",
                "passes": [
                    {"angles": ["flaw-hunt"], "aspects": ["contracts"],
                     "records": [{"id": "ev-1",
                                  "path": "harness/flush_loop.py"}],
                     "verdicts": {"contracts": {"basis": "dossier:ev-1@v1"}},
                     "cold_run": "reader B"},
                    {"angles": ["flaw-hunt", "red-team"],
                     "aspects": ["contracts"],
                     "records": [{"id": "ev-1",
                                  "path": "harness/flush_loop.py"}],
                     "verdicts": {"contracts": {"basis": "dossier:ev-1@v1"}},
                     "cold_run": "reader C"}],
                "dossier": {"axes": {"completeness": 96, "grounding": 96},
                            "basis": "dossier:ev-1@v1"}}), encoding="utf-8")
            opts = build_parser().parse_args(
                ["flush", "--campaign", str(campaign), "--json"])
            with redirect_stdout(io.StringIO()):
                result = _cmd_flush(opts, mock.Mock())
            self.assertTrue(result["stop_ready"], result["stop_reasons"])

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


class InstrumentRotationTests(unittest.TestCase):
    """FLUSH-2 instruments are distinct calls, not aliases of Jev."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = EventLedger(Path(self._tmp.name) / "events.jsonl")
        self.records = [EvidenceRecord("ev-1", "the pack")]

    def _judge(self, name, calls):
        def judge(*, aspect, angle, target, records):
            calls.append(name)
            return AspectVerdict(
                aspect=aspect, basis="dossier:ev-1@v1",
                scores={name: 90})
        return judge

    def test_panel_and_deterministic_rotate_with_the_instrument_identity(self):
        calls = []
        loop = FlushLoop(
            ledger=self.ledger,
            judges={JEV_INSTRUMENT: self._judge("jev", calls),
                    PANEL_INSTRUMENT: self._judge("panel", calls),
                    DETERMINISTIC_INSTRUMENT: self._judge(
                        "deterministic", calls)},
            supported_instruments=FLUSH_TWO_INSTRUMENTS,
            campaign="rotation")
        outcome = loop.run_pass(
            target="t", records=self.records, angles=["flaw-hunt"],
            aspects=["scale"],
            instruments=[JEV_INSTRUMENT, PANEL_INSTRUMENT,
                         DETERMINISTIC_INSTRUMENT])
        self.assertEqual(calls, ["jev", "panel", "deterministic"])
        self.assertTrue(outcome.expansion)
        self.assertEqual(outcome.describe()["verdicts"], 3)
        self.assertEqual(set(loop.scores),
                         {"jev", "panel", "deterministic"})
        event = self.ledger.read()[0]
        self.assertEqual(event["event"], "pass_admitted")
        self.assertEqual(set(event["instruments"]),
                         {JEV_INSTRUMENT, PANEL_INSTRUMENT,
                          DETERMINISTIC_INSTRUMENT})

    def test_default_loop_refuses_panel_before_the_judge(self):
        calls = []
        loop = FlushLoop(ledger=self.ledger,
                         judge=self._judge("jev", calls))
        with self.assertRaises(FlushError) as raised:
            loop.run_pass(
                target="t", records=self.records,
                angles=["flaw-hunt"], aspects=["scale"],
                instruments=[PANEL_INSTRUMENT])
        self.assertIn("FLUSH-1", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.read(), [])


class OscillationTests(unittest.TestCase):
    """A repeated cure/regression edge becomes one combined packet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = EventLedger(Path(self._tmp.name) / "events.jsonl")

    def _loop(self):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(
                aspect=aspect, basis="dossier:ev-1@v1",
                findings=(({"id": "gap-1", "summary": "the same gap",
                             "materiality": "violates_invariant"}),))
        return FlushLoop(ledger=self.ledger, judge=judge, campaign="osc")

    def _pass(self, loop, number):
        return loop.run_pass(
            target="t",
            records=[EvidenceRecord(f"ev-{number}", f"information {number}")],
            angles=["flaw-hunt"], aspects=["scale"])

    def test_oscillation_cycle_escalates_to_combined_packet(self):
        loop = self._loop()
        first = self._pass(loop, 1)
        self.assertIsNone(first.escalation)
        loop.close_finding("gap-1", cure="cure-1", read="read-1",
                           aspect="scale")
        second = self._pass(loop, 2)
        self.assertIsNone(second.escalation)
        self.assertEqual(second.regressions, ("gap-1",))
        loop.close_finding("gap-1", cure="cure-2", read="read-2",
                           aspect="scale")
        third = self._pass(loop, 3)
        self.assertIsNotNone(third.escalation)
        self.assertEqual(third.escalation["kind"], "combined_cure_packet")
        self.assertEqual(third.escalation["cycle"], ["gap-1"])
        self.assertEqual(third.escalation["cures"], {"gap-1": "cure-2"})
        self.assertEqual(third.regressions, ("gap-1",))
        self.assertFalse(third.clean)
        events = [event for event in self.ledger.read()
                  if event["event"] == "oscillation_escalated"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["packet"]["cycle"], ["gap-1"])
        ready, reasons = loop.stop_ready(
            {"axes": {"completeness": 96},
             "basis": "dossier:ev-1@v1"})
        self.assertFalse(ready)
        self.assertTrue(any("oscillation" in reason for reason in reasons),
                        reasons)


class CalibrationTests(unittest.TestCase):
    """A judge-version change resets scores, never findings."""

    def test_version_change_rebaselines_scores_not_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = EventLedger(Path(tmp) / "events.jsonl")

            def judge(*, aspect, angle, target, records):
                return AspectVerdict(
                    aspect=aspect, basis="dossier:ev-1@v1",
                    scores={"completeness": 96},
                    findings=(({"id": "gap-1", "summary": "a gap",
                                 "materiality": "violates_invariant"}),))

            loop = FlushLoop(ledger=ledger, judge=judge,
                             judge_version="jev-1", campaign="cal")
            loop.run_pass(
                target="t",
                records=[EvidenceRecord("ev-1", "information")],
                angles=["flaw-hunt"], aspects=["scale"])
            self.assertEqual(loop.scores, {"completeness": 96.0})
            event = loop.pin_judge_version(
                "jev-2", control_scores={"completeness": 70})
            self.assertEqual(loop.judge_version, "jev-2")
            self.assertEqual(loop.scores, {"completeness": 70.0})
            self.assertEqual(event["event"], "calibration_rebaselined")
            self.assertEqual(event["from"], "jev-1")
            self.assertEqual(event["to"], "jev-2")
            self.assertEqual(event["scores_before"], {"completeness": 96.0})
            finding = loop.register.get("gap-1")
            self.assertEqual(finding.state, OPEN)
            self.assertIn("gap-1",
                          [item.id for item in loop.register.open_findings()])


class FlushTwoCliTests(unittest.TestCase):
    """The executable surface for FLUSH-2 rotation and escalation."""

    def _run(self, spec, *args):
        from harness.cli import _cmd_flush
        from harness.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            campaign = Path(tmp) / "campaign.json"
            campaign.write_text(json.dumps(spec), encoding="utf-8")
            opts = build_parser().parse_args(
                ["flush", "--campaign", str(campaign), "--json", *args])
            with redirect_stdout(io.StringIO()):
                result = _cmd_flush(opts, mock.Mock())
            ledger = Path(str(campaign) + ".events.jsonl")
            events = [json.loads(line) for line in
                      ledger.read_text(encoding="utf-8").splitlines()]
            return result, events

    def _rotation_spec(self):
        return {
            "campaign": "rotation",
            "target": "t",
            "passes": [{
                "angles": ["flaw-hunt"], "aspects": ["scale"],
                "instruments": ["jev", "panel", "deterministic"],
                "records": [{"id": "ev-1", "content": "the pack"}],
                "verdicts": {"scale": {
                    "basis": "dossier:ev-1@v1",
                    "scores": {"completeness": 96}}},
                "instrument_verdicts": {
                    "panel": {"scale": {
                        "basis": "dossier:ev-1@v1",
                        "scores": {"grounding": 90}}},
                    "deterministic": {"scale": {
                        "basis": "dossier:ev-1@v1",
                        "scores": {"risk_honesty": 95}}}},
            }],
        }

    def test_flush_cli_rotates_panel_and_deterministic_instruments(self):
        result, events = self._run(
            self._rotation_spec(), "--extended-instruments")
        self.assertEqual(result["passes"][0]["verdicts"], 3)
        self.assertEqual(set(result["scores"]),
                         {"completeness", "grounding", "risk_honesty"})
        self.assertEqual(set(events[0]["instruments"]),
                         {"jev", "panel", "deterministic"})

    def test_flush_cli_escalates_an_oscillation_cycle(self):
        passes = []
        for number in (1, 2, 3):
            spec = {
                "angles": ["flaw-hunt"], "aspects": ["scale"],
                "records": [{"id": f"ev-{number}",
                             "content": f"information {number}"}],
                "verdicts": {"scale": {
                    "basis": "dossier:ev-1@v1",
                    "findings": [{
                        "id": "gap-1", "summary": "the same gap",
                        "materiality": "violates_invariant"}]}},
            }
            if number < 3:
                spec["close"] = [{
                    "finding": "gap-1", "cure": f"cure-{number}",
                    "read": f"read-{number}", "aspect": "scale"}]
            passes.append(spec)
        result, events = self._run({
            "campaign": "osc", "target": "t", "passes": passes})
        self.assertEqual(len(result["escalations"]), 1)
        self.assertEqual(result["passes"][-1]["escalation"]["kind"],
                         "combined_cure_packet")
        self.assertEqual(result["passes"][-1]["escalation"]["cycle"],
                         ["gap-1"])
        escalations = [event for event in events
                       if event["event"] == "oscillation_escalated"]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["packet"]["cycle"], ["gap-1"])


class ColdRunTests(unittest.TestCase):
    """FLUSH-3's cold run starts from a clean ledger and a fresh pass."""

    def test_cold_instrument_completes_from_a_clean_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign = Path(tmp) / "cold.json"
            campaign.write_text(json.dumps({
                "campaign": "cold",
                "target": "the cold target",
                "passes": [{
                    "angles": ["flaw-hunt"], "aspects": ["scale"],
                    "instruments": [COLD_INSTRUMENT],
                    "records": [{"id": "ev-1", "content": "the pack"}],
                    "instrument_verdicts": {COLD_INSTRUMENT: {
                        "scale": {"basis": "cold:reader-B@v1",
                                  "scores": {"completeness": 96}}}},
                }],
            }), encoding="utf-8")
            result = run_campaign_file(
                campaign, cold_run=True, extended_instruments=True)
            self.assertTrue(result["cold_run"]["clean_state"])
            self.assertEqual(result["cold_run"]["pass"], 1)
            self.assertEqual(result["cold_run"]["basis"],
                             "cold:reader-B@v1")
            self.assertTrue(Path(str(campaign) + ".cold.events.jsonl").is_file())
            with self.assertRaises(FlushError):
                run_campaign_file(
                    campaign, cold_run=True, extended_instruments=True)


class ShippedCanarySuiteTests(unittest.TestCase):
    """The suite the audit gate runs, and the gate's refusal when it is tampered."""

    SUITE = (Path(__file__).resolve().parent.parent
             / "packs" / "flush_canary_suite.json")

    def test_the_shipped_canary_suite_passes_as_a_gate(self):
        from harness.flush_loop import run_canary_suite_file
        self.assertTrue(self.SUITE.is_file(), self.SUITE)
        result = run_canary_suite_file(str(self.SUITE))
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["score"], 1.0)

    def test_a_suite_that_drops_a_planted_defect_fails_closed(self):
        from harness.flush_loop import run_canary_suite_file
        suite = json.loads(self.SUITE.read_text(encoding="utf-8"))
        # Tamper a HELD-OUT case: the score is measured on that set, so a
        # dropped planted id must move it below 1.0 as well as fail the gate.
        held_out = set(suite.get("held_out") or [])
        target = next(case for case in suite["canaries"]
                      if case["id"] in held_out)
        target["findings"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tampered.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            result = run_canary_suite_file(str(path))
        self.assertFalse(result["passed"])
        self.assertLess(result["score"], 1.0)


class CanaryGateTests(unittest.TestCase):
    """The planted-defect gate is a gate, not a report."""

    def _cases(self):
        return (
            CanaryCase(id="canary-1", target="target one",
                       planted=("defect-1",)),
            CanaryCase(id="canary-2", target="target two",
                       planted=("defect-2", "defect-3")),
        )

    def test_canary_loop_finds_all_planted_defects(self):
        detected = {
            "canary-1": ("defect-1",),
            "canary-2": ("defect-2", "defect-3"),
        }

        def judge(*, aspect, angle, target, records):
            return AspectVerdict(
                aspect=aspect, basis=f"canary:{target}@v1",
                findings=tuple({"id": defect, "summary": defect,
                                "materiality": "blocks_user_visible"}
                               for defect in detected[str(target)]))

        report = run_canary_gate(
            self._cases(), judge,
            held_out=["canary-1", "canary-2"], rotation=1)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(len(report["cases"]), 2)
        self.assertEqual(report["rotation"], 1)

    def test_canary_gate_fails_closed_when_a_planted_defect_is_missed(self):
        def judge(*, aspect, angle, target, records):
            if str(target) == "canary-2":
                return AspectVerdict(
                    aspect=aspect, basis=f"canary:{target}@v1",
                    findings=(({"id": "defect-2", "summary": "seen",
                                 "materiality": "blocks_user_visible"}),))
            return AspectVerdict(
                aspect=aspect, basis=f"canary:{target}@v1",
                findings=(({"id": "defect-1", "summary": "seen",
                             "materiality": "blocks_user_visible"}),))

        report = run_canary_gate(self._cases(), judge,
                                 held_out=["canary-2"])
        self.assertFalse(report["passed"])
        missed = [case for case in report["cases"]
                  if case["id"] == "canary-2"][0]
        self.assertEqual(missed["missed"], ["defect-3"])

    def test_canary_gate_fails_closed_when_the_judge_errors(self):
        def judge(*, aspect, angle, target, records):
            raise RuntimeError("detector crashed")

        report = run_canary_gate(self._cases(), judge)
        self.assertFalse(report["passed"])
        self.assertTrue(all(case["error"] for case in report["cases"]))

    def test_flush_canary_cli_passes_and_fails_closed(self):
        from harness.cli import _cmd_flush_canary
        from harness.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "suite.json"
            suite.write_text(json.dumps({
                "campaign": "canary-cli",
                "canaries": [{
                    "id": "canary-1", "target": "target one",
                    "planted": ["defect-1"],
                    "basis": "canary:canary-1@v1",
                    "findings": [{"id": "defect-1", "summary": "seen",
                                  "materiality": "blocks_user_visible"}],
                }],
                "held_out": ["canary-1"],
            }), encoding="utf-8")
            opts = build_parser().parse_args(
                ["flush-canary", "--suite", str(suite), "--json"])
            with redirect_stdout(io.StringIO()):
                result = _cmd_flush_canary(opts, mock.Mock())
            self.assertTrue(result["passed"])
            self.assertEqual(result["score"], 1.0)

            suite.write_text(json.dumps({
                "campaign": "canary-cli",
                "canaries": [{
                    "id": "canary-1", "target": "target one",
                    "planted": ["defect-1"],
                    "basis": "canary:canary-1@v1",
                    "findings": [],
                }],
                "held_out": ["canary-1"],
            }), encoding="utf-8")
            opts = build_parser().parse_args(
                ["flush-canary", "--suite", str(suite), "--json"])
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    _cmd_flush_canary(opts, mock.Mock())


class FlushSurfaceParityTests(unittest.TestCase):
    """CLI and MCP replay one campaign through one owner."""

    def test_mcp_and_cli_read_same_campaign_state(self):
        from harness.cli import _cmd_flush
        from harness.cli_parser import build_parser
        from tests.test_mcp import _serve, make_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            campaign = root / "campaign.json"
            campaign.write_text(json.dumps({
                "campaign": "parity",
                "target": "the parity target",
                "passes": [
                    {"angles": ["flaw-hunt"], "aspects": ["scale"],
                     "records": [{"id": "ev-1", "content": "one"}],
                     "verdicts": {"scale": {
                         "basis": "dossier:ev-1@v1",
                         "scores": {"completeness": 96}}}},
                    {"angles": ["flaw-hunt", "red-team"],
                     "aspects": ["scale"],
                     "records": [{"id": "ev-1", "content": "one"}],
                     "verdicts": {"scale": {
                         "basis": "dossier:ev-1@v1",
                         "scores": {"grounding": 96}}}},
                ],
                "dossier": {"axes": {"completeness": 96,
                                     "grounding": 96},
                            "basis": "dossier:ev-1@v1"},
            }), encoding="utf-8")
            cli_ledger = root / "cli.events.jsonl"
            opts = build_parser().parse_args([
                "flush", "--campaign", str(campaign),
                "--ledger", str(cli_ledger), "--json"])
            with redirect_stdout(io.StringIO()):
                cli_result = _cmd_flush(opts, mock.Mock())

            _, server = make_server(allowed_roots=[str(root)])
            frame = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "flush_run", "arguments": {
                    "campaign": str(campaign),
                    "ledger": str(root / "mcp.events.jsonl"),
                    "root": str(root), "allow_write": True}}}) + "\n"
            frames = _serve(server, frame)
            self.assertEqual(len(frames), 1, frames)
            self.assertNotIn("error", frames[0], frames[0])
            mcp_result = frames[0]["result"]["structuredContent"]
            for key in ("campaign", "passes", "stop_ready", "stop_reasons",
                        "clean_expansion_streak", "scores", "judge_version"):
                self.assertEqual(mcp_result[key], cli_result[key], key)


if __name__ == "__main__":
    unittest.main()
