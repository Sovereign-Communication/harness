"""Provider-free coverage/provenance regression checks (no live requests)."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "audits" / "self" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoverageIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.boundary = load_helper("coverage_isolation")

    def test_all_inherited_credentials_opt_ins_and_config_are_excluded(self):
        planted = {
            "OPENROUTER_API_KEY": "fake", "OPENROUTER_API_KEY_FILE": "fake-file",
            "HARNESS_JEV_KEY": "fake", "TYPESAFE_API_KEY": "fake", "JEV_API_KEY": "fake",
            "DRIVER_JEV_API_KEY": "fake", "UNKNOWN_PROVIDER_API_KEY": "fake",
            "HARNESS_TEST_ALLOW_NETWORK": "1", "HARNESS_JEV_LIVE_SMOKE": "1",
            "HARNESS_AUDIT_LIVE": "1", "HARNESS_USE_FREE": "false",
            "HARNESS_ALLOW_ESCALATION": "1", "HARNESS_MAX_COST": "9",
            "PYTHONPATH": "other-worktree", "PYTHONSTARTUP": "fake",
            "NODE_OPTIONS": "fake", "GIT_CONFIG_COUNT": "1",
            "HOME": "operator", "USERPROFILE": "operator", "LOCALAPPDATA": "operator",
        }
        with tempfile.TemporaryDirectory() as directory:
            env = self.boundary.isolated_env(directory, dict(planted, SYSTEMROOT="Windows"))
            for key in planted:
                if key in self.boundary._ROOTS:
                    self.assertEqual(env[key], str(Path(directory) / "home"))
                elif key in self.boundary._OPT_INS:
                    self.assertEqual(env[key], "0")
                else:
                    self.assertNotIn(key, env)
            self.assertEqual(env["SYSTEMROOT"], "Windows")
            self.assertFalse((Path(env["HOME"]) / ".config" / "harness" / "jev.env").exists())
            self.assertEqual(planted["HARNESS_TEST_ALLOW_NETWORK"], "1")

    def test_loopback_names_cannot_masquerade_as_addresses(self):
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"):
            self.assertTrue(self.boundary.loopback(host))
        for host in ("127.evil.example", "provider.invalid", "203.0.113.1"):
            self.assertFalse(self.boundary.loopback(host))

    def test_git_network_and_config_driven_children_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for argv in (["git", "fetch"], ["git", "push"], ["git", "clone", "remote"],
                         ["git", "-c", "core.hooksPath=bad", "status"],
                         ["git", "commit", "-m", "outside private sandbox"]):
                with self.assertRaises(ValueError):
                    self.boundary.git_command(argv, str(ROOT), directory)
            local = self.boundary.git_command(["git", "status"], str(ROOT), directory)
            self.assertIn("core.hooksPath=" + os.devnull, local)
            self.assertIn("core.fsmonitor=false", local)
            fixture = self.boundary.git_command(
                ["git", "init", "-q", directory], str(ROOT), directory)
            self.assertEqual(fixture[-3:], ["init", "-q", directory])


class CoverageContentIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = load_helper("coverage_evidence")

    def fixture(self, root):
        (root / "harness").mkdir()
        (root / "harness" / "sample.py").write_text("value = 1\n", encoding="utf-8")
        (root / "pyproject.toml").write_text('version = "1"\n', encoding="utf-8")
        return {"schema": "harness-coverage-v2", "commit": "b" * 40,
                "execution": {"head": "a" * 40, "tree": "c" * 40},
                "suite": {"successful": True}, "isolation": {"violations": []},
                "modules": {"harness/sample.py": [1]},
                "source_hashes": self.evidence.source_hashes(root)}

    def test_failed_checks_never_publish_a_baseline(self):
        runner = load_helper("refresh_coverage_baseline")
        self.assertTrue(runner.trace_can_publish(True, False, [], [], False))
        for inputs in ((False, False, [], [], False), (True, True, [], [], False),
                       (True, False, ["refused"], [], False),
                       (True, False, [], ["network"], False),
                       (True, False, [], [], True)):
            self.assertFalse(runner.trace_can_publish(*inputs))

    def test_old_numeric_baseline_is_not_current_execution_evidence(self):
        with self.assertRaisesRegex(ValueError, "measured source-content provenance"):
            self.evidence.validate_trace({"commit": "a" * 40, "modules": {}}, ROOT)

    def test_changed_content_invalidates_same_line_number_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = self.fixture(root)
            self.assertTrue(self.evidence.validate_trace(doc, root))
            (root / "harness" / "sample.py").write_text("different = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stale measured source content"):
                self.evidence.validate_trace(doc, root)

    def test_failed_suite_or_egress_never_qualifies_as_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = self.fixture(root)
            for field, value in (("suite", {"successful": False}),
                                 ("isolation", {"violations": ["refused"]})):
                bad = copy.deepcopy(doc)
                bad[field] = value
                with self.assertRaisesRegex(ValueError, "provider-free suite"):
                    self.evidence.validate_trace(bad, root)

    def test_diff_base_remains_independent_of_measured_execution_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = self.fixture(root)
            result = mock.Mock(stdout=("+++ b/harness/sample.py\n"
                                       "@@ -0,0 +1 @@\n+value = 1\n"))
            with mock.patch.object(self.evidence.subprocess, "run", return_value=result) as run:
                report = self.evidence.changed_line_coverage(root, doc["commit"], doc["modules"])
            self.assertIn(doc["commit"], run.call_args[0][0])
            self.assertNotIn(doc["execution"]["head"], run.call_args[0][0])
            self.assertEqual((report["executed"], report["checked"]), (1, 1))
            self.assertTrue(report["bar_met"])
            self.assertEqual(json.loads(json.dumps(report))["gaps"], {})


if __name__ == "__main__":
    unittest.main()
