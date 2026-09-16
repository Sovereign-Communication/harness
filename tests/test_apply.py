import os
import shutil
import tempfile
import unittest

from harness.apply import ApplyEngine
from harness.batch import BatchOptions
from harness.errors import HarnessError
from harness.filesafety import _atomic_write
from harness.ledger import AutonomyLedger
from harness.router import Router
from harness.spend import SpendGovernor
from tests._applyfixture import (APPLY, ApplyFixture, CODER_A, CODER_B, CHANGED,
                                 ESC, JUDGE, ORIGINAL, PARTIAL, scripted_run)
from tests._fake import FakeTransport, comp, consent, m


class ApplyTests(ApplyFixture):

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
        # The target file is UNTOUCHED: no gate has passed over the partial,
        # so it must never reach the working tree (round-1 dogfood found a
        # deferred run corrupting the tree this way).
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), ORIGINAL.strip())
        # The partial travels in the continuation state instead.
        self.assertEqual(result["continuation"].get("partial_content"), PARTIAL.strip())
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

    # ---- terminal api_error rounds name the real failure (behavior-driver finding) ----
    def test_terminal_api_error_names_http_status(self):
        """A 429 whose provider message text lacks a literal '429' ('Rate limit
        exceeded: free-models-per-day') must still be visible as a rate limit
        in the terminal round: the CLI's saturation guidance reads that text."""
        p = self.make_file()
        body = {"error": {"message": "Rate limit exceeded: free-models-per-day. "
                                     "Please try again later."}}
        fake, _, _, engine = self.make_env(
            posts=[("429", body), ("429", body), ("429", body)],
            run=scripted_run([(0, "")]),
            router_kw={"apply_pool": [CODER_A, CODER_B]})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        last = result["rounds"][-1]
        self.assertEqual(last["status"], "api_error")
        self.assertIn("HTTP 429", last["error"])

    def test_terminal_api_error_reasoning_only_is_human_readable(self):
        """Reasoning-only exhaustion ends on an HTTP 200 response (no error
        key), which used to dump the raw JSON body as the terminal error."""
        p = self.make_file()
        fake, _, _, engine = self.make_env(
            posts=[comp(None, reasoning="think"), comp(None, reasoning="think"),
                   comp(None, reasoning="think")],
            router_kw={"apply_pool": [CODER_A, CODER_B]})
        result = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                   verify_cmd="check", require_consent=False,
                                   max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        last = result["rounds"][-1]
        self.assertEqual(last["status"], "api_error")
        self.assertIn("no usable content", last["error"])
        self.assertNotIn("choices", last["error"])

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
        self.make_file()  # same original file path semantics; reuse state path
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

    def test_continuation_resumes_under_saved_task_id(self):
        """A resume is the same task: ledger attribution must stay under the
        original task id even when the resume goes through apply_batch (which
        used to fall back to its own default before consulting the saved
        state), and an explicit override must still win."""
        p = self.make_file()
        _, _, _, engine = self.make_env(
            posts=[comp(PARTIAL + "HARNESS_DEFER: "
                        '{"remaining_scope":"finish +0","reason":"low on tokens"}')],
            renew=False)
        r1 = engine.apply_edit(task_id="orig-task", file_path=p, instruction="add +0",
                               verify_cmd="check", require_consent=False)
        self.assertEqual(r1["status"], "deferred")
        self.make_file()
        fake2 = FakeTransport(models=[m(APPLY), m(JUDGE), m(ESC), m(CODER_A)],
                              posts=[comp(CHANGED)])
        gov2 = SpendGovernor(fake2, "sk-test")
        ledger2 = AutonomyLedger(os.path.join(self.dir.name, "ledger3.jsonl"))
        engine2 = ApplyEngine(fake2, "k", gov2, ledger2,
                              Router(["a"], JUDGE, APPLY),
                              default_require_consent=True, default_renew_consent=False)
        engine2.run_verify = lambda command: (0, "")
        r2 = engine2.apply_batch(
            [None], options=BatchOptions(
                instruction=None, verify_cmd="check",
                require_consent=False,
                continuation=r1["continuation"]))
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["task_id"], "orig-task")

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

    # ---- file safety ----
    def test_atomic_write_no_leftover_tmp(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                          verify_cmd="check", require_consent=False)
        self.assertEqual(self.leftovers(), [])

    def test_continuation_gate_does_not_leak_into_fresh_applies(self):
        """Regression (playtest): resuming a gated apply pinned the gate on the
        engine forever, so a later FRESH apply with a different gate was
        refused with 'verify gate changed'. The MCP server keeps one engine
        for its whole lifetime, so one resume would break every apply after
        it. Per-request gate state must reset at the start of each apply."""
        p = self.make_file()
        _, _, _, engine = self.make_env(
            # t1 fails verify each round (others are vacuous replays); the
            # resumed round must emit DIFFERENT content from the failed attempt
            # on disk, or the vacuous-success guard consumes rounds.
            posts=[comp(CHANGED), comp(CHANGED), comp(CHANGED),
                   comp(CHANGED + "\n"), comp(CHANGED)],
            run=scripted_run([(1, "boom"), (0, ""), (0, "")]))
        first = engine.apply_edit(task_id="t1", file_path=p, instruction="change",
                                  verify_cmd="gateA", require_consent=False)
        self.assertEqual(first["status"], "verify_failed")
        resumed = engine.apply_edit(continuation=first["continuation"],
                                    require_consent=False)
        self.assertEqual(resumed["status"], "ok")
        # The fresh apply on the SAME engine with a DIFFERENT gate must run.
        with open(p, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        fresh = engine.apply_edit(task_id="t3", file_path=p, instruction="fresh",
                                  verify_cmd="gateB", require_consent=False)
        self.assertEqual(fresh["status"], "ok")
        # Gate binding is request-local; no engine-level continuation state remains.

    def test_backup_filename_survives_slashed_task_ids(self):
        """Regression (playtest): bench names tasks 'bench/<name>' and the slash
        landed in the backup FILENAME, breaking open() on every platform -- so
        bench ran with its backup safety net silently disabled."""
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_edit(task_id="bench/add", file_path=p,
                                   instruction="change", verify_cmd="check",
                                   require_consent=False)
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["backup"])
        self.assertTrue(os.path.exists(result["backup"]))
        self.assertNotIn("/", os.path.basename(result["backup"]))

    # ---- multi-file batch (engine-owned; CLI and MCP both land here) ----
    def test_apply_batch_success_aggregates(self):
        a = self.make_file()
        b = os.path.join(self.dir.name, "other.py")
        with open(b, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        _, _, _, engine = self.make_env(posts=[comp(CHANGED), comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_batch(
            [a, b], options=BatchOptions(instruction="change",
                                         verify_cmd="check",
                                         require_consent=False))
        self.assertTrue(result["batch"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["statuses"], {"ok": 2})
        self.assertEqual(result["verify"], {"command": "check", "passed": True})
        for r in result["results"]:
            self.assertEqual(r["status"], "ok")

    def test_apply_batch_fails_fast(self):
        a = self.make_file()
        b = os.path.join(self.dir.name, "other.py")
        with open(b, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        _, gov, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(1, "boom")]))
        result = engine.apply_batch(
            [a, b], options=BatchOptions(instruction="change",
                                         verify_cmd="check",
                                         require_consent=False,
                                         max_rounds=1))
        # The first file's gate failed; the batch stops and b is never touched.
        # A multi-file batch returns the ENVELOPE even when fail-fast kills it
        # on file 1 -- consumers keying on "results" must be able to tell a
        # batch death from a single-file run (live playtest finding).
        self.assertEqual(result["status"], "verify_failed")
        self.assertTrue(result["batch"])
        self.assertEqual(result["statuses"], {"verify_failed": 1})
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["verify"], {"command": "check", "passed": False})
        self.assertEqual(len(gov.transport.chat_posts()), 1)
        with open(b, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)

    def test_apply_batch_keep_going_preserves_failures(self):
        """keep_going (fail-soft): the loop continues past file 1's gate
        failure; every per-file result -- failures included -- stays in
        the envelope, and the overall status names the FIRST failure (a
        later success must never mask it into a mixed-batch 'ok')."""
        a = self.make_file()
        b = os.path.join(self.dir.name, "other.py")
        with open(b, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        _, _, _, engine = self.make_env(posts=[comp(CHANGED), comp(CHANGED)],
                                        run=scripted_run([(1, "boom"), (0, "")]))
        result = engine.apply_batch(
            [a, b], options=BatchOptions(instruction="change",
                                         verify_cmd="check",
                                         require_consent=False,
                                         max_rounds=1),
            keep_going=True)
        self.assertTrue(result["batch"])
        self.assertEqual(result["status"], "verify_failed")
        self.assertEqual(result["statuses"], {"verify_failed": 1, "ok": 1})
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["status"], "verify_failed")
        self.assertEqual(result["results"][1]["status"], "ok")
        # The shared-gate verdict is honest for the mixed batch: not passed.
        self.assertEqual(result["verify"], {"command": "check", "passed": False})
        self.assertEqual(result["files"], [a, b])
        # The failed run rewound its file; the successful one applied.
        with open(a, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)
        with open(b, encoding="utf-8") as f:
            self.assertEqual(f.read(), CHANGED)

    def test_apply_batch_file2_death_reports_gate_not_passed(self):
        """The envelope's shared-gate verify block derives "passed" from the
        last file's actual verdict. The historical code hardcoded passed=True
        from the first file's gate -- reporting a failed batch as gate-passed."""
        a = self.make_file()
        b = os.path.join(self.dir.name, "other.py")
        with open(b, "w", encoding="utf-8") as f:
            f.write(ORIGINAL)
        _, _, _, engine = self.make_env(posts=[comp(CHANGED), comp(CHANGED)],
                                        run=scripted_run([(0, ""), (1, "boom")]))
        result = engine.apply_batch(
            [a, b], options=BatchOptions(instruction="change",
                                         verify_cmd="check",
                                         require_consent=False,
                                         max_rounds=1))
        self.assertEqual(result["status"], "verify_failed")
        self.assertEqual(result["statuses"], {"ok": 1, "verify_failed": 1})
        self.assertEqual(result["verify"], {"command": "check", "passed": False})

    def test_apply_batch_single_file_returns_bare_result(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_batch(
            [p], task_id="same-task",
            options=BatchOptions(instruction="change", verify_cmd="check",
                                 require_consent=False))
        self.assertNotIn("batch", result)
        self.assertEqual(result["status"], "ok")
        # An explicit task id passes through unsuffixed: one file is not a batch.
        self.assertEqual(result["task_id"], "same-task")

    def test_apply_batch_router_is_never_mutated(self):
        """Routing is per-request: capability ordering must order the request's
        pool without ever writing to the router (the MCP server shares one
        router across sessions; a mutation there leaks routing state)."""
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        before_pool = list(engine.router.apply_pool)
        engine.apply_batch([p], options=BatchOptions(
            instruction="change", verify_cmd="check",
            require_consent=False))
        self.assertEqual(engine.router.apply_pool, before_pool)

    def test_malformed_diff_is_retried_with_feedback_not_fatal(self):
        """A strict-merge rejection must feed the next round as feedback, not
        kill the run as FATAL (the contract the diff backend promised;
        dogfooding the harness on its own repo caught it escaping)."""
        p = self.make_file()
        bad = ("--- a/math.py\n+++ b/math.py\n@@ -1,2 +1,2 @@\n"
               "-def wrong(a, b):\n-    return a + b\n"
               "+def add(a, b):\n+    return a + b + 0\n")
        good = ("--- a/math.py\n+++ b/math.py\n@@ -1,2 +1,2 @@\n"
                "-def add(a, b):\n-    return a + b\n"
                "+def add(a, b):\n+    return a + b + 0\n")
        fake, _, _, engine = self.make_env(posts=[comp(bad), comp(good)],
                                           run=scripted_run([(0, "")]))
        result = engine.apply_batch(
            [p], options=BatchOptions(instruction="add +0",
                                      verify_cmd="check",
                                      require_consent=False,
                                      backend="diff"))
        self.assertEqual(result["status"], "ok")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), CHANGED)
        self.assertEqual(result["rounds"][0]["status"], "merge_failed")
        self.assertIn("diff merge failed", result["rounds"][0]["verify_output"])
        # The merge error reached the retry round as feedback.
        retry_msgs = fake.chat_posts()[1][2]["messages"]
        self.assertIn("does not match the source", retry_msgs[-1]["content"])

    def test_two_identical_merge_failures_are_not_gate_broken(self):
        """Live finding: two identical MERGE errors collided with the broken-
        gate detector (same verify_output twice) and aborted the run. Merge
        feedback is model error, not gate evidence -- it must keep retrying."""
        p = self.make_file()
        bad = ("--- a/math.py\n+++ b/math.py\n@@ -1,2 +1,2 @@\n"
               "-def wrong(a, b):\n-    return a + b\n"
               "+def add(a, b):\n+    return a + b + 0\n")
        fake, _, _, engine = self.make_env(posts=[comp(bad), comp(bad), comp(bad)],
                                           run=scripted_run([(1, "unused")]))
        result = engine.apply_batch(
            [p], options=BatchOptions(instruction="add +0",
                                      verify_cmd="check",
                                      require_consent=False,
                                      backend="diff"))
        statuses = [r["status"] for r in result["rounds"]]
        self.assertNotIn("gate_broken", statuses)
        self.assertEqual(statuses.count("merge_failed"), 3)

    def test_failed_run_rewinds_tree_to_pre_run_content(self):
        """Live finding: a run whose gate never passed left its failed edit in
        the target file. The tree must end a failed run exactly as it began."""
        p = self.make_file()
        good = ("--- a/math.py\n+++ b/math.py\n@@ -1,2 +1,2 @@\n"
                "-def add(a, b):\n-    return a + b\n"
                "+def add(a, b):\n+    return a + b + 0\n")
        fake, _, _, engine = self.make_env(posts=[comp(good), comp(good), comp(good)],
                                           run=scripted_run([(1, "E: gate failed"),
                                                             (1, "E: gate failed")]))
        result = engine.apply_batch(
            [p], options=BatchOptions(instruction="add +0",
                                      verify_cmd="check",
                                      require_consent=False,
                                      backend="diff"))
        self.assertEqual(result["status"], "verify_failed")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)

    def test_failed_run_rewind_preserves_crlf_bytes(self):
        """Live dogfood finding on a CRLF checkout: the failed-run rewind
        must restore the exact pre-run bytes, and the model's own LF write
        must already have preserved the tree style (no EOL laundering)."""
        p = self.make_file()
        crlf = ORIGINAL.replace("\n", "\r\n").encode("utf-8")
        with open(p, "wb") as f:
            f.write(crlf)
        good = ("--- a/math.py\n+++ b/math.py\n@@ -1,2 +1,2 @@\n"
                "-def add(a, b):\n-    return a + b\n"
                "+def add(a, b):\n+    return a + b + 0\n")
        fake, _, _, engine = self.make_env(posts=[comp(good), comp(good), comp(good)],
                                           run=scripted_run([(1, "E: gate failed"),
                                                             (1, "E: gate failed")]))
        result = engine.apply_batch(
            [p], options=BatchOptions(instruction="add +0",
                                      verify_cmd="check",
                                      require_consent=False,
                                      backend="diff"))
        self.assertEqual(result["status"], "verify_failed")
        with open(p, "rb") as f:
            self.assertEqual(f.read(), crlf)

    # Windows maps every non-read-only file to 0o666 and ignores chmod bits,
    # so mode preservation is only observable on POSIX.
    @unittest.skipIf(os.name == "nt", "Windows does not honor POSIX mode bits")
    def test_verify_only_exhaustion_reports_preview_exhausted_not_verify_failed(self):
        """Live finding: a --verify-only preview whose diffs all failed to
        merge reported status 'verify_failed' with 'diff merge failed' as the
        gate's output_tail -- fabricated gate evidence, since no gate runs in
        preview mode. The terminal must say the gate was never reached."""
        p = self.make_file()
        bad = ("--- a/math.py\n+++ b/math.py\n@@ -1,2 +1,2 @@\n"
               "-def wrong(a, b):\n-    return a + b\n"
               "+def add(a, b):\n+    return a + b + 0\n")
        fake, _, ledger, engine = self.make_env(posts=[comp(bad), comp(bad), comp(bad)])
        result = engine.apply_batch(
            [p], options=BatchOptions(instruction="add +0",
                                      verify_cmd="check",
                                      require_consent=False,
                                      backend="diff", verify_only=True))
        self.assertEqual(result["status"], "preview_exhausted")
        self.assertIsNone(result["verify"]["passed"])
        self.assertFalse(result["gate_ran"])
        self.assertIn("gate not run", result["verify"]["note"])
        self.assertFalse(result["continuation"]["verification_required"])
        aborts = [e for e in ledger.entries() if e["event"] == "abort"]
        self.assertEqual(aborts[-1]["reason"], "preview exhausted its rounds")
        # The target is untouched (preview never writes).
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), ORIGINAL)

    @unittest.skipIf(os.name == "nt", "Windows does not honor POSIX mode bits")
    def test_atomic_write_preserves_file_mode(self):
        """Regression (audit follow-up): tempfile.mkstemp creates 0600, so the
        atomic replace silently stripped the executable bit (and every other
        mode bit) from the target. A verify script or gate artifact rewritten
        by an apply round must keep its mode, like _backup already does."""
        import stat as _stat
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "gate.sh")
        with open(p, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
        _atomic_write(p, "#!/bin/sh\nexit 1\n")
        self.assertEqual(_stat.S_IMODE(os.stat(p).st_mode), 0o755)

    @unittest.skipIf(os.name == "nt", "Windows does not honor POSIX mode bits")
    def test_atomic_write_new_file_stays_private(self):
        """A brand-new target keeps mkstemp's safe 0600 default."""
        import stat as _stat
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "new.txt")
        _atomic_write(p, "content\n")
        self.assertEqual(_stat.S_IMODE(os.stat(p).st_mode), 0o600)

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

    def test_gate_broken_stops_retries_with_diagnostic(self):
        """A gate failing identically twice is broken; the engine must stop
        burning rounds and say so instead of re-prompting the model."""
        orig = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, orig, ignore_errors=True)
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


