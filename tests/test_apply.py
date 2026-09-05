import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.apply import ApplyEngine, _parse_ready, _extract_file_content
from harness.cli import main as cli_main
from harness.core import SpendGovernor, HarnessError
from harness.ledger import AutonomyLedger
from harness.router import Router
from tests._fake import FakeTransport, m, comp, consent

JUDGE = "inclusionai/ling-2.6-flash"
APPLY = "deepseek/deepseek-chat"
ESC = "qwen/qwen3-max"
CODER_A = "cohere/north-mini-code:free"
CODER_B = "z-ai/glm-5.2:free"
MORPH = "morph/morph-v3-fast"

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
        self.assertEqual(events, ["dispatch_start", "model_result", "verify_round", "complete"])

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

    def test_consent_and_rotation_costs_are_reported_together(self):
        """Consent, a failed primary call, and its replacement all appear in
        the same tracked total and remain below the configured ceiling."""
        p = self.make_file()
        accepted = consent("accept", "fits")
        accepted["usage"]["cost"] = 0.0001
        fake, gov, ledger, engine = self.make_env(
            posts=[
                accepted,
                (429, {"error": {"message": "rate limited"},
                       "usage": {"cost": 0.0002}}),
                comp(CHANGED, cost=0.0003),
            ],
            run=scripted_run([(0, "")]), default_consent=True,
            router_kw={"apply_pool": [CODER_A]})
        result = engine.apply_edit(task_id="costs", file_path=p,
                                   instruction="change", verify_cmd="check",
                                   max_rounds=1, task_max_cost=0.001)
        report = ledger.participation_report()
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(gov.spent, 0.0006, places=9)
        self.assertAlmostEqual(result["cost"], gov.spent, places=9)
        self.assertAlmostEqual(report["tracked_cost"], gov.spent, places=9)
        self.assertLessEqual(gov.spent, gov.max_cost)
        model_events = [e for e in ledger.entries() if e["event"] == "model_result"]
        self.assertEqual(len(model_events), 2)
        self.assertAlmostEqual(sum(e["cost"] for e in model_events), 0.0005, places=9)

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

    # ---- readiness verdict (forced self-check) ----
    def test_ready_defer_rotates_to_next_model(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp("HARNESS_READY: defer cannot verify timing safety\n"),
                   comp("HARNESS_READY: confident\n" + CHANGED)],
            run=scripted_run([(0, "")]),
            router_kw={"apply_pool": [CODER_A, CODER_B]})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rotations=3)
        self.assertEqual(result["status"], "ok")
        # the deferring model rotated away; a pool model completed the work
        ready_events = [e for e in ledger.entries() if e["event"] == "readiness"]
        self.assertEqual([e["decision"] for e in ready_events], ["defer", "confident"])

    def test_ready_defer_all_models_accepts_deferral(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp("HARNESS_READY: defer not qualified\n"),
                   comp("HARNESS_READY: defer still not qualified\n")],
            router_kw={"apply_pool": [CODER_A]})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rotations=3)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["category"], "readiness")
        self.assertIn("not qualified", result["reason"])
        events = [e["event"] for e in ledger.entries()]
        self.assertIn("defer_midtask", events)

    def test_ready_confident_proceeds(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp("HARNESS_READY: confident\n" + CHANGED)],
            run=scripted_run([(0, "")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False)
        self.assertEqual(result["status"], "ok")
        ready = [e for e in ledger.entries() if e["event"] == "readiness"]
        self.assertEqual(ready[0]["decision"], "confident")

    def test_ready_confident_that_fails_verify_counts_in_calibration(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp("HARNESS_READY: confident\n" + CHANGED)],
            run=scripted_run([(1, "boom")]))
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        # verify_round carries the readiness for calibration join
        vr = [e for e in ledger.entries() if e["event"] == "verify_round"][0]
        self.assertEqual(vr["readiness"], "confident")

    # ---- reasoning-only responses are never written to the file ----
    def test_reasoning_only_response_rotates_not_written(self):
        """A model returning only hidden reasoning (no content) must not have its
        reasoning trace written to the target file."""
        p = self.make_file()
        reason_only = comp(None, reasoning="I think a + b would be right...")
        fake, _, _, engine = self.make_env(
            posts=[reason_only, comp(CHANGED)],
            run=scripted_run([(0, "")]),
            router_kw={"apply_pool": [CODER_A, CODER_B]})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rotations=3)
        self.assertEqual(result["status"], "ok")
        # the reasoning trace never landed in the file
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), CHANGED)
        self.assertGreaterEqual(result["rotations"], 1)

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
        verify_calls = []
        engine2.run_verify = lambda command: (verify_calls.append(command) or (0, ""))
        r2 = engine2.apply_edit(continuation=state, require_consent=False)
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["task_id"], r1["task_id"])
        self.assertEqual(verify_calls, ["check"], "the saved gate must be reused")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), CHANGED.strip())

    def test_failed_continuation_requires_authoritative_verify_cmd(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(
            posts=[comp(CHANGED)], run=scripted_run([(1, "verification failed")]))
        failed = engine.apply_edit(task_id="gated", file_path=p, instruction="change",
                                   verify_cmd="authoritative-check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(failed["status"], "verify_failed")
        state = dict(failed["continuation"])
        state.pop("verify_cmd")

        fake2, _, _, engine2 = self.make_env(posts=[comp(CHANGED)])
        with self.assertRaisesRegex(HarnessError, "missing its authoritative verify_cmd"):
            engine2.apply_edit(continuation=state, require_consent=False)
        self.assertEqual(fake2.chat_posts(), [], "invalid state must fail before dispatch")

    def test_cli_rejects_ungated_continuation_before_key_setup(self):
        """The CLI boundary must reject a failed state before touching credentials."""
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"file_path": os.path.join(td, "math.py"),
                           "verify_only": False,
                           "verification_required": True}, f)
            with mock.patch("harness.cli._governor",
                            side_effect=AssertionError("key setup must not run")):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        cli_main(["apply", "--continue-from", state_path])
            self.assertEqual(ctx.exception.code, 1)

    def test_gated_continuation_rejects_verify_only_override(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(1, "verification failed")]))
        failed = engine.apply_edit(task_id="gated-preview", file_path=p,
                                   instruction="change", verify_cmd="check",
                                   require_consent=False, max_rounds=1)
        with self.assertRaisesRegex(HarnessError, "cannot be resumed as verify-only"):
            engine.apply_edit(continuation=failed["continuation"], verify_only=True,
                              require_consent=False)

    # ---- MorphLite-compatible backend ----
    def test_morph_verify_only_is_read_only(self):
        p = self.make_file()
        fake, _, ledger, engine = self.make_env(
            posts=[comp(CHANGED)],
            models=[m(APPLY), m(JUDGE), m(ESC), m(CODER_A), m(CODER_B), m(MORPH)])
        verify_calls = []
        engine.run_verify = lambda command: verify_calls.append(command)

        result = engine.apply_edit(
            task_id="morph-preview", file_path=p, instruction="add zero safely",
            edit_snippet="return a + b + 0", verify_cmd="must-not-run",
            require_consent=False, renew_consent=False, backend="morph",
            verify_only=True, max_tokens=64)

        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["backend"], "morph")
        self.assertTrue(result["verify_only"])
        self.assertEqual(result["proposed_content"], CHANGED)
        self.assertIsNone(result["backup"])
        self.assertEqual(verify_calls, [])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)
        payload = fake.payloads()[0]
        self.assertEqual(payload["model"], MORPH)
        prompt = payload["messages"][0]["content"]
        self.assertIn("<instruction>add zero safely</instruction>", prompt)
        self.assertIn("<code>" + ORIGINAL + "</code>", prompt)
        self.assertIn("<update>return a + b + 0</update>", prompt)
        self.assertEqual([e["event"] for e in ledger.entries()],
                         ["dispatch_start", "model_result", "complete"])

    def test_morph_verify_only_capability_deferral_does_not_write_partial(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(
            posts=[comp(PARTIAL + "HARNESS_DEFER: finish the timing proof")],
            models=[m(APPLY), m(JUDGE), m(ESC), m(CODER_A), m(CODER_B), m(MORPH)])

        result = engine.apply_edit(
            task_id="morph-defer-preview", file_path=p, instruction="fix timing safety",
            require_consent=False, renew_consent=False, backend="morph",
            verify_only=True, max_tokens=64)

        self.assertEqual(result["status"], "deferred")
        self.assertTrue(result["verify_only"])
        self.assertEqual(result["continuation"]["backend"], "morph")
        self.assertTrue(result["continuation"]["verify_only"])
        self.assertIsNone(result.get("backup"))
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)

    def test_morph_verify_only_continuation_stays_gate_free(self):
        """A preview continuation may carry a historical gate, but resuming it
        must remain read-only and must not execute that gate."""
        p = self.make_file()
        models = [m(APPLY), m(JUDGE), m(ESC), m(CODER_A), m(CODER_B), m(MORPH)]
        _, _, _, engine = self.make_env(
            posts=[comp(PARTIAL + "HARNESS_DEFER: finish the timing proof")],
            models=models)
        first = engine.apply_edit(
            task_id="morph-preview-resume", file_path=p, instruction="fix timing safety",
            verify_cmd="must-not-run", require_consent=False, renew_consent=False,
            backend="morph", verify_only=True, max_tokens=64)
        state = first["continuation"]
        self.assertEqual(state["verify_cmd"], "must-not-run")

        fake2, _, _, engine2 = self.make_env(posts=[comp(CHANGED)], models=models)
        verify_calls = []
        engine2.run_verify = lambda command: verify_calls.append(command)
        resumed = engine2.apply_edit(continuation=state, require_consent=False)
        self.assertEqual(resumed["status"], "preview")
        self.assertEqual(verify_calls, [])
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)
        self.assertEqual(len(fake2.chat_posts()), 1)

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


