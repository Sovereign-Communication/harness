"""Morph-backend behavior (harness/apply.py morph lane): preview read-only
semantics, capability deferral without partial writes, and gate-free
continuation resume."""
import unittest

from tests._applyfixture import (APPLY, ApplyFixture, CODER_A, CODER_B,
                                 CHANGED, ESC, JUDGE, MORPH, ORIGINAL, PARTIAL)
from tests._fake import comp, m


class MorphTests(ApplyFixture):
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
        self.assertEqual(payload["model"].replace(":floor", ""), MORPH)
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


if __name__ == "__main__":
    unittest.main()
