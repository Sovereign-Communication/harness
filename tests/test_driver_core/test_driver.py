"""End-to-end pipeline tests.

These are the ones that matter most, because the failure mode being defended
against is not a crash -- it is a *confident wrong action*, which looks
exactly like a passing run from the outside.

The tests are hermetic: no network, no model, no screen, no disk. Every
collaborator is a fake, which is only possible because the pipeline takes
them by injection.
"""
import unittest

from driver_core.actions import DEFAULT_VOCABULARY
from driver_core import osal
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver, StepResult
from driver_core.errors import ConsentError
from driver_core.ev import FakeJev, action_answer
from driver_core.executor import Consent, Executor
from driver_core.executor_registry import (
    build_driver_registry, build_read_only_registry,
)
from driver_core.extractors import (
    ExtractorPool, StructuredExtractor,
)
from driver_core.perception import Capture, StructuredSource
from driver_core.states import SCREEN_SCHEMA

GOOD = {
    "window_title": "Report - Editor",
    "foreground_app": "editor",
    "error_dialog_present": False,
}


def _state(**over):
    base = dict(GOOD)
    base.update(over)
    return base


def _extractor_reader(payload, *, ok=True):
    """A structured extractor reader: it is handed (capture, schema)."""
    def read(capture, schema):
        if not ok:
            return None
        return dict(_state() if payload is None else payload)
    return read


def _source_reader(payload, *, ok=True):
    """A perception source reader: it is handed the target only."""
    def read(target):
        if not ok:
            return None
        return dict(_state() if payload is None else payload)
    return read


def _agreed_pool(payload=None, slots=2):
    return ExtractorPool([
        StructuredExtractor(f"structured-{i}",
                            _extractor_reader(payload or _state()))
        for i in range(slots)
    ])


def _driver(*, allow_write=False, **kwargs):
    settings = load_settings(env={}, quorum=2, min_agreement=1.0,
                            confidence_threshold=0.7, run_ceiling_usd=1.0,
                            step_ceiling_usd=1.0, dry_run=True,
                            allow_write=allow_write)
    kwargs.setdefault("settings", settings)
    kwargs.setdefault("budget", Budget(1.0, step_ceiling_usd=1.0))
    kwargs.setdefault("audit", MemoryAuditLog())
    return Driver(**kwargs)


def _source(payload=None, ok=True):
    return StructuredSource("cli", _source_reader(
        _state() if payload is None else payload, ok=ok))


class HappyPathTests(unittest.TestCase):

    def test_agreed_state_reaches_the_decision_tier(self):
        jev = FakeJev(action_answer("observe", confidence=0.95))
        driver = _driver(pool=_agreed_pool(), sources=[_source()], jev=jev)
        result = driver.step("target")
        self.assertTrue(result.ok)
        self.assertEqual(result.stopped_at, "executed")
        self.assertEqual(result.decision.recommended_action, "observe")
        self.assertIsNotNone(result.receipt)

    def test_receipt_travels_with_the_decision(self):
        """A decision is only auditable if it carries the extraction that
        justified it."""
        jev = FakeJev(action_answer("observe", confidence=0.95))
        driver = _driver(pool=_agreed_pool(), sources=[_source()], jev=jev)
        result = driver.step("target")
        self.assertEqual(result.receipt["schema"], SCREEN_SCHEMA.identity())
        self.assertEqual(result.receipt["answering"], 2)
        self.assertEqual(result.receipt["contested_fields"], [])

    def test_the_agreed_state_handed_to_the_model_is_the_verified_one(self):
        seen = {}

        def spy(state, vocabulary, **kwargs):
            seen.update(state)
            return jev_decision()

        def jev_decision():
            from driver_core.jev_client import Decision, NATIVE
            return Decision(NATIVE, recommended_action="observe",
                            confidence=0.99, native=True,
                            guards={"state_is_stable": 1.0,
                                    "a_blocking_choice_is_required": 0.1})

        driver = _driver(pool=_agreed_pool(), sources=[_source()],
                         jev=FakeJev(decision_fn=spy))
        driver.step("target")
        self.assertEqual(seen["window_title"], "report - editor")


