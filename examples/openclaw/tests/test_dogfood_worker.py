import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[3]))
spec = importlib.util.spec_from_file_location("dogfood_worker", ROOT / "runtime" / "dogfood_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "a.txt").write_text("old\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        self.task = {"task_id": "real-task-001", "repo": str(self.repo),
                     "repo_sha": worker.git(self.repo, "rev-parse", "HEAD"),
                     "file": "a.txt", "instruction": "Replace old with new.",
                     "verify_argv": [sys.executable, "-c", "assert True"]}
        self.manifest = self.root / "task.json"
        self.manifest.write_text(json.dumps(self.task), encoding="utf-8")
        self.db = self.root / "tasks.sqlite"

    def test_duplicate_intake_is_idempotent_and_conflict_refused(self):
        worker.enqueue(self.db, self.manifest)
        worker.enqueue(self.db, self.manifest)
        self.assertEqual(len(worker.status(self.db)), 1)
        self.task["instruction"] = "A different task."
        self.manifest.write_text(json.dumps(self.task), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "duplicate_id_conflict"):
            worker.enqueue(self.db, self.manifest)

    def test_noop_and_extra_file_refused(self):
        self.assertEqual(worker.validate_diff(
            "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n",
            "a.txt", "old\n"), "new\n")
        with self.assertRaisesRegex(ValueError, "diff_noop_or_mismatch"):
            worker.validate_diff("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n old\n", "a.txt", "old\n")
        extra = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-x\n+y\n"
        with self.assertRaisesRegex(ValueError, "diff_scope"):
            worker.validate_diff(extra, "a.txt", "old\n")
        self.task["file"] = "../escape.txt"
        with self.assertRaisesRegex(ValueError, "file_scope"):
            worker.validate_manifest(self.task)

    def test_native_fallback_refused(self):
        fake = mock.Mock(settings=mock.Mock(), keyed=True)
        fake.evaluate_candidate.return_value = (
            worker.dataclasses.make_dataclass("Result", [("verdict", str)])("pass"),
            {"verdict": "pass", "is_fallback": True})
        policy = worker.NativeOnlyPolicy(fake)
        result, _ = policy.evaluate_candidate("a", "b", "change", "file")
        self.assertEqual(result.verdict, "fail")

    def test_interrupted_edit_stays_uncertain_without_replay(self):
        worker.enqueue(self.db, self.manifest)
        def interrupted(*_args, **_kwargs):
            with worker.connect(self.db) as db:
                db.execute("UPDATE tasks SET phase='editing' WHERE task_id=?", (self.task["task_id"],))
            return subprocess.CompletedProcess([], 2, "child_failed", "")
        with mock.patch.object(worker.subprocess, "run", side_effect=interrupted):
            result = worker.run_once(self.db)
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(worker.status(self.db)[0]["attempt"], 1)
        self.assertEqual(worker.run_once(self.db), {"state": "idle"})

    def test_empty_queue_does_not_start_child(self):
        with mock.patch.object(worker.subprocess, "run") as run:
            self.assertEqual(worker.run_once(self.db), {"state": "idle"})
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

