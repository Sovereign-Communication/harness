import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
