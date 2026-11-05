"""The flush loop: its contracts, and the operator that must honour them.

FLUSH-0 (the contracts) and FLUSH-1 (the loop + CLI surface) of the saturation
(flush) loop commissioned 2026-11-04. The contracts are the rules a pass
cannot talk its way around -- Rule 0 ("never without changes"), the gap
register's two exit states, materiality with its tail promotion, and the
append-only event ledger. The loop rotates angles, admits or refuses passes,
records what the judge said, and reports the stop rule with its reasons. There
is no panel, no deterministic rotation, no oscillation breaker and no cold-run
instrument here: those are FLUSH-2 and FLUSH-3, and neither the loop nor the
CLI makes a network call -- the judge is injected, and ``harness flush``
replays the verdicts a campaign file carries.

The reason the rules live in their own layer is the failure they prevent. A
saturation claim is only worth what its weakest pass is worth, and two passes
over the same information are one pass counted twice: a loop that accepts an
unchanged target can report "two clean expansion passes" without having
learned anything. So an unchanged information set is refused by name rather
than recorded as a quiet pass, and the refusal is itself an event.

Nothing in this module reads the network, spawns a process, or scores
anything. ``harness flush`` (FLUSH-1) is the caller.
"""
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import HarnessError

#: The two ways a finding may leave the register. There is deliberately no
#: third: "noted" is not a state, because a finding that is only noted is a
#: finding nobody owns.
CLOSED = "closed"
DECISION_READY = "decision_ready"
EXIT_STATES = (CLOSED, DECISION_READY)

#: The finding states that are not an exit. An open finding is still the
#: loop's problem; it has not been dispositioned.
OPEN = "open"

#: Why a finding is material. A finding that moves no axis, blocks nothing a
#: user can see, violates no invariant and changes no ship decision is tail
#: material: recorded, counted, and promoted when its bucket gets loud.
MATERIAL_REASONS = (
    "axis_move",              # moves a score axis by at least one level
    "blocks_user_visible",    # blocks a behavior a user can observe
    "violates_invariant",     # breaks a stated invariant
    "changes_ship_decision",  # would change whether this ships
)
TAIL_PROMOTION_THRESHOLD = 5

#: The four things that make a pass a pass. A delta must be one of them; byte
#: churn is not.
DELTA_KINDS = ("evidence", "target", "angle", "instrument")


class PassRefused(HarnessError):
    """A pass was refused because it carried no semantic delta."""

    kind = "flush_pass_refused"


class GapRegisterError(HarnessError):
    """The register was asked to do something it has no state for."""

    kind = "flush_gap_register"


