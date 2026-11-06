"""D12 coverage for ``harness/flush_loop.py``: the refusal and edge lines.

The traced battery (audit D12) named the changed lines in flush_loop.py the
suite never executed. They are almost all refusals -- the contracts failing
by name -- plus a few state edges: a ledger read over blank lines, an
oscillation cycle that escalates exactly once, a repeated non-Jev call label,
and the campaign/canary file surfaces rejecting malformed input. Each test
here asserts the branch's actual outcome (the exception and its message, the
returned state, or the ledger's contents), not merely that the line ran.

No live call is exercised: judges are injected fakes, files live in temp
directories, and nothing touches the network or a screen.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from harness.flush_loop import (
    CLOSED,
    FLUSH_TWO_INSTRUMENTS,
    JEV_INSTRUMENT,
    PANEL_INSTRUMENT,
    AspectVerdict,
    CanaryCase,
    EventLedger,
    EvidenceRecord,
    Finding,
    FlushError,
    FlushLoop,
    GapRegister,
    GapRegisterError,
    OscillationBreaker,
    PassRefused,
    canonical_digest,
    pass_identity,
    run_campaign_file,
    run_canary_gate,
    run_canary_suite_file,
)


def _clean_judge(*, aspect, angle, target, records):
    return AspectVerdict(aspect=aspect, basis="dossier:ev-1@v1")


class _LoopCase(unittest.TestCase):
    """A loop over a temp ledger, with the standard one-record dossier."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = EventLedger(Path(self._tmp.name) / "events.jsonl")
        self.records = [EvidenceRecord("ev-1", "the pack")]

    def _pass(self, loop, **overrides):
        spec = {"target": "t", "records": self.records,
                "angles": ["flaw-hunt"], "aspects": ["scale"]}
        spec.update(overrides)
        return loop.run_pass(**spec)


class DigestAndIdentityTests(unittest.TestCase):
    def test_canonical_digest_accepts_bytes(self):
        self.assertEqual(canonical_digest(b"alpha\r\nbeta\r\n"),
                         canonical_digest("alpha\nbeta"))
        self.assertNotEqual(canonical_digest(b"alpha\nbeta"),
                            canonical_digest(b"alpha\ngamma"))
        # Undecodable bytes degrade to replacement characters, not a crash.
        self.assertEqual(canonical_digest(b"\xff"),
                         canonical_digest("\ufffd"))

    def test_an_evidence_record_needs_an_id(self):
        with self.assertRaises(PassRefused) as raised:
            EvidenceRecord("  ", "content")
        self.assertIn("an evidence record needs an id",
                      str(raised.exception))

    def test_an_evidence_record_rejects_an_unknown_kind(self):
        with self.assertRaises(PassRefused) as raised:
            EvidenceRecord("ev-1", "content", kind="rumor")
        self.assertIn("unknown record kind 'rumor'", str(raised.exception))

    def test_pass_identity_rejects_empty_entry_ids(self):
        with self.assertRaises(PassRefused) as raised:
            pass_identity(records=[("", "content")])
        self.assertIn("an evidence id cannot be empty",
                      str(raised.exception))
        with self.assertRaises(PassRefused) as raised:
            pass_identity(records=[], angles=[""])
        self.assertIn("an angle id cannot be empty", str(raised.exception))
        with self.assertRaises(PassRefused) as raised:
            pass_identity(records=[], instruments=[" "])
        self.assertIn("an instrument id cannot be empty",
                      str(raised.exception))


