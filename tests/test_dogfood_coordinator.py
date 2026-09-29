"""Local SCMessenger <-> Harness dogfood coordinator tests."""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness.cli import main
from harness import dogfood_coordinator as coordinator_module
from harness.dogfood_coordinator import CoordinatorConfig, _coordinator_lock, coordinate


GIT = shutil.which("git") is not None


def _git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=False, shell=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@unittest.skipUnless(GIT, "platform: git executable unavailable")
class DogfoodCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "coordinator-state"
        self.repos = None
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self._remove_candidate_worktrees)
        self.repos = {
            "harness": self._make_repo("harness", "1.0.0"),
            "scmessenger": self._make_repo("scmessenger", "2.0.0"),
        }

    def _make_repo(self, name, version):
        remote = self.root / f"{name}-remote.git"
        seed = self.root / f"{name}-seed"
        local = self.root / f"{name}-local"
        _git(self.root, "init", "--bare", "-q", "-b", "main", str(remote))
        _git(self.root, "init", "-q", "-b", "main", str(seed))
        _git(seed, "config", "user.name", "Coordinator Test")
        _git(seed, "config", "user.email", "coordinator@example.invalid")
        if name == "harness":
            _write(seed / "pyproject.toml", (
                "[project]\nname = \"fake-harness\"\n"
                f"version = \"{version}\"\n"
            ))
            _write(seed / "harness" / "__init__.py", "")
            _write(seed / "harness" / "cli.py", "# candidate harness CLI\n")
            _write(seed / "tests" / "test_candidate.py", (
                "import unittest\n\n"
                "class CandidateTests(unittest.TestCase):\n"
                "    def test_candidate(self):\n"
                "        self.assertTrue(True)\n"
            ))
        else:
            _write(seed / "Cargo.toml", (
                "[workspace]\nmembers = []\n\n[workspace.package]\n"
                f"version = \"{version}\"\n"
            ))
            _write(seed / "scripts" / "verify_versions.sh", "#!/usr/bin/env bash\nexit 0\n")
        _write(seed / "gate-pass.txt", "ok\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", "base")
        _git(seed, "remote", "add", "origin", str(remote))
        _git(seed, "push", "-q", "-u", "origin", "main")
        _git(self.root, "clone", "-q", str(remote), str(local))
        return local

    def _remove_candidate_worktrees(self):
        if self.repos is None:
            return
        state_path = self.state_dir / "known-good.json"
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for name, record in state.get("known_good", {}).items():
            if record and name in self.repos and record.get("worktree"):
                _git(self.repos[name], "worktree", "remove", "--force",
                      record["worktree"], check=False)

    def _push_update(self, name, version, gate_ok=True, failing_test=False):
        work = self.root / f"{name}-push-{uuid.uuid4().hex[:8]}"
        _git(self.root, "clone", "-q", str(self.root / f"{name}-remote.git"), str(work))
        _git(work, "config", "user.name", "Coordinator Test")
        _git(work, "config", "user.email", "coordinator@example.invalid")
        if name == "harness":
            text = (work / "pyproject.toml").read_text(encoding="utf-8")
            _write(work / "pyproject.toml", text.replace(
                text.split('version = "')[1].split('"')[0], version))
        else:
            text = (work / "Cargo.toml").read_text(encoding="utf-8")
            _write(work / "Cargo.toml", text.replace(
                text.split('version = "')[1].split('"')[0], version))
        if not gate_ok:
            (work / "gate-pass.txt").unlink()
        if failing_test:
            if name != "harness":
                raise AssertionError("failing_test is only used for Harness")
            _write(work / "tests" / "test_candidate.py", (
                "import unittest\n\n"
                "class CandidateTests(unittest.TestCase):\n"
                "    def test_candidate(self):\n"
                "        self.fail('candidate test failed')\n"
            ))
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "candidate")
        _git(work, "push", "-q", "origin", "main")
        return _git(work, "rev-parse", "HEAD").stdout.strip()

    def _config(self, *, apply=False):
        gate = (
            sys.executable, "-c",
            "import pathlib,sys; "
            "sys.exit(0 if pathlib.Path('gate-pass.txt').read_text().strip() == 'ok' else 1)",
        )
        return CoordinatorConfig(
            harness_repo=str(self.repos["harness"]),
            scmessenger_repo=str(self.repos["scmessenger"]),
            state_dir=str(self.state_dir),
            apply=apply,
            harness_verify=gate,
            scmessenger_verify=gate,
        )

    def test_stale_and_current_detection_records_exact_metadata(self):
        newer = self._push_update("harness", "1.1.0")
        _git(self.repos["harness"], "fetch", "-q", "origin", "main")

        result = coordinate(self._config())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["repositories"]["harness"]["relation"], "stale")
        self.assertEqual(result["repositories"]["harness"]["available_revision"], newer)
        self.assertEqual(result["repositories"]["harness"]["available_version"], "1.1.0")
        self.assertEqual(result["repositories"]["scmessenger"]["relation"], "current")
        self.assertEqual(result["repositories"]["scmessenger"]["version"], "2.0.0")
        self.assertEqual(result["planned_targets"]["harness"], newer)
        self.assertFalse(result["receipts_written"])
        self.assertFalse(self.state_dir.exists())

    def test_apply_refuses_dirty_worktree_before_mutation(self):
        self._push_update("harness", "1.1.0")
        _write(self.repos["harness"] / "uncommitted.txt", "keep me\n")
        fetch_head = self.repos["harness"] / ".git" / "FETCH_HEAD"
        if fetch_head.exists():
            fetch_head.unlink()

        result = coordinate(self._config(apply=True))

        self.assertEqual(result["status"], "failed")
        self.assertIn("harness: source worktree is dirty", result["blockers"])
        self.assertEqual(result["receipts"], {})
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(fetch_head.exists())
        self.assertEqual(
            (self.repos["harness"] / "uncommitted.txt").read_text(encoding="utf-8"),
            "keep me\n",
        )

    def test_apply_uses_private_refs_without_mutating_fetch_head(self):
        fetch_head = self.repos["harness"] / ".git" / "FETCH_HEAD"
        self.assertFalse(fetch_head.exists())

        first = coordinate(self._config(apply=True))

        self.assertEqual(first["status"], "ok", first)
        self.assertFalse(fetch_head.exists())
        self.assertEqual(
            _git(self.repos["harness"], "for-each-ref", "--format=%(refname)",
                 "refs/harness-dogfood/").stdout, "")
        _write(fetch_head, "operator sentinel\n")
        second = coordinate(self._config(apply=True))

        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(fetch_head.read_text(encoding="utf-8"), "operator sentinel\n")
        self.assertEqual(
            _git(self.repos["harness"], "for-each-ref", "--format=%(refname)",
                 "refs/harness-dogfood/").stdout, "")

    def _assert_invalid_known_good_refused(self, mutate, expected_error):
        first = coordinate(self._config(apply=True))
        self.assertEqual(first["status"], "ok", first)
        state_path = self.state_dir / "known-good.json"
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        fetch_head = self.repos["harness"] / ".git" / "FETCH_HEAD"
        _write(fetch_head, "operator sentinel\n")
        mutate(previous)
        _write(state_path, json.dumps(previous))

        result = coordinate(self._config(apply=True))

        self.assertEqual(result["status"], "failed")
        self.assertIn(expected_error, result["error"])
        self.assertEqual(result["deployment"]["rollback"]["status"], "not_needed")
        self.assertEqual(fetch_head.read_text(encoding="utf-8"), "operator sentinel\n")
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8")), previous)
        self.assertEqual(result["receipts"], {})

    def test_apply_refuses_missing_known_good_before_fetch(self):
        self._assert_invalid_known_good_refused(
            lambda state: state["known_good"]["harness"].update({
                "worktree": str(self.root / "missing-harness-worktree"),
            }),
            "known-good harness worktree is missing",
        )

    def test_apply_refuses_dirty_known_good_before_fetch(self):
        def dirty(state):
            _write(Path(state["known_good"]["harness"]["worktree"])
                   / "uncommitted.txt", "dirty\n")

        self._assert_invalid_known_good_refused(
            dirty, "known-good harness worktree is dirty")

    def test_apply_refuses_wrong_head_known_good_before_fetch(self):
        def change_head(state):
            worktree = state["known_good"]["harness"]["worktree"]
            _git(worktree, "config", "user.name", "Coordinator Test")
            _git(worktree, "config", "user.email", "coordinator@example.invalid")
            _git(worktree, "commit", "--allow-empty", "-q", "-m", "different head")

        self._assert_invalid_known_good_refused(
            change_head, "known-good harness HEAD")

    def test_apply_refuses_malformed_known_good_before_fetch(self):
        self._assert_invalid_known_good_refused(
            lambda state: state["known_good"]["harness"].pop("worktree"),
            "known-good harness record has an invalid shape",
        )

    def test_apply_audits_both_repositories_and_writes_receipts(self):
        harness_revision = self._push_update("harness", "1.1.0")
        scm_revision = self._push_update("scmessenger", "2.1.0")

        result = coordinate(self._config(apply=True))

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["targets"]["harness"]["revision"], harness_revision)
        self.assertEqual(result["targets"]["scmessenger"]["revision"], scm_revision)
        self.assertEqual({item["status"] for item in result["validations"]}, {"passed"})
        self.assertEqual({item["status"] for item in result["audits"].values()}, {"passed"})
        self.assertTrue(result["deployment"]["changed"])
        self.assertFalse(result["deployment"]["live_deployment"])
        for name, counterpart in (("harness", "scmessenger"),
                                  ("scmessenger", "harness")):
            receipt_path = Path(result["receipts"][name])
            self.assertTrue(receipt_path.is_file())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["repository"], name)
            self.assertEqual(receipt["counterpart_repository"], counterpart)
            self.assertEqual(receipt["target_revision"], result["targets"][name]["revision"])
            self.assertEqual(receipt["validation"]["status"], "passed")
            self.assertEqual(receipt["audit"]["status"], "passed")
            self.assertEqual(receipt["counterpart_audit"]["status"], "passed")
            self.assertEqual(
                receipt["counterpart_audit"]["receipt_path"],
                result["receipts"][counterpart],
            )
        state = json.loads(
            (self.state_dir / "known-good.json").read_text(encoding="utf-8"))
        self.assertEqual(state["known_good"]["harness"]["revision"], harness_revision)
        self.assertEqual(state["known_good"]["scmessenger"]["revision"], scm_revision)

    def test_failed_candidate_rolls_back_last_known_good(self):
        first = coordinate(self._config(apply=True))
        self.assertEqual(first["status"], "ok", first)
        previous = json.loads(
            (self.state_dir / "known-good.json").read_text(encoding="utf-8"))
        previous_worktree = previous["known_good"]["scmessenger"]["worktree"]
        self._push_update("scmessenger", "2.1.0", gate_ok=False)

        failed = coordinate(self._config(apply=True))

        self.assertEqual(failed["status"], "failed")
        self.assertIn("candidate validation failed", failed["error"])
        self.assertEqual(failed["deployment"]["rollback"]["status"], "preserved")
        after = json.loads(
            (self.state_dir / "known-good.json").read_text(encoding="utf-8"))
        self.assertEqual(after, previous)
        self.assertTrue(Path(previous_worktree).is_dir())
        for path in failed["receipts"].values():
            receipt = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(receipt["validation"]["status"], "failed")

    def test_receipt_write_failure_restores_previous_state(self):
        first = coordinate(self._config(apply=True))
        self.assertEqual(first["status"], "ok", first)
        state_path = self.state_dir / "known-good.json"
        previous = state_path.read_text(encoding="utf-8")
        original_write = coordinator_module._write_json
        failed = False

        def fail_harness_receipt_once(path, value):
            nonlocal failed
            if Path(path).name == "harness.json" and not failed:
                failed = True
                raise OSError("forced receipt failure")
            return original_write(path, value)

        with patch.object(coordinator_module, "_write_json",
                          side_effect=fail_harness_receipt_once):
            result = coordinate(self._config(apply=True))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["deployment"]["rollback"]["status"], "restored")
        self.assertEqual(state_path.read_text(encoding="utf-8"), previous)

    def test_persistent_receipt_failure_reports_only_written_receipts(self):
        first = coordinate(self._config(apply=True))
        self.assertEqual(first["status"], "ok", first)
        original_write = coordinator_module._write_json

        def fail_scmessenger_receipt(path, value):
            if Path(path).name == "scmessenger.json":
                raise OSError("forced persistent receipt failure")
            return original_write(path, value)

        with patch.object(coordinator_module, "_write_json",
                          side_effect=fail_scmessenger_receipt):
            result = coordinate(self._config(apply=True))

        self.assertEqual(result["status"], "failed")
        self.assertIn("failure receipt write failed", result["error"])
        self.assertEqual(set(result["receipts"]), {"harness"})
        self.assertTrue(Path(result["receipts"]["harness"]).is_file())
        self.assertEqual(result["deployment"]["rollback"]["status"], "restored")

    def test_apply_refuses_a_live_lock_and_recovers_after_owner_exits(self):
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; from harness.dogfood_coordinator import "
             "_coordinator_lock; "
             "ctx=_coordinator_lock(sys.argv[1]); ctx.__enter__(); "
             "print('locked', flush=True); time.sleep(60)", str(self.state_dir)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with patch("harness.dogfood_coordinator.LOCK_TIMEOUT", 0.01):
                result = coordinate(self._config(apply=True))
            self.assertEqual(result["status"], "failed")
            self.assertIn("coordinator lock is busy", result["error"])
            self.assertEqual(result["receipts"], {})
        finally:
            if holder.poll() is None:
                holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()
            holder.stderr.close()

        recovered = coordinate(self._config(apply=True))
        self.assertEqual(recovered["status"], "ok", recovered)

    def test_coordinator_lock_releases_after_an_exception(self):
        with self.assertRaisesRegex(RuntimeError, "release me"):
            with _coordinator_lock(str(self.state_dir)):
                raise RuntimeError("release me")
        with _coordinator_lock(str(self.state_dir)):
            self.assertTrue((self.state_dir / "coordinator.lock").is_file())

    def test_public_cli_rejects_blank_paths_before_git_inspection(self):
        for option in ("--harness-repo", "--scmessenger-repo", "--state-dir"):
            values = {
                "--harness-repo": str(self.repos["harness"]),
                "--scmessenger-repo": str(self.repos["scmessenger"]),
                "--state-dir": str(self.state_dir),
            }
            values[option] = ""
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("harness.dogfood_coordinator._inspect_repository") as inspect:
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(
                        stdout), contextlib.redirect_stderr(stderr):
                    main([
                        "dogfood-coordinator",
                        "--harness-repo", values["--harness-repo"],
                        "--scmessenger-repo", values["--scmessenger-repo"],
                        "--state-dir", values["--state-dir"],
                        "--quiet",
                    ])
            result = json.loads(stdout.getvalue())
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(result["status"], "failed")
            self.assertIn("must be a nonblank path", result["error"])
            inspect.assert_not_called()
            self.assertFalse(self.state_dir.exists())

    def test_public_cli_rejects_blank_remote_or_branch_before_state(self):
        for option in ("--remote", "--branch"):
            values = {"--remote": "origin", "--branch": "main"}
            values[option] = ""
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("harness.dogfood_coordinator._inspect_repository") as inspect:
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(
                        stdout), contextlib.redirect_stderr(stderr):
                    main([
                        "dogfood-coordinator",
                        "--harness-repo", str(self.repos["harness"]),
                        "--scmessenger-repo", str(self.repos["scmessenger"]),
                        "--state-dir", str(self.state_dir),
                        "--remote", values["--remote"],
                        "--branch", values["--branch"],
                        "--quiet",
                    ])
            result = json.loads(stdout.getvalue())
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(result["status"], "failed")
            self.assertIn("must be nonblank", result["error"])
            inspect.assert_not_called()
            self.assertFalse(self.state_dir.exists())

    def test_public_cli_default_gate_keeps_candidate_clean(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("harness.output.QUIET", False), patch(
                "harness.render.set_color_enabled"), contextlib.redirect_stdout(
                    stdout), contextlib.redirect_stderr(stderr):
            main([
                "dogfood-coordinator",
                "--harness-repo", str(self.repos["harness"]),
                "--scmessenger-repo", str(self.repos["scmessenger"]),
                "--state-dir", str(self.state_dir),
                "--apply",
                "--quiet",
            ])

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "ok", result)
        state = json.loads(
            (self.state_dir / "known-good.json").read_text(encoding="utf-8"))
        for record in state["known_good"].values():
            self.assertEqual(
                _git(record["worktree"], "status", "--porcelain=v1",
                     "--untracked-files=all").stdout, "")
        validation_logs = [item["stdout_path"] for item in result["validations"]]
        self.assertEqual(len(validation_logs), len(set(validation_logs)))

    def test_public_cli_default_gate_rejects_failing_candidate_test(self):
        self._push_update("harness", "1.1.0", failing_test=True)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("harness.output.QUIET", False), patch(
                "harness.render.set_color_enabled"), contextlib.redirect_stdout(
                    stdout), contextlib.redirect_stderr(stderr), self.assertRaises(
                        SystemExit) as raised:
            main([
                "dogfood-coordinator",
                "--harness-repo", str(self.repos["harness"]),
                "--scmessenger-repo", str(self.repos["scmessenger"]),
                "--state-dir", str(self.state_dir),
                "--apply",
                "--quiet",
            ])

        result = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "candidate validation failed")
        harness_validation = next(
            item for item in result["validations"]
            if item["repository"] == "harness" and item["status"] == "failed")
        self.assertIn("unittest", harness_validation["command"])
        self.assertFalse((self.state_dir / "known-good.json").exists())

    def test_public_cli_entry_point_defaults_to_read_only_dry_run(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("harness.output.QUIET", False), patch(
                "harness.render.set_color_enabled"), contextlib.redirect_stdout(
                    stdout), contextlib.redirect_stderr(stderr):
            main([
                "dogfood-coordinator",
                "--harness-repo", str(self.repos["harness"]),
                "--scmessenger-repo", str(self.repos["scmessenger"]),
                "--state-dir", str(self.state_dir),
                "--quiet",
            ])

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "dry-run")
        self.assertTrue(result["apply_eligible"])
        self.assertFalse(result["receipts_written"])
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
