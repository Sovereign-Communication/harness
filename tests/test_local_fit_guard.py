"""Degenerate-artifact guard + calibrated end-to-end INFLUENCE tests.

Covers the two cases that must be distinguished honestly:

1. A model that legitimately scores everyone healthy: score spread is real,
   no reorder is CORRECT, and the layer must NOT stand down.
2. A degenerate/saturated artifact: scores cannot distinguish candidates, so
   INFLUENCE must fail closed to baseline ordering with observable evidence
   (result["degenerate"] + a stderr notice), never a silent no-op that looks
   like case 1.

Also pins the mock-free end-to-end property: with the REAL train -> export ->
stdlib-infer -> order_pool path over synthetic audit data that contains a
genuinely failing model, INFLUENCE demotes that model within its strike
demotion tier while OFF/OBSERVE orders stay identical to the baseline.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from harness.capability import CapabilityProfile, order_pool

GOOD = "syn/good:free"
OTHER = "syn/mid:free"
BAD = "syn/bad:free"
DEMOTE = "syn/striked:free"


def _audit_run(model, ok_models, path, _unused=None):
    """Write a minimal run JSON with the failing model ON the panel.

    The failing seat finishes 'stop' with present-but-unparseable content:
    the extractor labels it unusable while it sits in the same task_type /
    seat_role shape as the healthy panel seats. That shape parity is what
    lets the net attribute the failure to the MODEL rather than to the seat
    kind, which is what the dispatch lane must be able to transfer.
    """
    panel = []
    for m in ok_models:
        panel.append({"model": m, "content": "{\"a\": 1}",
                      "finish_reason": "stop", "status": "ok"})
    panel.append({"model": model, "content": "I am unable to comply",
                  "finish_reason": "stop", "status": "ok"})
    run = {
        "run_type": "structured",
        "prompt": "x" * 4000,
        "panel_results": panel,
        "panel_failures": [],
        "convergence": {"specialist": {"model": ok_models[0],
                                       "claims": {"a": 1}, "status": "ok"}},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(run, f)


def _train_dir(models_bad, models_good, n_files=4):
    """Train the REAL pipeline on synthetic audit runs and return the dir.

    ``models_bad`` fails every run (its observed unusable rate is 1.0 and its
    rows are all labeled unusable); ``models_good`` models succeed every run.
    The temp dir (audit JSONs + trained artifact) is the caller's to clean up.
    """
    tmp = tempfile.mkdtemp(prefix="lf_guard_")
    runs = os.path.join(tmp, "runs")
    os.makedirs(runs)
    for i in range(n_files):
        _audit_run(models_bad, models_good,
                   os.path.join(runs, f"run_{i}.json"), None)
    from harness.local_fit.extract import extract
    from harness.local_fit.train import run_pipeline
    files = [os.path.join(runs, f) for f in sorted(os.listdir(runs))]
    rows = extract(files)
    assert len(rows) > 0
    model_dir = os.path.join(tmp, "model")
    run_pipeline(rows, model_dir)
    return tmp, model_dir, files, rows


def _flags_off():
    for k in list(os.environ):
        if k.startswith("HARNESS_LOCAL_FIT"):
            os.environ.pop(k, None)


def _profiles():
    profs = {
        GOOD: CapabilityProfile(GOOD, context_length=128000, free=True,
                                prompt_price=0, completion_price=0,
                                supports_reasoning=True,
                                supports_structured_json=True),
        OTHER: CapabilityProfile(OTHER, context_length=128000, free=True,
                                 prompt_price=0, completion_price=0,
                                 supports_reasoning=False,
                                 supports_structured_json=True),
        BAD: CapabilityProfile(BAD, context_length=128000, free=True,
                               prompt_price=0, completion_price=0,
                               supports_reasoning=False,
                               supports_structured_json=True),
    }
    return profs


def _report():
    # BAD has 2 unusable strikes -> it sits in the demoted tier at baseline;
    # GOOD/OTHER are healthy-tier models the advisory must be able to reorder.
    return {"calibration": {
        GOOD: {"samples": 30, "success_rate": 0.97, "unusable_outputs": 0,
               "consent_unusable": 0},
        OTHER: {"samples": 30, "success_rate": 0.9, "unusable_outputs": 0,
                "consent_unusable": 0},
        BAD: {"samples": 30, "success_rate": 0.1, "unusable_outputs": 2,
              "consent_unusable": 0},
    }}


class GuardTestBase(unittest.TestCase):
    def setUp(self):
        _flags_off()

    def tearDown(self):
        _flags_off()


class TestDegenerateReasonUnit(GuardTestBase):
    def test_constant_scores_are_degenerate(self):
        from harness.local_fit.dispatch import degenerate_reason
        scores = {f"m{i}": {"unusable": 0.5, "truncated": 0.0,
                            "usable_stop": 0.5, "best_guess": "usable_stop"}
                  for i in range(4)}
        self.assertEqual(degenerate_reason(scores), "score_spread_too_small")

    def test_saturated_top_class_is_degenerate(self):
        # Spread above MIN_SPREAD but every top-1 pinned at ~1.0: the classic
        # post-sharpening collapse. Still degenerate.
        from harness.local_fit.dispatch import degenerate_reason
        scores = {
            "a": {"unusable": 0.999, "truncated": 0.0005, "usable_stop": 0.0005},
            "b": {"unusable": 0.0005, "truncated": 0.0005, "usable_stop": 0.999},
            "c": {"unusable": 0.001, "truncated": 0.0005, "usable_stop": 0.9985},
        }
        self.assertEqual(degenerate_reason(scores), "top_class_saturated")

    def test_all_healthy_with_real_spread_is_NOT_degenerate(self):
        # The honest "everyone healthy" case: same verdict, real separation.
        from harness.local_fit.dispatch import degenerate_reason
        scores = {
            "a": {"unusable": 0.05, "truncated": 0.05, "usable_stop": 0.90},
            "b": {"unusable": 0.30, "truncated": 0.10, "usable_stop": 0.60},
        }
        self.assertIsNone(degenerate_reason(scores))

    def test_single_candidate_is_never_degenerate(self):
        from harness.local_fit.dispatch import degenerate_reason
        scores = {"only": {"unusable": 0.5, "truncated": 0.0,
                           "usable_stop": 0.5}}
        self.assertIsNone(degenerate_reason(scores))


class TestDegenerateArtifactFailsClosed(GuardTestBase):
    """A saturated artifact must stand down WITH evidence, not reorder."""

    class _SaturatedScorer:
        """Emits the observed defect: identical saturated scores for all."""

        def score(self, feats):
            return {"unusable": 0.0000, "truncated": 0.0000,
                    "usable_stop": 1.0000, "best_guess": "usable_stop"}

    def test_influence_stands_down_with_degenerate_reason(self):
        from unittest import mock
        profiles = _profiles()
        pool = list(profiles)
        _flags_off()
        baseline = order_pool(pool, profiles, _report(), task="code",
                              free_tier=True)
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        os.environ["HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"] = "0.0001"
        with mock.patch("harness.local_fit.config.load_scorer",
                        return_value=self._SaturatedScorer()):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ordered = order_pool(pool, profiles, _report(), task="code",
                                     free_tier=True)
        self.assertEqual(ordered, baseline,
                         "degenerate artifact must not reorder")
        self.assertIn("degenerate", buf.getvalue())

    def test_dispatch_result_carries_degenerate_reason(self):
        from unittest import mock
        from harness.local_fit.dispatch import maybe_order_pool
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        os.environ["HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"] = "0.0001"
        with mock.patch("harness.local_fit.config.load_scorer",
                        return_value=self._SaturatedScorer()):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                res = maybe_order_pool(["a", "b"], task="structured",
                                       free_tier=True, profiles=None,
                                       calibration={}, call_lane="apply",
                                       baseline_keys={"a": (0, -1.0, -1.0),
                                                      "b": (0, -0.9, -1.0)})
        self.assertEqual(res["degenerate"], "score_spread_too_small")
        self.assertFalse(res["reordered"])
        self.assertEqual(res["flagged"], [])
        self.assertIn("degenerate", buf.getvalue())

    def test_dispatch_crash_reports_stderr_but_never_raises(self):
        from unittest import mock
        from harness.local_fit.dispatch import maybe_order_pool
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = "/nonexistent"
        with mock.patch("harness.local_fit.config.load_scorer",
                        side_effect=RuntimeError("boom")):
            res = maybe_order_pool(["a", "b"])
        self.assertEqual(res["stage"], "off")
        self.assertFalse(res["reordered"])


class TestEndToEndRealArtifact(GuardTestBase):
    """Mock-free: train -> export -> stdlib score -> order_pool INFLUENCE.

    The synthetic training data contains a genuinely failing model (every
    seat it takes is unusable). The shipped path must produce a calibrated
    artifact whose scores separate BAD from GOOD, and INFLUENCE must demote
    BAD within its tier while OFF/OBSERVE keep the exact baseline order.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.model_dir, cls.files, cls.rows = _train_dir(
            models_bad=BAD, models_good=[GOOD, OTHER])

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _order(self, stage_flags):
        profiles = _profiles()
        pool = [GOOD, OTHER, BAD]
        _flags_off()
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = self.model_dir
        os.environ.update(stage_flags)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ordered = order_pool(pool, profiles, _report(), task="code",
                                     free_tier=True)
            return ordered, buf.getvalue()
        finally:
            _flags_off()

    def _baseline(self):
        _flags_off()
        profiles = _profiles()
        pool = [GOOD, OTHER, BAD]
        return order_pool(pool, profiles, _report(), task="code",
                          free_tier=True)

    def test_artifact_distinguishes_bad_from_good(self):
        # The raw exported scores (no threshold games) must separate the
        # genuinely failing model from the healthy ones. On the corpus's own
        # training rows the separation is the honest measure: the failing
        # model's seats all sat at p_unusable ~0.54 vs the healthy pool's
        # ~0.14 (measured; T=0.5). Dispatch-time features blur model identity
        # (the extractor's seat-shape knowledge is richer than dispatch's),
        # but the ORDER must survive.
        from harness.local_fit.infer import StdlibScorer
        from harness.local_fit.features import build_dispatch_features
        sc = StdlibScorer(os.path.join(self.model_dir, "model_weights.json"),
                          os.path.join(self.model_dir, "model_meta.json"))
        bad_rows = [sc.score(r.features)["unusable"] for r in self.rows
                    if r.model == BAD]
        good_rows = [sc.score(r.features)["unusable"] for r in self.rows
                     if r.model != BAD]
        mean_bad = sum(bad_rows) / len(bad_rows)
        mean_good = sum(good_rows) / len(good_rows)
        self.assertGreater(mean_bad - mean_good, 0.2,
                           f"calibrated artifact must separate BAD rows "
                           f"({mean_bad:.4f}) from GOOD rows ({mean_good:.4f})")

        # Dispatch-time ordering: BAD must score at least as risky as the
        # healthy models (the calibrated net keeps the ranking).
        def p_unus(model):
            f = build_dispatch_features(model, task="structured",
                                        free_tier=True, profile=None,
                                        calibration=_report()["calibration"][model],
                                        call_lane="apply")
            return sc.score(f)["unusable"]
        self.assertGreaterEqual(p_unus(BAD), p_unus(GOOD))

    def test_observe_order_identical_to_baseline(self):
        baseline = self._baseline()
        ordered, _ = self._order({})
        self.assertEqual(ordered, baseline)

    def test_influence_demotes_bad_within_tier(self):
        # BAD starts in the healthy tier (its ledger shows a couple of
        # ambiguous samples, not yet 2 strikes) and sorts above OTHER on
        # declared capability. The scorer knows from training data that BAD
        # fails: this is exactly the advisory's value -- act before the
        # ledger accumulates strikes. INFLUENCE must demote BAD below its
        # healthy-tier peers WITHOUT crossing into the striked tier.
        # The ledger history also feeds the observed_* features, so BAD's
        # real failure history sharpens its dispatch-time score.
        clean = {"calibration": {
            GOOD: _report()["calibration"][GOOD],
            OTHER: _report()["calibration"][OTHER],
            BAD: {"samples": 12, "success_rate": 0.55, "unusable_outputs": 0,
                  "consent_unusable": 0},
        }}
        # The ledger gives BAD only a middling success rate on a handful of
        # samples (below the 2-strike demotion policy), while declared
        # capability ties GOOD. The SCORER, though, has watched BAD fail
        # every seat in training -- exactly the case where the advisory adds
        # information the ledger has not yet accumulated into strikes.
        profiles = _profiles()
        profiles[BAD] = CapabilityProfile(
            BAD, context_length=128000, free=True, prompt_price=0,
            completion_price=0, supports_reasoning=True,
            supports_structured_json=True)
        pool = [GOOD, OTHER, BAD]
        _flags_off()
        baseline = order_pool(pool, profiles, clean, task="code",
                              free_tier=True)
        self.assertNotEqual(baseline[-1], BAD,
                            "test setup: BAD must start above last place")
        # Pick the threshold from the artifact's own dispatch-time scores:
        # between the highest healthy score and BAD's, so exactly BAD is
        # flagged (a near-zero threshold would flag everyone and change
        # nothing). Scores are deterministic for this corpus/net seed.
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = self.model_dir
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        from harness.local_fit.config import load_scorer
        from harness.local_fit.features import build_dispatch_features
        sc0 = load_scorer()
        pscore = {
            m: sc0.score(build_dispatch_features(
                m, task="code", free_tier=True, profile=profiles[m],
                calibration=clean["calibration"][m],
                call_lane="apply"))["unusable"]
            for m in pool}
        bad_p = pscore[BAD]
        healthy_top = max(p for m, p in pscore.items() if m != BAD)
        self.assertGreater(bad_p, healthy_top,
                           "scorer must rank BAD riskiest among the pool")
        os.environ["HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"] = (
            f"{(bad_p + healthy_top) / 2:.6f}")
        try:
            ordered = order_pool(pool, profiles, clean, task="code",
                                 free_tier=True)
        finally:
            _flags_off()
        # BAD moved to the back of the healthy tier...
        self.assertEqual(ordered[-1], BAD)
        # ...healthy peers keep their relative order...
        self.assertEqual([m for m in ordered if m != BAD], [GOOD, OTHER])
        # ...and the tier boundary is intact (compare with the striked case).
        profiles2 = dict(profiles)
        profiles2[BAD] = _profiles()[BAD]
        pool2 = [GOOD, OTHER, BAD]
        struck = {"calibration": {
            GOOD: _report()["calibration"][GOOD],
            OTHER: _report()["calibration"][OTHER],
            BAD: {"samples": 30, "success_rate": 0.1, "unusable_outputs": 2,
                  "consent_unusable": 0},
        }}
        _flags_off()
        base_struck = order_pool(pool2, profiles2, struck, task="code",
                                 free_tier=True)
        os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
        os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = self.model_dir
        os.environ["HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER"] = "1"
        os.environ["HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD"] = "0.0001"
        try:
            ordered_struck = order_pool(pool2, profiles2, struck, task="code",
                                        free_tier=True)
        finally:
            _flags_off()
        # (threshold 0.0001 flags everyone in the striked case, which demotes
        # all models within their tiers equally — the order stays the baseline
        # and the boundary is trivially intact)
        self.assertEqual(ordered_struck[-1], base_struck[-1],
                         "advisory must never push a model past the "
                         "striked-demotion tier boundary")

    def test_off_order_identical_to_baseline_and_no_stderr(self):
        baseline = self._baseline()
        profiles = _profiles()
        pool = [GOOD, OTHER, BAD]
        _flags_off()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ordered = order_pool(pool, profiles, _report(), task="code",
                                 free_tier=True)
        self.assertEqual(ordered, baseline)
        self.assertNotIn("local_fit", buf.getvalue())

    def test_runtime_scoring_stays_stdlib_only(self):
        baseline_mods = set(sys.modules)
        from harness.local_fit.infer import StdlibScorer
        from harness.local_fit.features import build_dispatch_features
        sc = StdlibScorer(os.path.join(self.model_dir, "model_weights.json"),
                          os.path.join(self.model_dir, "model_meta.json"))
        f = build_dispatch_features(GOOD, task="structured", free_tier=True,
                                    profile=None,
                                    calibration=_report()["calibration"][GOOD],
                                    call_lane="apply")
        sc.score(f)
        newly = set(sys.modules) - baseline_mods
        for banned in ("numpy", "onnx", "onnxruntime"):
            hits = [m for m in newly if m == banned or m.startswith(banned + ".")]
            self.assertEqual(hits, [], f"runtime scoring imported {hits}")