class BatchOptionsPinTests(ApplyFixture):
    """The BatchOptions bundle contract: run-level pool/cancel-check and
    continuation reach the per-file payload through the options path."""

    def test_bundle_path_merges_run_level_pool_and_cancel_check(self):
        """Run-level pool/cancel-check reach the per-file payload even
        when the caller passes a bundle."""
        from harness.batch import BatchOptions
        import unittest.mock
        seen = {}
        real_prepare = ApplyEngine._prepare

        def spying_prepare(self, kwargs):
            seen.update(kwargs)
            return real_prepare(self, kwargs)

        a = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        marker_pool, marker_cancel = [CODER_A], (lambda *a, **k: False)
        with unittest.mock.patch.object(ApplyEngine, "_prepare",
                                        spying_prepare):
            engine.apply_batch(
                [a], options=BatchOptions(instruction="change",
                                             verify_cmd="check",
                                             require_consent=False),
                apply_pool=marker_pool, cancel_check=marker_cancel)
        self.assertEqual(seen["apply_pool"], marker_pool)
        self.assertEqual(seen["cancel_check"], marker_cancel)

    def test_run_level_continuation_param_reaches_payload(self):
        """The run-level continuation parameter is validated and delivered
        to the per-file payload (the seam the dual-mode options path
        silently dropped to None, breaking resume)."""
        from harness.batch import run_batch
        from harness.continuation import gate_id
        seen = {}

        class _StubEngine:
            def apply_edit(self, **kw):
                seen.update(kw)
                return {"status": "ok"}

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        target = os.path.join(td.name, "resume.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        cont = {"file_path": target, "task_id": "t9",
                "verify_cmd": "check", "verify_gate_id": gate_id("check"),
                "rounds": []}
        run_batch(_StubEngine(), [None], options=BatchOptions(),
                  task_id="t9", continuation=cont)
        self.assertIs(seen["continuation"], cont)
        self.assertEqual(seen["file_path"], target)


if __name__ == "__main__":
    unittest.main()
