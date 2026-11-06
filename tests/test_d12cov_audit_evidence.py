"""D12 coverage tests: audit/evidence branches the main suites never reach.

Each test asserts the branch's real outcome (value, exception, or recorded
state), not mere execution.  Everything is hermetic: git fixtures and state
databases live in temp dirs, resolvers and policies are local fakes, and the
egress proxy is exercised with scripted fake sockets -- no network, screen,
or browser.
"""
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import osal, session, site_evidence, source_audit
from harness import citation_verify as cv
from harness.citation_verify import (
    CitationRecord, SourceRecord, assess_claim, verify_citation,
)
from harness.errors import HarnessError
from harness.jev import jev_cost
from harness.jev_policy import JEV_MAX_INPUT_TOKENS
from harness.source_audit import SourceAuditError

DOI = "10.1234/abc"
META = {
    "doi": DOI,
    "title": "A Study of Things",
    "authors": ["Ada Lovelace", "Grace Hopper"],
    "year": 2024,
    "venue": "Journal of Tests",
    "version": "published",
}
SOURCE = {
    "source_id": "src-1",
    "url": "https://example.test/paper",
    "source_class": "journal",
    "retrieved_at": "2026-11-04T00:00:00Z",
    "doi": DOI,
    "locator": "results, paragraph 3",
    "passage": "The measured effect was 12 percent across all cohorts.",
    "request_id": "req-1",
}


def _found_resolver(metadata=None, **extra):
    def _resolve(doi):
        result = {"found": True, "doi": DOI,
                  "metadata": dict(META if metadata is None else metadata),
                  "metadata_source": "crossref:fixture"}
        result.update(extra)
        return result
    return _resolve