class FindingAndRegisterTests(unittest.TestCase):
    def test_a_finding_needs_an_id_and_a_summary(self):
        with self.assertRaises(GapRegisterError) as raised:
            Finding("", "a summary")
        self.assertIn("a finding needs an id", str(raised.exception))
        with self.assertRaises(GapRegisterError) as raised:
            Finding("gap-1", "   ")
        self.assertIn("finding 'gap-1' needs a summary",
                      str(raised.exception))

    def test_a_finding_cannot_be_recorded_twice(self):
        register = GapRegister()
        register.add(Finding("gap-1", "one"), materiality="axis_move")
        with self.assertRaises(GapRegisterError) as raised:
            register.add(Finding("gap-1", "again"),
                         materiality="axis_move")
        self.assertIn("finding 'gap-1' is already recorded",
                      str(raised.exception))
        register.add(Finding("tail-1", "small", bucket="style"))
        with self.assertRaises(GapRegisterError):
            register.add(Finding("tail-1", "again", bucket="style"))
        self.assertEqual(register.tail_count("style"), 1)

    def test_get_names_a_tail_finding_as_unpromoted(self):
        register = GapRegister()
        register.add(Finding("tail-1", "small", bucket="style"))
        with self.assertRaises(GapRegisterError) as raised:
            register.get("tail-1")
        self.assertIn("is tail material and not yet promoted",
                      str(raised.exception))

    def test_get_names_an_unknown_finding(self):
        register = GapRegister()
        with self.assertRaises(GapRegisterError) as raised:
            register.get("gap-404")
        self.assertIn("unknown finding 'gap-404'", str(raised.exception))

    def test_decide_ready_needs_a_packet_and_an_owner_act(self):
        register = GapRegister()
        register.add(Finding("gap-1", "needs a ruling"),
                     materiality="changes_ship_decision")
        with self.assertRaises(GapRegisterError) as raised:
            register.decide_ready("gap-1", packet=" ", owner_act="owner")
        self.assertIn("needs a packet to be decision-ready",
                      str(raised.exception))
        with self.assertRaises(GapRegisterError) as raised:
            register.decide_ready("gap-1", packet="docs/packet.md",
                                  owner_act="")
        self.assertIn("needs the owner act it waits on",
                      str(raised.exception))
        self.assertEqual(register.get("gap-1").state, "open")

    def test_reopen_refuses_a_finding_that_is_not_closed(self):
        register = GapRegister()
        register.add(Finding("gap-1", "still open"),
                     materiality="axis_move")
        with self.assertRaises(GapRegisterError) as raised:
            register.reopen("gap-1")
        self.assertIn("is not closed and cannot be reopened",
                      str(raised.exception))
        self.assertEqual(register.get("gap-1").state, "open")


class LedgerEdgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "events.jsonl"

    def test_an_event_needs_a_name(self):
        ledger = EventLedger(self.path)
        for bad in ({}, {"event": ""}, "not-a-dict", None):
            with self.assertRaises(GapRegisterError) as raised:
                ledger.append(bad)
            self.assertIn("an event needs a name", str(raised.exception))
        self.assertEqual(ledger.read(), [])

    def test_read_skips_blank_lines(self):
        self.path.write_text(
            '\n{"event": "pass_admitted"}\n\n\n{"event": "stop"}\n\n',
            encoding="utf-8")
        events = EventLedger(self.path).read()
        self.assertEqual([event["event"] for event in events],
                         ["pass_admitted", "stop"])


