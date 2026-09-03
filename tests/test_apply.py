import os
import tempfile
import unittest

from harness.apply import ApplyEngine
from harness.core import SpendGovernor, HarnessError
from harness.ledger import AutonomyLedger
from harness.router import Router
from tests._fake import FakeTransport, m, comp, consent

JUDGE = "inclusionai/ling-2.6-flash"
APPLY = "deepseek/deepseek-chat"
ESC = "qwen/qwen3-max"

ORIGINAL = "def add(a, b):\n    return a + b\n"
CHANGED = "def add(a, b):\n    return a + b + 0\n"


def scripted_run(results):
    state = {"n": 0}

    def runner(cmd):
        rc, out = results[min(state["n"], len(results) - 1)]
        state["n"] += 1
        return rc, out

    return runner


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.dir.name, "ledger.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def make_file(self, content=ORIGINAL):
        p = os.path.join(self.dir.name, "math.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def make_env(self, posts=None, run=None, router_kw=None, default_consent=True):
        fake = FakeTransport(models=[m(APPLY), m(JUDGE), m(ESC)], posts=posts)
        gov = SpendGovernor(fake, "sk-test")
        ledger = AutonomyLedger(self.ledger_path)
        router = Router(["a", "b"], JUDGE, APPLY, **(router_kw or {}))
        engine = ApplyEngine(fake, "k", gov, ledger, router,
                             default_require_consent=default_consent)
        if run:
            engine.run_verify = run
        return fake, gov, ledger, engine

    def leftovers(self):
        return [n for n in os.listdir(self.dir.name) if n.endswith(".tmp")]

    # ---- scope gates ----
    def test_missing_file_refused(self):
        _, _, _, engine = self.make_env()
        with self.assertRaises(HarnessError):
            engine.apply_edit(task_id="t1", file_path=os.path.join(self.dir.name, "nope.py"),
                              instruction="change it", require_consent=False)

    def test_file_too_large_refused(self):
        p = self.make_file("\n".join(f"line {i}" for i in range(501)) + "\n")
        _, _, _, engine = self.make_env()
        with self.assertRaises(HarnessError):
            engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                              require_consent=False)

    def test_instruction_too_long_refused(self):
        p = self.make_file()
        _, _, _, engine = self.make_env()
        with self.assertRaises(HarnessError):
            engine.apply_edit(task_id="t1", file_path=p, instruction="x" * 1001,
                              require_consent=False)

    # ---- verification loop ----
    def test_happy_path(self):
        p = self.make_file()
        fake, gov, ledger, engine = self.make_env(
            posts=[comp(CHANGED)], run=scripted_run([(0, "")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="add +0",
                                   verify_cmd="python -m py_compile math.py",
                                   require_consent=False)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["changed"])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), CHANGED)
        self.assertTrue(os.path.exists(result["backup"]))
        events = [e["event"] for e in ledger.entries()]
        self.assertEqual(events, ["dispatch_start", "verify_round", "complete"])

    def test_verify_fail_then_pass(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(
            posts=[comp(CHANGED + "\n"), comp(CHANGED)],
            run=scripted_run([(1, "syntax error"), (0, "")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="fix",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["rounds"][0]["status"], "verify_failed")
        self.assertIn("syntax error", result["rounds"][0]["verify_output"])

    def test_vacuous_success_rejected(self):
        """Verify passed but the model returned no change: NOT success."""
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(ORIGINAL)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        self.assertEqual(result["rounds"][-1]["status"], "vacuous")

    def test_rounds_exhausted(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED), comp(CHANGED)],
                                        run=scripted_run([(1, "err"), (1, "err")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="fix",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=2)
        self.assertEqual(result["status"], "verify_failed")
        self.assertEqual(result["verify"]["passed"], False)

    def test_escalation_after_exhaustion(self):
        p = self.make_file()
        _, _, ledger, engine = self.make_env(
            posts=[comp(CHANGED), comp(CHANGED + "#esc")],
            run=scripted_run([(1, "err"), (0, "")]),
            router_kw={"escalation_model": ESC, "allow_escalation": True})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="fix",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result.get("escalated"))
        events = [e["event"] for e in ledger.entries()]
        self.assertIn("escalate", events)

    def test_no_escalation_without_gate(self):
        p = self.make_file()
        _, _, ledger, engine = self.make_env(posts=[comp(CHANGED)],
                                             run=scripted_run([(1, "err")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="fix",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        self.assertNotIn("escalate", [e["event"] for e in ledger.entries()])

    # ---- sovereignty gate ----
    def test_consent_decline_blocks_dispatch(self):
        p = self.make_file()
        fake, _, _, engine = self.make_env(posts=[consent("decline", "not aligned")],
                                           default_consent=True)
        result = engine.apply_edit(task_id="t1", file_path=p,
                                   instruction="rewrite the protocol core")
        self.assertEqual(result["status"], "consent_blocked")
        self.assertEqual(result["decision"], "decline")
        self.assertEqual(len(fake.chat_posts()), 1, "no apply call after decline")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL, "file must be untouched")

    def test_consent_accept_then_apply(self):
        p = self.make_file()
        fake, _, _, engine = self.make_env(
            posts=[consent("accept", "fits"), comp(CHANGED)],
            run=scripted_run([(0, "")]), default_consent=True)
        result = engine.apply_edit(task_id="t1", file_path=p,
                                   instruction="add +0", verify_cmd="check")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(fake.chat_posts()), 2)

    # ---- file safety ----
    def test_atomic_write_no_leftover_tmp(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                          verify_cmd="check", require_consent=False)
        self.assertEqual(self.leftovers(), [])

    def test_fenced_block_content_extracted(self):
        p = self.make_file()
        fenced = "```python\n" + CHANGED + "```"
        _, _, _, engine = self.make_env(posts=[comp(fenced)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False)
        self.assertEqual(result["status"], "ok")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), CHANGED)


if __name__ == "__main__":
    unittest.main()