class ShortfallStopsThePipelineTests(unittest.TestCase):
    """The load-bearing integration property."""

    def test_a_shortfall_never_reaches_the_decision_tier(self):
        called = []

        def must_not_run(state, vocabulary, **kwargs):
            called.append(state)
            return None

        pool = ExtractorPool([
            StructuredExtractor("a", _extractor_reader(_state())),
            StructuredExtractor("b", lambda c, s: None),   # declines
        ])
        driver = _driver(pool=pool, sources=[_source()],
                         jev=FakeJev(decision_fn=must_not_run))
        result = driver.step("target")
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_at, "agreement")
        self.assertEqual(result.reason, "insufficient_agreement")
        self.assertEqual(called, [], "the model was asked to decide on an "
                                     "unverified state")

    def test_a_disagreement_never_reaches_the_decision_tier(self):
        called = []

        def must_not_run(state, vocabulary, **kwargs):
            called.append(state)
            return None

        pool = ExtractorPool([
            StructuredExtractor("a", _extractor_reader(_state(window_title="Alpha"))),
            StructuredExtractor("b", _extractor_reader(_state(window_title="Beta"))),
        ])
        driver = _driver(pool=pool, sources=[_source()],
                         jev=FakeJev(decision_fn=must_not_run))
        result = driver.step("target")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "extraction_disagreement")
        self.assertEqual(called, [])

    def test_a_refusal_is_a_normal_successful_outcome_of_the_system(self):
        """Not an exception. The driver working correctly and declining to
        act is a result, and is logged as a first-class event."""
        pool = ExtractorPool([StructuredExtractor("a", lambda c, s: None)])
        driver = _driver(pool=pool, sources=[_source()],
                         jev=FakeJev(action_answer("observe", confidence=0.9)))
        result = driver.step("target")
        self.assertIsInstance(result, StepResult)
        self.assertFalse(result.ok)
        kinds = [r["kind"] for r in driver.audit.read_all()]
        self.assertIn("refusal", kinds)


class DecisionGateTests(unittest.TestCase):

    def _run(self, **decision_kwargs):
        jev = FakeJev(action_answer(**decision_kwargs))
        return _driver(pool=_agreed_pool(), sources=[_source()],
                       jev=jev).step("target")

    def test_low_confidence_escalates_without_executing(self):
        result = self._run(action="observe", confidence=0.3)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "confidence_below_threshold")
        self.assertIsNone(result.execution)

    def test_confidence_exactly_at_the_threshold_passes(self):
        result = self._run(action="observe", confidence=0.7)
        self.assertTrue(result.ok)

    def test_an_unstable_state_is_refused_even_when_confident(self):
        result = self._run(action="observe", confidence=0.99,
                           guards={"state_is_stable": 0.2,
                                   "a_blocking_choice_is_required": 0.1})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "state_not_stable")

    def test_a_missing_guard_is_not_a_passing_guard(self):
        """The guard returned nothing. Treating that as 'fine' is exactly
        the assumption this refuses to make."""
        result = self._run(action="observe", confidence=0.99,
                           guards={"a_blocking_choice_is_required": 0.1})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "decision_not_usable")

    def test_no_action_is_a_first_class_outcome(self):
        result = self._run(action="no_action", confidence=0.99)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_action_recommended")

    def test_a_model_naming_an_undeclared_action_yields_no_execution(self):
        """The refusal must be a refusal, not a crash.

        This is reachable whenever a hand-built envelope or a validation
        drift lets an undeclared name through, and the whole value of a
        closed vocabulary is that the bad name stops at the boundary rather
        than propagating into an executor.
        """
        from driver_core.jev_client import Decision, NATIVE
        jev = FakeJev(Decision(NATIVE, recommended_action="rm_rf",
                               confidence=0.99, native=True,
                               guards={"state_is_stable": 1.0,
                                       "a_blocking_choice_is_required": 0.1}))
        result = _driver(pool=_agreed_pool(), sources=[_source()],
                         jev=jev).step("target")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "undeclared_action")
        self.assertIsNone(result.execution)
        self.assertIn("rm_rf", result.detail)


