"""Durable, bounded replay protection for REST-issued consent IDs."""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager

from .errors import PersistentStateUnavailable


MAX_RECORDS = 16_384
_BUSY_TIMEOUT_SECONDS = 5
_REST_CONSENT_SCOPE = hashlib.sha256(
    b"driver-core/rest-consent-ids/v1").digest()


class ConsentLedgerError(ValueError):
    """A consent ID cannot be safely accepted or checked."""


class ConsentLedgerFull(PersistentStateUnavailable):
    """The ledger reached its fixed capacity and refuses new IDs."""


class ConsentLedgerUnavailable(PersistentStateUnavailable):
    """Persistent replay state could not be read or durably changed."""


class RestConsentLedger:
    """Store fixed-size grant fingerprints and states in a SQLite sidecar.

    A row is never evicted: doing so would eventually make an old spent grant
    replayable. At capacity the ledger refuses new IDs. The bearer token and
    consent ID are stored only as SHA-256 digests; action parameters are
    represented by a fixed-size digest as well. The ID namespace is stable
    across service restarts and bearer-token rotation.
    """

    def __init__(self, path, *, max_records=MAX_RECORDS):
        if not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.path = os.path.abspath(os.fspath(path))
        self.scope = _REST_CONSENT_SCOPE
        self.max_records = max_records

    def bind(self, consent_id, grant):
        """Bind an ID to one exact REST grant and return its execution guard."""
        id_hash = hashlib.sha256(consent_id.encode("utf-8")).digest()
        material = json.dumps(
            grant, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        grant_hash = hashlib.sha256(material).digest()

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT grant_hash FROM rest_consent_ids "
                "WHERE scope = ? AND consent_id = ?",
                (self.scope, id_hash)).fetchone()
            if row is not None:
                if row[0] != grant_hash:
                    raise ConsentLedgerError(
                        "consent_id is already bound to a different grant")
            else:
                count = connection.execute(
                    "SELECT COUNT(*) FROM rest_consent_ids").fetchone()[0]
                if count >= self.max_records:
                    raise ConsentLedgerFull(
                        "persistent REST consent ledger is full; "
                        "new consent IDs are refused")
                connection.execute(
                    "INSERT INTO rest_consent_ids "
                    "(scope, consent_id, grant_hash, state) "
                    "VALUES (?, ?, ?, 'available')",
                    (self.scope, id_hash, grant_hash))
        return _ReplayGuard(self, id_hash, grant_hash)

    def _available(self, id_hash, grant_hash):
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, grant_hash FROM rest_consent_ids "
                "WHERE scope = ? AND consent_id = ?",
                (self.scope, id_hash)).fetchone()
            return row is not None and row == ("available", grant_hash)

    def _claim(self, id_hash, grant_hash):
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE rest_consent_ids SET state = 'pending' "
                "WHERE scope = ? AND consent_id = ? AND grant_hash = ? "
                "AND state = 'available'",
                (self.scope, id_hash, grant_hash))
            return result.rowcount == 1

    def _finish(self, id_hash, grant_hash, *, succeeded):
        state = "spent" if succeeded else "available"
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE rest_consent_ids SET state = ? "
                "WHERE scope = ? AND consent_id = ? AND grant_hash = ? "
                "AND state = 'pending'",
                (state, self.scope, id_hash, grant_hash))
            if result.rowcount != 1:
                raise ConsentLedgerUnavailable(
                    "persistent REST consent state changed unexpectedly")

    @contextmanager
    def _transaction(self):
        connection = None
        committed = False
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            connection = sqlite3.connect(
                self.path, timeout=_BUSY_TIMEOUT_SECONDS)
            connection.execute(
                f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_SECONDS * 1000}")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS rest_consent_ids ("
                "scope BLOB NOT NULL CHECK(length(scope) = 32), "
                "consent_id BLOB NOT NULL CHECK(length(consent_id) = 32), "
                "grant_hash BLOB NOT NULL CHECK(length(grant_hash) = 32), "
                "state TEXT NOT NULL CHECK(state IN "
                "('available', 'pending', 'spent')), "
                "PRIMARY KEY(scope, consent_id))")
            yield connection
            connection.commit()
            committed = True
        except (OSError, sqlite3.Error):
            raise ConsentLedgerUnavailable(
                "persistent REST consent ledger is unavailable") from None
        finally:
            if connection is not None:
                if not committed:
                    connection.rollback()
                connection.close()


class _ReplayGuard:
    """Atomic claim/release hook attached to a REST Consent."""

    def __init__(self, ledger, id_hash, grant_hash):
        self._ledger = ledger
        self._id_hash = id_hash
        self._grant_hash = grant_hash

    def available(self):
        return self._ledger._available(self._id_hash, self._grant_hash)

    def claim(self):
        return self._ledger._claim(self._id_hash, self._grant_hash)

    def finish(self, *, succeeded):
        self._ledger._finish(
            self._id_hash, self._grant_hash, succeeded=succeeded)
