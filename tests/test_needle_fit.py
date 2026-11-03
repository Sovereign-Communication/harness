"""Tests for the local Needle 3 lane.

Hermetic by construction: no model weights, no network, no spend, and no real
process launch. The process seam is injected as a fake ``runner``, so this suite
runs anywhere the wheel installs and cannot be made to pass or fail by a 35MB
checkpoint appearing or vanishing on the box.

The load-bearing tests are the ones that pin the *negative*. A reviewer reading
only this file should be able to conclude the lane cannot influence anything,
without opening ``verdict.py``.
"""
import json
import os
import sys
import unittest
from unittest import mock

# The shadowed-install trap, asserted rather than assumed: this suite must
# run against the clone, never a site-packages copy. See AGENTS.md.
import harness  # noqa: E402

from harness import needle_fit  # noqa: E402
from harness.needle_fit import config, probe, verdict  # noqa: E402

_CLONE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
assert os.path.abspath(harness.__file__).startswith(_CLONE), (
    f"harness resolved to {harness.__file__}, not inside the clone at "
    f"{_CLONE} -- this suite would be testing an installed copy")


def fake_runner(payload, code=0):
    """A stand-in for the host's process seam, returning one JSON payload."""
    def _run(argv, input_text, timeout):
        return code, json.dumps(payload)
    return _run


def echoing_runner(text):
    def _run(argv, input_text, timeout):
        return 0, text
    return _run


class VerdictIsPinnedNegativeTests(unittest.TestCase):
    """The measurement is a negative, and it is evaluated, not typed."""

    def test_authority_is_not_earned(self):
        self.assertFalse(verdict.AUTHORITY_EARNED)
        self.assertFalse(needle_fit.authority_earned())
        self.assertFalse(needle_fit.describe()["authority_earned"])

    def test_measured_accuracy_is_below_the_floor(self):
        self.assertLess(verdict.NEEDLE_MEASURED_ACCURACY,
                        verdict.LOCAL_MIN_ACCURACY)

    def test_earned_needs_both_conditions(self):
        self.assertTrue(verdict.earned(accuracy=0.95, missed_drift=0))
        # A clean missed-drift count is worthless if drift was never measured.
        self.assertFalse(verdict.earned(accuracy=0.95, missed_drift=9))
        self.assertFalse(verdict.earned(accuracy=0.50, missed_drift=0))

    def test_earned_is_derived_from_the_measurement(self):
        """Change the constant, the answer moves. Nothing is hardcoded twice."""
        with mock.patch.object(verdict, "NEEDLE_MEASURED_ACCURACY", 0.99):
            self.assertTrue(verdict.earned())

    def test_the_lure_is_recorded(self):
        """The level that scored 10/10 stays in the file, with its cost.

        A future reader who finds only 'L1 = 100%' would wire a model that
        always says yes in front of the consent path.
        """
        self.assertEqual(verdict.NEEDLE_BEST_LEVEL, "L1_goal")
        self.assertEqual(verdict.NEEDLE_BEST_LEVEL_ACCURACY_ON_DRIFT_ROWS, 1.0)
        self.assertEqual(
            verdict.NEEDLE_BEST_LEVEL_FALSE_ALARMS_ON_ONGOAL_ROWS, 14)

    def test_the_mirror_image_is_recorded_too(self):
        """The level that actually tops the complete set is always-NO.

        Reporting only the best accuracy would read as 'L3 nearly works' when
        its 0.5227 comes from never claiming anything.
        """
        self.assertEqual(verdict.NEEDLE_TOP_LEVEL, "L3_goal_history")
        self.assertEqual(verdict.NEEDLE_TOP_LEVEL_MISSED_DRIFT, 0)
        self.assertGreater(verdict.NEEDLE_TOP_LEVEL_FALSE_ALARMS, 20)

    def test_measurement_names_its_own_denominator(self):
        """0.5227 must never be quotable without the set it came from."""
        self.assertEqual(verdict.NEEDLE_MEASURED_CASES, 44)
        self.assertEqual(verdict.NEEDLE_MEASURED_LEVELS, 4)

    def test_agent_reuse_degradation_is_recorded(self):
        """Reusing an agent scores far worse; the sweep must not have done it.

        If a future change reuses one agent across rows, the number it produces
        is not comparable to the pinned one and this documents why.
        """
        self.assertLess(verdict.NEEDLE_REUSED_AGENT_ACCURACY,
                        verdict.NEEDLE_FRESH_AGENT_ACCURACY)
        self.assertTrue(verdict.NEEDLE_SWEEP_USED_FRESH_AGENT_PER_ROW)