class VerdictAndBreakerTests(unittest.TestCase):
    def test_a_verdict_needs_its_aspect(self):
        with self.assertRaises(FlushError) as raised:
            AspectVerdict(aspect=" ", basis="dossier:ev-1@v1")
        self.assertIn("a verdict needs the aspect it judges",
                      str(raised.exception))

    def test_an_oscillation_cure_needs_an_id_and_a_cure(self):
        breaker = OscillationBreaker()
        with self.assertRaises(FlushError) as raised:
            breaker.record_cure(" ", "cure-1")
        self.assertIn("an oscillation cure needs a finding id",
                      str(raised.exception))
        with self.assertRaises(FlushError) as raised:
            breaker.record_cure("gap-1", " ")
        self.assertIn("cannot enter the oscillation graph",
                      str(raised.exception))
        self.assertEqual(breaker.cures, {})

    def test_a_regression_without_a_cure_is_refused(self):
        breaker = OscillationBreaker()
        with self.assertRaises(FlushError) as raised:
            breaker.record_regression("gap-9", aspect="scale")
        self.assertIn("regressed without a recorded cure",
                      str(raised.exception))
        self.assertEqual(breaker.packets, [])

    def test_a_two_member_cycle_escalates_once_then_stays_seen(self):
        breaker = OscillationBreaker()
        breaker.record_cure("gap-a", "cure-a")
        breaker.record_cure("gap-b", "cure-b")
        self.assertIsNone(breaker.record_regression("gap-a", aspect="s"))
        self.assertIsNone(breaker.record_regression("gap-b", aspect="s"))
        self.assertIsNone(breaker.record_regression("gap-a", aspect="s"))
        packet = breaker.record_regression("gap-b", aspect="s")
        self.assertIsNotNone(packet)
        self.assertEqual(packet["cycle"], ["gap-a", "gap-b"])
        self.assertEqual(packet["cures"],
                         {"gap-a": "cure-a", "gap-b": "cure-b"})
        self.assertEqual(len(breaker.packets), 1)

    def test_a_cycle_that_already_escalated_is_not_escalated_twice(self):
        breaker = OscillationBreaker()
        breaker.record_cure("gap-a", "cure-a")
        self.assertIsNone(breaker.record_regression("gap-a", aspect="s"))
        packet = breaker.record_regression("gap-a", aspect="s")
        self.assertIsNotNone(packet)
        self.assertEqual(packet["cycle"], ["gap-a"])
        # The same cycle closing a third time is recognised as seen and
        # returns no new packet: one escalation per cycle, by design.
        self.assertIsNone(breaker.record_regression("gap-a", aspect="s"))
        self.assertEqual(len(breaker.packets), 1)


