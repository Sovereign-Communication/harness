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
CODER_A = "cohere/north-mini-code:free"
CODER_B = "z-ai/glm-5.2:free"

ORIGINAL = "def add(a, b):\n    return a + b\n"
CHANGED = "def add(a, b):\n    return a + b + 0\n"
PARTIAL = "def add(a, b):\n    return a + b  # WIP\n"


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

    def make_env(self, posts=None, run=None, router_kw=None, default_consent=True,
                 renew=False, models=None):
        fake = FakeTransport(models=models or [m(APPLY), m(JUDGE), m(ESC),
                                               m(CODER_A), m(CODER_B)], posts=posts)
        gov = SpendGovernor(fake, "sk-test")
        ledger = AutonomyLedger(self.ledger_path)
        router = Router(["a", "b"], JUDGE, APPLY, **(router_kw or {}))
        engine = ApplyEngine(fake, "k", gov, ledger, router,
                             default_require_consent=default_consent,
                             default_renew_consent=renew)
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
        # verify_failed hands back a continuation so the next iteration can resume
        self.assertIn("continuation", result)

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

    # ---- continued consensus (renewal) ----
    def test_consent_renew_defer_stops_mid_task(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[consent("accept", "ok"), consent("defer", "changed my mind")],
            default_consent=True, renew=True)
        result = engine.apply_edit(task_id="t1", file_path=p,
                                   instruction="rewrite the parser")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["category"], "consent")
        self.assertIn("continuation", result)
        events = [e["event"] for e in ledger.entries()]
        self.assertIn("defer_midtask", events)
        self.assertEqual(len(fake.chat_posts()), 2, "no apply model call after deferral")

    # ---- capability-blocker dovetail ----
    def test_capability_deferral_captures_prose_reason(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp(PARTIAL + "HARNESS_DEFER: not qualified to make this constant-time")],
            renew=False)
        result = engine.apply_edit(task_id="t1", file_path=p,
                                   instruction="fix the crypto", verify_cmd="check",
                                   require_consent=False)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["category"], "capability")
        self.assertIn("not qualified", result["reason"])

    def test_capability_deferral_preserves_partial(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp(PARTIAL + "HARNESS_DEFER: "
                        '{"remaining_scope":"finish error handling",'
                        '"reason":"out of my depth on the crypto edge case"}')],
            renew=False)
        result = engine.apply_edit(task_id="t1", file_path=p,
                                   instruction="add error handling",
                                   verify_cmd="check", require_consent=False)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["category"], "capability")
        self.assertEqual(result["remaining_scope"], "finish error handling")
        self.assertIn("continuation", result)
        # partial work was written to the file
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), PARTIAL.strip())
        events = [e["event"] for e in ledger.entries()]
        self.assertIn("defer_midtask", events)
        self.assertEqual(len(fake.chat_posts()), 1, "no verify / no further model calls")

    # ---- rotation on error ----
    def test_rotation_on_model_error(self):
        p = self.make_file()
        fake, _, _, engine = self.make_env(
            posts=[(429, {"error": {"message": "rate limited"}}), comp(CHANGED)],
            run=scripted_run([(0, "")]),
            router_kw={"apply_pool": [CODER_A, CODER_B]})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False)
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["rotations"], 1)
        # the failing model (APPLY) was skipped; a pool model did the work
        used_models = {r["model"] for r in result["rounds"]}
        self.assertIn(CODER_A, used_models)
        self.assertNotIn(APPLY, used_models)

    # ---- continuation ----
    def test_continuation_resumes_deferred_task(self):
        p = self.make_file()
        # run 1: model defers
        _, _, _, engine = self.make_env(
            posts=[comp(PARTIAL + "HARNESS_DEFER: "
                        '{"remaining_scope":"finish +0","reason":"low on tokens"}')],
            renew=False)
        r1 = engine.apply_edit(task_id=None, file_path=p, instruction="add +0",
                               verify_cmd="check", require_consent=False)
        self.assertEqual(r1["status"], "deferred")
        state = r1["continuation"]
        self.assertEqual(state["remaining_scope"], "finish +0")

        # run 2: resume from the deferred state with a fresh model
        p2 = self.make_file()  # same original file path semantics; reuse state path
        # reuse the same engine but with a completing model response
        fake2 = FakeTransport(models=[m(APPLY), m(JUDGE), m(ESC), m(CODER_A), m(CODER_B)],
                              posts=[comp(CHANGED)])
        gov2 = SpendGovernor(fake2, "sk-test")
        ledger2 = AutonomyLedger(os.path.join(self.dir.name, "ledger2.jsonl"))
        router2 = Router(["a", "b"], JUDGE, APPLY)
        engine2 = ApplyEngine(fake2, "k", gov2, ledger2, router2,
                              default_require_consent=True, default_renew_consent=False)
        engine2.run_verify = scripted_run([(0, "")])
        r2 = engine2.apply_edit(continuation=state, verify_cmd="check",
                                require_consent=False)
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["task_id"], r1["task_id"])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), CHANGED.strip())

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