class CapturePreferenceTests(unittest.TestCase):

    def test_structured_source_is_preferred_over_pixels(self):
        from driver_core.perception import ScreenSource

        class ExplodingScreen(ScreenSource):
            def capture(self, target=None):
                raise AssertionError("pixels must not be reached")

        driver = _driver(pool=_agreed_pool(), sources=[_source()],
                         screen=ExplodingScreen(),
                         jev=FakeJev(action_answer("observe", confidence=0.9)))
        result = driver.step("target")
        self.assertTrue(result.ok)
        self.assertEqual(result.capture.source, "cli")

    def test_no_usable_source_stops_with_a_named_reason(self):
        pool = _agreed_pool()
        driver = _driver(pool=pool, sources=[_source(ok=False)],
                         jev=FakeJev(action_answer("observe", confidence=0.9)))
        result = driver.step("target")
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_at, "capture")
        self.assertEqual(result.reason, "no_capture")

    def test_capture_summary_never_carries_the_payload(self):
        capture = Capture("cli", "t", {"secret": "s"}, fingerprint="abc")
        self.assertNotIn("secret", str(capture.summary()))


class ExecutionSafetyTests(unittest.TestCase):

    def test_irreversible_action_without_matching_consent_is_refused(self):
        registry = build_read_only_registry()
        registry.register("delete_file", lambda a, p: {"deleted": p})
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            dry_run=False, audit=MemoryAuditLog())
        consent = Consent(True, "delete_file", {"path": "other.txt"})
        with self.assertRaises(ConsentError):
            executor.execute("delete_file", {"path": "wanted.txt"},
                             consent=consent)

    def test_read_only_actions_need_no_consent_at_all(self):
        registry = build_read_only_registry(state_provider=lambda: _state())
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=MemoryAuditLog())
        result = executor.execute("read_value", {"field": "window_title"},
                                  consent=Consent(False))
        self.assertTrue(result.ok)

    def test_unregistered_executor_is_refused_not_skipped(self):
        registry = build_read_only_registry()
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=MemoryAuditLog())
        with self.assertRaises(Exception) as ctx:
            executor.execute("click", {"target": "OK"},
                             consent=Consent(True, "click", {"target": "OK"}))
        self.assertIn("not registered", str(ctx.exception))

    def test_the_refusal_says_whether_it_was_policy_or_wiring(self):
        """An operator reading "not registered" must be able to tell whether
        the build is incapable or the wiring is broken."""
        from driver_core import osal
        registry = build_driver_registry(allow_write=False)
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            audit=MemoryAuditLog())
        path = osal.resolve_path("/tmp/x")
        with self.assertRaises(Exception) as ctx:
            executor.execute("delete_file", {"path": "/tmp/x"},
                             consent=Consent(True, "delete_file",
                                             {"path": path}))
        self.assertIn("DRIVER_ALLOW_WRITE", str(ctx.exception))

    def test_undeclared_parameter_is_refused(self):
        executor = Executor(vocabulary=DEFAULT_VOCABULARY,
                            registry=build_read_only_registry(),
                            audit=MemoryAuditLog())
        with self.assertRaises(Exception) as ctx:
            executor.execute("read_value", {"field": "x", "extra": 1})
        self.assertIn("undeclared parameter", str(ctx.exception))

    def test_dry_run_produces_the_record_without_the_effect(self):
        performed = []
        registry = build_read_only_registry()
        registry.register("click", lambda a, p: performed.append(p))
        executor = Executor(vocabulary=DEFAULT_VOCABULARY, registry=registry,
                            dry_run=True, audit=MemoryAuditLog())
        result = executor.execute("click", {"target": "OK"},
                                  consent=Consent(True, "click", {"target": "OK"}))
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertEqual(performed, [])


