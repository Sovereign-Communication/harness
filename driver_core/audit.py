"""Append-only, hash-chained audit log.

A driver that acts on a screen makes consequences. When something goes wrong
the only question that matters afterwards is *what did it think it saw, what
did it decide, and what did it do* -- so those three facts are recorded
together, in order, and chained so none of them can be quietly edited later.

The chain is the point. An append-only file that anyone can rewrite is not
evidence; a file where each record carries the hash of the previous one is at
least internally consistent, and :func:`verify_chain` will say so out loud
rather than leaving a reader to assume it.

Every record carries the provenance of the extraction it rests on, so a
decision is always traceable to the consensus round that justified it. A
decision recorded without its extraction receipt is a decision that cannot be
audited, and the driver treats that as a reason to refuse rather than to
write a thinner record.

This log is driver-core's own. It shares no path, no file and no format with
any neighbouring project's ledger.

An existing log is not evidence merely because it parses. It is opened and
extended only after one locked snapshot has been checked end to end: the
genesis pointer, every sequence number, every ``previous`` link and every
record digest. A chain that fails any check is refused before a byte is
written, so a payload edited without updating its stored hash cannot be
extended. The refusal names the first bad record; a valid chain still reopens
and appends exactly one correctly linked record.
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from .osal import audit_path_lock

#: The genesis constant. A chain's first record links to this rather than to
#: a magic empty string, so the linkage rule is uniform at every position.
GENESIS = "driver-core/audit/v1"

#: The owner label a verification report carries. Driver-core has its own
#: audit store; this keeps a verify result from being read as evidence from
#: the Harness autonomy ledger or any other project's chain.
AUDIT_STORE_SCOPE = "driver-core"

#: The record kinds this log can hold, in the order one step produces them.
#:
#: These are the log's vocabulary, and the producers below import them rather
#: than writing the strings. A declared kind nothing writes is a claim about
#: the log that can rot with nothing to notice it; a kind written as a literal
#: is the same claim with no owner at all. Both were true here at once: seven
#: constants, five kinds in use, and every producer spelling its own string.
#:
#: ``KIND_CONSENT`` was declared and nothing produced it. It is gone rather
#: than wired up, because a consent *is* recorded -- inside the ``action``
#: record it authorises, with the exact parameters it was bound to -- and
#: adding a second record for the same fact would change what a run writes.
#: That is not a change this package makes casually: see
#: ``tests/test_audit_compat.py``, which exists to catch exactly that.
KIND_CAPTURE = "capture"
KIND_EXTRACTION = "extraction"
KIND_DECISION = "decision"
KIND_ACTION = "action"
KIND_VERIFICATION = "verification"
KIND_ESCALATION = "escalation"
KIND_REFUSAL = "refusal"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def required(log, owner):
    """The chain an object that acts on the machine must write to.

    ``None`` is not a chain, so it is refused rather than stored: an
    executor holding no log runs an irreversible action and records nothing,
    which is the one outcome this package exists to make impossible. There
    is no null object to reach for instead -- :class:`MemoryAuditLog` is not
    one. It builds the same chain and holds the same records, it just keeps
    them off the disk, which is what a test or a dry run actually wants.

    Passed explicitly by every caller rather than defaulted, so forgetting
    the argument is a ``TypeError`` at the call rather than a silent gap in
    the log. :mod:`tests.test_declarations` checks the signature itself.
    """
    if log is None:
        raise TypeError(
            f"{owner} acts on the machine, so it needs a chain to write to. "
            f"Pass audit=AuditLog(path) to keep one on disk, or "
            f"audit=MemoryAuditLog() to keep the records in memory only.")
    return log


def same_chain(given, wanted, owner):
    """The chain an object that acts must write to is *this* one.

    A second chain is worse than none. It is not missing evidence, it is
    evidence that cannot be linked: the capture, the decision and the action
    would sit in two files, and only one of them would carry the ``previous``
    that ties an action to the decision that caused it. A run would read as
    though the action never happened.
    """
    if given is not wanted:
        raise TypeError(
            f"{owner} records to a different chain than the one it was given, "
            f"so nothing would link its records to the step that led to them. "
            f"Build it with audit=<the driver's own log>, or pass no executor "
            f"at all and let the driver build one that shares its chain.")
    return given


def _canonical(payload):
    """Byte-stable serialisation.

    Sorted keys and no incidental whitespace, so two runs producing the same
    facts produce the same bytes and therefore the same hash. A chain whose
    hash depends on dict ordering is a chain that verifies once and then
    mysteriously fails.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def digest(record, previous):
    """The chained digest of one record."""
    material = f"{previous}\n{_canonical(record)}".encode()
    return hashlib.sha256(material).hexdigest()