class MarkerLeakTests(unittest.TestCase):
    """Regression (live playtest, openrouter/free): the readiness marker was
    emitted after blank lines and leaked verbatim into the written file."""

    def test_ready_marker_after_blank_lines_is_parsed_and_stripped(self):
        body = '\n\nHARNESS_READY: confident\n"""doc"""\ndef f():\n    pass\n'
        decision, reason, rest = _parse_ready(body)
        self.assertEqual(decision, "confident")
        self.assertNotIn("HARNESS_READY", rest)
        self.assertNotIn("HARNESS_READY", _extract_file_content(rest))

    def test_ready_marker_anywhere_never_lands_in_file(self):
        for placement in (
            'HARNESS_READY: confident\ncode\n',
            '\n\n\nHARNESS_READY: confident\ncode\n',
            'code before marker\nHARNESS_READY: confident\nmore code\n',
        ):
            content = _extract_file_content(placement)
            self.assertNotIn("HARNESS_READY", content,
                             f"marker leaked for {placement!r}")

    def test_no_marker_unchanged_behavior(self):
        decision, _, rest = _parse_ready("plain code\n")
        self.assertEqual(decision, "missing")
        self.assertEqual(rest, "plain code\n")
        self.assertEqual(_extract_file_content("```python\ncode\n```\n"), "code\n")

    def test_gate_broken_stops_retries_with_diagnostic(self):
        """A gate failing identically twice is broken; the engine must stop
        burning rounds and say so instead of re-prompting the model."""
        orig = tempfile.mkdtemp()
        target = os.path.join(orig, "t.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 0\n")
        calls = {"n": 0}
        def runner(cmd):
            calls["n"] += 1
            return 1, "FileNotFoundError: nope"
        fake = FakeTransport(models=[m(CODER_A)],
                             posts=[comp("HARNESS_READY: confident\nx = 1\n"),
                                    comp("HARNESS_READY: confident\nx = 2\n"),
                                    comp("HARNESS_READY: confident\nx = 3\n")])
        gov = SpendGovernor(fake, "sk-test")
        ledger = AutonomyLedger(os.path.join(orig, "led.jsonl"))
        engine = ApplyEngine(fake, "k", gov, ledger,
                             Router([CODER_A], CODER_A, CODER_A),
                             default_require_consent=False,
                             run_verify=runner, default_renew_consent=False)
        result = engine.apply_edit(task_id="gb", file_path=target,
                                   instruction="change x", verify_cmd="false",
                                   max_rounds=3)
        self.assertLess(calls["n"], 3,
                        "broken gate must not consume the full retry budget")
        self.assertEqual(result["rounds"][-1]["status"], "gate_broken")
        self.assertIn("broken", result["rounds"][-1]["reason"])


if __name__ == "__main__":
    unittest.main()