class ParameterProvenanceTests(unittest.TestCase):
    """Parameters come from the caller, never from the decision tier.

    A closed vocabulary bounds what the model may *name*. It does nothing
    about what it may *invent*: a model asked to produce a path or a string
    to type will produce a plausible one, and the whole design rests on that
    never happening. So the parameters have no route from the decision tier
    into the executor, and these tests check the route is absent rather than
    trusting the docstring that says it is.
    """

    def _step(self, action, params=None, consent=None):
        jev = FakeJev(action_answer(action, confidence=0.99))
        return _driver(allow_write=True, pool=_agreed_pool(),
                       sources=[_source()],
                       jev=jev).step("target", params=params,
                                     consent=consent)

    def test_caller_parameters_reach_the_executor(self):
        consent = Consent(True, "write_file",
                          {"path": osal.resolve_path("/tmp/x"),
                           "content": "hi"})
        result = self._step("write_file",
                            params={"path": "/tmp/x", "content": "hi"},
                            consent=consent)
        # Dry run in the test driver: the record is produced, nothing happens.
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.execution.params["content"], "hi")

    def test_the_consent_must_match_the_parameters_actually_run(self):
        """Consent and parameters are checked against each other.

        Two spellings of the same pair, compared rather than assumed equal,
        because a caller that supplies both should be told when they
        disagree -- not have one silently win.
        """
        result = self._step("write_file",
                            params={"path": osal.resolve_path("/tmp/x"),
                                    "content": "hi"},
                            consent=Consent(True, "write_file",
                                            {"path": osal.resolve_path("/tmp/x"),
                                             "content": "different"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")

    def test_a_parametrised_action_with_no_parameters_refuses(self):
        """Rather than executing with defaults, which would be inventing
        the arguments this design forbids anyone from inventing."""
        result = self._step("write_file")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("missing required parameter", result.detail)

    def test_an_action_taking_no_parameters_refuses_being_given_some(self):
        """A parameter nobody declared is something outside this contract
        trying to influence execution; discarding it quietly would hide
        that."""
        result = self._step("observe", params={"sneaky": 1})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("takes no parameters", result.detail)

    def test_the_decision_envelope_carries_no_parameters_to_execute(self):
        """There is no field on the envelope for them to arrive in."""
        from driver_core.jev_client import Decision, NATIVE
        jev = FakeJev(Decision(NATIVE, recommended_action="write_file",
                               confidence=0.99, native=True,
                               guards={"state_is_stable": 1.0,
                                       "a_blocking_choice_is_required": 0.1}))
        result = _driver(pool=_agreed_pool(), sources=[_source()],
                         jev=jev).step("target")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")

    def test_a_write_is_impossible_while_writes_are_switched_off(self):
        """The default build cannot write, and says which switch to turn."""
        result = _driver(pool=_agreed_pool(), sources=[_source()],
                         jev=FakeJev(action_answer("write_file",
                                                   confidence=0.99))).step(
            "target",
            params={"path": osal.resolve_path("/tmp/x"), "content": "hi"},
            consent=Consent(True, "write_file",
                            {"path": osal.resolve_path("/tmp/x"),
                             "content": "hi"}))
        self.assertFalse(result.ok)
        self.assertIn("DRIVER_ALLOW_WRITE", result.detail)


class NonInterferenceTests(unittest.TestCase):
    """driver-core must not read or write a neighbouring project's state."""

    def test_settings_ignore_foreign_environment_variables(self):
        env = {
            "HARNESS_LEDGER": "C:/somewhere/shared.jsonl",
            "OPENROUTER_API_KEY": "sk-not-ours",
            "DRIVER_JEV_API_KEY": "ours",
        }
        settings = load_settings(env=env)
        self.assertEqual(settings.jev_api_key, "ours")
        self.assertNotIn("shared.jsonl", str(settings.redacted()))

    def test_a_foreign_read_is_detectable(self):
        from driver_core.config import assert_no_foreign_reads, ConfigError
        with self.assertRaises(ConfigError):
            assert_no_foreign_reads(["DRIVER_PORT", "HARNESS_LEDGER"])
        self.assertTrue(assert_no_foreign_reads(["DRIVER_PORT"]))

    def test_the_default_audit_path_is_namespaced(self):
        # The path is under the user's state directory, which may itself
        # contain the word "harness" (a checkout or a home named that way).
        # Pin the base so only the part this package chooses is judged.
        import os
        from pathlib import Path
        from unittest import mock
        from driver_core.config import default_audit_path
        base = Path("/neutral-state-base")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base)}):
            path = Path(default_audit_path())
        relative = path.relative_to(base).as_posix()
        self.assertEqual(relative, "driver-core/audit.jsonl")
        self.assertNotIn("harness", relative.lower())
        env = {k: v for k, v in os.environ.items()
               if k not in ("LOCALAPPDATA", "XDG_STATE_HOME")}
        with mock.patch.dict(os.environ, env, clear=True),                 mock.patch.object(Path, "home", return_value=base):
            fallback = Path(default_audit_path())
        self.assertEqual(fallback.relative_to(base).as_posix(),
                         ".driver-core/audit.jsonl")


if __name__ == "__main__":
    unittest.main()
