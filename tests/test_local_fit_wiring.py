"""C4 wiring tests: capability.order_pool <-> local_fit.dispatch.

Property pins:
- OFF (default): the advisory hook is never even imported/called, and the
  order equals the un-wired baseline.
- OBSERVE: the hook scores but the returned order is the baseline order.
- INFLUENCE: only scorer-flagged models move, and only within their demotion
  tier. Healthy-unflagged relative order, the demotion boundary, and paid-tier
  price ordering are all preserved exactly.
- Every failure mode degrades to the baseline order; the hook never raises.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    import numpy  # noqa: F401
    import onnx  # noqa: F401
    _TRAIN_OK = True
except ImportError:
    _TRAIN_OK = False

needs_train = unittest.skipUnless(
    _TRAIN_OK, "train-time deps (numpy+onnx) required")

from harness.capability import CapabilityProfile, order_pool

GOOD = "acme/good:free"
MID = "acme/mid:free"
FLAKY = "acme/flaky:free"
DEMOTE = "acme/demoted:free"

CAL = {
    GOOD: {"samples": 40, "success_rate": 0.95, "unusable_outputs": 0, "consent_unusable": 0},
    MID: {"samples": 20, "success_rate": 0.8, "unusable_outputs": 0, "consent_unusable": 0},
    FLAKY: {"samples": 15, "success_rate": 0.6, "unusable_outputs": 1, "consent_unusable": 0},
    DEMOTE: {"samples": 10, "success_rate": 0.3, "unusable_outputs": 2, "consent_unusable": 0},
}


def _profiles(extra=None):
    profs = {
        GOOD: CapabilityProfile(GOOD, context_length=200000, free=True,
                                prompt_price=0, completion_price=0,
                                supports_reasoning=True, supports_structured_json=True),
        MID: CapabilityProfile(MID, context_length=128000, free=True,
                               prompt_price=0, completion_price=0,
                               supports_reasoning=False, supports_structured_json=True),
        FLAKY: CapabilityProfile(FLAKY, context_length=128000, free=True,
                                 prompt_price=0, completion_price=0,
                                 supports_reasoning=False, supports_structured_json=True),
    }
    if extra:
        profs.update(extra)
    return profs


def _demoted_profile():
    return CapabilityProfile(DEMOTE, context_length=64000, free=True,
                             prompt_price=0, completion_price=0,
                             supports_reasoning=False, supports_structured_json=False)


def _report():
    return {"calibration": dict(CAL)}


def _flags_off():
    for k in list(os.environ):
        if k.startswith("HARNESS_LOCAL_FIT"):
            os.environ.pop(k, None)


def _baseline_order(profiles):
    """Reference order with the advisory code fully disabled."""
    _flags_off()
    pool = list(profiles)
    return order_pool(pool, profiles, _report(), task="code", free_tier=True)


class _FakeScorer:
    """Scores p_unusable per model id without any model artifact."""

    def __init__(self, unusable_by_model):
        self.unusable_by_model = unusable_by_model

    def score(self, feats):
        # features carry the stable model hash; map back for the fake.
        from harness.local_fit.features import hash_model_id
        by_hash = {hash_model_id(m): m for m in self.unusable_by_model}
        model = by_hash.get(int(feats.get("model_id_hash", -1)))
        p = self.unusable_by_model.get(model, 0.1)
        return {"unusable": p, "truncated": 0.0, "usable_stop": 1.0 - p,
                "best_guess": "unusable" if p >= 0.5 else "usable_stop"}


class WiringTestBase(unittest.TestCase):
    def setUp(self):
        _flags_off()

    def tearDown(self):
        _flags_off()


class TestOffByDefault(WiringTestBase):
    def test_hook_not_called_when_disabled(self):
        profiles = _profiles()
        pool = list(profiles)
        with mock.patch("harness.local_fit.dispatch.maybe_order_pool",
                        side_effect=AssertionError("hook must not run when disabled")) as spy:
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        spy.assert_not_called()
        self.assertEqual(ordered, _baseline_order(profiles))

    def test_capability_import_stays_lazy(self):
        # If local_fit was not imported before, an OFF order_pool must not
        # import it (the import sits inside the guarded try block).
        profiles = _profiles()
        pool = list(profiles)
        was_imported = "harness.local_fit" in sys.modules
        _flags_off()
        order_pool(pool, profiles, _report(), task="code", free_tier=True)
        if not was_imported:
            self.assertNotIn("harness.local_fit", sys.modules)


class TestObserve(WiringTestBase):
    def test_observe_scores_but_never_reorders(self):
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)

        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent-does-not-matter"
        # Stub the scorer so OBSERVE is exercised even without an artifact.
        with mock.patch("harness.local_fit.config.load_scorer",
                        return_value=_FakeScorer({FLAKY: 0.9})):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered, baseline,
                         "OBSERVE must not change the order even when a model is flagged")


class TestInfluence(WiringTestBase):
    def _enable(self, unusable_map, threshold="0.6"):
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        if threshold is not None:
            os.environ["HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"] = threshold
        return mock.patch("harness.local_fit.config.load_scorer",
                          return_value=_FakeScorer(unusable_map))

    def test_flagged_model_moves_after_healthy_peers(self):
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        # MID is not last in the baseline, so flagging it must move it.
        self.assertNotEqual(baseline[-1], MID)
        with self._enable({MID: 0.9}):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered[-1], MID)
        # Unflagged models keep their relative order.
        unflagged = [m for m in baseline if m != MID]
        self.assertEqual([m for m in ordered if m != MID], unflagged)

    def test_flagging_last_place_is_a_noop(self):
        """A model already last in its tier cannot move further: order unchanged."""
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        self.assertEqual(baseline[-1], FLAKY)
        with self._enable({FLAKY: 0.9}):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered, baseline)

    def test_demotion_boundary_never_crossed(self):
        profiles = _profiles({DEMOTE: _demoted_profile()})
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        self.assertEqual(baseline[-1], DEMOTE)  # 2 strikes -> demoted tier
        # Flag EVERY healthy model; none may cross below the demoted one.
        with self._enable({GOOD: 0.99, MID: 0.99, FLAKY: 0.99}):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered[-1], DEMOTE,
                         "advisory flag must never push a healthy model below the demoted tier")

    def test_no_flags_no_change(self):
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        with self._enable({GOOD: 0.1, MID: 0.1, FLAKY: 0.1}):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered, baseline)

    def test_threshold_is_inclusive_and_configurable(self):
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        with self._enable({MID: 0.6}, threshold="0.6"):  # == threshold -> flagged
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertNotEqual(ordered, baseline)
        _flags_off()
        baseline2 = _baseline_order(profiles)
        with self._enable({MID: 0.599}, threshold="0.6"):  # just below -> not flagged
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered, baseline2)

    def test_paid_tier_respects_price_order(self):
        paid = {
            "p/cheap": CapabilityProfile("p/cheap", context_length=128000, free=False,
                                         prompt_price=0.1, completion_price=0.1,
                                         supports_structured_json=True),
            "p/costly": CapabilityProfile("p/costly", context_length=128000, free=False,
                                          prompt_price=0.9, completion_price=0.9,
                                          supports_structured_json=True),
        }
        cal = {
            "p/cheap": {"samples": 5, "success_rate": 0.9, "unusable_outputs": 0, "consent_unusable": 0},
            "p/costly": {"samples": 5, "success_rate": 0.9, "unusable_outputs": 0, "consent_unusable": 0},
        }
        pool = list(paid)
        _flags_off()
        baseline = order_pool(pool, paid, {"calibration": cal}, task="code", free_tier=False)
        self.assertEqual(baseline, ["p/cheap", "p/costly"])  # price ascending
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        with mock.patch("harness.local_fit.config.load_scorer",
                        return_value=_FakeScorer({"p/cheap": 0.99})):
            ordered = order_pool(pool, paid, {"calibration": cal}, task="code", free_tier=False)
        # cheap is flagged: it must move after its same-tier peer, but never
        # reorder unflagged models among themselves.
        self.assertEqual(ordered, ["p/costly", "p/cheap"])


class TestFailClosed(WiringTestBase):
    def test_scorer_crash_degrades_to_baseline(self):
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        boom = _FakeScorer({FLAKY: 0.9})
        def explode(feats):
            raise RuntimeError("model exploded")
        boom.score = explode
        with mock.patch("harness.local_fit.config.load_scorer", return_value=boom):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered, baseline)

    def test_dispatch_crash_degrades_to_baseline(self):
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        with mock.patch("harness.local_fit.dispatch.maybe_order_pool",
                        side_effect=RuntimeError("dispatch blew up")):
            ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
        self.assertEqual(ordered, baseline)


@needs_train
class TestEndToEndWithRealArtifact(WiringTestBase):
    def test_order_pool_loads_stdlib_scorer_without_numpy(self):
        """Full path: real model dir -> order_pool -> INFLUENCE, no numpy import."""
        from harness.local_fit.extract import all_run_files, extract
        from harness.local_fit.train import run_pipeline

        rows = extract(all_run_files("audits"))
        self.assertGreater(len(rows), 0)
        profiles = _profiles()
        pool = list(profiles)
        baseline = _baseline_order(profiles)

        with tempfile.TemporaryDirectory() as d:
            run_pipeline(rows, d)  # exports model_weights.json (train deps OK here)
            os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
            os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = d
            os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
            os.environ["HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"] = "0.999999"
            try:
                before = set(sys.modules)
                ordered = order_pool(pool, profiles, _report(), task="code", free_tier=True)
                # With an absurd threshold nothing is flagged: order is baseline.
                self.assertEqual(ordered, baseline)
                newly = set(sys.modules) - before
                for banned in ("numpy", "onnx", "onnxruntime"):
                    hits = [m for m in newly if m == banned or m.startswith(banned + ".")]
                    self.assertEqual(hits, [], f"enabled ordering imported {hits}")
            finally:
                for k in ("HARNESS_LOCAL_FIT_ENABLE", "HARNESS_LOCAL_FIT_MODEL_DIR",
                          "HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER",
                          "HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
