"""Hermetic pins for the closure pass: saturation policy (item 1), the
dogfood --verify preflight (item 2), and reasoning-only demotion (item 3)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.errors import HarnessError
from harness.filesafety import validate_verify_command
from harness.ledger import AutonomyLedger
from harness.saturation import is_saturated, advise, pre_run_warning


class IsSaturatedTests(unittest.TestCase):
    def test_all_429_engine_rounds(self):
        rounds = [{"status": "api_error", "error": "429 rate limited"},
                  {"status": "api_error", "error": "HTTP 429: daily cap"}]
        self.assertTrue(is_saturated(engine_rounds=rounds))

    def test_engine_429_without_literal_status_in_message(self):
        """Real OpenRouter bodies say 'Rate limit exceeded: free-models-per-day'
        with no '429' anywhere; the engine prefixes the HTTP status, and the
        predicate must read it (behavior-driver finding)."""
        rounds = [{"status": "api_error",
                   "error": "HTTP 429: Rate limit exceeded: free-models-per-day. "
                            "Please try again later."}]
        self.assertTrue(is_saturated(engine_rounds=rounds))

    def test_engine_reasoning_only_exhaustion(self):
        """Reasoning-only exhaustion lands as api_error with the engine's
        'no usable content' phrasing (behavior-driver finding)."""
        rounds = [{"status": "api_error",
                   "error": "model returned a reasoning-only response (no usable content)"}]
        self.assertTrue(is_saturated(engine_rounds=rounds))

    def test_all_reasoning_only_engine_rounds(self):
        rounds = [{"status": "api_error", "error": "no model reachable"},
                  {"status": "api_error", "error": "no model reachable"}]
        self.assertTrue(is_saturated(engine_rounds=rounds))

    def test_gate_failure_is_never_saturation(self):
        rounds = [{"status": "verify_failed", "verify_output": "assert failed"},
                  {"status": "verify_failed", "verify_output": "assert failed"}]
        self.assertFalse(is_saturated(engine_rounds=rounds))

    def test_all_tier_conditions_across_lanes_still_saturate(self):
        # A 429 in the engine lane plus a reasoning-only panel failure are
        # both tier conditions: every attempted model failed recoverably.
        self.assertTrue(is_saturated(
            engine_rounds=[{"status": "api_error", "error": "429"}],
            panel_failures=[{"status": "invalid_output",
                             "reason": "reasoning-only output (no visible content)"}]))

    def test_mixed_evidence_is_not_saturation(self):
        # One model failed the recoverable way and one failed a real way
        # (timeout): not a tier condition.
        self.assertFalse(is_saturated(
            engine_rounds=[{"status": "api_error", "error": "429"},
                           {"status": "api_error", "error": "connection timeout"}]))

    def test_panel_failures(self):
        failures = [{"status": "429", "reason": "rate limited"},
                    {"status": "invalid_output",
                     "reason": "reasoning-only output (no visible content)"}]
        self.assertTrue(is_saturated(panel_failures=failures))
        self.assertFalse(is_saturated(panel_failures=[]))
        self.assertFalse(is_saturated(panel_failures=None))

    def test_empty_is_not_saturated(self):
        self.assertFalse(is_saturated())
        self.assertFalse(is_saturated(engine_rounds=[], panel_failures=[]))


class AdviseTests(unittest.TestCase):
    def test_advise_prints_on_saturation(self):
        import io
        import contextlib
        from harness import output
        buf = io.StringIO()
        old = output.QUIET
        output.QUIET = False
        try:
            with contextlib.redirect_stderr(buf):
                said = advise(engine_rounds=[{"status": "api_error", "error": "429"}])
        finally:
            output.QUIET = old
        self.assertTrue(said)
        text = buf.getvalue()
        self.assertIn("[saturation]", text)
        self.assertIn("wait for the daily tier reset", text)
        self.assertIn("--no-consent", text)
        self.assertIn("BYOK", text)

    def test_advise_silent_when_not_saturated(self):
        import io
        import contextlib
        from harness import output
        buf = io.StringIO()
        old = output.QUIET
        output.QUIET = False
        try:
            with contextlib.redirect_stderr(buf):
                said = advise(engine_rounds=[{"status": "verify_failed",
                                              "verify_output": "x"}])
        finally:
            output.QUIET = old
        self.assertFalse(said)
        self.assertEqual(buf.getvalue(), "")


class PreRunWarningTests(unittest.TestCase):
    """The look-ahead half of the policy: before a run spends, plain-language
    advice from evidence the session already holds. Advice, never a gate."""

    def setUp(self):
        # The once-per-process dedup is module state; every test starts clean.
        from harness import saturation
        saturation._warned_this_process = False

    def _fed_ledger(self, n_429s, other=3):
        with tempfile.TemporaryDirectory() as d:
            led = AutonomyLedger(os.path.join(d, "l.jsonl"))
            for i in range(n_429s):
                led.append("model_result", task_id=f"t{i}", model="m:free",
                           status="HTTP 429", reason="Rate limit exceeded")
            for i in range(other):
                led.append("model_result", task_id=f"o{i}", model="m:free",
                           status="ok")
            yield led

    def test_fires_with_counted_numbers(self):
        for led in self._fed_ledger(4):
            gov = mock.Mock()
            gov.key_info = {"remaining": 0.50}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fired = pre_run_warning(governor=gov, ledger=led, use_free=True)
            self.assertTrue(fired)
            self.assertIn("4 429s in recent runs", err.getvalue())
            self.assertIn("--no-consent", err.getvalue())

    def test_healthy_ledger_stays_silent(self):
        for led in self._fed_ledger(0, other=6):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fired = pre_run_warning(governor=mock.Mock(), ledger=led, use_free=True)
            self.assertFalse(fired)
            self.assertEqual(err.getvalue(), "")

    def test_below_threshold_stays_silent(self):
        # 2 rate-limited events is noise, not saturation.
        for led in self._fed_ledger(2):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertFalse(pre_run_warning(governor=mock.Mock(), ledger=led, use_free=True))

    def test_401_faults_do_not_count(self):
        # Auth faults are a key condition, not tier saturation -- and the
        # message must count what it names.
        with tempfile.TemporaryDirectory() as d:
            led = AutonomyLedger(os.path.join(d, "l.jsonl"))
            for i in range(5):
                led.append("model_result", task_id=f"a{i}", model="m:free",
                           status="HTTP 401", reason="invalid key")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertFalse(pre_run_warning(governor=mock.Mock(), ledger=led, use_free=True))
            self.assertEqual(err.getvalue(), "")

    def test_key_near_limit_qualifies_byok_advice(self):
        for led in self._fed_ledger(3):
            gov = mock.Mock()
            gov.key_info = {"remaining": 0.01}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(pre_run_warning(governor=gov, ledger=led, use_free=True))
            self.assertIn("$0.01 left", err.getvalue())

    def test_key_near_limit_alone_never_triggers(self):
        # A $0 free-tier run needs no paid headroom; the key state qualifies
        # advice, it never triggers the warning.
        for led in self._fed_ledger(0, other=4):
            gov = mock.Mock()
            gov.key_info = {"remaining": 0.0}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertFalse(pre_run_warning(governor=gov, ledger=led, use_free=True))
            self.assertEqual(err.getvalue(), "")

    def test_warns_once_per_process(self):
        for led in self._fed_ledger(3):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(pre_run_warning(governor=mock.Mock(), ledger=led, use_free=True))
                self.assertFalse(pre_run_warning(governor=mock.Mock(), ledger=led, use_free=True))
            self.assertEqual(err.getvalue().count("[saturation]"), 1)

    def test_never_blocks_or_raises(self):
        # A governor whose key probe explodes must not take the run down --
        # and must not suppress the tier warning either: key-state is
        # garnish, the 429 evidence is the trigger.
        for led in self._fed_ledger(3):
            gov = mock.Mock()
            gov.key_info = property(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(pre_run_warning(governor=gov, ledger=led, use_free=True))
            self.assertIn("[saturation]", err.getvalue())
        # A ledger that explodes degrades to silence; a missing ledger too.
        broken = mock.Mock()
        broken.tail.side_effect = RuntimeError("boom")
        self.assertFalse(pre_run_warning(governor=mock.Mock(), ledger=broken, use_free=True))
        self.assertFalse(pre_run_warning(governor=None, ledger=None, use_free=True))

class ValidateVerifyCommandTests(unittest.TestCase):
    def test_accepts_path_tools(self):
        # `python` must exist in any test environment.
        validate_verify_command("python --version")

    def test_rejects_unknown_executable(self):
        with self.assertRaises(HarnessError) as ctx:
            validate_verify_command("definitely-not-a-real-tool-xyz -c x")
        self.assertIn("not found", str(ctx.exception))

    def test_rejects_garbage(self):
        with self.assertRaises(HarnessError):
            validate_verify_command("")
        with self.assertRaises(HarnessError):
            validate_verify_command("unbalanced 'quote")

    def test_rejects_missing_script_path(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-gate.py")
        with self.assertRaises(HarnessError) as ctx:
            validate_verify_command(f"{missing} --flag")
        self.assertIn("not found", str(ctx.exception))


class ReasoningOnlyDemotionTests(unittest.TestCase):
    def test_demotion_reorders_pool(self):
        from harness.capability import order_pool

        profiles = {p.model_id: p for p in (build_profile("acme/reasoner:free"),
                                            build_profile("acme/coder:free"))}
        ledger_events = [
            # The reasoner failed twice the way the engine actually records it
            # (apply lane: HTTP 200 but no usable content).
            {"event": "model_result", "task_id": "t1", "model": "acme/reasoner:free",
             "status": "error", "reason": "no usable content"},
            {"event": "model_result", "task_id": "t1", "model": "acme/reasoner:free",
             "status": "error", "reason": "no usable content"},
        ]
        with tempfile.TemporaryDirectory() as d:
            from harness.ledger import AutonomyLedger
            led = AutonomyLedger(os.path.join(d, "led.jsonl"))
            for e in ledger_events:
                led.append(e.pop("event"), **e)
            report = led.participation_report()
        ordered = order_pool(
            ["acme/reasoner:free", "acme/coder:free"], profiles, report,
            task="code", free_tier=True)
        self.assertEqual(ordered[0], "acme/coder:free")

    def test_no_evidence_keeps_capability_order(self):
        from harness.capability import order_pool
        profiles = {p.model_id: p for p in (build_profile("acme/reasoner:free"),
                                            build_profile("acme/coder:free"))}
        ordered = order_pool(["acme/reasoner:free", "acme/coder:free"], profiles,
                             None, task="code", free_tier=True)
        self.assertEqual(ordered, ["acme/reasoner:free", "acme/coder:free"])


def build_profile(model_id):
    """A free reasoning-capable profile with equal capability for both ids."""
    from harness.capability import build_profiles_from_models
    models = [{
        "id": model_id,
        "context_length": 262144,
        "supported_parameters": ["max_tokens", "reasoning", "structured_outputs"],
        "pricing": {"prompt": "0", "completion": "0"},
        "input_modalities": ["text"],
    }]
    return build_profiles_from_models(models)[model_id]


if __name__ == "__main__":
    unittest.main()