class AuditLog:
    """A hash-chained JSONL log."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._last = GENESIS
        self._count = 0
        if path and os.path.exists(path):
            self._last, self._count = self._tail()

    def _tail(self):
        """Resume from the existing chain so a restarted run extends it.

        Validate the complete snapshot while holding the same path lock used
        by append and verify. An incomplete final line is never extended.
        """
        with self._lock, audit_path_lock(self.path):
            records = self._read_all_unlocked()
            verdict = self._verify_records(records)
            if not verdict.ok:
                raise AuditError(
                    f"append refused: {verdict.detail}",
                    record_index=verdict.records)
            return (records[-1]["hash"] if records else GENESIS, len(records))

    @property
    def count(self):
        with self._lock:
            return self._count

    def head(self):
        with self._lock:
            return self._last

    def append(self, kind, **fields):
        """Append one chained record and return it."""
        if not kind:
            raise ValueError("a record needs a kind")
        with self._lock, audit_path_lock(self.path):
            records = self._read_all_unlocked()
            verdict = self._verify_records(records)
            if not verdict.ok:
                raise AuditError(
                    f"append refused: {verdict.detail}",
                    record_index=verdict.records)
            previous = records[-1]["hash"] if records else GENESIS
            record = {
                "seq": len(records),
                "kind": kind,
                "at": _now(),
                "previous": previous,
            }
            record.update(fields)
            record["hash"] = digest(
                {k: v for k, v in record.items() if k != "hash"}, previous)
            self._write(record)
            self._last = record["hash"]
            self._count = len(records) + 1
            return record

    def _write(self, record):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_all_unlocked(self):
        """Read one complete JSONL snapshot; caller holds the path lock."""
        if not self.path:
            return []
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return []
        if not raw:
            return []

        if not raw.endswith(b"\n"):
            line_number = raw.count(b"\n") + 1
            record_index = sum(1 for line in raw.splitlines()[:-1]
                               if line.strip())
            raise AuditError(
                f"{self.path}:{line_number} is truncated (missing final "
                f"newline); the chain cannot be trusted past this point",
                record_index=record_index)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            line_number = raw[:exc.start].count(b"\n") + 1
            record_index = sum(
                1 for line in raw[:exc.start].splitlines() if line.strip())
            raise AuditError(
                f"{self.path}:{line_number} is not valid UTF-8; the chain "
                f"cannot be trusted past this point",
                record_index=record_index) from None

        out = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditError(
                    f"{self.path}:{lineno} is not valid JSON ({exc.msg}); the "
                    f"chain cannot be trusted past this point",
                    record_index=len(out)) from None
            if not isinstance(record, dict):
                raise AuditError(
                    f"{self.path}:{lineno} is not a JSON object; the chain "
                    f"cannot be trusted past this point",
                    record_index=len(out))
            out.append(record)
        return out

    def read_all(self):
        """Every record from one locked snapshot, in order."""
        with self._lock, audit_path_lock(self.path):
            return self._read_all_unlocked()

    @staticmethod
    def _verify_records(records):
        previous = GENESIS
        for index, record in enumerate(records):
            sequence = record.get("seq")
            if type(sequence) is not int or sequence != index:
                return ChainVerdict(False, index,
                                    f"record {index} has seq {sequence!r}")
            if record.get("previous") != previous:
                if index == 0:
                    return ChainVerdict(
                        False, index,
                        "record 0 does not point to the genesis hash")
                return ChainVerdict(
                    False, index,
                    f"record {index} does not chain to its predecessor")
            expected = digest(
                {key: value for key, value in record.items()
                 if key != "hash"}, previous)
            if record.get("hash") != expected:
                return ChainVerdict(False, index,
                                    f"record {index} has been altered")
            previous = record["hash"]
        return ChainVerdict(True, len(records), "chain verified")

    @property
    def store(self):
        """The absolute store path named in a verification report.

        A relative ``DRIVER_AUDIT_PATH`` is resolved against the caller's
        cwd, so the report cannot be mistaken for a different local store
        after a run from an empty directory. An in-memory log has no path and
        is named explicitly instead of looking like an on-disk chain.
        """
        return os.path.abspath(self.path) if self.path else "<memory>"

    def verify(self):
        """Re-hash one locked snapshot; return a verdict, never drift claims."""
        with self._lock, audit_path_lock(self.path):
            store = self.store
            try:
                records = self._read_all_unlocked()
            except AuditError as exc:
                index = (exc.record_index
                         if exc.record_index is not None else 0)
                return ChainVerdict(False, index, str(exc), store=store,
                                    head=None)
            verdict = self._verify_records(records)
            verdict.store = store
            verdict.head = (records[-1]["hash"] if records else GENESIS)
            if not verdict.ok:
                # A broken chain has no trusted head. Reporting the last
                # parsed hash would make tampering look like a shorter but
                # still identifiable chain.
                verdict.head = None
            return verdict


class AuditError(Exception):
    """The log is not a trustworthy chain, so it may not be extended."""

    def __init__(self, message, *, record_index=None):
        super().__init__(message)
        self.record_index = record_index


#: The description formula every MCP tool contract must follow (#284).
MCP_DESCRIPTION_LABELS = ("When:", "In:", "Out:")


def lint_tool_descriptions(schemas):
    """Enforce When / In / Out on every served MCP tool description.

    A description is the only documentation a model sees before choosing a
    tool, so it has to answer the same three questions in the same order:
    when to use the tool, what goes in, and what comes out. The check is
    deliberately literal -- labels, order and non-empty segments -- because
    a prose heuristic would let a description drift back to "does a thing"
    while still passing. Raises :class:`AuditError`: the catalog may not be
    served when its contracts do not state those three facts.
    """
    problems = []
    for schema in schemas:
        name = str(schema.get("name") or "<unnamed>")
        description = schema.get("description")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"{name}: description is missing")
            continue
        text = " ".join(description.split())
        if not text.startswith(MCP_DESCRIPTION_LABELS[0]):
            problems.append(f"{name}: description must start with When:")
        positions = []
        for label in MCP_DESCRIPTION_LABELS:
            index = text.find(label)
            if index < 0:
                problems.append(f"{name}: missing {label}")
                positions = None
                break
            positions.append(index)
        if positions is None:
            continue
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            problems.append(f"{name}: labels are out of order")
            continue
        for offset, label in enumerate(MCP_DESCRIPTION_LABELS):
            start = positions[offset] + len(label)
            end = (positions[offset + 1]
                   if offset + 1 < len(positions) else len(text))
            if not text[start:end].strip():
                problems.append(f"{name}: {label} is empty")
    if problems:
        raise AuditError("MCP tool description lint failed: " + "; ".join(problems))
    return len(schemas)


class ChainVerdict:
    __slots__ = ("ok", "records", "detail", "store", "head")

    def __init__(self, ok, records, detail, *, store="", head=None):
        self.ok = ok
        self.records = records
        self.detail = detail
        self.store = store
        self.head = head

    def to_dict(self):
        return {
            "ok": self.ok,
            "records": self.records,
            "detail": self.detail,
            "store": self.store,
            "store_scope": AUDIT_STORE_SCOPE,
            "head": self.head,
        }

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"ChainVerdict(ok={self.ok}, records={self.records})"


class MemoryAuditLog(AuditLog):
    """An in-memory log for tests and dry runs. Never touches the disk."""

    def __init__(self):
        super().__init__(path="")
        self.records = []

    def _write(self, record):
        self.records.append(dict(record))

    def _read_all_unlocked(self):
        return list(self.records)
