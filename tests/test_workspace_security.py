"""Focused regression coverage for explicit, confined workspace access."""
import inspect
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agent import AutonomousAgent
from harness.apply import ApplyEngine
from harness.errors import HarnessError
from harness.repo_scope import (_rebase_path, discover_target_files,
                                discover_verification_gate, enumerate_repo_files,
                                gate_for_targets)
from harness.server import (_dispatch_file, _dispatch_file_list,
                            validate_dispatch)
from harness.workspace import (choose_allowed_workspace, is_sensitive_path,
                               require_workspace, resolve_allowed_path,
                               resolve_workspace_path)


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside.txt"
        self.outside.write_text("outside", encoding="utf-8")
        self.sample = self.root / "sample.txt"
        self.sample.write_text("safe", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()


class WorkspacePathTests(WorkspaceFixture):
    def test_workspace_must_be_explicit_absolute_and_existing(self):
        with self.assertRaises(HarnessError):
            require_workspace(None)
        with self.assertRaises(HarnessError):
            require_workspace("relative/path")
        self.assertEqual(require_workspace(str(self.root)), self.root.resolve())

    def test_workspace_path_reports_missing_inputs_and_can_check_safe_templates(self):
        with self.assertRaisesRegex(HarnessError, "not a directory"):
            require_workspace(self.sample)
        with self.assertRaisesRegex(HarnessError, "File path is required"):
            resolve_workspace_path(self.root, " ")
        with self.assertRaisesRegex(HarnessError, "unavailable"):
            resolve_workspace_path(self.root, "missing.txt", must_exist=True)
        self.assertEqual(resolve_workspace_path(
            self.root, str(self.sample), must_exist=True), self.sample.resolve())
        self.assertEqual(resolve_workspace_path(
            self.root, ".env.example", reject_sensitive=False),
            (self.root / ".env.example").resolve())

    def test_sensitive_name_suffix_and_stem_rules_are_case_insensitive(self):
        for raw in ("id_ed25519", "private.pem", "token-secret.json",
                    "db.CREDENTIALS.yaml", ".env.production", "SECRETS/config"):
            with self.subTest(path=raw):
                self.assertTrue(is_sensitive_path(raw))
        for raw in (".env.example", "README.md", "settings.py"):
            with self.subTest(path=raw):
                self.assertFalse(is_sensitive_path(raw))

    def test_workspace_path_refuses_traversal_outside_and_credentials(self):
        for raw in ("../outside.txt", str(self.outside), ".env",
                    ".ssh/id_ed25519", ".git/config"):
            with self.subTest(path=raw), self.assertRaises(HarnessError):
                resolve_workspace_path(self.root, raw)

    def test_workspace_path_refuses_symlink_escape(self):
        link = self.root / "linked.txt"
        try:
            link.symlink_to(self.outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(HarnessError):
            resolve_workspace_path(self.root, "linked.txt", must_exist=True)

    def test_workspace_path_refuses_in_root_symlink_components(self):
        file_link = self.root / "linked-file.txt"
        directory_link = self.root / "linked-directory"
        try:
            file_link.symlink_to(self.sample)
            directory_link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        for raw in ("linked-file.txt", "linked-directory/sample.txt"):
            with self.subTest(path=raw), self.assertRaisesRegex(
                    HarnessError, "symlink targets are excluded"):
                resolve_workspace_path(self.root, raw, must_exist=True)

    def test_absolute_workspace_alias_does_not_break_containment(self):
        alias = Path(self.temp.name) / "workspace-alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        self.assertEqual(resolve_workspace_path(
            self.root, alias / self.sample.name, must_exist=True),
            self.sample.resolve())

    def test_absolute_workspace_alias_uses_filesystem_identity(self):
        alias = Path(self.temp.name) / "workspace-alias"

        def same_workspace(candidate, workspace):
            return Path(candidate) == alias and Path(workspace) == self.root.resolve()

        with patch("harness.workspace.os.path.samefile", side_effect=same_workspace):
            self.assertEqual(resolve_workspace_path(
                self.root, alias / self.sample.name, must_exist=True),
                self.sample.resolve())

    def test_mcp_paths_need_and_stay_inside_configured_roots(self):
        with self.assertRaises(HarnessError):
            choose_allowed_workspace(None, [])
        self.assertEqual(choose_allowed_workspace(None, [self.root]),
                         self.root.resolve())
        with self.assertRaises(HarnessError):
            choose_allowed_workspace(self.outside.parent, [self.root])
        self.assertEqual(resolve_allowed_path(
            "sample.txt", [self.root]), self.sample.resolve())
        with self.assertRaises(HarnessError):
            resolve_allowed_path(str(self.outside), [self.root])

    def test_multiple_allowed_roots_require_an_unambiguous_selection(self):
        second = Path(self.temp.name) / "second"
        second.mkdir()
        (second / "sample.txt").write_text("second", encoding="utf-8")
        roots = [self.root, second]
        with self.assertRaisesRegex(HarnessError, "more than one"):
            choose_allowed_workspace(None, roots)
        with self.assertRaisesRegex(HarnessError, "absolute root"):
            choose_allowed_workspace("child", roots)
        with self.assertRaisesRegex(HarnessError, "matches more than one"):
            resolve_allowed_path("sample.txt", roots)
        self.assertEqual(resolve_allowed_path(
            "sample.txt", roots, workspace=self.root), self.sample.resolve())
        with self.assertRaisesRegex(HarnessError, "outside every configured"):
            resolve_allowed_path(str(self.outside), roots)

    def test_discovery_never_defaults_to_process_directory_or_reads_secrets(self):
        (self.root / ".env.local").write_text("secret", encoding="utf-8")
        self.assertEqual(enumerate_repo_files(None), [])
        self.assertEqual(discover_target_files("read sample.txt", None), [])
        found = enumerate_repo_files(self.root)
        self.assertIn("sample.txt", found)
        self.assertNotIn(".env.local", found)

    def test_discovery_supports_explicit_and_heuristic_targets_safely(self):
        source = self.root / "harness"
        source.mkdir()
        (source / "agent.py").write_text("pass", encoding="utf-8")
        target = self.root / "sample.py"
        target.write_text("pass", encoding="utf-8")
        self.assertEqual(discover_target_files("read sample.py", self.root),
                         ["sample.py"])
        self.assertEqual(discover_target_files("please inspect agent", self.root),
                         ["harness/agent.py"])

    def test_inventory_filters_sensitive_and_soft_skipped_paths_and_caps_results(self):
        nested = self.root / "src"
        nested.mkdir()
        (nested / "safe.py").write_text("pass", encoding="utf-8")
        (nested / "access.KEY").write_text("secret", encoding="utf-8")
        secret_dir = self.root / "credentials"
        secret_dir.mkdir()
        (secret_dir / "token.txt").write_text("secret", encoding="utf-8")
        audit_dir = self.root / "audits"
        audit_dir.mkdir()
        (audit_dir / "history.log").write_text("log", encoding="utf-8")
        (self.root / "cache.PYC").write_text("cache", encoding="utf-8")
        normal = enumerate_repo_files(self.root)
        self.assertIn("src/safe.py", normal)
        self.assertNotIn("src/access.KEY", normal)
        self.assertNotIn("credentials/token.txt", normal)
        self.assertNotIn("audits/history.log", normal)
        self.assertIn("audits/history.log", enumerate_repo_files(
            self.root, include_soft_skipped=True))
        self.assertEqual(len(enumerate_repo_files(self.root, limit=1)), 1)

    def test_verification_gate_helpers_do_not_reintroduce_cwd(self):
        self.assertIsNone(discover_verification_gate(["sample.py"]))
        self.assertIsNone(discover_verification_gate([], self.root))
        self.assertEqual(gate_for_targets(["sample.py"]), None)
        self.assertEqual(gate_for_targets([], declared="  custom gate  "),
                         "custom gate")
        self.assertEqual(_rebase_path("sample.py", None, None), "sample.py")
        self.assertEqual(_rebase_path(str(self.outside), self.root, self.root),
                         str(self.outside))

    def test_discovery_does_not_follow_windows_junctions(self):
        if os.name != "nt":
            self.skipTest("Windows junction behavior")
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "escape.py").write_text("outside", encoding="utf-8")
        junction = self.root / "linked"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            self.skipTest("platform/privilege: junction creation is unavailable")

        self.assertNotIn("linked/escape.py", enumerate_repo_files(self.root))
        self.assertEqual(
            discover_target_files("read linked/escape.py", self.root), [])

    def test_explicit_directory_candidates_and_failed_heuristics_are_skipped(self):
        (self.root / "looks_like_file.py").mkdir()
        self.assertEqual(
            discover_target_files("read looks_like_file.py", self.root), [])
        source = self.root / "harness"
        source.mkdir()
        (source / "agent.py").write_text("pass", encoding="utf-8")
        with patch("harness.repo_scope.resolve_workspace_path",
                   side_effect=HarnessError("blocked")):
            self.assertEqual(discover_target_files("agent", self.root), [])

    def test_inventory_limit_containment_and_non_regular_entries(self):
        self.assertEqual(enumerate_repo_files(self.root, limit=0), [])
        with patch("harness.repo_scope.osal.is_within", return_value=False):
            self.assertEqual(enumerate_repo_files(self.root), [])
        with patch("harness.repo_scope.stat.S_ISREG", return_value=False):
            self.assertEqual(enumerate_repo_files(self.root), [])

    def test_rebase_without_source_root_preserves_path_contract(self):
        self.assertEqual(_rebase_path("sample.txt", None, self.root),
                         str((self.root / "sample.txt").resolve()))
        absolute = str(self.sample.resolve())
        self.assertEqual(_rebase_path(absolute, None, self.root), absolute)

    def test_scoped_apply_rejects_sensitive_targets(self):
        engine = object.__new__(ApplyEngine)
        engine.allowed_roots = [str(self.root.resolve())]
        with self.assertRaisesRegex(HarnessError, "credential and private-key"):
            engine._enforce_roots(str(self.root / ".env"))

    def test_private_dispatch_file_helpers_validate_inputs(self):
        self.assertIsNone(_dispatch_file(self.root, None, "optional", required=False))
        self.assertIsNone(_dispatch_file_list(self.root, None, "files"))
        for raw in ([], [1], ""):
            with self.subTest(raw=raw), self.assertRaises(HarnessError):
                _dispatch_file_list(self.root, raw, "files")
        with self.assertRaisesRegex(HarnessError, "workspace folder"):
            _dispatch_file(None, "sample.txt", "target")
        with self.assertRaisesRegex(HarnessError, "must be a file"):
            _dispatch_file(self.root, ".", "target")
        self.assertEqual(_dispatch_file_list(self.root, "sample.txt", "files"),
                         [str(self.sample.resolve())])


