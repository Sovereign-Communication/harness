"""Deterministic safety, resume, and receipt tests for issue #423."""
import json
import os
import stat
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from harness import source_audit
from harness import cli
from harness import cli_parser
from harness.errors import HarnessError
from harness.source_audit import SourceAuditError
from harness.jev import jev_cost
from harness.jev_policy import JEV_MAX_INPUT_TOKENS


def _pack(pack_id):
    return {
        "id": pack_id,
        "axes": {
            "finding": {
                "instructions": "Classify the source chunk using the declared choices.",
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
                "instructions": "Whether this chunk contains actionable evidence.",
                "true": "actionable",
                "false": "not_actionable",
            },
        },
    }


class FakePolicy:
    keyed = True

    def __init__(self, *, interrupt_first=False, interrupt_at=None, fail_at=None):
        self.calls = []
        self.interrupt_first = interrupt_first
        self.interrupt_at = interrupt_at
        self.fail_at = fail_at

    def evaluate_repo_summary(self, state, pack, *, site, task_id, node_id,
                              max_input_tokens):
        self.calls.append({"state": state, "site": site, "node_id": node_id})
        if ((self.interrupt_first and len(self.calls) == 1)
                or len(self.calls) == self.interrupt_at):
            raise KeyboardInterrupt()
        if len(self.calls) == self.fail_at:
            raise RuntimeError("fixture provider failure")
        fallback = state["path"].endswith("fallback.py")
        discarded = state["path"].endswith("discarded.py")
        structural = {
            "site": site,
            "model": "fixture-model",
            "cost": 0.001,
            "input_tokens": 123,
            "output_tokens": 17,
            "discarded": discarded,
        }
        judgment = {
            "axes": {"finding": "lead"},
            "attention": {"level": "medium", "value": 0.7,
                          "confidence": 0.8},
            "nouls": {"actionable": 0.7},
            "pack_id": pack["id"],
            "is_fallback": fallback,
        }
        return SimpleNamespace(), structural, judgment


class SourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(self.root), check=True)
        subprocess.run(["git", "config", "user.name", "Source Audit Test"],
                       cwd=str(self.root), check=True)
        subprocess.run(["git", "config", "user.email", "source-audit@example.invalid"],
                       cwd=str(self.root), check=True)
        self._write(".gitignore", ".env.local\nconfig/ignored.json\n.harness/\n")
        self._write("src/app.py", "def safe():\n    return 'source sentinel'\n")
        self._write("src/fallback.py", "def fallback():\n    return None\n")
        self._write("src/discarded.py", "def discarded():\n    return None\n")
        self.test_api_key = "sk-" + ("A" * 32)
        self._write("src/provider.py",
                    'OPENAI_API_KEY = "{}"\n'.format(self.test_api_key))
        self._write("docs/readme.md", "# Documentation\nA safe fixture.\n")
        self._write("evidence/events.jsonl", '{"event":"fixture"}\n')
        self._write("secrets/token.txt", "SOURCE_AUDIT_SECRET_SENTINEL")
        self._write(".env.local", "ENV_AUDIT_SECRET_SENTINEL")
        self._write("config/ignored.json", '{"ignored":true}\n')
        self._write("assets/image.png", b"\x89PNG\r\n\x1a\n")
        self._write("generated/build.py", "GENERATED_AUDIT_SENTINEL\n")
        self._write("notes.unknown", "unsupported content\n")
        subprocess.run(["git", "add", ".gitignore", "src", "docs", "evidence",
                        "secrets", "assets", "generated", "config", "notes.unknown"],
                       cwd=str(self.root), check=True)
        subprocess.run(["git", "add", "-f", "config/ignored.json"],
                       cwd=str(self.root), check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"],
                       cwd=str(self.root), check=True)
        self.packs = {
            "code": _pack("code-pack-v1"),
            "docs": _pack("docs-pack-v1"),
            "evidence": _pack("evidence-pack-v1"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    def _run(self, policy, *, dispositions=None, run_budget=0.1,
             recover_interrupted=False, state_name="source-audit.sqlite3",
             make_policy=None):
        policy_factory = make_policy or (lambda _spent: policy)
        return source_audit.run_source_audit(
            self.root, self.packs, policy_factory,
            state_path=self.root / ".harness" / state_name,
            run_budget=run_budget, chunk_chars=512,
            dispositions=dispositions, task_id="source-audit-fixture",
            recover_interrupted=recover_interrupted)

    def test_manifest_is_deterministic_and_excludes_sensitive_ignored_binary_generated(self):
        read_paths = []
        original = source_audit._read_text

        def record_reads(path):
            # macOS may spell a temp path as /var or /private/var; Windows
            # may use an 8.3 alias. Compare resolved identities, not aliases.
            read_paths.append(
                Path(path).resolve().relative_to(self.root.resolve()).as_posix())
            return original(path)

        with patch.object(source_audit, "_read_text", side_effect=record_reads):
            first = source_audit.build_manifest(
                self.root, self.packs, chunk_chars=512,
                exclude=[self.root / ".harness" / "source-audit.sqlite3"])
            second = source_audit.build_manifest(
                self.root, self.packs, chunk_chars=512,
                exclude=[self.root / ".harness" / "source-audit.sqlite3"])

        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        entries = {row["path"]: row for row in first["files"]}
        self.assertEqual(entries["src/app.py"]["pass"], "code")
        self.assertEqual(entries["src/provider.py"]["reason"],
                         "credential_content")
        self.assertEqual(entries["docs/readme.md"]["pass"], "docs")
        self.assertEqual(entries["evidence/events.jsonl"]["pass"], "evidence")
        self.assertEqual(entries["secrets/token.txt"]["reason"], "sensitive_path")
        self.assertEqual(entries[".env.local"]["reason"], "sensitive_path")
        self.assertEqual(entries["config/ignored.json"]["reason"], "git_ignored")
        self.assertEqual(entries["assets/image.png"]["reason"], "binary_file")
        self.assertEqual(entries["generated/build.py"]["reason"], "generated_artifact")
        self.assertEqual(entries["notes.unknown"]["reason"], "unsupported_file_type")
        self.assertNotIn("secrets/token.txt", read_paths)
        self.assertNotIn(".env.local", read_paths)
        self.assertNotIn("config/ignored.json", read_paths)
        self.assertNotIn("assets/image.png", read_paths)
        self.assertNotIn("generated/build.py", read_paths)
        self.assertNotIn("credential sentinel", json.dumps(first))

    def test_resume_reuses_matching_chunks_and_invalidates_only_changed_source_or_pack(self):
        policy = FakePolicy()
        first = self._run(policy)
        self.assertTrue(first["coverage"]["complete"])
        self.assertEqual(first["coverage"]["completed_chunks"], 6)
        self.assertEqual(len(policy.calls), 6)
        self.assertEqual(
            first["spend"]["conservative_reservations_usd"],
            round(6 * jev_cost(JEV_MAX_INPUT_TOKENS), 6))
        self.assertAlmostEqual(
            first["spend"]["conservative_reservations_usd"]
            + first["spend"]["reservation_overrun_usd"],
            first["spend"]["actual_spend_usd"]
            + first["spend"]["released_reservations_usd"])

        second = self._run(policy)
        self.assertEqual(second["spend"]["calls_this_run"], 0)
        self.assertEqual(len(policy.calls), 6)

        self._write("src/app.py", "def changed():\n    return 'changed'\n")
        third = self._run(policy)
        self.assertEqual(third["spend"]["calls_this_run"], 1)
        self.assertEqual(len(policy.calls), 7)

        self.packs["docs"] = _pack("docs-pack-v2")
        fourth = self._run(policy)
        self.assertEqual(fourth["spend"]["calls_this_run"], 1)
        self.assertEqual(len(policy.calls), 8)

    def test_completed_receipt_with_mismatched_identity_is_refused(self):
        first = self._run(FakePolicy())
        state_file = self.root / ".harness" / "source-audit.sqlite3"
        chunk_id = first["receipts"][0]["chunk_id"]
        with sqlite3.connect(str(state_file)) as db:
            row = db.execute(
                "SELECT payload FROM chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            payload = json.loads(row[0])
            payload["pack_hash"] = "different-policy"
            db.execute("UPDATE chunks SET payload=? WHERE chunk_id=?",
                       (json.dumps(payload, sort_keys=True), chunk_id))

        resumed = FakePolicy()
        with self.assertRaisesRegex(SourceAuditError, "state is corrupt"):
            self._run(resumed)
        self.assertEqual(resumed.calls, [])

    def test_chunk_boundaries_are_stable_for_empty_and_large_lines(self):
        large = "x" * 1300 + "\n"
        first = list(source_audit._iter_text_chunks(large, 512))
        second = list(source_audit._iter_text_chunks(large, 512))
        self.assertEqual(first, second)
        self.assertEqual([len(chunk[0]) for chunk in first], [512, 512, 277])
        self.assertTrue(all((start, end) == (1, 1)
                            for _text, start, end in first))
        self.assertEqual(list(source_audit._iter_text_chunks("", 512)), [])

    def test_inflight_chunks_require_explicit_recovery_and_reuse_completed_receipts(self):
        state_name = "interrupted.sqlite3"
        interrupted = FakePolicy(interrupt_at=3)
        with self.assertRaises(KeyboardInterrupt):
            self._run(interrupted, state_name=state_name)
        self.assertEqual(len(interrupted.calls), 3)

        ordinary_resume = FakePolicy()
        pending = self._run(ordinary_resume, state_name=state_name)
        self.assertEqual(len(ordinary_resume.calls), 3)
        self.assertEqual(pending["coverage"]["pending_chunks"], 1)
        self.assertEqual(pending["coverage"]["stop_reason"], "resume_pending")
        self.assertIsNotNone(pending["coverage"]["resume_cursor"])
        self.assertAlmostEqual(
            pending["spend"]["pending_possible_spend_usd"],
            jev_cost(JEV_MAX_INPUT_TOKENS))

        recovered_policy = FakePolicy()
        recovered = self._run(
            recovered_policy, recover_interrupted=True, state_name=state_name)
        self.assertEqual(len(recovered_policy.calls), 1)
        self.assertTrue(recovered["coverage"]["complete"])
        self.assertEqual(recovered["coverage"]["recovered_chunks"], 1)
        self.assertAlmostEqual(
            recovered["spend"]["unknown_prior_attempt_reservations_usd"],
            jev_cost(JEV_MAX_INPUT_TOKENS))
        self.assertAlmostEqual(recovered["spend"]["actual_spend_usd"], 0.006)

        final_policy = FakePolicy()
        final = self._run(final_policy, state_name=state_name)
        self.assertEqual(final_policy.calls, [])
        self.assertEqual(final["spend"]["calls_this_run"], 0)
        self.assertAlmostEqual(
            final["spend"]["unknown_prior_attempt_reservations_usd"],
            jev_cost(JEV_MAX_INPUT_TOKENS))

    def test_failed_chunk_can_be_recovered_without_repeating_completed_receipts(self):
        state_name = "failed.sqlite3"
        failed_policy = FakePolicy(fail_at=2)
        failed = self._run(failed_policy, state_name=state_name)
        self.assertEqual(failed["coverage"]["stop_reason"], "error")
        self.assertEqual(failed["coverage"]["completed_chunks"], 1)
        self.assertEqual(failed["coverage"]["pending_chunks"], 5)

        ordinary_resume = FakePolicy()
        pending = self._run(ordinary_resume, state_name=state_name)
        self.assertEqual(len(ordinary_resume.calls), 4)
        self.assertEqual(pending["coverage"]["pending_chunks"], 1)

        recovered_policy = FakePolicy()
        recovered = self._run(
            recovered_policy, recover_interrupted=True, state_name=state_name)
        self.assertEqual(len(recovered_policy.calls), 1)
        self.assertTrue(recovered["coverage"]["complete"])
        self.assertEqual(recovered["coverage"]["recovered_chunks"], 1)
        self.assertEqual(recovered["spend"]["calls"], 6)
        self.assertAlmostEqual(
            recovered["spend"]["unknown_prior_attempt_reservations_usd"],
            jev_cost(JEV_MAX_INPUT_TOKENS))

    def test_recovery_preflight_counts_unknown_prior_reservation(self):
        state_name = "recovery-budget.sqlite3"
        with self.assertRaises(KeyboardInterrupt):
            self._run(FakePolicy(interrupt_first=True), state_name=state_name)

        reservation = jev_cost(JEV_MAX_INPUT_TOKENS)
        budget = reservation * 1.5
        no_call = FakePolicy()
        report = self._run(
            no_call, state_name=state_name, run_budget=budget,
            recover_interrupted=True,
            make_policy=lambda spent: (
                None if spent + reservation > budget else no_call))
        self.assertEqual(no_call.calls, [])
        self.assertEqual(report["coverage"]["stop_reason"], "run_budget")
        self.assertEqual(report["coverage"]["recovered_chunks"], 1)
        self.assertAlmostEqual(report["spend"]["pending_possible_spend_usd"],
                               reservation)

        resumed = FakePolicy()
        final = self._run(resumed, state_name=state_name)
        self.assertEqual(len(resumed.calls), 6)
        self.assertTrue(final["coverage"]["complete"])
        self.assertAlmostEqual(
            final["spend"]["unknown_prior_attempt_reservations_usd"],
            reservation)

    def test_same_state_lock_refuses_another_process_and_releases_on_close(self):
        state_file = self.root / ".harness" / "locked.sqlite3"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = source_audit._acquire_source_audit_lock(state_file)
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from harness.source_audit import _acquire_source_audit_lock, SourceAuditError\n"
            "p = Path(sys.argv[1])\n"
            "try:\n"
            "    _acquire_source_audit_lock(p)\n"
            "except SourceAuditError as exc:\n"
            "    print(str(exc))\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n"
        )
        try:
            child = subprocess.run(
                [sys.executable, "-c", script, str(state_file)],
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("file lock is already held", child.stdout)
        finally:
            os.close(lock_fd)
        released_fd = source_audit._acquire_source_audit_lock(state_file)
        os.close(released_fd)

    def test_state_lock_file_is_excluded_from_following_manifest(self):
        policy = FakePolicy()
        state_file = self.root / "source-audit.sqlite3"
        first = source_audit.run_source_audit(
            self.root, self.packs, lambda _spent: policy,
            state_path=state_file, run_budget=0.1, chunk_chars=512)
        self.assertTrue(first["coverage"]["complete"])

        second = source_audit.run_source_audit(
            self.root, self.packs, lambda _spent: policy,
            state_path=state_file, run_budget=0.1, chunk_chars=512)
        lock = next(entry for entry in second["manifest"]["files"]
                    if entry["path"] == "source-audit.sqlite3.lock")
        self.assertFalse(lock["included"])
        self.assertEqual(lock["reason"], "audit_output_or_state")
        self.assertEqual(second["spend"]["calls_this_run"], 0)

    def test_leads_are_deduplicated_dispositioned_and_reports_never_include_source(self):
        policy = FakePolicy()
        report = self._run(policy)
        lead = report["leads"][0]
        updated = self._run(policy, dispositions={
            lead["lead_id"]: "historical_fixture"})
        by_id = {row["lead_id"]: row for row in updated["leads"]}
        self.assertEqual(by_id[lead["lead_id"]]["disposition"], "historical_fixture")
        self.assertTrue(all("locations" in row for row in updated["leads"]))
        serialized = json.dumps(updated, sort_keys=True)
        self.assertNotIn("source sentinel", serialized)
        self.assertNotIn("SOURCE_AUDIT_SECRET_SENTINEL", serialized)
        self.assertNotIn("ENV_AUDIT_SECRET_SENTINEL", serialized)
        self.assertNotIn(self.test_api_key, serialized)
        self.assertTrue(any(row["is_fallback"] for row in updated["receipts"]))
        self.assertTrue(any(row["discarded"] for row in updated["receipts"]))
        self.assertEqual(updated["spend"]["fallbacks"], 1)
        self.assertEqual(updated["spend"]["discarded_calls"], 1)
        self.assertFalse(any("provider.py" in call["state"]["path"]
                             for call in policy.calls))

    def test_budget_refusal_occurs_before_any_call_and_reports_pending_coverage(self):
        policy = FakePolicy()
        report = source_audit.run_source_audit(
            self.root, self.packs, lambda _spent: None,
            state_path=self.root / ".harness" / "budget.sqlite3",
            run_budget=0.000001, chunk_chars=512)
        self.assertEqual(policy.calls, [])
        self.assertEqual(report["coverage"]["stop_reason"], "run_budget")
        self.assertEqual(report["coverage"]["completed_chunks"], 0)
        self.assertEqual(report["coverage"]["pending_chunks"], 6)
        self.assertFalse(report["coverage"]["complete"])

    def test_invalid_disposition_is_rejected(self):
        policy = FakePolicy()
        report = self._run(policy)
        lead_id = report["leads"][0]["lead_id"]
        with self.assertRaises(HarnessError):
            self._run(policy, dispositions={lead_id: "file_issue"})

    def test_pack_credential_content_is_rejected_before_any_policy_call(self):
        packs = dict(self.packs)
        packs["code"] = _pack("code-with-credential")
        packs["code"]["axes"]["finding"]["instructions"] = (
            'api_key="{}"'.format("sk-" + ("B" * 32)))
        policy = FakePolicy()
        with self.assertRaisesRegex(HarnessError, "pack contains credential-like content"):
            source_audit.run_source_audit(
                self.root, packs, lambda _spent: policy,
                state_path=self.root / ".harness" / "pack.sqlite3",
                run_budget=0.1, chunk_chars=512)
        self.assertEqual(policy.calls, [])

    def test_output_paths_reject_dangling_symlinks(self):
        link = self.root / "dangling-state.sqlite3"
        outside_target = Path(self.temp.name) / "outside-state.sqlite3"
        original_lstat = Path.lstat

        def fake_lstat(path):
            if path == link:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original_lstat(path)

        with patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat):
            with self.assertRaisesRegex(HarnessError, "cannot be symlinks"):
                source_audit._relative_output(self.root, link)
            with self.assertRaisesRegex(HarnessError, "cannot be a symlink"):
                source_audit._open_state(link)
            with self.assertRaisesRegex(HarnessError, "cannot be a symlink"):
                source_audit.write_report({"safe": True}, link)
        self.assertFalse(outside_target.exists())

    def test_cli_wires_three_packs_and_persistent_state_without_provider_calls(self):
        root = self.root
        packs = {name: _pack("{}-cli".format(name))
                 for name in ("code", "docs", "evidence")}
        captured = {}

        class Ledger:
            def ensure_offered_task_id(self, task_id, **fields):
                captured["task_id"] = task_id

        opts = SimpleNamespace(
            root=str(root), run_budget=0.01, chunk_chars=512,
            code_pack="code.json", docs_pack="docs.json",
            evidence_pack="evidence.json", state=None,
            dispositions=None, task_id="source-audit-cli-test",
            save_to="source-audit.json", out=None,
            recover_interrupted=True)
        with (patch.object(cli, "_read_json",
                           side_effect=lambda path, _what: packs[path.split(".")[0]]),
              patch.object(cli, "_ledger", return_value=Ledger()),
              patch.object(cli, "_source_audit_policy_factory",
                           return_value=lambda _spent: object()),
              patch.object(cli, "_source_audit_run",
                           return_value={"schema": "source-audit-v1"}) as run,
              patch.object(cli, "_source_audit_write_report",
                           return_value="source-audit.json") as write_report,
              patch.object(cli, "_emit") as emit):
            cli._cmd_source_audit(opts, SimpleNamespace(max_cost=0.1))
        self.assertEqual(captured["task_id"], "source-audit-cli-test")
        self.assertEqual(run.call_args.args[0], root.resolve())
        self.assertEqual(run.call_args.args[1], packs)
        self.assertEqual(run.call_args.kwargs["run_budget"], 0.01)
        self.assertEqual(run.call_args.kwargs["state_path"],
                         root.resolve() / ".harness" / "source-audit.sqlite3")
        self.assertTrue(run.call_args.kwargs["recover_interrupted"])
        self.assertEqual(emit.call_args.args[0]["task_id"],
                         "source-audit-cli-test")
        write_report.assert_called_once()
        written_report, written_path = write_report.call_args.args
        self.assertEqual(written_path, "source-audit.json")
        self.assertEqual(written_report["task_id"],
                         "source-audit-cli-test")
        self.assertEqual(written_report["written"],
                         {"report": "source-audit.json"})
        self.assertEqual(written_report["state_path"],
                         str(root.resolve() / ".harness" /
                             "source-audit.sqlite3"))

    def test_cli_parser_exposes_interrupted_recovery_option(self):
        opts = cli_parser.build_parser().parse_args([
            "source-audit", "--code-pack", "code.json",
            "--docs-pack", "docs.json", "--evidence-pack", "evidence.json",
            "--run-budget", "0.01", "--recover-interrupted",
        ])
        self.assertTrue(opts.recover_interrupted)

    def test_cli_source_audit_rejects_invalid_chunk_limit_and_dispositions(self):
        opts = SimpleNamespace(
            root=str(self.root), run_budget=0.01, chunk_chars=511,
            code_pack="code.json", docs_pack="docs.json",
            evidence_pack="evidence.json", state=None,
            dispositions=None, task_id="source-audit-invalid-cli",
            save_to=None, out=None)
        with self.assertRaisesRegex(HarnessError, "chunk_chars must be between"):
            cli._cmd_source_audit(opts, SimpleNamespace(max_cost=0.1))

        opts.chunk_chars = 512
        opts.dispositions = "dispositions.json"
        with (patch.object(cli, "_read_json", return_value=[]),
              self.assertRaisesRegex(HarnessError,
                                     "dispositions must be a JSON object")):
            cli._cmd_source_audit(opts, SimpleNamespace(max_cost=0.1))

    def test_git_snapshot_and_output_path_refusals_are_explicit(self):
        for code in (126, 127):
            with self.subTest(git_returncode=code):
                with patch.object(source_audit.osal, "run_bytes",
                                  return_value=SimpleNamespace(
                                      returncode=code, stdout=b"", stderr=b"")):
                    with self.assertRaisesRegex(SourceAuditError,
                                                "available Git executable"):
                        source_audit._git(self.root, "status")
        with patch.object(source_audit.osal, "run_bytes",
                          return_value=SimpleNamespace(
                              returncode=2, stdout=b"", stderr=b"bad repo")):
            with self.assertRaisesRegex(SourceAuditError,
                                        "valid Git repository snapshot"):
                source_audit._git(self.root, "status")

        with patch.object(source_audit, "_git", side_effect=OSError("read")):
            with self.assertRaisesRegex(SourceAuditError,
                                        "cannot read the Git repository snapshot"):
                source_audit._repo_snapshot(self.root)
        with patch.object(source_audit, "_git", side_effect=[
                os.fsencode(str(self.root.parent)), b"fixture-commit"]):
            with self.assertRaisesRegex(SourceAuditError,
                                        "root must be the Git repository root"):
                source_audit._repo_snapshot(self.root)

        def empty_git(root, *args, **_kwargs):
            if args == ("rev-parse", "--show-toplevel"):
                return os.fsencode(str(root))
            if args == ("rev-parse", "HEAD"):
                return b"fixture-commit"
            return b""

        with patch.object(source_audit, "_git", side_effect=empty_git):
            commit, paths, ignored = source_audit._repo_snapshot(self.root)
        self.assertEqual(commit, "fixture-commit")
        self.assertEqual(paths, [])
        self.assertEqual(ignored, set())
        self.assertIsNone(source_audit._relative_output(self.root, None))
        with self.assertRaisesRegex(SourceAuditError, "stay inside"):
            source_audit._relative_output(self.root, "..")

        class Uninspectable:
            def lstat(self):
                raise PermissionError("fixture")

        with self.assertRaisesRegex(SourceAuditError,
                                    "cannot safely inspect source audit output"):
            source_audit._is_link_or_reparse(cast(Path, Uninspectable()))

    def test_snapshot_fails_closed_when_git_ignore_rules_cannot_be_read(self):
        original = source_audit.osal.run_bytes

        for code, message in ((127, "available Git executable"),
                              (2, "cannot read Git ignored-path rules")):
            def fake_run(argv, **kwargs):
                if argv[:2] == ["git", "check-ignore"]:
                    return SimpleNamespace(returncode=code, stdout=b"",
                                           stderr=b"fixture")
                return original(argv, **kwargs)

            with self.subTest(returncode=code), patch.object(
                    source_audit.osal, "run_bytes", side_effect=fake_run):
                with self.assertRaisesRegex(SourceAuditError, message):
                    source_audit._repo_snapshot(self.root)

    def test_file_safety_helpers_classify_unreadable_binary_and_secret_content(self):
        missing = self.root / "missing.py"
        self.assertEqual(source_audit._exclusion_reason(
            "missing.py", missing, set(), set()), "unavailable_path")
        directory = self.root / "directory"
        directory.mkdir()
        self.assertEqual(source_audit._exclusion_reason(
            "directory", directory, set(), set()), "non_regular_file")

        large = self.root / "large.py"
        large.write_bytes(b"x" * (source_audit.MAX_FILE_BYTES + 1))
        self.assertEqual(source_audit._exclusion_reason(
            "large.py", large, set(), set()), "file_too_large")
        self.assertEqual(source_audit._read_text(large), (b"", "file_too_large"))

        unreadable = self.root / "unreadable.py"
        self.assertEqual(source_audit._read_text(unreadable),
                         (b"", "unreadable_file"))
        nul_file = self.root / "nul.py"
        nul_file.write_bytes(b"safe\0binary")
        self.assertEqual(source_audit._read_text(nul_file),
                         (b"safe\0binary", "binary_file"))
        non_utf8 = self.root / "bad-encoding.py"
        non_utf8.write_bytes(b"\xff\xfe")
        self.assertEqual(source_audit._read_text(non_utf8),
                         (b"\xff\xfe", "non_utf8_file"))

        self.assertFalse(source_audit._contains_credentials(
            'API_KEY="example" TOKEN=${TOKEN} OTHER=os.environ["X"]'))
        credential_text = "api_key=" + '"fixture-' + "secret-value" + '"'
        self.assertTrue(source_audit._contains_credentials(credential_text))
        self.assertEqual(source_audit._exclusion_reason(
            "assets/image.png", self.root / "assets" / "image.png",
            set(), set()), "binary_file")

    def test_chunking_flushes_prior_lines_before_splitting_a_long_line(self):
        chunks = list(source_audit._iter_text_chunks(
            "a\nb\n" + "z" * 9 + "\n" + "c\n", 4))
        self.assertEqual(chunks, [
            ("a\nb\n", 1, 2),
            ("zzzz", 3, 3),
            ("zzzz", 3, 3),
            ("z\nc\n", 3, 4),
        ])

    def test_manifest_validation_rejects_bad_roots_packs_and_chunk_limits(self):
        with self.assertRaisesRegex(SourceAuditError, "root must be a directory"):
            source_audit.build_manifest(self.root / "src" / "app.py", self.packs)
        with self.assertRaisesRegex(SourceAuditError, "chunk_chars"):
            source_audit.build_manifest(self.root, self.packs, chunk_chars=511)
        with self.assertRaisesRegex(SourceAuditError, "requires operator packs"):
            source_audit.build_manifest(self.root, {"code": self.packs["code"]})

        bad = dict(self.packs)
        bad["code"] = {"id": "invalid"}
        with self.assertRaisesRegex(SourceAuditError, "invalid code pass pack"):
            source_audit.build_manifest(self.root, bad)

        non_json = dict(self.packs)
        non_json["code"] = dict(self.packs["code"], opaque={"not-json"})
        with self.assertRaisesRegex(SourceAuditError, "not canonical JSON"):
            source_audit.build_manifest(self.root, non_json)

        no_docs = dict(self.packs)
        del no_docs["docs"]
        with self.assertRaisesRegex(SourceAuditError, "requires operator packs"):
            source_audit._prepared_packs(no_docs)

    def test_source_audit_state_corruption_and_sqlite_failures_refuse(self):
        corrupt_file = self.root / "corrupt.sqlite3"
        corrupt_file.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(SourceAuditError, "cannot open source audit"):
            source_audit._open_state(corrupt_file)

        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, status TEXT, payload TEXT)")
        db.execute("INSERT INTO chunks VALUES ('bad-json', 'in_flight', '{')")
        db.execute("INSERT INTO chunks VALUES ('not-object', 'in_flight', '[]')")
        db.commit()
        with self.assertRaisesRegex(SourceAuditError, "state is corrupt"):
            source_audit._state_get(db, "bad-json")
        with self.assertRaisesRegex(SourceAuditError, "state is corrupt"):
            source_audit._state_get(db, "not-object")
        identity = {"chunk_id": "bad-json"}
        with self.assertRaisesRegex(SourceAuditError, "state is corrupt"):
            source_audit._state_claim(db, "bad-json", identity)
        with self.assertRaisesRegex(SourceAuditError, "invalid source audit receipt"):
            source_audit._state_finish(db, "bad-json", "in_flight", identity)
        db.close()

        missing_table = sqlite3.connect(":memory:")
        with self.assertRaisesRegex(SourceAuditError, "cannot claim source audit chunk"):
            source_audit._state_claim(missing_table, "chunk", {"chunk_id": "chunk"})
        with self.assertRaisesRegex(SourceAuditError, "cannot persist source audit chunk"):
            source_audit._state_finish(missing_table, "chunk", "complete", {})
        missing_table.close()

    def test_run_source_audit_refuses_unbounded_or_nonpersistent_state(self):
        policy = FakePolicy()
        with self.assertRaisesRegex(SourceAuditError, "positive USD ceiling"):
            source_audit.run_source_audit(
                self.root, self.packs, lambda _spent: policy,
                state_path=self.root / ".harness" / "state.sqlite3",
                run_budget=0)
        with self.assertRaisesRegex(SourceAuditError, "persistent.*state path"):
            source_audit.run_source_audit(
                self.root, self.packs, lambda _spent: policy,
                state_path=None, run_budget=0.1)
        self.assertEqual(policy.calls, [])

    def test_source_changes_unclaimed_chunks_and_provider_errors_are_resumable(self):
        real_read = source_audit._read_text
        counts = {}

        def changed_on_reread(path):
            result = real_read(path)
            rel = Path(path).resolve().relative_to(self.root.resolve()).as_posix()
            counts[rel] = counts.get(rel, 0) + 1
            if rel == "src/app.py" and counts[rel] > 1:
                return b"def replaced():\n    return 'different'\n", None
            return result

        with patch.object(source_audit, "_read_text", side_effect=changed_on_reread):
            changed = self._run(FakePolicy())
        self.assertEqual(changed["coverage"]["stop_reason"], "source_changed")
        self.assertIn("src/app.py", changed["coverage"]["stop_detail"])

        with patch.object(source_audit, "_state_claim",
                          side_effect=lambda _db, _chunk_id, identity, **_kwargs:
                          ("in_flight", identity, False)):
            pending = self._run(FakePolicy())
        self.assertEqual(pending["coverage"]["stop_reason"], "resume_pending")
        self.assertTrue(pending["coverage"]["pending_chunks"])

        class BrokenPolicy:
            keyed = True

            def evaluate_repo_summary(self, *_args, **_kwargs):
                raise RuntimeError("fixture provider failure")

        failed = self._run(BrokenPolicy())
        self.assertEqual(failed["coverage"]["stop_reason"], "error")
        self.assertGreater(failed["coverage"]["pending_chunks"], 0)
        self.assertNotIn("source sentinel", json.dumps(failed))

    def test_policy_factory_reuses_policy_and_refuses_unfunded_calls(self):
        worst = jev_cost(JEV_MAX_INPUT_TOKENS)
        ledger = object()
        policy = object()
        governor = SimpleNamespace(remaining=lambda: worst)
        with (patch("harness._http.HttpTransport", return_value=object()),
              patch("harness.jev_policy.policy_for", return_value=policy) as make,
              patch("harness.session.jev_face_governor") as reserve):
            settings = SimpleNamespace(jev_api_key=None, max_cost=worst * 3)
            factory = source_audit.source_audit_policy_factory(
                settings, worst * 3, ledger)
            self.assertIs(factory(0), policy)
            self.assertIs(factory(0), policy)
            make.assert_called_once()
            reserve.assert_not_called()

        with patch("harness._http.HttpTransport", return_value=object()):
            settings = SimpleNamespace(jev_api_key="fixture-key",
                                       max_cost=worst / 2)
            factory = source_audit.source_audit_policy_factory(
                settings, worst / 2, ledger)
            self.assertIsNone(factory(0))
            settings.max_cost = worst * 3
            factory = source_audit.source_audit_policy_factory(
                settings, worst * 3, ledger)
            self.assertIsNone(factory(worst * 2.5))

        with (patch("harness._http.HttpTransport", return_value=object()),
              patch("harness.session.jev_face_governor", return_value=governor) as reserve,
              patch("harness.jev_policy.policy_for", return_value=policy) as make):
            settings = SimpleNamespace(jev_api_key="fixture-key",
                                       max_cost=worst * 3)
            factory = source_audit.source_audit_policy_factory(
                settings, worst * 3, ledger)
            self.assertIs(factory(0), policy)
            self.assertIs(factory(0), policy)
            reserve.assert_called_once()
            make.assert_called_once()

    def test_report_writer_is_atomic_and_cleans_failed_temporary_file(self):
        report_path = self.root / "reports" / "audit.json"
        written = source_audit.write_report({"schema": "source-audit-v1"}, report_path)
        self.assertEqual(Path(written), report_path)
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")),
                         {"schema": "source-audit-v1"})
        self.assertTrue(report_path.read_bytes().endswith(b"\n"))

        failed_path = self.root / "reports" / "failed.json"
        with patch.object(source_audit.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaisesRegex(SourceAuditError,
                                        "cannot write source audit report"):
                source_audit.write_report({"safe": True}, failed_path)
        self.assertFalse(failed_path.exists())
        self.assertEqual(list(failed_path.parent.glob(".source-audit-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
