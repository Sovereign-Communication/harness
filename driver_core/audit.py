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
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone

#: The genesis constant. A chain's first record links to this rather than to
#: a magic empty string, so the linkage rule is uniform at every position.
GENESIS = "driver-core/audit/v1"

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

        A truncated final line (a crash mid-write) is reported rather than
        silently skipped, because a log with an unexplained hole is not
        evidence of what happened.
        """
        last, count = GENESIS, 0
        with open(self.path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AuditError(
                        f"{self.path}:{lineno} is not valid JSON ({exc}); the "
                        f"chain cannot be trusted past this point") from None
                if count and record.get("previous") != last:
                    raise AuditError(
                        f"{self.path}:{lineno} does not chain to its "
                        f"predecessor; the log has been altered")
                last = record["hash"]
                count += 1
        return last, count

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
        with self._lock:
            record = {
                "seq": self._count,
                "kind": kind,
                "at": _now(),
                "previous": self._last,
            }
            record.update(fields)
            record["hash"] = digest(
                {k: v for k, v in record.items() if k != "hash"}, self._last)
            self._last = record["hash"]
            self._count += 1
            self._write(record)
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

    def read_all(self):
        """Every record, in order."""
        if not self.path or not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify(self):
        """Re-hash the whole chain. Returns a verdict, never raises on drift."""
        previous = GENESIS
        for index, record in enumerate(self.read_all()):
            if record.get("seq") != index:
                return ChainVerdict(False, index,
                                    f"record {index} has seq "
                                    f"{record.get('seq')!r}")
            if record.get("previous") != previous:
                return ChainVerdict(
                    False, index,
                    f"record {index} does not chain to its predecessor")
            expected = digest(
                {k: v for k, v in record.items() if k != "hash"}, previous)
            if record.get("hash") != expected:
                return ChainVerdict(False, index,
                                    f"record {index} has been altered")
            previous = record["hash"]
        return ChainVerdict(True, len(self.read_all()), "chain verified")


class AuditError(Exception):
    """The log is not a trustworthy chain, so it may not be extended."""


class ChainVerdict:
    __slots__ = ("ok", "records", "detail")

    def __init__(self, ok, records, detail):
        self.ok = ok
        self.records = records
        self.detail = detail

    def to_dict(self):
        return {"ok": self.ok, "records": self.records, "detail": self.detail}

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

    def read_all(self):
        return list(self.records)