class DispatchWorkspaceTests(WorkspaceFixture):
    def test_chat_is_chat_only_without_workspace_and_auto_apply_is_off(self):
        args = validate_dispatch("chat", {"prompt": "What is Harness?"})
        self.assertFalse(args["auto_apply"])
        self.assertNotIn("root_dir", args)
        edit_chat = validate_dispatch("chat", {"prompt": "Update sample.py"})
        self.assertFalse(edit_chat["auto_apply"])

    def test_unscoped_edit_discussion_is_allowed_but_apply_and_provision_are_not(self):
        args = validate_dispatch("chat", {
            "prompt": "Edit sample.py", "auto_apply": False})
        self.assertFalse(args["auto_apply"])
        self.assertNotIn("root_dir", args)
        for payload in (
                {"prompt": "Edit sample.py", "auto_apply": True},
                {"prompt": "Provision environment"}):
            with self.subTest(payload=payload), self.assertRaises(HarnessError):
                validate_dispatch("chat", payload)

    def test_file_dispatch_requires_workspace_and_canonical_containment(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("apply", {"file": str(self.sample),
                                         "instruction": "edit it"})
        args = validate_dispatch("apply", {
            "root_dir": str(self.root), "file": "sample.txt",
            "instruction": "edit it"})
        self.assertEqual(args["file"], str(self.sample.resolve()))
        self.assertEqual(args["root_dir"], str(self.root.resolve()))
        with self.assertRaises(HarnessError):
            validate_dispatch("apply", {
                "root_dir": str(self.root), "file": "../outside.txt",
                "instruction": "edit it"})

    def test_plan_and_verify_inputs_are_resolved_within_selected_workspace(self):
        plan = validate_dispatch("plan", {
            "root_dir": str(self.root), "goal": "review", "files": "sample.txt"})
        self.assertEqual(plan["files"], [str(self.sample.resolve())])
        for name in ("claims.json", "source.txt", "definitions.txt"):
            (self.root / name).write_text("{}", encoding="utf-8")
        verify = validate_dispatch("verify", {
            "root_dir": str(self.root), "prompt_file": "sample.txt",
            "claims_file": "claims.json", "source_file": "source.txt",
            "definitions_file": "definitions.txt"})
        self.assertEqual(verify["prompt_file"], str(self.sample.resolve()))
        self.assertEqual(verify["definitions_file"],
                         str((self.root / "definitions.txt").resolve()))


class AgentDefaultsTests(unittest.TestCase):
    def test_agent_apis_do_not_default_to_auto_apply_or_cwd(self):
        self.assertIs(AutonomousAgent.run_prompt.__defaults__[0], False)
        hourglass = inspect.signature(AutonomousAgent.run_hourglass_request)
        self.assertIs(hourglass.parameters["auto_apply"].default, False)

    def test_unscoped_edit_intent_can_be_discussed_but_not_applied(self):
        agent = AutonomousAgent(settings=object())
        with patch.object(agent, "_handle_conversation",
                          return_value={"status": "ok"}) as conversation:
            result = agent.run_prompt(
                "refactor the whole engine", force_conversation=True,
                auto_apply=False)
        self.assertEqual(result["status"], "ok")
        conversation.assert_called_once()
        with self.assertRaises(HarnessError):
            agent.run_prompt(
                "refactor the whole engine", force_conversation=True,
                auto_apply=True)

    def test_edit_intent_without_workspace_is_rejected_before_dispatch(self):
        agent = AutonomousAgent(settings=object())
        with self.assertRaisesRegex(HarnessError, "Select a workspace folder"):
            agent.run_prompt("refactor the whole engine", auto_apply=False)

    def test_context_skips_a_candidate_that_fails_workspace_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sample.txt").write_text("safe", encoding="utf-8")
            agent = AutonomousAgent(settings=object(), root_dir=root)
            ledger = SimpleNamespace(append=lambda *args, **kwargs: None)
            with (patch("harness.chat_lifecycle.discover_target_files",
                        return_value=["sample.txt"]),
                  patch("harness.chat_lifecycle.resolve_workspace_path",
                        side_effect=HarnessError("blocked")),
                  patch("harness.chat_lifecycle.ledger_for", return_value=ledger)):
                context, files, _brief, _web = agent._hourglass_request_context(
                    "summarize sample")
            self.assertEqual(files, {})
            self.assertIsInstance(context, str)


if __name__ == "__main__":
    unittest.main()