class NeverInfluencesTests(unittest.TestCase):
    """The lane cannot reach anything that decides, whatever the model says."""

    def test_signal_never_may_influence(self):
        signal = probe.NeedleSignal(True, 0.99, "model_claim", 12.0,
                                    "cactus-needle-subprocess")
        self.assertTrue(signal.claimed)
        self.assertFalse(signal.may_influence)
        self.assertFalse(signal.to_dict()["may_influence"])

    def test_may_influence_tracks_the_verdict_not_the_call(self):
        # Patches the single owner and expects the signal to follow. If the
        # signal captured the value at import, this fails -- which is the point.
        with mock.patch.object(verdict, "AUTHORITY_EARNED", True):
            signal = probe.NeedleSignal(True, 0.99, "model_claim", 1.0, "x")
            self.assertTrue(signal.may_influence)
        self.assertFalse(probe.NeedleSignal(
            True, 0.99, "model_claim", 1.0, "x").may_influence)

    def test_escalate_is_the_documented_path(self):
        out = needle_fit.escalate("run the gate", goal="ship the freeze")
        self.assertTrue(out["escalate"])
        self.assertEqual(out["reason"], "local_lane_not_earned")
        self.assertFalse(out["authority_earned"])
        self.assertIn("signal", out)

    def test_escalate_escalates_even_when_the_model_agrees(self):
        """The local claim cannot flip the outcome, in either direction."""
        with mock.patch.object(needle_fit, "observe") as obs:
            obs.return_value = probe.NeedleSignal(
                False, 0.99, "no_claim", 1.0, "cactus-needle-subprocess")
            out = needle_fit.escalate("all fine", goal="ship the freeze")
        self.assertTrue(out["escalate"])
        self.assertFalse(out["signal"]["claimed"])


