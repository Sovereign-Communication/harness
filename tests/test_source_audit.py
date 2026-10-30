"""Deterministic safety, resume, and receipt tests for issue #423."""
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import source_audit
from harness import cli
from harness.errors import HarnessError
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

    def __init__(self, *, interrupt_first=False):
        self.calls = []
        self.interrupt_first = interrupt_first

    def evaluate_repo_summary(self, state, pack, *, site, task_id, node_id,
                              max_input_tokens):
        self.calls.append({"state": state, "site": site, "node_id": node_id})
        if self.interrupt_first and len(self.calls) == 1:
            raise KeyboardInterrupt()
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

    def _run(self, policy, *, dispositions=None, run_budget=0.1):
        return source_audit.run_source_audit(
            self.root, self.packs, lambda _spent: policy,
            state_path=self.root / ".harness" / "source-audit.sqlite3",
            run_budget=run_budget, chunk_chars=512,
            dispositions=dispositions, task_id="source-audit-fixture")

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

    def test_chunk_boundaries_are_stable_for_empty_and_large_lines(self):
        large = "x" * 1300 + "\n"
        first = list(source_audit._iter_text_chunks(large, 512))
        second = list(source_audit._iter_text_chunks(large, 512))
        self.assertEqual(first, second)
        self.assertEqual([len(chunk[0]) for chunk in first], [512, 512, 277])
        self.assertTrue(all((start, end) == (1, 1)
                            for _text, start, end in first))
        self.assertEqual(list(source_audit._iter_text_chunks("", 512)), [])

    def test_resume_keeps_interrupted_chunk_in_flight_without_duplicate_call(self):
        interrupted = FakePolicy(interrupt_first=True)
        with self.assertRaises(KeyboardInterrupt):
            self._run(interrupted)
        self.assertEqual(len(interrupted.calls), 1)

        resumed = FakePolicy()
        report = self._run(resumed)
        self.assertEqual(len(resumed.calls), 5)
        self.assertEqual(report["coverage"]["pending_chunks"], 1)
        self.assertEqual(report["coverage"]["stop_reason"], "resume_pending")
        self.assertIsNotNone(report["coverage"]["resume_cursor"])

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
            save_to=None, out=None)
        with (patch.object(cli, "_read_json",
                           side_effect=lambda path, _what: packs[path.split(".")[0]]),
              patch.object(cli, "_ledger", return_value=Ledger()),
              patch.object(cli, "_source_audit_policy_factory",
                           return_value=lambda _spent: object()),
              patch.object(cli, "_source_audit_run",
                           return_value={"schema": "source-audit-v1"}) as run,
              patch.object(cli, "_emit") as emit):
            cli._cmd_source_audit(opts, SimpleNamespace(max_cost=0.1))
        self.assertEqual(captured["task_id"], "source-audit-cli-test")
        self.assertEqual(run.call_args.args[0], root.resolve())
        self.assertEqual(run.call_args.args[1], packs)
        self.assertEqual(run.call_args.kwargs["run_budget"], 0.01)
        self.assertEqual(run.call_args.kwargs["state_path"],
                         root.resolve() / ".harness" / "source-audit.sqlite3")
        self.assertEqual(emit.call_args.args[0]["task_id"],
                         "source-audit-cli-test")


if __name__ == "__main__":
    unittest.main()
