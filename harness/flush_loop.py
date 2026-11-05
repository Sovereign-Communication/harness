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
from dataclasses import dataclass, field

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


class FlushLoop:
    """A campaign: passes over one target, judged by injected instruments.

    The judge is a callable ``judge(aspect=..., angle=..., target=...,
    records=...) -> AspectVerdict``. It is never called for a pass Rule 0
    refused, and never twice for the same aspect, angle and information set --
    a repeated call is how a loop spends money learning nothing, so it marks
    the pass not clean instead of counting as fresh evidence.
    """

    def __init__(self, *, ledger, judge, register=None, campaign="",
                 bar=STOP_COMBINED, axis_floor=AXIS_FLOOR,
                 absolute_floor=ABSOLUTE_FLOOR):
        self.ledger = ledger
        self.judge = judge
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

    # -- one pass ---------------------------------------------------------
    def run_pass(self, *, target, records, angles, aspects,
                 instruments=(JEV_INSTRUMENT,)):
        """Admit, judge and record one pass. Refusal raises ``PassRefused``."""
        angles = tuple(dict.fromkeys(str(a) for a in angles))
        instruments = tuple(dict.fromkeys(str(i) for i in instruments))
        if not angles:
            raise FlushError("a pass needs at least one angle to read through")
        if not aspects:
            raise FlushError("a pass needs at least one aspect to judge")
        if set(instruments) != {JEV_INSTRUMENT}:
            raise FlushError(
                "FLUSH-1 judges with exactly one instrument, "
                f"{JEV_INSTRUMENT!r}; declared {list(instruments)!r}. The "
                "different-family panel and deterministic checks arrive "
                "with FLUSH-2")
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
        record_pass(self.ledger, identity=identity, delta=delta,
                    campaign=self.campaign)
        self.previous = identity
        new_angles = set(angles) - self._angles_seen
        new_instruments = set(instruments) - self._instruments_seen
        self._angles_seen |= set(angles)
        self._instruments_seen |= set(instruments)
        expansion = bool(new_angles or new_instruments)

        material, info, verdicts, repeated = [], [], [], []
        for aspect in aspects:
            for angle in angles:
                key = (str(aspect), angle, identity.digest)
                if key in self._calls:
                    repeated.append(f"{aspect}/{angle}")
                    continue
                self._calls.add(key)
                verdict = self.judge(aspect=aspect, angle=angle,
                                     target=target, records=records)
                if not isinstance(verdict, AspectVerdict):
                    raise FlushError(
                        "the judge must return AspectVerdict, not "
                        f"{type(verdict).__name__}")
                if verdict.aspect != str(aspect):
                    raise FlushError(
                        f"verdict is for {verdict.aspect!r} but aspect "
                        f"{aspect!r} was asked")
                verdicts.append(verdict)
                if verdict.missing_record.strip():
                    request = InfoRequest(aspect=str(aspect),
                                          missing_record=verdict.missing_record)
                    record_info_request(self.ledger, aspect=request.aspect,
                                        missing_record=request.missing_record,
                                        campaign=self.campaign)
                    info.append(request)
                    continue
                for raw in verdict.findings:
                    finding, reason = _as_finding(raw)
                    self.register.add(finding, materiality=reason)
                    if reason is not None:
                        material.append(finding.id)

        clean = expansion and not material and not info and not repeated
        self.clean_expansion_streak = (
            self.clean_expansion_streak + 1 if clean else 0)
        outcome = PassOutcome(
            number=len(self.passes) + 1, identity=identity, delta=delta,
            expansion=expansion, clean=clean,
            material_findings=tuple(material),
            info_requests=tuple(info), verdicts=tuple(verdicts),
            repeated_calls=tuple(repeated))
        self.passes.append(outcome)
        return outcome

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
            "open_findings": [f.id for f in self.register.open_findings()],
            "tail": {bucket: len(ids)
                     for bucket, ids in self.register.tail.items()},
        }


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