class FailClosedTests(unittest.TestCase):
    """Every failure is a named no-signal, never an exception."""

    def _observe(self, runner, goal="ship the freeze", text="run the gate",
                 **env):
        environ = {"HARNESS_NEEDLE_FIT_STAGE": "observe",
                   "HARNESS_NEEDLE_FIT_PYTHON": sys.executable}
        environ.update(env)
        with mock.patch.dict(os.environ, environ, clear=False):
            return probe.observe(goal, text, runner)

    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_NEEDLE_FIT_STAGE", None)
            self.assertEqual(config.stage(), "off")

    def test_unknown_stage_resolves_to_quietest(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_NEEDLE_FIT_STAGE": "influence-ish"}):
            self.assertEqual(config.stage(), "off")

    def test_runner_not_configured(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_NEEDLE_FIT_STAGE": "observe"}):
            self.assertEqual(probe.observe("goal", "text").reason,
                             "runner_not_configured")

    def test_interpreter_absent(self):
        signal = self._observe(echoing_runner("{}"),
                               HARNESS_NEEDLE_FIT_PYTHON="/nope/python")
        self.assertEqual(signal.reason, "interpreter_absent")
        self.assertFalse(signal.available)
        self.assertFalse(signal.claimed)

    def test_checkpoint_absent(self):
        signal = self._observe(echoing_runner("{}"),
                               HARNESS_NEEDLE_FIT_CHECKPOINT="/nope.cact")
        self.assertEqual(signal.reason, "checkpoint_absent")

    def test_no_goal_and_empty_context(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_NEEDLE_FIT_STAGE": "observe"}):
            self.assertEqual(
                probe.observe("  ", "text", echoing_runner("{}")).reason,
                "no_goal")
            self.assertEqual(
                probe.observe("goal", "   ", echoing_runner("{}")).reason,
                "empty_context")

    def test_no_signal_reasons_are_all_registered(self):
        with self.assertRaises(ValueError):
            probe._no_signal("because_i_said_so", 0.0)

    def test_needle_absent_is_named(self):
        signal = self._observe(fake_runner(
            {"ok": False, "reason": "needle_absent",
             "error": "ModuleNotFoundError"}))
        self.assertEqual(signal.reason, "needle_absent")
        self.assertFalse(signal.may_influence)

    def test_model_error_is_unreadable_not_a_claim(self):
        signal = self._observe(fake_runner({"ok": False, "reason": "model_error"}))
        self.assertEqual(signal.reason, "child_unreadable")
        self.assertIn("model_error", signal.notes)

    def test_osal_timeout_code_is_no_signal(self):
        signal = self._observe(fake_runner({}, code=probe.RC_TIMEOUT))
        self.assertEqual(signal.reason, "timeout")
        self.assertFalse(signal.claimed)

    def test_osal_not_found_code_is_interpreter_absent(self):
        signal = self._observe(fake_runner({}, code=probe.RC_NOT_FOUND))
        self.assertEqual(signal.reason, "interpreter_absent")

    def test_osal_not_executable_code(self):
        signal = self._observe(fake_runner({}, code=probe.RC_NOT_EXECUTABLE))
        self.assertEqual(signal.reason, "interpreter_not_executable")

    def test_unreadable_child_is_no_signal(self):
        signal = self._observe(echoing_runner("not json at all"))
        self.assertEqual(signal.reason, "child_unreadable")

    def test_empty_stdout_is_no_signal(self):
        signal = self._observe(echoing_runner(""))
        self.assertEqual(signal.reason, "child_unreadable")

    def test_suppressed_tool_call_is_not_a_claim(self):
        """The bridge's rule: an unsuccessful or suppressed run is not a weak
        opinion, it is a failure."""
        signal = self._observe(fake_runner(
            {"ok": True, "claimed": True, "confidence": 0.9, "suppressed": 1}))
        self.assertFalse(signal.claimed)
        self.assertIn("tool_call_suppressed", signal.notes)
        self.assertEqual(signal.reason, "tool_call_suppressed")

    def test_a_real_claim_carries_provenance(self):
        signal = self._observe(fake_runner(
            {"ok": True, "claimed": True, "confidence": 0.4, "suppressed": 0}))
        self.assertTrue(signal.claimed)
        self.assertEqual(signal.provenance, "cactus-needle-subprocess")
        self.assertFalse(signal.may_influence)


class PromptShapeTests(unittest.TestCase):
    """The lane sends the level that earned its place, and no more."""

    def test_prompt_is_the_measured_l1_shape(self):
        prompt = probe.build_prompt("ship the freeze", "run the gate")
        self.assertEqual(prompt, "GOAL: ship the freeze\n\nLATEST: run the gate")
        self.assertNotIn("CURRENT PLAN", prompt)
        self.assertNotIn("ALREADY ACCEPTED", prompt)

    def test_goal_is_required(self):
        with self.assertRaises(ValueError):
            probe.build_prompt("   ", "text")

    def test_context_window_is_bounded_and_reported(self):
        seen = {}

        def capturing_runner(argv, input_text, timeout):
            seen["prompt"] = input_text
            return 0, json.dumps({"ok": True, "claimed": False,
                                  "confidence": 0.0, "suppressed": 0})

        with mock.patch.dict(os.environ, {
                "HARNESS_NEEDLE_FIT_STAGE": "observe",
                "HARNESS_NEEDLE_FIT_PYTHON": sys.executable,
                "HARNESS_NEEDLE_FIT_MAX_CHARS": "100"}, clear=False):
            signal = probe.observe("ship the freeze", "x" * 5000,
                                   capturing_runner)
        self.assertEqual(signal.observed_chars, 100)
        self.assertTrue(signal.truncated)
        # The window is real: the oversized text was cut before it reached the
        # child, rather than reported as truncated after the fact.
        self.assertNotIn("x" * 200, seen["prompt"])

    def test_the_child_never_receives_a_shell(self):
        """argv is a list, so no host can turn the prompt into a command."""
        seen = {}

        def capturing_runner(argv, input_text, timeout):
            seen["argv"] = argv
            return 0, "{}"

        with mock.patch.dict(os.environ, {
                "HARNESS_NEEDLE_FIT_STAGE": "observe",
                "HARNESS_NEEDLE_FIT_PYTHON": sys.executable}, clear=False):
            probe.observe("ship the freeze", "text", capturing_runner)
        self.assertIsInstance(seen["argv"], list)
        self.assertEqual(seen["argv"][1], "-c")


class DescribeTests(unittest.TestCase):
    def test_status_reports_the_negative_first(self):
        out = needle_fit.describe()
        self.assertEqual(out["lane"], "needle_fit")
        self.assertFalse(out["authority_earned"])
        self.assertIn("NOT EARNED", out["verdict"])
        self.assertLess(out["measured_accuracy"], out["min_accuracy_required"])

    def test_status_reports_the_path_never_the_weights(self):
        """The path is the point of a status face; the weights are not.

        A second copy of the default path is how a measurement ends up
        describing a checkpoint nothing else uses, so the resolved path must be
        visible. The weights must not be.
        """
        out = needle_fit.describe()
        self.assertIn("checkpoint", out["config"])
        for banned in ("size", "sha256", "weights_bytes", "content"):
            self.assertNotIn(banned, out["config"])
        self.assertLess(len(json.dumps(out)), 4096)


class ConfigTests(unittest.TestCase):
    def test_timeout_is_floored_and_defaults_bounded(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_NEEDLE_FIT_TIMEOUT_S": "0"}):
            self.assertGreaterEqual(config.timeout_s(), 0.05)
        with mock.patch.dict(os.environ,
                             {"HARNESS_NEEDLE_FIT_TIMEOUT_S": "nonsense"}):
            self.assertEqual(config.timeout_s(), 1.5)

    def test_bad_max_chars_falls_back(self):
        with mock.patch.dict(os.environ,
                             {"HARNESS_NEEDLE_FIT_MAX_CHARS": "nope"}):
            self.assertEqual(config.max_context_chars(), 1000)


if __name__ == "__main__":
    unittest.main()