class CitationVerifyCoverageTests(unittest.TestCase):
    def test_authors_supplied_as_one_string_are_split_and_normalized(self):
        record = CitationRecord.from_mapping({
            "citation_id": "ref-str", "doi": DOI,
            "authors": "Grace Hopper; Ada Lovelace, Alan Turing",
        })
        self.assertEqual(record.authors,
                         ["ada lovelace", "alan turing", "grace hopper"])

    def test_year_normalization_absent_and_unparseable_forms(self):
        self.assertIsNone(cv._norm_year(None))
        self.assertIsNone(cv._norm_year(""))
        self.assertEqual(cv._norm_year("n.d."), "n.d.")
        result = verify_citation(
            {"citation_id": "ref-year", "doi": DOI, "year": "n.d."},
            _found_resolver({"year": "N.D."}))
        self.assertEqual(result["status"], "supported", result)
        self.assertTrue(result["checks"]["year"]["match"])

    def test_citation_must_be_an_object(self):
        with self.assertRaisesRegex(HarnessError, "citation must be an object"):
            CitationRecord.from_mapping(["not", "a", "mapping"])

    def test_source_must_be_an_object(self):
        with self.assertRaisesRegex(HarnessError, "source must be an object"):
            SourceRecord.from_mapping("not-a-mapping")

    def test_source_metadata_must_be_an_object(self):
        raw = dict(SOURCE, metadata=["not", "a", "mapping"])
        with self.assertRaisesRegex(HarnessError, "metadata must be an object"):
            SourceRecord.from_mapping(raw)

    def test_missing_resolver_is_recorded_not_raised(self):
        result = verify_citation({"citation_id": "ref-1", "doi": DOI,
                                  "title": "A Study of Things"})
        self.assertEqual(result["status"], "not_verifiable")
        self.assertEqual(result["metadata_source"], "injected-resolver")
        self.assertIn("resolver_error: no DOI resolver was supplied",
                      result["reasons"])

    def test_resolver_object_with_resolve_method_is_used(self):
        seen = []

        class Resolver:
            def resolve(self, doi):
                seen.append(doi)
                return {"found": True, "doi": DOI, "metadata": dict(META),
                        "metadata_source": "object:fixture"}

        citation = {"citation_id": "ref-1", "doi": DOI,
                    "title": "A Study of Things",
                    "authors": ["Ada Lovelace", "Grace Hopper"], "year": 2024,
                    "venue": "Journal of Tests", "version": "published"}
        result = verify_citation(citation, Resolver())
        self.assertEqual(seen, [DOI])
        self.assertEqual(result["status"], "supported", result)
        self.assertEqual(result["metadata_source"], "object:fixture")

    def test_resolver_returning_none_means_not_found(self):
        result = verify_citation({"citation_id": "ref-1", "doi": DOI},
                                 lambda doi: None)
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("doi_not_found", result["reasons"])

    def test_resolver_returning_a_non_object_is_an_error(self):
        result = verify_citation({"citation_id": "ref-1", "doi": DOI},
                                 lambda doi: "plain text")
        self.assertEqual(result["status"], "not_verifiable")
        self.assertTrue(any(
            reason.startswith(
                "resolver_error: resolver returned a non-object result")
            for reason in result["reasons"]), result)

    def test_resolver_metadata_must_be_an_object(self):
        result = verify_citation(
            {"citation_id": "ref-1", "doi": DOI},
            lambda doi: {"found": True, "doi": DOI, "metadata": "nope"})
        self.assertEqual(result["status"], "not_verifiable")
        self.assertEqual(result["reasons"], ["resolver_metadata_not_an_object"])

    def test_citation_with_a_doi_but_no_metadata_is_absent(self):
        result = verify_citation({"citation_id": "ref-bare", "doi": DOI},
                                 _found_resolver())
        self.assertEqual(result["status"], "not_verifiable")
        self.assertEqual(result["reasons"], ["metadata_absent"])

    def test_claim_must_be_an_object(self):
        with self.assertRaisesRegex(HarnessError, "claim must be an object"):
            assess_claim("not-a-mapping", {})

    def test_claim_source_ids_accept_a_single_string(self):
        record = SourceRecord.from_mapping(SOURCE)
        result = assess_claim(
            {"claim_id": "claim-1", "source_ids": "src-1"},
            {"src-1": record}, semantic={"status": "supported"})
        self.assertEqual(result["source_ids"], ["src-1"])
        self.assertEqual(result["status"], "supported", result)

    def test_claim_without_sources_is_a_hard_gap(self):
        result = assess_claim({"claim_id": "claim-1"}, {})
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("no_source_records", result["reasons"])

    def test_semantic_status_outside_the_closed_set_is_invalid(self):
        record = SourceRecord.from_mapping(SOURCE)
        result = assess_claim(
            {"claim_id": "claim-1", "source_ids": ["src-1"]},
            {"src-1": record}, semantic={"status": "definitely"})
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("semantic_status_invalid", result["reasons"])

    def test_semantic_not_verifiable_is_a_hard_gap(self):
        record = SourceRecord.from_mapping(SOURCE)
        result = assess_claim(
            {"claim_id": "claim-1", "source_ids": ["src-1"]},
            {"src-1": record}, semantic={"status": "not_verifiable"})
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("semantic_not_verifiable", result["reasons"])


class _FakeSocket:
    """A scripted socket stand-in: recv replays chunks, sendall records."""

    def __init__(self, chunks=(), *, close_error=None, timeout_error=None):
        self._chunks = list(chunks)
        self.sent = []
        self.closed = False
        self._close_error = close_error
        self._timeout_error = timeout_error

    def settimeout(self, _seconds):
        if self._timeout_error is not None:
            raise self._timeout_error

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _bare_proxy():
    proxy = object.__new__(site_evidence._ValidatedEgressProxy)
    proxy._resolve = lambda host, port, **_kw: []
    proxy._connect = lambda address, timeout=None: _FakeSocket()
    return proxy