class LoopAdmissionTests(_LoopCase):
    def test_a_loop_needs_a_judge(self):
        with self.assertRaises(FlushError) as raised:
            FlushLoop(ledger=self.ledger)
        self.assertIn("a flush loop needs at least one instrument judge",
                      str(raised.exception))

    def test_a_loop_needs_a_supported_instrument(self):
        with self.assertRaises(FlushError) as raised:
            FlushLoop(ledger=self.ledger, judge=_clean_judge,
                      supported_instruments=())
        self.assertIn("a flush loop needs at least one supported "
                      "instrument", str(raised.exception))

    def test_a_pass_needs_an_angle(self):
        calls = []

        def judge(**kwargs):
            calls.append(kwargs)
            return _clean_judge(**kwargs)

        loop = FlushLoop(ledger=self.ledger, judge=judge)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop, angles=[])
        self.assertIn("at least one angle", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.read(), [])

    def test_a_pass_needs_an_instrument(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop, instruments=[])
        self.assertIn("at least one instrument to judge",
                      str(raised.exception))
        self.assertEqual(self.ledger.read(), [])

    def test_an_unsupported_instrument_is_named_against_flush_two(self):
        judges = {name: _clean_judge for name in FLUSH_TWO_INSTRUMENTS}
        loop = FlushLoop(ledger=self.ledger, judges=judges,
                         supported_instruments=FLUSH_TWO_INSTRUMENTS)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop, instruments=["cold"])
        self.assertIn("unsupported instrument(s) ['cold']",
                      str(raised.exception))
        self.assertEqual(self.ledger.read(), [])

    def test_an_instrument_without_a_judge_is_refused_before_admission(self):
        loop = FlushLoop(ledger=self.ledger,
                         judges={JEV_INSTRUMENT: _clean_judge},
                         supported_instruments=FLUSH_TWO_INSTRUMENTS)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop, instruments=[PANEL_INSTRUMENT])
        self.assertIn("no judge registered for instrument(s) ['panel']",
                      str(raised.exception))
        self.assertEqual(self.ledger.read(), [])

    def test_a_pass_can_pin_the_judge_version(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        self.assertIsNone(loop.judge_version)
        self._pass(loop, judge_version="jev-9")
        self.assertEqual(loop.judge_version, "jev-9")


class LoopVerdictHandlingTests(_LoopCase):
    def test_a_judge_must_return_an_aspect_verdict(self):
        loop = FlushLoop(ledger=self.ledger,
                         judge=lambda **kwargs: "a plain string")
        with self.assertRaises(FlushError) as raised:
            self._pass(loop)
        self.assertIn("the judge must return AspectVerdict, not str",
                      str(raised.exception))

    def test_a_verdict_for_another_aspect_is_refused(self):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(aspect="other", basis="dossier:ev-1@v1")

        loop = FlushLoop(ledger=self.ledger, judge=judge)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop)
        self.assertIn("verdict is for 'other' but aspect 'scale' was "
                      "asked", str(raised.exception))

    def test_a_closed_finding_without_a_cure_cannot_form_an_edge(self):
        register = GapRegister()
        register.add(Finding("gap-1", "a gap"), materiality="axis_move")
        register.set_state("gap-1", CLOSED)  # closed by hand: no cure set
        self.assertEqual(register.get("gap-1").cure, "")

        def judge(*, aspect, angle, target, records):
            return AspectVerdict(
                aspect=aspect, basis="dossier:ev-1@v1",
                findings=({"id": "gap-1", "summary": "a gap",
                           "materiality": "axis_move"},))

        loop = FlushLoop(ledger=self.ledger, judge=judge,
                         register=register)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop)
        self.assertIn("is closed without a cure", str(raised.exception))

    def test_a_finding_reported_twice_in_one_pass_is_recorded_once(self):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(
                aspect=aspect, basis="dossier:ev-1@v1",
                findings=({"id": "gap-1", "summary": "a gap",
                           "materiality": "axis_move"},))

        loop = FlushLoop(ledger=self.ledger, judge=judge)
        outcome = self._pass(loop, aspects=["scale", "grounding"])
        self.assertEqual(outcome.material_findings, ("gap-1",))
        self.assertEqual([f.id for f in loop.register.open_findings()],
                         ["gap-1"])

    def test_a_tail_finding_reported_twice_stays_one_tail_entry(self):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(
                aspect=aspect, basis="dossier:ev-1@v1",
                findings=({"id": "tail-1", "summary": "small",
                           "bucket": "style"},))

        loop = FlushLoop(ledger=self.ledger, judge=judge)
        outcome = self._pass(loop, aspects=["scale", "grounding"])
        self.assertEqual(outcome.material_findings, ())
        self.assertEqual(loop.register.tail_count("style"), 1)
        self.assertEqual(loop.register.findings, {})

    def test_a_finding_object_keeps_its_own_materiality(self):
        planted = Finding("gap-7", "a gap", materiality="axis_move")

        def judge(*, aspect, angle, target, records):
            return AspectVerdict(aspect=aspect, basis="dossier:ev-1@v1",
                                 findings=(planted,))

        loop = FlushLoop(ledger=self.ledger, judge=judge)
        outcome = self._pass(loop)
        self.assertEqual(outcome.material_findings, ("gap-7",))
        self.assertEqual(loop.register.get("gap-7").materiality,
                         "axis_move")

    def test_a_finding_that_is_neither_object_nor_dict_is_refused(self):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(aspect=aspect, basis="dossier:ev-1@v1",
                                 findings=("just-a-string",))

        loop = FlushLoop(ledger=self.ledger, judge=judge)
        with self.assertRaises(FlushError) as raised:
            self._pass(loop)
        self.assertIn("a finding must be a Finding or a dict, not str",
                      str(raised.exception))

    def test_a_repeated_panel_call_is_labelled_with_its_instrument(self):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(aspect=aspect, basis="dossier:ev-1@v1")

        loop = FlushLoop(
            ledger=self.ledger, judges={PANEL_INSTRUMENT: judge},
            supported_instruments=FLUSH_TWO_INSTRUMENTS)
        outcome = self._pass(loop, aspects=["scale", "scale"],
                             instruments=[PANEL_INSTRUMENT])
        self.assertEqual(outcome.repeated_calls,
                         ("scale/flaw-hunt/panel",))
        self.assertFalse(outcome.clean)