def canonical_digest(text):
    """A digest that ignores formatting churn and nothing else.

    Line endings, trailing whitespace and blank lines are formatting: they are
    normalized away so a re-saved or reflowed file is the same information.
    Every other byte is content and changes the digest. Normalizing more than
    this -- collapsing interior whitespace in prose, lowercasing, stripping
    markdown -- would let a real edit hide inside "formatting", which is the
    delta-gaming failure the commission's red-team pass named. Leading
    indentation is deliberately kept: in code and in quoted blocks it is
    structure, not churn.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    kept = [line.rstrip() for line in normalized.split("\n")]
    kept = [line for line in kept if line != ""]
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    """One record in the information set: an id and the digest of its content.

    The id is what a report cites; the digest is what makes a changed record a
    change. Two records with one id and different digests are different
    information.
    """

    id: str
    content: str
    kind: str = "evidence"

    def __post_init__(self):
        if not str(self.id).strip():
            raise PassRefused("an evidence record needs an id")
        if self.kind not in ("evidence", "angle", "instrument", "target"):
            raise PassRefused(f"unknown record kind {self.kind!r}")

    @property
    def digest(self):
        return canonical_digest(self.content)


@dataclass(frozen=True)
class PassIdentity:
    """What a pass was run against. Two equal identities are the same pass."""

    records: tuple
    angles: tuple = ()
    instruments: tuple = ()
    target_digest: str = ""

    def describe(self):
        return {
            "records": [list(record) for record in self.records],
            "angles": list(self.angles),
            "instruments": list(self.instruments),
            "target": self.target_digest,
        }

    @property
    def digest(self):
        payload = json.dumps(self.describe(), sort_keys=True,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pass_identity(*, records, angles=(), instruments=(), target=None):
    """The identity of a pass, from the records it will read.

    Records may be :class:`EvidenceRecord` objects or ``(id, content)`` pairs.
    Angles and instruments are ids; ``target`` is the target's text, whose
    digest is what a target change is measured against.
    """
    entries = []
    for record in records:
        if isinstance(record, EvidenceRecord):
            entries.append((record.kind, record.id, record.digest))
        else:
            identifier, content = record
            entries.append(("evidence", str(identifier),
                            canonical_digest(content)))
    for value in angles:
        entries.append(("angle", str(value), ""))
    for value in instruments:
        entries.append(("instrument", str(value), ""))
    for entry in entries:
        if not entry[1].strip():
            raise PassRefused(f"an {entry[0]} id cannot be empty")
    return PassIdentity(
        records=tuple(sorted(entries, key=lambda e: (e[0], e[1], e[2]))),
        angles=tuple(sorted(str(a) for a in angles)),
        instruments=tuple(sorted(str(i) for i in instruments)),
        target_digest=("" if target is None else canonical_digest(target)),
    )


def semantic_delta(previous, current):
    """Which of the four delta kinds this pass carries, if any.

    ``previous`` may be ``None`` for a first pass, which is always admitted:
    there is no earlier information to be repeating.
    """
    if previous is None:
        return ("first_pass",)
    found = []
    previous_records = {entry[1]: entry[2] for entry in previous.records}
    current_records = {entry[1]: entry[2] for entry in current.records}
    for identifier, digest in current_records.items():
        if identifier not in previous_records:
            found.append("evidence")
            break
        if previous_records[identifier] != digest:
            found.append("evidence")
            break
    if previous.target_digest != current.target_digest:
        found.append("target")
    if set(current.angles) - set(previous.angles):
        found.append("angle")
    if set(current.instruments) - set(previous.instruments):
        found.append("instrument")
    return tuple(found)


def begin_pass(previous, *, records, angles=(), instruments=(), target=None):
    """Admit a pass, or refuse it by name (Rule 0).

    Refusal is the default for an unchanged state: a re-run over the same
    information is not a second pass, and counting it as one is how a loop
    reports saturation it never reached.
    """
    current = pass_identity(records=records, angles=angles,
                            instruments=instruments, target=target)
    delta = semantic_delta(previous, current)
    if not delta:
        raise PassRefused(
            "pass refused: no semantic delta against the previous pass "
            "(new evidence record, changed target, new angle or new "
            "instrument); formatting churn is not a delta")
    return current, delta


@dataclass
class Finding:
    """One gap, and the state it is in."""

    id: str
    summary: str
    bucket: str = ""
    state: str = OPEN
    materiality: str = ""
    cure: str = ""
    read: str = ""
    packet: str = ""
    owner_act: str = ""

    def __post_init__(self):
        if not str(self.id).strip():
            raise GapRegisterError("a finding needs an id")
        if not str(self.summary).strip():
            raise GapRegisterError(f"finding {self.id!r} needs a summary")


@dataclass
class GapRegister:
    """Findings, their materiality, and the two exits.

    The register has no "noted" state on purpose. A finding is either still
    open (the loop's problem), closed with a cure and its re-score read, or
    decision-ready with a packet and the named owner act. Anything else is a
    way of sounding finished without finishing.
    """

    findings: dict = field(default_factory=dict)
    tail: dict = field(default_factory=dict)
    threshold: int = TAIL_PROMOTION_THRESHOLD

    def add(self, finding, *, materiality=None):
        """Record a finding. ``materiality`` is a reason from MATERIAL_REASONS.

        A finding with no material reason lands in the tail register, counted
        per bucket, and is promoted into the main register when its bucket
        reaches the threshold.
        """
        if finding.id in self.findings or finding.id in {
                key for bucket in self.tail.values() for key in bucket}:
            raise GapRegisterError(f"finding {finding.id!r} is already recorded")
        if materiality is None:
            self.tail.setdefault(finding.bucket or "unbucketed", [])
            self.tail[finding.bucket or "unbucketed"].append(finding.id)
            if len(self.tail[finding.bucket or "unbucketed"]) >= self.threshold:
                return self._promote(finding.bucket or "unbucketed")
            return finding
        if materiality not in MATERIAL_REASONS:
            raise GapRegisterError(
                f"unknown materiality reason {materiality!r}; declared: "
                f"{list(MATERIAL_REASONS)}")
        finding.materiality = materiality
        self.findings[finding.id] = finding
        return finding

    def _promote(self, bucket):
        promoted = self.tail.pop(bucket)
        for identifier in promoted:
            finding = Finding(id=identifier, summary=f"promoted from {bucket}")
            finding.materiality = "changes_ship_decision"
            self.findings[identifier] = finding
        return promoted

    def get(self, finding_id):
        try:
            return self.findings[finding_id]
        except KeyError:
            if any(finding_id in ids for ids in self.tail.values()):
                raise GapRegisterError(
                    f"finding {finding_id!r} is tail material and not yet "
                    f"promoted; promote its bucket before dispositioning it"
                ) from None
            raise GapRegisterError(
                f"unknown finding {finding_id!r}") from None

    def set_state(self, finding_id, state):
        """Move a finding to an exit state, or refuse by name."""
        if state not in EXIT_STATES:
            raise GapRegisterError(
                f"{state!r} is not a register exit; a finding leaves only as "
                f"{CLOSED!r} or {DECISION_READY!r} -- there is no 'noted' "
                f"state where a finding can sit without an owner")
        finding = self.get(finding_id)
        finding.state = state
        return finding

    def note(self, finding_id, text=""):
        """Refused by design. Kept so the attempt has one loud answer."""
        raise GapRegisterError(
            "the register has no 'noted' state: record the finding as a "
            "material gap, a tail gap, a closed cure with its read, or a "
            "decision-ready packet")

    def close(self, finding_id, *, cure, read):
        """Close a finding: a landed cure *and* the read that re-scored it."""
        if not str(cure).strip():
            raise GapRegisterError(
                f"finding {finding_id!r} cannot close without a cure")
        if not str(read).strip():
            raise GapRegisterError(
                f"finding {finding_id!r} cannot close without the read that "
                f"re-scored the changed artifact")
        finding = self.set_state(finding_id, CLOSED)
        finding.cure = str(cure)
        finding.read = str(read)
        return finding

    def decide_ready(self, finding_id, *, packet, owner_act):
        """A finding the loop cannot cure: a packet and the named owner act."""
        if not str(packet).strip():
            raise GapRegisterError(
                f"finding {finding_id!r} needs a packet to be decision-ready")
        if not str(owner_act).strip():
            raise GapRegisterError(
                f"finding {finding_id!r} needs the owner act it waits on")
        finding = self.set_state(finding_id, DECISION_READY)
        finding.packet = str(packet)
        finding.owner_act = str(owner_act)
        return finding

    def open_findings(self):
        return [f for f in self.findings.values() if f.state == OPEN]

    def tail_count(self, bucket):
        return len(self.tail.get(bucket, []))

    def find(self, finding_id):
        """The main-register finding, or ``None`` for an unknown/tail id."""
        return self.findings.get(finding_id)

    def known(self, finding_id):
        """Whether the id is in the main register or the tail."""
        return finding_id in self.findings or any(
            finding_id in ids for ids in self.tail.values())

    def reopen(self, finding_id):
        """Reopen a closed finding as a regression, preserving its history."""
        finding = self.get(finding_id)
        if finding.state != CLOSED:
            raise GapRegisterError(
                f"finding {finding_id!r} is not closed and cannot be "
                f"reopened as a regression (state {finding.state!r})")
        finding.state = OPEN
        return finding


class EventLedger:
    """Append-only record of every register move, one JSON line per event.

    Corrupt lines fail closed: a ledger that silently skips a line is a ledger
    whose "no events" and "unreadable events" look the same, and the second is
    the one that matters.
    """

    def __init__(self, path):
        self.path = str(path)

    def append(self, event):
        if not isinstance(event, dict) or not event.get("event"):
            raise GapRegisterError("an event needs a name")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".",
                    exist_ok=True)
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self):
        if not os.path.exists(self.path):
            return []
        events = []
        with open(self.path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    events.append(json.loads(text))
                except json.JSONDecodeError as exc:
                    raise GapRegisterError(
                        f"{self.path}:{number} is not a JSON event ({exc}); "
                        f"the ledger fails closed rather than skipping it"
                    ) from exc
        return events


def record_pass(ledger, *, identity, delta, campaign=""):
    """Log an admitted pass with the delta that admitted it."""
    event = {
        "event": "pass_admitted",
        "campaign": campaign,
        "identity": identity.digest,
        "delta": list(delta),
        "records": len(identity.records),
        "angles": list(identity.angles),
        "instruments": list(identity.instruments),
    }
    ledger.append(event)
    return event


def record_refusal(ledger, *, reason, campaign=""):
    """Log a refusal: a refused pass is a fact about the loop, not a no-op."""
    event = {"event": "pass_refused", "campaign": campaign,
             "reason": str(reason)}
    ledger.append(event)
    return event

# ---------------------------------------------------------------- the loop
#
# FLUSH-1: the operator over the contracts above. It still writes no code,
# merges nothing and dispatches nothing -- it rotates angles, refuses the
# passes that carry no delta, records what the judge said, and reports whether
# the stop rule is actually met. A campaign is a sequence of passes over one
# target; the loop's job is to make "saturated" mean two consecutive expansion
# passes that learned nothing new *and* a bar met on a dossier that names its
# information basis.

#: The four angles the commission names. Rotation means a pass brings at least
#: one angle the campaign has not read the target through before.
ANGLES = ("flaw-hunt", "red-team", "missing-evidence", "production-failure")

#: FLUSH-1 speaks to one instrument: Jev diagnostics. FLUSH-2 adds the
#: different-family panel and the deterministic checks, FLUSH-3 the cold run.
JEV_INSTRUMENT = "jev"
PANEL_INSTRUMENT = "panel"
DETERMINISTIC_INSTRUMENT = "deterministic"

#: FLUSH-1's declared instrument set is one member. An extended loop must opt
#: into the FLUSH-2 instruments explicitly; the default path keeps the
#: FLUSH-1 refusal intact.
FLUSH_ONE_INSTRUMENTS = (JEV_INSTRUMENT,)
FLUSH_TWO_INSTRUMENTS = (JEV_INSTRUMENT, PANEL_INSTRUMENT,
                         DETERMINISTIC_INSTRUMENT)
COLD_INSTRUMENT = "cold"
FLUSH_THREE_INSTRUMENTS = FLUSH_TWO_INSTRUMENTS + (COLD_INSTRUMENT,)
CANARY_INSTRUMENT = "canary"
FLUSH_CANARY_INSTRUMENTS = (CANARY_INSTRUMENT,)

#: The stop bar, verbatim from the commission: combined >= 95, every axis >= 85,
#: no axis below 60.
STOP_COMBINED = 95.0
AXIS_FLOOR = 85.0
ABSOLUTE_FLOOR = 60.0

#: Two consecutive clean expansion passes, not one. One clean pass is a
#: coincidence; two in a row, each under a new angle, is evidence.
CLEAN_EXPANSION_STREAK = 2


class FlushError(HarnessError):
    """The loop was asked to do something it cannot stand behind."""

    kind = "flush_loop"


@dataclass(frozen=True)
class AspectVerdict:
    """What one instrument said about one aspect, and on what information.

    ``missing_record`` is the stall channel: the instrument names the exact
    record it could not get, and only that aspect parks. ``basis`` is the
    information basis the commission requires every verdict to name (dossier
    hash plus target version); a verdict without one is inadmissible rather
    than scored at zero.
    """

    aspect: str
    scores: dict = field(default_factory=dict)
    findings: tuple = ()
    missing_record: str = ""
    basis: str = ""

    def __post_init__(self):
        if not str(self.aspect).strip():
            raise FlushError("a verdict needs the aspect it judges")
        if not self.basis.strip():
            raise FlushError(
                f"verdict for {self.aspect!r} names no information basis; a "
                f"score without its dossier hash and target version cannot be "
                f"re-read, so it is inadmissible")


@dataclass(frozen=True)
class InfoRequest:
    """A pass stalled on missing information: the exact record, one aspect."""

    aspect: str
    missing_record: str

    def describe(self):
        return {"aspect": self.aspect, "missing_record": self.missing_record}


class OscillationBreaker:
    """Track cure -> regression cycles and escalate a repeated cycle once.

    A regression is the same finding returning after a cure. The sequence is
    kept per aspect; a cycle is a repeated suffix of length at most three, so
    ``A, B, A, B`` escalates as ``A, B`` after its second occurrence. The
    escalation is one combined packet naming every cure in the cycle: curing
    one member in isolation is how an oscillation is mistaken for progress.
    """

    def __init__(self, max_cycle=3):
        self.max_cycle = int(max_cycle)
        self.regressions = {}
        self.cures = {}
        self.packets = []
        self._seen = set()

    def record_cure(self, finding_id, cure):
        finding_id = str(finding_id).strip()
        cure = str(cure).strip()
        if not finding_id:
            raise FlushError("an oscillation cure needs a finding id")
        if not cure:
            raise FlushError(
                f"finding {finding_id!r} cannot enter the oscillation graph "
                f"without a landed cure")
        self.cures[finding_id] = cure
        return finding_id

    def record_regression(self, finding_id, *, aspect):
        """Return a combined packet when this regression closes a cycle."""
        finding_id = str(finding_id).strip()
        aspect = str(aspect or "unbucketed")
        if finding_id not in self.cures:
            raise FlushError(
                f"finding {finding_id!r} regressed without a recorded cure; "
                f"the oscillation graph has no edge to follow")
        sequence = self.regressions.setdefault(aspect, [])
        sequence.append(finding_id)
        limit = min(self.max_cycle, len(sequence) // 2)
        for length in range(1, limit + 1):
            cycle = tuple(sequence[-length:])
            if tuple(sequence[-2 * length:-length]) != cycle:
                continue
            key = (aspect, cycle)
            if key in self._seen:
                return None
            self._seen.add(key)
            packet = {
                "kind": "combined_cure_packet",
                "aspect": aspect,
                "cycle": list(cycle),
                "seen": 2,
                "cures": {member: self.cures.get(member, "")
                          for member in cycle},
                "instruction": (
                    "cure the cycle as one packet; do not re-cure a member "
                    "in isolation and call it progress"),
            }
            self.packets.append(packet)
            return packet
        return None


@dataclass
class PassOutcome:
    """What one pass did: its delta, its verdicts, and whether it was clean."""

    number: int
    identity: PassIdentity
    delta: tuple
    expansion: bool
    clean: bool
    material_findings: tuple = ()
    info_requests: tuple = ()
    verdicts: tuple = ()
    repeated_calls: tuple = ()
    regressions: tuple = ()
    escalation: dict = None

    def describe(self):
        return {
            "pass": self.number,
            "identity": self.identity.digest,
            "delta": list(self.delta),
            "expansion": self.expansion,
            "clean": self.clean,
            "material_findings": list(self.material_findings),
            "info_requests": [r.describe() for r in self.info_requests],
            "repeated_calls": list(self.repeated_calls),
            "regressions": list(self.regressions),
            "escalation": self.escalation,
            "verdicts": len(self.verdicts),
        }


def record_info_request(ledger, *, aspect, missing_record, campaign=""):
    """Log a stall with the exact missing record, so it can be supplied."""
    if not str(missing_record).strip():
        raise FlushError(
            "an INFO REQUEST must name the exact missing record; a request "
            "for 'more information' cannot be answered")
    event = {"event": "info_request", "campaign": campaign,
             "aspect": str(aspect), "missing_record": str(missing_record)}
    ledger.append(event)
    return event


def record_stop(ledger, *, campaign="", streak, basis, combined):
    """Log a stop, with the evidence it rests on."""
    event = {"event": "stop", "campaign": campaign, "streak": streak,
             "basis": str(basis), "combined": float(combined)}
    ledger.append(event)
    return event


def record_oscillation(ledger, *, packet, campaign=""):
    """Log one combined cure packet for a detected oscillation cycle."""
    event = {
        "event": "oscillation_escalated",
        "campaign": campaign,
        "kind": packet.get("kind", "combined_cure_packet"),
        "aspect": packet.get("aspect", ""),
        "cycle": list(packet.get("cycle") or ()),
        "cures": dict(packet.get("cures") or {}),
        "packet": dict(packet),
    }
    ledger.append(event)
    return event


class FlushLoop:
    """A campaign: passes over one target, judged by injected instruments.

    The default loop is FLUSH-1: exactly one instrument (Jev), so the FLUSH-1
    refusal still holds. A caller that owns the FLUSH-2 adapters opts into the
    panel and deterministic instruments explicitly. Every verdict is keyed by
    aspect, angle and instrument; a repeated call is marked rather than
    counted as fresh evidence.
    """

    def __init__(self, *, ledger, judge=None, judges=None, register=None,
                 campaign="", bar=STOP_COMBINED, axis_floor=AXIS_FLOOR,
                 absolute_floor=ABSOLUTE_FLOOR,
                 supported_instruments=FLUSH_ONE_INSTRUMENTS,
                 judge_version=None):
        self.ledger = ledger
        self.judges = {str(key): value
                       for key, value in (judges or {}).items()}
        if judge is not None:
            self.judges.setdefault(JEV_INSTRUMENT, judge)
        if not self.judges:
            raise FlushError(
                "a flush loop needs at least one instrument judge")
        self.supported_instruments = tuple(dict.fromkeys(
            str(instrument) for instrument in supported_instruments))
        if not self.supported_instruments:
            raise FlushError(
                "a flush loop needs at least one supported instrument")
        self.register = register if register is not None else GapRegister()
        self.campaign = campaign
        self.bar = float(bar)
        self.axis_floor = float(axis_floor)
        self.absolute_floor = float(absolute_floor)
        self.previous = None
        self.passes = []
        self.clean_expansion_streak = 0
        self.cold_run_pass = None
        self.cold_run_basis = ""
        self._calls = set()
        self._angles_seen = set()
        self._instruments_seen = set()
        self.oscillation = OscillationBreaker()
        self.escalations = []
        self.judge_version = None
        self.scores = {}
        self.score_history = []
        if judge_version is not None:
            self.pin_judge_version(judge_version)

    # -- one pass ---------------------------------------------------------
    def run_pass(self, *, target, records, angles, aspects,
                 instruments=(JEV_INSTRUMENT,), judge_version=None):
        """Admit, judge and record one pass. Refusal raises ``PassRefused``."""
        angles = tuple(dict.fromkeys(str(angle) for angle in angles))
        instruments = tuple(dict.fromkeys(str(i) for i in instruments))
        if not angles:
            raise FlushError("a pass needs at least one angle to read through")
        if not aspects:
            raise FlushError("a pass needs at least one aspect to judge")
        if not instruments:
            raise FlushError(
                "a pass needs at least one instrument to judge")
        unknown = set(instruments) - set(self.supported_instruments)
        if unknown:
            if set(self.supported_instruments) == {JEV_INSTRUMENT}:
                raise FlushError(
                    "FLUSH-1 judges with exactly one instrument, "
                    f"{JEV_INSTRUMENT!r}; declared {list(instruments)!r}. "
                    "The different-family panel and deterministic checks "
                    "arrive with FLUSH-2")
            raise FlushError(
                f"unsupported instrument(s) {sorted(unknown)!r}; declared "
                f"{list(self.supported_instruments)!r}")
        missing = [instrument for instrument in instruments
                   if instrument not in self.judges]
        if missing:
            raise FlushError(
                f"no judge registered for instrument(s) {missing!r}; "
                "refusing before the pass is admitted")
        try:
            identity, delta = begin_pass(self.previous, records=records,
                                         angles=angles,
                                         instruments=instruments, target=target)
        except PassRefused as exc:
            # A refused pass is a fact about the campaign, not a no-op: it is
            # how "the loop was asked to run twice with no new information"
            # stays visible in the record.
            record_refusal(self.ledger, reason=str(exc), campaign=self.campaign)
            raise
        if judge_version is not None:
            self.pin_judge_version(judge_version)
        record_pass(self.ledger, identity=identity, delta=delta,
                    campaign=self.campaign)
        self.previous = identity
        new_angles = set(angles) - self._angles_seen
        new_instruments = set(instruments) - self._instruments_seen
        self._angles_seen |= set(angles)
        self._instruments_seen |= set(instruments)
        expansion = bool(new_angles or new_instruments)

        material, info, verdicts, repeated = [], [], [], []
        regressions = []
        escalation = None
        cold_basis = ""
        for aspect in aspects:
            for angle in angles:
                for instrument in instruments:
                    key = (str(aspect), angle, instrument, identity.digest)
                    if key in self._calls:
                        repeated.append(
                            _call_label(aspect, angle, instrument))
                        continue
                    self._calls.add(key)
                    verdict = self.judges[instrument](
                        aspect=aspect, angle=angle, target=target,
                        records=records)
                    if not isinstance(verdict, AspectVerdict):
                        raise FlushError(
                            "the judge must return AspectVerdict, not "
                            f"{type(verdict).__name__}")
                    if verdict.aspect != str(aspect):
                        raise FlushError(
                            f"verdict is for {verdict.aspect!r} but aspect "
                            f"{aspect!r} was asked")
                    verdicts.append(verdict)
                    if (instrument == COLD_INSTRUMENT
                            and not verdict.missing_record.strip()
                            and not cold_basis):
                        cold_basis = verdict.basis
                    for axis, value in verdict.scores.items():
                        self.scores[str(axis)] = float(value)
                    if verdict.missing_record.strip():
                        request = InfoRequest(
                            aspect=str(aspect),
                            missing_record=verdict.missing_record)
                        record_info_request(
                            self.ledger, aspect=request.aspect,
                            missing_record=request.missing_record,
                            campaign=self.campaign)
                        info.append(request)
                        continue
                    for raw in verdict.findings:
                        finding, reason = _as_finding(raw)
                        existing = self.register.find(finding.id)
                        if existing is not None:
                            if existing.state == CLOSED:
                                if not str(existing.cure).strip():
                                    raise FlushError(
                                        f"finding {existing.id!r} is closed "
                                        f"without a cure; the register cannot "
                                        f"form an oscillation edge")
                                self.oscillation.record_cure(
                                    existing.id, existing.cure)
                                self.register.reopen(existing.id)
                                regressions.append(existing.id)
                                packet = self.oscillation.record_regression(
                                    existing.id, aspect=str(aspect))
                                if packet is not None:
                                    self.escalations.append(packet)
                                    record_oscillation(
                                        self.ledger, packet=packet,
                                        campaign=self.campaign)
                                    escalation = packet
                                    break
                            continue
                        if self.register.known(finding.id):
                            continue
                        self.register.add(finding, materiality=reason)
                        if reason is not None:
                            material.append(finding.id)
                    if escalation is not None:
                        break
                if escalation is not None:
                    break
            if escalation is not None:
                break

        clean = (expansion and not material and not info and not repeated
                 and not regressions)
        self.clean_expansion_streak = (
            self.clean_expansion_streak + 1 if clean else 0)
        outcome = PassOutcome(
            number=len(self.passes) + 1, identity=identity, delta=delta,
            expansion=expansion, clean=clean,
            material_findings=tuple(material),
            info_requests=tuple(info), verdicts=tuple(verdicts),
            repeated_calls=tuple(repeated), regressions=tuple(regressions),
            escalation=escalation)
        self.passes.append(outcome)
        if cold_basis:
            # A cold instrument that actually answered is the cold run for
            # this pass. The manual marker remains for FLUSH-1/2 campaigns.
            self.cold_run_pass = outcome.number
            self.cold_run_basis = cold_basis
        return outcome

    def close_finding(self, finding_id, *, cure, read, aspect=""):
        """Close a finding and record the cure in the oscillation graph."""
        finding = self.register.close(finding_id, cure=cure, read=read)
        self.oscillation.record_cure(finding.id, cure)
        return finding

    def pin_judge_version(self, version, *, control_scores=None):
        """Pin a judge version; a change rebaselines scores, not findings."""
        version = str(version).strip()
        if not version:
            raise FlushError("a calibration pin needs a judge version")
        scores = {}
        if control_scores is not None:
            scores = {str(axis): float(value)
                      for axis, value in dict(control_scores).items()}
        if self.judge_version == version:
            return None
        previous = self.judge_version
        if previous is None:
            self.judge_version = version
            if control_scores is not None:
                self.scores = scores
            return None
        before = dict(self.scores)
        self.judge_version = version
        self.scores = scores
        event = {
            "event": "calibration_rebaselined",
            "campaign": self.campaign,
            "from": previous,
            "to": version,
            "scores_before": before,
            "scores_after": dict(scores),
        }
        self.score_history.append(event)
        self.ledger.append(event)
        return event

    # -- the stop rule ----------------------------------------------------
    def mark_cold_run(self, *, basis):
        """Record that the most recent pass included a cold (non-author) run."""
        if not self.passes:
            raise FlushError("a cold run has to belong to a pass")
        if not str(basis).strip():
            raise FlushError(
                "a cold run must name who read the target and on what basis")
        self.cold_run_pass = self.passes[-1].number
        self.cold_run_basis = str(basis)
        return self.cold_run_pass

    def bar_met(self, dossier):
        """The bar, from a dossier of axis scores that names its basis."""
        axes = dict(dossier.get("axes") or {})
        basis = str(dossier.get("basis") or "")
        if not axes:
            return False, ("the dossier carries no axis scores",)
        if not basis.strip():
            return False, ("the dossier names no information basis",)
        scores = {axis: float(value) for axis, value in axes.items()}
        combined = sum(scores.values()) / len(scores)
        reasons = []
        if combined < self.bar:
            reasons.append(
                f"combined {combined:.2f} is below the bar {self.bar:.2f}")
        for axis, value in sorted(scores.items()):
            if value < self.axis_floor:
                reasons.append(
                    f"axis {axis} scores {value:.2f}, below the "
                    f"{self.axis_floor:.0f} floor")
            elif value < self.absolute_floor:
                reasons.append(
                    f"axis {axis} scores {value:.2f}, below the absolute "
                    f"floor {self.absolute_floor:.0f}")
        return (not reasons), tuple(reasons)

    def stop_ready(self, dossier=None):
        """Whether saturation may be claimed, and exactly what is missing."""
        reasons = []
        if not self.passes:
            reasons.append("no pass has run")
            return False, tuple(reasons)
        if self.clean_expansion_streak < CLEAN_EXPANSION_STREAK:
            reasons.append(
                f"{self.clean_expansion_streak} consecutive clean expansion "
                f"pass(es); {CLEAN_EXPANSION_STREAK} are required")
        if self.cold_run_pass != self.passes[-1].number:
            reasons.append("the final pass includes no cold run")
        if self.escalations:
            reasons.append(
                f"{len(self.escalations)} oscillation cycle(s) escalated; "
                "the combined packet awaits a cure")
        if self.register.open_findings():
            reasons.append(
                f"{len(self.register.open_findings())} finding(s) are still "
                f"open; the register has two exits and neither was taken")
        if dossier is None:
            reasons.append("no dossier was supplied, so the bar is unmet")
        else:
            _ok, bar_reasons = self.bar_met(dossier)
            reasons.extend(bar_reasons)
        return (not reasons), tuple(reasons)

    def mark_stopped(self, dossier):
        """Claim the stop. Refused, by name, when the rule is not met."""
        ready, reasons = self.stop_ready(dossier)
        if not ready:
            raise FlushError(
                "the loop is not saturated: " + "; ".join(reasons))
        scores = {axis: float(value)
                  for axis, value in (dossier.get("axes") or {}).items()}
        combined = sum(scores.values()) / len(scores)
        return record_stop(self.ledger, campaign=self.campaign,
                           streak=self.clean_expansion_streak,
                           basis=dossier.get("basis", ""), combined=combined)

    def describe(self):
        return {
            "campaign": self.campaign,
            "passes": [p.describe() for p in self.passes],
            "clean_expansion_streak": self.clean_expansion_streak,
            "cold_run_pass": self.cold_run_pass,
            "judge_version": self.judge_version,
            "scores": dict(self.scores),
            "score_history": list(self.score_history),
            "escalations": list(self.escalations),
            "oscillation_packets": list(self.oscillation.packets),
            "open_findings": [f.id for f in self.register.open_findings()],
            "tail": {bucket: len(ids)
                     for bucket, ids in self.register.tail.items()},
        }


@dataclass(frozen=True)
class CanaryCase:
    """One planted defect the canary gate must find.

    ``planted`` is the set of defect ids the target actually contains.
    ``findings`` is the recorded detector output for the CLI/MCP replay path;
    an injected judge can supply fresh findings instead.
    """

    id: str
    target: str
    planted: tuple
    aspect: str = "canary"
    angle: str = "flaw-hunt"
    findings: tuple = ()
    basis: str = ""

    def __post_init__(self):
        case_id = str(self.id).strip()
        if not case_id:
            raise FlushError("a canary needs an id")
        if not str(self.target).strip():
            raise FlushError(f"canary {case_id!r} has no target content")
        planted = tuple(str(item).strip() for item in self.planted)
        if not planted or any(not item for item in planted):
            raise FlushError(
                f"canary {case_id!r} must plant at least one defect id")
        object.__setattr__(self, "id", case_id)
        object.__setattr__(self, "planted", planted)
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(
            self, "basis", str(self.basis or f"canary:{case_id}@v1"))


def run_canary_gate(cases, judge, *, held_out=None, rotation=0, ledger=None,
                    campaign="canary"):
    """Run every canary and fail closed unless every planted defect is found.

    Canaries rotate deterministically by ``rotation``. The score is measured
    on ``held_out`` (all cases when omitted); the pass verdict is stricter and
    requires every executed case to be clean. A judge error, a missing
    planted id, an empty suite, or an unknown held-out id is a gate failure,
    never an admitted result.
    """
    cases = tuple(cases)
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise FlushError("canary ids must be unique")
    if not cases:
        return {"gate": "flush_canary", "campaign": campaign,
                "passed": False, "score": 0.0, "held_out": [],
                "rotation": int(rotation), "cases": [], "ledger": "",
                "reason": "no canary cases"}
    scored_ids = set(str(item) for item in
                     (ids if held_out is None else held_out))
    unknown = scored_ids - set(ids)
    if unknown:
        raise FlushError(
            f"held-out canary id(s) not in the suite: {sorted(unknown)!r}")
    rotation = int(rotation) % len(cases)
    ordered = cases[rotation:] + cases[:rotation]

    temporary = None
    if ledger is None:
        temporary = tempfile.TemporaryDirectory()
        active = EventLedger(Path(temporary.name) / "canary.events.jsonl")
        ledger_label = ""
    elif isinstance(ledger, EventLedger):
        active = ledger
        ledger_label = str(getattr(ledger, "path", ""))
    else:
        active = EventLedger(str(ledger))
        ledger_label = str(ledger)

    results = []
    for case in ordered:
        found = set()
        error = ""
        try:
            loop = FlushLoop(
                ledger=active, judges={CANARY_INSTRUMENT: judge},
                supported_instruments=FLUSH_CANARY_INSTRUMENTS,
                campaign=f"{campaign}:{case.id}")
            outcome = loop.run_pass(
                target=case.id,
                records=[EvidenceRecord(case.id, case.target)],
                angles=[case.angle], aspects=[case.aspect],
                instruments=[CANARY_INSTRUMENT])
            for verdict in outcome.verdicts:
                for raw in verdict.findings:
                    finding, _reason = _as_finding(raw)
                    found.add(finding.id)
        except Exception as exc:  # the gate fails closed on any judge error
            error = f"{type(exc).__name__}: {exc}"
        planted = set(case.planted)
        missed = sorted(planted - found)
        passed = bool(planted) and not missed and not error
        results.append({"id": case.id, "passed": passed,
                        "found": sorted(planted & found),
                        "missed": missed, "error": error})

    scored = [result for result in results
              if result["id"] in scored_ids]
    total = sum(len(case.planted) for case in cases
                if case.id in scored_ids)
    hit = sum(len(result["found"]) for result in scored)
    report = {
        "gate": "flush_canary",
        "campaign": campaign,
        "passed": bool(scored) and all(result["passed"]
                                        for result in results),
        "score": round(hit / total, 4) if total else 0.0,
        "held_out": sorted(scored_ids),
        "rotation": rotation,
        "cases": results,
        "ledger": ledger_label,
    }
    if temporary is not None:
        temporary.cleanup()
    return report


def run_canary_suite_file(suite_path, *, ledger_path=None):
    """Replay a canary suite file through the same gate the tests exercise."""
    path = Path(suite_path)
    if not path.is_file():
        raise FlushError(f"canary suite not found: {path}")
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlushError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(suite, dict):
        raise FlushError(f"{path} must contain a JSON object")
    cases = []
    for raw in suite.get("canaries") or []:
        if not isinstance(raw, dict):
            raise FlushError("every canary must be an object")
        cases.append(CanaryCase(
            id=str(raw.get("id") or ""),
            target=str(raw.get("target") or ""),
            planted=tuple(raw.get("planted") or ()),
            aspect=str(raw.get("aspect") or "canary"),
            angle=str(raw.get("angle") or "flaw-hunt"),
            findings=tuple(raw.get("findings") or ()),
            basis=str(raw.get("basis") or "")))
    by_id = {case.id: case for case in cases}

    def judge(*, aspect, angle, target, records):
        case = by_id.get(str(target))
        if case is None:
            raise FlushError(f"no canary case for target {target!r}")
        return AspectVerdict(aspect=aspect, basis=case.basis,
                             findings=case.findings)

    return run_canary_gate(
        cases, judge, held_out=suite.get("held_out"),
        rotation=int(suite.get("rotation") or 0), ledger=ledger_path,
        campaign=str(suite.get("campaign") or path.stem))


def run_campaign_file(campaign_path, *, ledger_path=None,
                      extended_instruments=False, cold_run=False):
    """Replay a campaign file. The CLI and MCP both call this one owner."""
    campaign_path = Path(campaign_path)
    if not campaign_path.is_file():
        raise FlushError(f"campaign file not found: {campaign_path}")
    try:
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlushError(f"{campaign_path} is not valid JSON: {exc}") from exc
    if not isinstance(campaign, dict):
        raise FlushError(f"{campaign_path} must contain a JSON object")
    base = campaign_path.parent
    repo_root = Path(__file__).resolve().parent.parent

    def _resolve(text):
        if not text:
            return None
        for candidate in (base / text, Path.cwd() / text, repo_root / text):
            if candidate.is_file():
                return candidate
        return None

    def _target_text(spec):
        text = str(spec or "")
        candidate = _resolve(text)
        if candidate is not None:
            return candidate.read_text(encoding="utf-8")
        return text

    def _records(specs):
        resolved = []
        for spec in specs or []:
            if not isinstance(spec, dict) or not spec.get("id"):
                raise FlushError("every record needs an id")
            if "content" in spec:
                resolved.append((str(spec["id"]), str(spec["content"])))
                continue
            named = str(spec.get("path") or "")
            path = _resolve(named)
            if path is None:
                raise FlushError(
                    f"record {spec['id']!r} names no readable path: {named} "
                    f"(tried {base / named} and the repo root)")
            resolved.append((str(spec["id"]),
                             path.read_text(encoding="utf-8")))
        return resolved

    state = {"pass": {}}
    supported = (FLUSH_THREE_INSTRUMENTS if extended_instruments
                 else FLUSH_ONE_INSTRUMENTS)

    def _verdict_spec(instrument, aspect):
        if instrument == JEV_INSTRUMENT:
            verdicts = state["pass"].get("verdicts") or {}
        else:
            nested = state["pass"].get("instrument_verdicts") or {}
            verdicts = nested.get(instrument) or {}
        return verdicts.get(aspect)

    def _make_judge(instrument):
        def judge(*, aspect, angle, target, records):
            spec = _verdict_spec(instrument, aspect)
            if spec is None:
                raise FlushError(
                    f"the campaign names no verdict for instrument "
                    f"{instrument!r} aspect {aspect!r}; the loop does not "
                    f"invent scores")
            return AspectVerdict(
                aspect=aspect,
                scores=dict(spec.get("scores") or {}),
                findings=tuple(spec.get("findings") or ()),
                missing_record=str(spec.get("missing_record") or ""),
                basis=str(spec.get("basis") or ""))
        return judge

    judges = {instrument: _make_judge(instrument)
              for instrument in supported}

    def _validate_pass(spec):
        instruments = tuple(dict.fromkeys(
            str(i) for i in (spec.get("instruments")
                             or (JEV_INSTRUMENT,))))
        unknown = set(instruments) - set(supported)
        if unknown:
            if set(supported) == {JEV_INSTRUMENT}:
                raise FlushError(
                    "FLUSH-1 judges with exactly one instrument, "
                    f"{JEV_INSTRUMENT!r}; declared {list(instruments)!r}. "
                    "Pass --extended-instruments for the FLUSH-2/3 rotation")
            raise FlushError(
                f"unsupported instrument(s) {sorted(unknown)!r}; declared "
                f"{list(supported)!r}")
        aspects = tuple(spec.get("aspects") or ())
        for instrument in instruments:
            for aspect in aspects:
                if _verdict_spec(instrument, aspect) is None:
                    raise FlushError(
                        f"the campaign names no verdict for instrument "
                        f"{instrument!r} aspect {aspect!r}; the loop does "
                        f"not invent scores")
        closes = spec.get("close") or []
        if not isinstance(closes, list):
            raise FlushError("a pass 'close' entry must be a list")
        for close in closes:
            if (not isinstance(close, dict) or not close.get("finding")
                    or not close.get("cure") or not close.get("read")):
                raise FlushError("every close needs finding, cure and read")
        return instruments, closes

    chosen_ledger = ledger_path or (
        f"{campaign_path}.cold.events.jsonl" if cold_run
        else f"{campaign_path}.events.jsonl")
    if cold_run and Path(chosen_ledger).exists():
        raise FlushError(
            f"a cold run needs a clean ledger; {chosen_ledger} already exists")
    loop = FlushLoop(
        ledger=EventLedger(chosen_ledger), judges=judges,
        supported_instruments=supported,
        campaign=str(campaign.get("campaign") or campaign_path.stem),
        judge_version=campaign.get("judge_version"))
    target = _target_text(campaign.get("target"))
    for spec in campaign.get("passes") or []:
        if not isinstance(spec, dict):
            raise FlushError("every pass must be an object")
        state["pass"] = spec
        instruments, closes = _validate_pass(spec)
        outcome = loop.run_pass(
            target=target, records=_records(spec.get("records")),
            angles=spec.get("angles") or (),
            aspects=spec.get("aspects") or (), instruments=instruments,
            judge_version=spec.get("judge_version"))
        if spec.get("cold_run"):
            loop.mark_cold_run(basis=str(spec["cold_run"]))
        if outcome.info_requests:
            sys.stderr.write("[flush] pass {} stalled on {}\n".format(
                outcome.number,
                ", ".join(f"{r.aspect} needs {r.missing_record}"
                          for r in outcome.info_requests)))
        if outcome.escalation is not None:
            break
        for close in closes:
            loop.close_finding(
                str(close["finding"]), cure=str(close["cure"]),
                read=str(close["read"]),
                aspect=str(close.get("aspect") or ""))

    if cold_run:
        if not loop.passes:
            raise FlushError("a cold run needs at least one admitted pass")
        if loop.cold_run_pass != loop.passes[-1].number:
            raise FlushError(
                "the cold run did not complete in the final pass; add a "
                "cold_run basis or the cold instrument to that pass")

    dossier = campaign.get("dossier")
    stopped = None
    if campaign.get("stop") and not loop.escalations:
        stopped = loop.mark_stopped(dossier) if dossier else None
        if dossier is None:
            raise FlushError("a campaign that claims a stop needs a dossier")
    ready, reasons = loop.stop_ready(dossier)
    result = {"campaign": loop.campaign, "ledger": str(chosen_ledger),
              "stop_ready": ready, "stop_reasons": list(reasons),
              "stopped": stopped is not None, **loop.describe()}
    if cold_run:
        result["cold_run"] = {
            "clean_state": True,
            "ledger": str(chosen_ledger),
            "pass": loop.cold_run_pass,
            "basis": loop.cold_run_basis,
        }
    return result


def _as_finding(raw):
    """Accept a ``Finding`` or a plain dict; return it with its materiality."""
    if isinstance(raw, Finding):
        return raw, (raw.materiality or None)
    if not isinstance(raw, dict):
        raise FlushError(
            f"a finding must be a Finding or a dict, not {type(raw).__name__}")
    reason = raw.get("materiality") or None
    finding = Finding(id=str(raw.get("id", "")),
                      summary=str(raw.get("summary", "")),
                      bucket=str(raw.get("bucket", "")))
    return finding, reason


def _call_label(aspect, angle, instrument):
    """Keep the FLUSH-1 repeated-call label stable for the Jev path."""
    if instrument == JEV_INSTRUMENT:
        return f"{aspect}/{angle}"
    return f"{aspect}/{angle}/{instrument}"