class SiteEvidenceCoverageTests(unittest.TestCase):
    def test_dns_failure_is_a_refusal_with_the_cause_attached(self):
        def failing_resolver(host, port, **_kw):
            raise OSError("name or service not known")

        with self.assertRaises(HarnessError) as caught:
            site_evidence._public_addresses(
                "bad.test", 443, resolve=failing_resolver)
        self.assertIn("DNS resolution failed for 'bad.test'",
                      str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_split_authority_edges(self):
        with self.assertRaisesRegex(ValueError, "unterminated IPv6 literal"):
            site_evidence._split_authority("[2001:db8::1")
        with self.assertRaisesRegex(ValueError, "malformed authority"):
            site_evidence._split_authority("[2001:db8::1]garbage")
        self.assertEqual(site_evidence._split_authority("example.test"),
                         ("example.test", 443))
        self.assertEqual(site_evidence._split_authority("[::1]:8443"),
                         ("::1", 8443))

    def test_read_head_returns_none_when_the_peer_sends_nothing(self):
        self.assertIsNone(site_evidence._ValidatedEgressProxy._read_head(
            _FakeSocket()))

    def test_read_head_rejects_an_oversized_head(self):
        client = _FakeSocket([b"x" * 4096] * 20)
        with self.assertRaisesRegex(ValueError, "request head too large"):
            site_evidence._ValidatedEgressProxy._read_head(client)

    def test_handle_ignores_a_peer_that_sends_nothing(self):
        client = _FakeSocket()
        self.assertIsNone(_bare_proxy()._handle(client))
        self.assertEqual(client.sent, [])
        self.assertTrue(client.closed)

    def test_handle_refuses_a_malformed_connect_destination(self):
        client = _FakeSocket(
            [b"CONNECT [::1 HTTP/1.1\r\nHost: x\r\n\r\n"])
        self.assertIsNone(_bare_proxy()._handle(client))
        reply = b"".join(client.sent)
        self.assertIn(b"403", reply)
        self.assertIn(b"malformed destination", reply)

    def test_handle_swallows_handler_errors_and_still_closes(self):
        client = _FakeSocket(timeout_error=RuntimeError("timer exploded"))
        self.assertIsNone(_bare_proxy()._handle(client))
        self.assertTrue(client.closed)

    def test_handle_tolerates_a_failing_socket_close(self):
        client = _FakeSocket(close_error=OSError("close exploded"))
        self.assertIsNone(_bare_proxy()._handle(client))
        self.assertTrue(client.closed)

    def test_close_tolerates_a_server_close_error(self):
        class BrokenServer:
            def close(self):
                raise OSError("already gone")

        proxy = _bare_proxy()
        proxy._closed = threading.Event()
        proxy._server = BrokenServer()
        proxy._thread = SimpleNamespace(join=lambda timeout=None: None)
        proxy._workers = []
        self.assertIsNone(proxy.close())
        self.assertTrue(proxy._closed.is_set())

    def test_serve_exits_when_accept_fails(self):
        def accept():
            raise OSError("listener gone")

        proxy = _bare_proxy()
        proxy._closed = threading.Event()
        proxy._server = SimpleNamespace(accept=accept)
        self.assertIsNone(proxy._serve())

    def test_relay_returns_when_select_fails(self):
        with mock.patch.object(site_evidence.select, "select",
                               side_effect=OSError("select exploded")):
            self.assertIsNone(site_evidence._ValidatedEgressProxy._relay(
                _FakeSocket(), _FakeSocket()))

    def test_relay_returns_on_an_errored_socket(self):
        client, upstream = _FakeSocket(), _FakeSocket()
        with mock.patch.object(
                site_evidence.select, "select",
                return_value=([], [], [client])):
            self.assertIsNone(site_evidence._ValidatedEgressProxy._relay(
                client, upstream))

    def test_relay_returns_when_both_sides_go_idle(self):
        with mock.patch.object(
                site_evidence.select, "select",
                return_value=([], [], [])):
            self.assertIsNone(site_evidence._ValidatedEgressProxy._relay(
                _FakeSocket(), _FakeSocket()))

    def test_relay_forwards_bytes_to_the_peer(self):
        client = _FakeSocket([b"ping"])
        upstream = _FakeSocket()
        reads = [([client], [], []), ([upstream], [], [])]
        with mock.patch.object(site_evidence.select, "select",
                               side_effect=reads):
            self.assertIsNone(site_evidence._ValidatedEgressProxy._relay(
                client, upstream))
        self.assertEqual(upstream.sent, [b"ping"])

    def test_guard_survives_route_and_abort_failures(self):
        class ExplodingRoute:
            @property
            def request(self):
                raise RuntimeError("request unavailable")

            def abort(self, _reason=None):
                raise RuntimeError("abort unavailable")

            def continue_(self):
                raise AssertionError("must not continue")

        guard = site_evidence._request_guard()
        self.assertIsNone(guard(ExplodingRoute()))


def _pack(pack_id):
    return {
        "id": pack_id,
        "axes": {
            "finding": {
                "instructions": "Classify the source chunk.",
                "criteria": {
                    "lead": "Potential issue lead requiring human review.",
                    "none": "No issue lead was observed.",
                },
            },
        },
        "score": {
            "id": "attention",
            "instructions": "Rate the attention warranted by this chunk.",
            "levels": ["low", "medium", "high"],
        },
        "nouls": {
            "actionable": {
                "instructions": "Whether this chunk contains evidence.",
                "true": "actionable",
                "false": "not_actionable",
            },
        },
    }


class _FakePolicy:
    keyed = True

    def __init__(self, *, interrupt_first=False):
        self.calls = []
        self.interrupt_first = interrupt_first

    def evaluate_repo_summary(self, state, pack, *, site, task_id, node_id,
                              max_input_tokens):
        self.calls.append(node_id)
        if self.interrupt_first and len(self.calls) == 1:
            raise KeyboardInterrupt()
        structural = {"site": site, "model": "fixture-model", "cost": 0.001,
                      "input_tokens": 10, "output_tokens": 2,
                      "discarded": False}
        judgment = {"axes": {"finding": "lead"},
                    "attention": {"level": "medium", "value": 0.7,
                                  "confidence": 0.8},
                    "nouls": {"actionable": 0.7}, "pack_id": pack["id"],
                    "is_fallback": False}
        return SimpleNamespace(), structural, judgment


IDENTITY = {
    "chunk_id": "chunk-1", "pass": "code", "path": "src/app.py",
    "source_hash": "a" * 64, "chunk_hash": "b" * 64,
    "line_start": 1, "line_end": 2,
    "pack_id": "code-pack-v1", "pack_hash": "c" * 64,
}


class SourceAuditPathLockExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_relative_output_refuses_to_escape_the_repository(self):
        with self.assertRaisesRegex(
                SourceAuditError, "must stay inside the repository"):
            source_audit._relative_output(self.root, "../outside.txt")

    def test_lock_path_cannot_be_a_symlink(self):
        target = self.root / "target.bin"
        target.write_bytes(b"x")
        os.symlink(str(target), str(self.root / "state.sqlite3.lock"))
        with self.assertRaisesRegex(
                SourceAuditError, "cannot be a symlink or reparse point"):
            source_audit._acquire_source_audit_lock(
                self.root / "state.sqlite3")

    def test_lock_contention_is_a_source_audit_error(self):
        lock_path = self.root / "state.sqlite3.lock"
        fd = osal.acquire_file_lock(str(lock_path))
        try:
            with self.assertRaisesRegex(
                    SourceAuditError, "cannot acquire source audit lock"):
                source_audit._acquire_source_audit_lock(
                    self.root / "state.sqlite3")
        finally:
            osal.release_file_lock(fd)

    def test_generated_directory_is_excluded_before_any_read(self):
        reason = source_audit._exclusion_reason(
            "build/output.py", Path("never-read"), set(), set())
        self.assertEqual(reason, "generated_artifact")

    def test_generated_suffix_is_excluded_before_any_read(self):
        reason = source_audit._exclusion_reason(
            "src/debug.log", Path("never-read"), set(), set())
        self.assertEqual(reason, "generated_artifact")

    def test_symlink_is_excluded_as_a_link_or_reparse_point(self):
        target = self.root / "real.py"
        target.write_text("x = 1\n", encoding="utf-8")
        link = self.root / "link.py"
        os.symlink(str(target), str(link))
        reason = source_audit._exclusion_reason(
            "link.py", link, set(), set())
        self.assertEqual(reason, "link_or_reparse_point")


class SourceAuditStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "state.sqlite3"
        self.db = source_audit._open_state(self.state_path)
        self.addCleanup(self.db.close)

    def _insert(self, chunk_id, status, payload_text):
        self.db.execute(
            "INSERT INTO chunks(chunk_id,status,payload) VALUES(?,?,?)",
            (chunk_id, status, payload_text))
        self.db.commit()

    def test_open_state_survives_a_failing_close(self):
        class BrokenConnection:
            def execute(self, *_args):
                raise sqlite3.OperationalError("execute failed")

            def close(self):
                raise sqlite3.OperationalError("close failed")

        with mock.patch.object(source_audit.sqlite3, "connect",
                               return_value=BrokenConnection()):
            with self.assertRaisesRegex(
                    SourceAuditError, "cannot open source audit resume state"):
                source_audit._open_state(
                    Path(self.temp.name) / "other.sqlite3")

    def test_state_cost_rejects_bad_values_and_accepts_good_ones(self):
        for bad in ("soon", True, 10 ** 400, -0.5, float("inf")):
            with self.subTest(bad=repr(bad)):
                with self.assertRaisesRegex(
                        SourceAuditError, "resume state is corrupt"):
                    source_audit._state_cost({"k": bad}, "k")
        self.assertEqual(source_audit._state_cost({"k": 2}, "k"), 2.0)
        self.assertEqual(source_audit._state_cost({}, "k", 1.25), 1.25)

    def test_claim_on_a_fresh_chunk_inserts_and_returns_in_flight(self):
        status, payload, claimed = source_audit._state_claim(
            self.db, "chunk-9", dict(IDENTITY, chunk_id="chunk-9"))
        self.assertTrue(claimed)
        self.assertEqual(status, "in_flight")
        self.assertEqual(payload["path"], "src/app.py")
        stored = source_audit._state_get(self.db, "chunk-9")
        self.assertEqual(stored["status"], "in_flight")

    def test_claim_refuses_a_pending_row_with_a_foreign_identity(self):
        self._insert("chunk-1", "pending",
                     json.dumps({"chunk_id": "chunk-1", "pass": "docs"}))
        with self.assertRaisesRegex(
                SourceAuditError, "resume state is corrupt"):
            source_audit._state_claim(self.db, "chunk-1", IDENTITY)

    def test_claim_rereads_the_row_when_a_peer_finishes_it_first(self):
        class ConcurrentFinishConnection(sqlite3.Connection):
            finish_payload = None

            def execute(self, sql, parameters=()):
                if (isinstance(sql, str)
                        and sql.startswith(
                            "UPDATE chunks SET status='in_flight'")
                        and self.finish_payload is not None):
                    super().execute(
                        "UPDATE chunks SET status='complete',payload=? "
                        "WHERE chunk_id=?",
                        (self.finish_payload, parameters[1]))
                return super().execute(sql, parameters)

        path = Path(self.temp.name) / "raced.sqlite3"
        source_audit._open_state(path).close()
        db = sqlite3.connect(str(path), factory=ConcurrentFinishConnection)
        self.addCleanup(db.close)
        db.execute(
            "INSERT INTO chunks(chunk_id,status,payload) VALUES(?,?,?)",
            ("chunk-1", "pending",
             json.dumps({**IDENTITY, "unknown_prior_spend_usd": 0.0})))
        db.commit()
        db.finish_payload = json.dumps(
            {**IDENTITY, "status": "complete", "cost_usd": 0.0,
             "reserved_usd": 0.0, "unknown_prior_spend_usd": 0.0},
            sort_keys=True)
        status, payload, claimed = source_audit._state_claim(
            db, "chunk-1", IDENTITY)
        self.assertEqual(status, "complete")
        self.assertFalse(claimed)
        self.assertEqual(payload["cost_usd"], 0.0)

    def test_requeue_reports_a_missing_chunk(self):
        result = source_audit._state_requeue_pending(
            self.db, "absent", IDENTITY)
        self.assertEqual(result, ("missing", None, False))

    def test_requeue_refuses_corrupt_json_with_the_cause_attached(self):
        self._insert("chunk-1", "failed", "not json {")
        with self.assertRaisesRegex(
                SourceAuditError, "resume state is corrupt") as caught:
            source_audit._state_requeue_pending(self.db, "chunk-1", IDENTITY)
        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_requeue_refuses_a_foreign_identity(self):
        self._insert("chunk-1", "failed", json.dumps({"pass": "docs"}))
        with self.assertRaisesRegex(
                SourceAuditError, "resume state is corrupt") as caught:
            source_audit._state_requeue_pending(self.db, "chunk-1", IDENTITY)
        self.assertIsNone(caught.exception.__cause__)

    def test_requeue_leaves_a_completed_chunk_untouched(self):
        self._insert("chunk-1", "complete", json.dumps(IDENTITY))
        status, payload, changed = source_audit._state_requeue_pending(
            self.db, "chunk-1", IDENTITY)
        self.assertEqual(status, "complete")
        self.assertFalse(changed)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["pack_id"], "code-pack-v1")

    def test_requeue_surfaces_sqlite_failures(self):
        self.db.execute("DROP TABLE chunks")
        self.db.commit()
        with self.assertRaisesRegex(
                SourceAuditError, "cannot requeue source audit chunk") as caught:
            source_audit._state_requeue_pending(self.db, "chunk-1", IDENTITY)
        self.assertIsInstance(caught.exception.__cause__, sqlite3.Error)

    def test_aggregate_leads_rejects_unknown_disposition_ids(self):
        with self.assertRaisesRegex(
                SourceAuditError, "dispositions contain unknown lead ids"):
            source_audit._aggregate_leads([], {}, {"ghost": "verified"})

    def test_recover_interrupted_must_be_a_boolean(self):
        with self.assertRaisesRegex(
                SourceAuditError, "recover_interrupted must be a boolean"):
            source_audit.run_source_audit(
                self.state_path.parent, {}, lambda spent: None,
                state_path="state.sqlite3", run_budget=1.0,
                recover_interrupted="yes")


class SourceAuditRepoRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        for args in (["git", "init", "-q"],
                     ["git", "config", "user.name", "Coverage Test"],
                     ["git", "config", "user.email", "cov@example.invalid"]):
            subprocess.run(args, cwd=str(self.root), check=True)
        self._write("src/app.py", "def safe():\n    return 1\n")
        self._write("src/binary.py", b"x = 1\n\x00\n")
        self._write("docs/readme.md", "# Docs\nA safe fixture.\n")
        self._write("evidence/events.jsonl", '{"event":"fixture"}\n')
        self._write("notes.unknown", "unsupported content\n")
        subprocess.run(["git", "add", "."], cwd=str(self.root), check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"],
                       cwd=str(self.root), check=True)
        self.packs = {"code": _pack("code-pack-v1"),
                      "docs": _pack("docs-pack-v1"),
                      "evidence": _pack("evidence-pack-v1")}

    def _write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    def _run(self, policy, **kwargs):
        return source_audit.run_source_audit(
            self.root, self.packs, lambda _spent: policy,
            state_path=self.root / ".harness" / "state.sqlite3",
            run_budget=1.0, chunk_chars=512,
            task_id="d12-coverage-fixture", **kwargs)

    def test_manifest_marks_unsupported_file_types(self):
        manifest = source_audit.build_manifest(
            self.root, self.packs, chunk_chars=512)
        entries = {row["path"]: row for row in manifest["files"]}
        self.assertEqual(entries["notes.unknown"]["reason"],
                         "unsupported_file_type")
        self.assertFalse(entries["notes.unknown"]["included"])
        self.assertEqual(entries["src/binary.py"]["reason"], "binary_file")
        self.assertFalse(entries["src/binary.py"]["included"])
        self.assertTrue(entries["src/app.py"]["included"])
        self.assertEqual(entries["docs/readme.md"]["pass"], "docs")
        self.assertEqual(entries["evidence/events.jsonl"]["pass"],
                         "evidence")

    def test_interrupted_chunk_is_listed_pending_with_a_resume_cursor(self):
        with self.assertRaises(KeyboardInterrupt):
            self._run(_FakePolicy(interrupt_first=True))
        policy = _FakePolicy()
        report = self._run(policy)
        coverage = report["coverage"]
        self.assertEqual(coverage["stop_reason"], "resume_pending")
        self.assertEqual(coverage["pending_chunks"], 1)
        self.assertIsNotNone(coverage["resume_cursor"])
        self.assertEqual(coverage["resume_cursor"]["path"],
                         "docs/readme.md")
        self.assertEqual(len(policy.calls), 2)
        self.assertAlmostEqual(
            report["spend"]["pending_possible_spend_usd"],
            jev_cost(JEV_MAX_INPUT_TOKENS))

    def test_chunk_with_an_unknown_stored_status_is_listed_pending(self):
        manifest = source_audit.build_manifest(
            self.root, self.packs, chunk_chars=512)
        entry = {row["path"]: row for row in manifest["files"]}[
            "docs/readme.md"]
        text = (self.root / "docs" / "readme.md").read_text(
            encoding="utf-8")
        chunk_text, line_start, line_end = list(
            source_audit._iter_text_chunks(text, 512))[0]
        chunk_id = source_audit._chunk_id(
            "docs", entry["path"], entry["source_hash"],
            source_audit.hashlib.sha256(
                chunk_text.encode("utf-8")).hexdigest(),
            line_start, line_end,
            manifest["passes"]["docs"]["pack_hash"], 512)
        state_file = self.root / ".harness" / "state.sqlite3"
        db = source_audit._open_state(state_file)
        db.execute(
            "INSERT INTO chunks(chunk_id,status,payload) VALUES(?,?,?)",
            (chunk_id, "archived", json.dumps({"chunk_id": chunk_id})))
        db.commit()
        db.close()

        policy = _FakePolicy()
        report = self._run(policy)
        coverage = report["coverage"]
        self.assertEqual(coverage["stop_reason"], "resume_pending")
        self.assertEqual(coverage["pending_chunks"], 1)
        self.assertIsNotNone(coverage["resume_cursor"])
        self.assertEqual(coverage["resume_cursor"]["chunk_id"], chunk_id)
        self.assertEqual(len(policy.calls), 2)
        self.assertEqual(len(report["receipts"]), 2)

    def test_chunk_finished_by_another_invocation_is_reused_from_the_claim(
            self):
        real_claim = source_audit._state_claim

        def claim_as_finished(db, chunk_id, identity, **kwargs):
            stored = {
                **identity, "status": "complete", "cost_usd": 0.002,
                "reserved_usd": 0.0, "unknown_prior_spend_usd": 0.0,
                "axes": {"finding": "lead"},
                "attention": {"level": "medium", "value": 0.7,
                              "confidence": 0.8},
                "nouls": {"actionable": 0.7},
                "is_fallback": False, "discarded": False,
            }
            db.execute(
                "INSERT OR REPLACE INTO chunks(chunk_id,status,payload) "
                "VALUES(?,?,?)",
                (chunk_id, "complete", json.dumps(stored, sort_keys=True)))
            db.commit()
            return real_claim(db, chunk_id, identity, **kwargs)

        policy = _FakePolicy()
        with mock.patch.object(source_audit, "_state_claim",
                               side_effect=claim_as_finished):
            report = self._run(policy)
        self.assertEqual(policy.calls, [])
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(report["spend"]["calls_this_run"], 0)
        self.assertEqual(len(report["receipts"]), 3)
        self.assertAlmostEqual(report["spend"]["actual_spend_usd"], 0.006)

    def test_policy_factory_returns_none_when_no_governor_is_available(self):
        settings = SimpleNamespace(jev_api_key="fixture-key",
                                   max_cost=100.0)
        with mock.patch.object(session, "jev_face_governor",
                               return_value=None):
            factory = source_audit.source_audit_policy_factory(
                settings, 50.0, ledger=None)
            self.assertIsNone(factory(0.0))

    def test_write_report_failure_with_an_unremovable_temp_is_reported(self):
        def replace_then_remove(src, _dst):
            Path(src).unlink()
            raise OSError("simulated replace failure")

        target = Path(self.temp.name) / "report.json"
        with mock.patch.object(source_audit.os, "replace",
                               side_effect=replace_then_remove):
            with self.assertRaisesRegex(
                    SourceAuditError, "cannot write source audit report"):
                source_audit.write_report({"ok": True}, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