class CalibrationAndStopTests(_LoopCase):
    def test_a_calibration_pin_needs_a_version(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        with self.assertRaises(FlushError) as raised:
            loop.pin_judge_version("  ")
        self.assertIn("a calibration pin needs a judge version",
                      str(raised.exception))
        self.assertIsNone(loop.judge_version)

    def test_repinning_the_same_version_is_a_no_op(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge,
                         judge_version="jev-1")
        self.assertIsNone(loop.pin_judge_version("jev-1"))
        self.assertEqual(loop.judge_version, "jev-1")
        self.assertEqual(loop.score_history, [])

    def test_a_first_pin_with_control_scores_sets_the_baseline(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        result = loop.pin_judge_version(
            "jev-1", control_scores={"completeness": 88})
        self.assertIsNone(result)
        self.assertEqual(loop.judge_version, "jev-1")
        self.assertEqual(loop.scores, {"completeness": 88.0})

    def test_a_cold_run_must_belong_to_a_pass(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        with self.assertRaises(FlushError) as raised:
            loop.mark_cold_run(basis="reader B")
        self.assertIn("a cold run has to belong to a pass",
                      str(raised.exception))

    def test_a_cold_run_must_name_its_basis(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        self._pass(loop)
        with self.assertRaises(FlushError) as raised:
            loop.mark_cold_run(basis=" ")
        self.assertIn("a cold run must name who read the target",
                      str(raised.exception))
        self.assertIsNone(loop.cold_run_pass)

    def test_a_dossier_without_axes_cannot_meet_the_bar(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        ok, reasons = loop.bar_met({"axes": {}, "basis": "dossier:x@v1"})
        self.assertFalse(ok)
        self.assertEqual(reasons, ("the dossier carries no axis scores",))
        ok, reasons = loop.bar_met({})
        self.assertFalse(ok)
        self.assertEqual(reasons, ("the dossier carries no axis scores",))

    def test_an_axis_between_the_floors_names_the_absolute_floor(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge,
                         bar=50.0, axis_floor=50.0, absolute_floor=60.0)
        ok, reasons = loop.bar_met({"axes": {"a": 55},
                                    "basis": "dossier:x@v1"})
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 1)
        self.assertIn("axis a scores 55.00, below the absolute floor 60",
                      reasons[0])

    def test_stop_ready_before_any_pass_says_so(self):
        loop = FlushLoop(ledger=self.ledger, judge=_clean_judge)
        ready, reasons = loop.stop_ready()
        self.assertFalse(ready)
        self.assertEqual(reasons, ("no pass has run",))


class CanaryGateEdgeTests(unittest.TestCase):
    def _judge(self, detected):
        def judge(*, aspect, angle, target, records):
            return AspectVerdict(
                aspect=aspect, basis=f"canary:{target}@v1",
                findings=tuple(
                    {"id": defect, "summary": defect,
                     "materiality": "blocks_user_visible"}
                    for defect in detected.get(str(target), ())))
        return judge

    def test_a_canary_needs_an_id_a_target_and_a_planted_defect(self):
        with self.assertRaises(FlushError) as raised:
            CanaryCase(id=" ", target="t", planted=("d-1",))
        self.assertIn("a canary needs an id", str(raised.exception))
        with self.assertRaises(FlushError) as raised:
            CanaryCase(id="c-1", target=" ", planted=("d-1",))
        self.assertIn("canary 'c-1' has no target content",
                      str(raised.exception))
        with self.assertRaises(FlushError) as raised:
            CanaryCase(id="c-1", target="t", planted=())
        self.assertIn("must plant at least one defect id",
                      str(raised.exception))
        with self.assertRaises(FlushError):
            CanaryCase(id="c-1", target="t", planted=("d-1", " "))

    def test_canary_ids_must_be_unique(self):
        cases = (CanaryCase(id="c-1", target="one", planted=("d-1",)),
                 CanaryCase(id="c-1", target="two", planted=("d-2",)))
        with self.assertRaises(FlushError) as raised:
            run_canary_gate(cases, self._judge({}))
        self.assertIn("canary ids must be unique", str(raised.exception))

    def test_an_empty_canary_suite_reports_no_cases(self):
        report = run_canary_gate((), self._judge({}))
        self.assertFalse(report["passed"])
        self.assertEqual(report["reason"], "no canary cases")
        self.assertEqual(report["score"], 0.0)
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["held_out"], [])

    def test_an_unknown_held_out_id_is_refused(self):
        cases = (CanaryCase(id="c-1", target="one", planted=("d-1",)),)
        with self.assertRaises(FlushError) as raised:
            run_canary_gate(cases, self._judge({}), held_out=["c-404"])
        self.assertIn("held-out canary id(s) not in the suite",
                      str(raised.exception))

    def test_a_supplied_ledger_is_used_and_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gate.events.jsonl"
            ledger = EventLedger(path)
            cases = (CanaryCase(id="c-1", target="one",
                                planted=("d-1",)),)
            report = run_canary_gate(
                cases, self._judge({"c-1": ("d-1",)}), ledger=ledger)
            self.assertTrue(report["passed"], report)
            self.assertEqual(report["ledger"], str(path))
            events = ledger.read()
            self.assertEqual(events[0]["event"], "pass_admitted")


class CanarySuiteFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_missing_suite_file_is_refused(self):
        with self.assertRaises(FlushError) as raised:
            run_canary_suite_file(str(self.root / "absent.json"))
        self.assertIn("canary suite not found", str(raised.exception))

    def test_a_suite_file_must_be_valid_json(self):
        path = self._write("bad.json", "{not json")
        with self.assertRaises(FlushError) as raised:
            run_canary_suite_file(str(path))
        self.assertIn("is not valid JSON", str(raised.exception))

    def test_a_suite_file_must_hold_an_object(self):
        path = self._write("list.json", "[1, 2, 3]")
        with self.assertRaises(FlushError) as raised:
            run_canary_suite_file(str(path))
        self.assertIn("must contain a JSON object", str(raised.exception))

    def test_every_canary_in_the_file_must_be_an_object(self):
        path = self._write(
            "mixed.json", json.dumps({"canaries": ["oops"]}))
        with self.assertRaises(FlushError) as raised:
            run_canary_suite_file(str(path))
        self.assertIn("every canary must be an object",
                      str(raised.exception))


class CampaignFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, spec, name="campaign.json"):
        path = self.root / name
        text = spec if isinstance(spec, str) else json.dumps(spec)
        path.write_text(text, encoding="utf-8")
        return path

    def _pass_spec(self, **overrides):
        spec = {"angles": ["flaw-hunt"], "aspects": ["scale"],
                "records": [{"id": "ev-1", "content": "the pack"}],
                "verdicts": {"scale": {"basis": "dossier:ev-1@v1",
                                       "scores": {"completeness": 96}}}}
        spec.update(overrides)
        return spec

    def test_a_missing_campaign_file_is_refused(self):
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(self.root / "absent.json"))
        self.assertIn("campaign file not found", str(raised.exception))

    def test_a_campaign_file_must_be_valid_json(self):
        path = self._write("{not json", "bad.json")
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("is not valid JSON", str(raised.exception))

    def test_a_campaign_file_must_hold_an_object(self):
        path = self._write("[1, 2, 3]", "list.json")
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("must contain a JSON object", str(raised.exception))

    def test_a_campaign_without_a_target_replays_with_empty_target(self):
        path = self._write({"campaign": "no-target",
                            "passes": [self._pass_spec()]})
        result = run_campaign_file(str(path))
        self.assertEqual(len(result["passes"]), 1)
        self.assertEqual(result["passes"][0]["pass"], 1)

    def test_every_record_needs_an_id(self):
        for records in ([{"content": "no id"}], [42]):
            path = self._write(
                {"passes": [self._pass_spec(records=records)]})
            with self.assertRaises(FlushError) as raised:
                run_campaign_file(str(path))
            self.assertIn("every record needs an id",
                          str(raised.exception))

    def test_a_record_with_an_unreadable_path_is_refused(self):
        path = self._write({"passes": [self._pass_spec(
            records=[{"id": "ev-9", "path": ""}])]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("record 'ev-9' names no readable path",
                      str(raised.exception))
        path = self._write({"passes": [self._pass_spec(
            records=[{"id": "ev-9",
                      "path": "d12cov-missing-9f3b.txt"}])]},
            "missing.json")
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("names no readable path", str(raised.exception))

    def test_flush_one_campaign_refuses_a_panel_pass(self):
        path = self._write({"passes": [self._pass_spec(
            instruments=["panel"],
            instrument_verdicts={"panel": {"scale": {
                "basis": "dossier:ev-1@v1"}}})]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("FLUSH-1 judges with exactly one instrument",
                      str(raised.exception))

    def test_an_extended_campaign_names_an_unknown_instrument(self):
        path = self._write({"passes": [self._pass_spec(
            instruments=["bogus"])]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path), extended_instruments=True)
        self.assertIn("unsupported instrument(s) ['bogus']",
                      str(raised.exception))

    def test_a_close_entry_must_be_a_list(self):
        path = self._write({"passes": [self._pass_spec(
            close={"finding": "gap-1"})]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("a pass 'close' entry must be a list",
                      str(raised.exception))

    def test_every_close_needs_finding_cure_and_read(self):
        path = self._write({"passes": [self._pass_spec(
            close=[{"finding": "gap-1", "cure": "cure-1"}])]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("every close needs finding, cure and read",
                      str(raised.exception))

    def test_every_pass_must_be_an_object(self):
        path = self._write({"passes": ["oops"]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("every pass must be an object",
                      str(raised.exception))

    def test_a_stalled_pass_is_reported_on_stderr(self):
        path = self._write({"passes": [self._pass_spec(verdicts={
            "scale": {"basis": "dossier:ev-1@v1",
                      "missing_record": "shot:gate-9.png"}})]})
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = run_campaign_file(str(path))
        self.assertIn("[flush] pass 1 stalled on", stderr.getvalue())
        self.assertIn("scale needs shot:gate-9.png", stderr.getvalue())
        self.assertEqual(len(result["passes"][0]["info_requests"]), 1)

    def test_a_cold_run_needs_at_least_one_admitted_pass(self):
        path = self._write({"campaign": "cold", "passes": []})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path), cold_run=True)
        self.assertIn("a cold run needs at least one admitted pass",
                      str(raised.exception))

    def test_a_cold_run_must_complete_in_the_final_pass(self):
        path = self._write({"campaign": "cold",
                            "passes": [self._pass_spec()]})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path), cold_run=True)
        self.assertIn("the cold run did not complete in the final pass",
                      str(raised.exception))

    def test_a_stop_claim_needs_a_dossier(self):
        path = self._write({"campaign": "stop",
                            "passes": [self._pass_spec()], "stop": True})
        with self.assertRaises(FlushError) as raised:
            run_campaign_file(str(path))
        self.assertIn("a campaign that claims a stop needs a dossier",
                      str(raised.exception))


if __name__ == "__main__":
    unittest.main()
