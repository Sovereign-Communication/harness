import glob
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import cli
from harness.apply import ApplyEngine
from harness.bench import load_manifest, run_bench, TaskSandbox
from harness.spend import SpendGovernor
from harness.ledger import AutonomyLedger
from harness.router import Router
from tests._fake import FakeTransport, m, comp

JUDGE = "inclusionai/ling-2.6-flash"
APPLY = "deepseek/deepseek-chat"
ESC = "qwen/qwen3-max"

MODELS = [m(APPLY), m(JUDGE), m(ESC)]


def make_task(root, name, code):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "mod.py"), "w", encoding="utf-8") as f:
        f.write(code)
    with open(os.path.join(d, "check.py"), "w", encoding="utf-8") as f:
        f.write("from mod import x\nassert x == 1\n")
    with open(os.path.join(d, "task.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "file": "mod.py",
                   "instruction": "make mod.x equal 1", "verify": "python check.py"}, f)


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def make_engine(self, posts, ledger_name="ledger.jsonl"):
        fake = FakeTransport(models=MODELS, posts=posts)
        gov = SpendGovernor(fake, "sk-test")
        ledger = AutonomyLedger(os.path.join(self.dir.name, ledger_name))
        router = Router(["a", "b"], JUDGE, APPLY)
        return ApplyEngine(fake, "k", gov, ledger, router,
                           default_require_consent=True, default_renew_consent=False)

    def test_load_manifest_directory(self):
        for name, code in [("a", "x = 0\n"), ("b", "x = 0\n")]:
            make_task(self.dir.name, name, code)
        tasks = load_manifest(self.dir.name)
        self.assertEqual(sorted(t["name"] for t in tasks), ["a", "b"])
        for t in tasks:
            self.assertEqual(t["dir"], os.path.join(self.dir.name, t["name"]))

    def test_load_manifest_single_task_object(self):
        """Regression (playtest): the README documents 'a task is one JSON file',
        but a bare task object produced a ZERO-task manifest that ran nothing
        and exited 0. It must load as a one-task manifest."""
        make_task(self.dir.name, "solo", "x = 0\n")
        task_path = os.path.join(self.dir.name, "solo", "task.json")
        tasks = load_manifest(task_path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "solo")
        self.assertEqual(tasks[0]["file"], "mod.py")

    def test_load_manifest_rejects_garbage_shape(self):
        p = os.path.join(self.dir.name, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('"just a string"')
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            load_manifest(p)

    def test_task_sandbox_restores(self):
        make_task(self.dir.name, "a", "x = 0\n")
        task = load_manifest(self.dir.name)[0]
        sb = TaskSandbox(task)
        sb.restore()
        with open(sb.file, "w", encoding="utf-8") as f:
            f.write("x = 99\n")
        sb.restore()
        with open(sb.file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "x = 0\n")

    def test_task_sandbox_refuses_parent_dir_symlink_escape(self):
        """A taskdir/link -> /outside with file=link/victim must not pass
        containment: abspath is lexical, realpath is not."""
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        from harness.errors import HarnessError
        outside = os.path.join(self.dir.name, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("victim\n")
        taskdir = os.path.join(self.dir.name, "evil")
        os.makedirs(taskdir)
        try:
            os.symlink(outside, os.path.join(taskdir, "link"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        task = {"name": "evil", "dir": taskdir, "file": "link/victim.txt"}
        with self.assertRaisesRegex(HarnessError, "escapes its task directory"):
            TaskSandbox(task)
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "victim\n")

    def test_task_sandbox_refuses_planted_snapshot_link(self):
        """A pre-planted file.orig symlink would redirect the snapshot
        read/write to an arbitrary file on restore."""
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        from harness.errors import HarnessError
        make_task(self.dir.name, "a", "x = 0\n")
        task = load_manifest(self.dir.name)[0]
        sb = TaskSandbox(task)
        victim = os.path.join(self.dir.name, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("victim\n")
        try:
            os.symlink(victim, sb.snapshot)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(HarnessError, "symlink"):
            sb.restore()
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "victim\n")

    def test_missing_instruction_fails_clean_not_keyerror(self):
        """Every other manifest schema error aborts the run as a clean
        HarnessError (missing file, garbage shape); a missing instruction
        must not surface as a raw KeyError traceback instead."""
        from harness.errors import HarnessError
        make_task(self.dir.name, "a", "x = 0\n")
        engine = self.make_engine([comp("x = 1\n")])
        task = {"name": "a", "dir": os.path.join(self.dir.name, "a"),
                "file": "mod.py", "verify": "python check.py"}
        with self.assertRaisesRegex(HarnessError,
                                    "missing required key 'instruction'"):
            run_bench(engine, [task], runner=lambda cmd: (0, ""))

    def test_sandbox_restore_is_byte_exact_across_styles(self):
        """The snapshot's bytes are authoritative: restoring a CRLF snapshot
        over an LF-dirtied tree must bring back CRLF bytes exactly, not the
        tree's current style."""
        make_task(self.dir.name, "a", "x = 0\n")
        task = load_manifest(self.dir.name)[0]
        sb = TaskSandbox(task)
        with open(sb.file, "rb") as f:
            original = f.read()
        sb.restore()  # take the snapshot
        with open(sb.file, "wb") as f:
            f.write(b"x = 99\r\n")
        sb.restore()
        with open(sb.file, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_run_bench_all_pass(self):
        for name, code in [("a", "x = 0\n"), ("b", "x = 0\n")]:
            make_task(self.dir.name, name, code)
        tasks = load_manifest(self.dir.name)
        # one apply POST per task (consent off); content differs from original so
        # the vacuous-success guard is satisfied; fake runner always passes verify.
        engine = self.make_engine([comp("x = 1\n"), comp("x = 1\n")])
        report = run_bench(engine, tasks, runner=lambda cmd: (0, ""))
        b = report["bench"]
        self.assertEqual(b["tasks"], 2)
        self.assertEqual(b["statuses"], {"ok": 2})
        self.assertEqual(b["pass_rate"], 1.0)
        self.assertEqual(len(b["results"]), 2)
        self.assertEqual(report["dispatch_starts"], 2)

    def test_run_bench_records_one_failure(self):
        make_task(self.dir.name, "a", "x = 0\n")
        make_task(self.dir.name, "b", "x = 0\n")
        tasks = load_manifest(self.dir.name)
        # task a: 1 apply call + verify pass. task b: verify fails all 3 rounds
        # (max_rounds=3), so it needs 3 apply calls -> one ok, one verify_failed.
        engine = self.make_engine([comp("x = 1\n"), comp("x = 1\n"),
                                   comp("x = 1\n"), comp("x = 1\n")])
        # one verify result per apply round: a passes once, b fails all 3 rounds
        state = {"n": 0}
        results = [(0, ""), (1, "boom"), (1, "boom"), (1, "boom")]
        def runner(cmd):
            rc, out = results[state["n"]]
            state["n"] += 1
            return rc, out
        report = run_bench(engine, tasks, runner=runner)
        b = report["bench"]
        self.assertEqual(b["statuses"].get("ok"), 1)
        self.assertEqual(b["statuses"].get("verify_failed"), 1)
        self.assertEqual(b["pass_rate"], 0.5)


class BenchBudgetTests(unittest.TestCase):
    """The bench CLI can fund a task without editing its manifest, and the
    bundled fixtures carry a budget that covers the engine's own three-attempt
    worst case."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _engine(self):
        engine = mock.Mock()
        engine.ledger.participation_report.return_value = {}
        engine.apply_edit.return_value = {"status": "ok", "cost": 0.0}
        return engine

    def _task(self, name="a", task_max_cost=None):
        make_task(self.dir.name, name, "x = 0\n")
        path = os.path.join(self.dir.name, name, "task.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if task_max_cost is not None:
            data["task_max_cost"] = task_max_cost
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        return load_manifest(path)[0]

    def test_cli_default_fills_missing_task_budget(self):
        engine = self._engine()
        run_bench(engine, [self._task()], runner=lambda cmd: (0, ""),
                  task_max_cost=0.25)
        self.assertEqual(
            engine.apply_edit.call_args.kwargs["task_max_cost"], 0.25)

    def test_task_budget_wins_over_cli_default(self):
        engine = self._engine()
        run_bench(engine, [self._task(task_max_cost=0.4)],
                  runner=lambda cmd: (0, ""), task_max_cost=0.25)
        self.assertEqual(
            engine.apply_edit.call_args.kwargs["task_max_cost"], 0.4)

    def test_cli_forwards_task_max_cost(self):
        captured = {}

        def fake_run_bench(engine, tasks, **kwargs):
            captured.update(kwargs)
            return {"bench": {"statuses": {"ok": 1}}}

        with mock.patch.object(cli, "load_manifest", return_value=[{"name": "t"}]), \
             mock.patch.object(cli, "run_bench", side_effect=fake_run_bench), \
             mock.patch.object(cli, "_session", return_value=mock.Mock()):
            cli.main(["bench", "manifest.json", "--max-cost", "0.5",
                      "--task-max-cost", "0.3"])
        self.assertEqual(captured["task_max_cost"], 0.3)

    def test_bundled_fixtures_declare_an_affordable_budget(self):
        """A fixture without task_max_cost inherits bench's 0.05 fallback,
        which a trust-stepped paid route cannot fund (worst case ~$0.168), so
        every shipped fixture must carry its own ceiling."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(glob.glob(os.path.join(root, "bench", "tasks", "*",
                                              "task.json")))
        self.assertEqual(len(paths), 9)
        for path in paths:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertGreaterEqual(
                data.get("task_max_cost", 0.0), 0.2,
                f"{path} must pin a task_max_cost above the engine's "
                f"three-attempt worst case")


if __name__ == "__main__":
    unittest.main()