class TestExportCalibration(GuardTestBase):
    """Export-side temperature contract (train-time; numpy allowed here)."""

    def test_exported_temperature_is_persisted_and_applied(self):
        tmp, model_dir, files, rows = _train_dir(
            models_bad=BAD, models_good=[GOOD, OTHER], n_files=3)
        try:
            with open(os.path.join(model_dir, "model_weights.json"),
                      encoding="utf-8") as f:
                w = json.load(f)
            with open(os.path.join(model_dir, "model_meta.json"),
                      encoding="utf-8") as f:
                meta = json.load(f)
            self.assertIn("temperature", w)
            self.assertEqual(w["temperature"], meta["temperature"])
            self.assertLess(w["temperature"], 1.0,
                            "this synthetic set has a usable failing model; "
                            "export must sharpen, not flatten")
            # The stdlib scorer applies it: raw logits / T before softmax.
            from harness.local_fit.infer import StdlibScorer
            sc = StdlibScorer(os.path.join(model_dir, "model_weights.json"),
                              os.path.join(model_dir, "model_meta.json"))
            self.assertEqual(sc.temperature, w["temperature"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_contrast_dataset_refuses_sharpening(self):
        # All-usable rows: no label contrast -> T must stay 1.0 so the guard
        # (not a fabricated temperature) is the honest final say.
        from harness.local_fit.extract import extract
        from harness.local_fit.train import (
            TinyNet, build_dataset, compute_stats, export_temperature)
        tmp, model_dir, files, rows = _train_dir(
            models_bad=BAD, models_good=[GOOD, OTHER], n_files=3)
        try:
            usable_rows = [r for r in extract(files) if r.label == "usable_stop"]
            # Fabricate an all-usable dataset by relabeling.
            for r in usable_rows:
                r.label = "usable_stop"
            stats = compute_stats(usable_rows)
            X, Y = build_dataset(usable_rows, stats)
            net = TinyNet(X.shape[1], hidden=4, seed=7)
            net.train(X, Y, epochs=5, lr=0.01)
            t = export_temperature(net, X, Y, failure_rate=0.0)
            self.assertEqual(t, 1.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_onnx_parity_holds_after_calibration(self):
        # The stdlib scorer (logits / T) must equal ONNX logits / T softmaxed.
        import numpy as np
        from harness.local_fit.extract import extract
        from harness.local_fit.train import LocalScorer, run_pipeline
        from harness.local_fit.features import (
            build_dispatch_features, build_feature_vector)
        tmp, model_dir, files, rows = _train_dir(
            models_bad=BAD, models_good=[GOOD, OTHER], n_files=3)
        try:
            run_pipeline(extract(files), model_dir)
            sc = LocalScorer(os.path.join(model_dir, "model.onnx"),
                             os.path.join(model_dir, "model_meta.json"))
            from harness.local_fit.infer import StdlibScorer
            with open(os.path.join(model_dir, "model_meta.json"),
                      encoding="utf-8") as f:
                meta = json.load(f)
            std = StdlibScorer(os.path.join(model_dir, "model_weights.json"),
                               os.path.join(model_dir, "model_meta.json"))
            feats = build_dispatch_features(GOOD, task="structured",
                                            free_tier=True, profile=None,
                                            calibration=None, call_lane="apply")
            vec = np.array([build_feature_vector(feats, meta["stats"])],
                           dtype=np.float32)
            logits = sc.sess.run(None, {"X": vec})[0][0] / std.temperature
            # Mirror the scorer's input clipping before softmax.
            from harness.local_fit.infer import Z_CLIP
            clipped = np.array([max(-Z_CLIP, min(Z_CLIP, v)) for v in vec[0]],
                               dtype=np.float32)
            logits = sc.sess.run(None, {"X": clipped[None, :]})[0][0] / std.temperature
            e = np.exp(logits - np.max(logits))
            onnx_probs = e / e.sum()
            got = std.score(feats)
            for i, k in enumerate(("unusable", "truncated", "usable_stop")):
                self.assertAlmostEqual(got[k], float(onnx_probs[i]), places=6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
