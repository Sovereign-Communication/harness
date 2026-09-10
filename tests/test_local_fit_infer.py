"""C2 tests: pure-stdlib inference, weights export, and import isolation.

Guards the zero-runtime-dependency promise of the enabled advisory path.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _synth_weights(in_dim: int, hidden: int = 4, out: int = 3):
    """Deterministic hand-computable weights."""
    w1 = [[0.01 * ((i + 1) * (j + 1) % 7 - 3) for j in range(hidden)] for i in range(in_dim)]
    b1 = [0.1 * (j + 1) for j in range(hidden)]
    w2 = [[0.02 * ((j + 1) * (k + 2) % 5 - 2) for k in range(out)] for j in range(hidden)]
    b2 = [0.05, -0.05, 0.0]
    return {"w1": w1, "b1": b1, "w2": w2, "b2": b2}


def _write_model(d: str, in_dim: int, meta_extra=None):
    from harness.local_fit.features import expected_vector_length

    in_dim = in_dim or expected_vector_length()
    w = _synth_weights(in_dim)
    weights = {"format": "harness-local-fit-stdlib-weights", "version": 1,
               "in_dim": in_dim, "hidden": 4, "outputs": 3,
               "activation": "relu", **w}
    meta = {
        "input_dim": in_dim,
        "output_classes": ["unusable", "truncated", "usable_stop"],
        "stats": {},
    }
    if meta_extra:
        meta.update(meta_extra)
    with open(os.path.join(d, "model_weights.json"), "w", encoding="utf-8") as f:
        json.dump(weights, f)
    with open(os.path.join(d, "model_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return weights


class TestStdlibScorer(unittest.TestCase):
    def test_forward_pass_matches_hand_computation(self):
        from harness.local_fit.infer import StdlibScorer, _softmax
        from harness.local_fit.features import expected_vector_length

        in_dim = expected_vector_length()
        with tempfile.TemporaryDirectory() as d:
            weights = _write_model(d, in_dim)
            s = StdlibScorer(os.path.join(d, "model_weights.json"),
                             os.path.join(d, "model_meta.json"))

        feats = {"task_type": "verify_panel", "seat_role": "panel",
                 "reasoning_effort": "low"}
        vec = s.score(feats)

        # Hand-compute the forward pass from the same weights.
        from harness.local_fit.features import build_feature_vector
        x = build_feature_vector(feats, {})
        w1, b1, w2, b2 = weights["w1"], weights["b1"], weights["w2"], weights["b2"]
        h = []
        for j in range(len(b1)):
            acc = b1[j] + sum(x[i] * w1[i][j] for i in range(in_dim))
            h.append(max(0.0, acc))
        logits = [b2[k] + sum(h[j] * w2[j][k] for j in range(len(b1))) for k in range(3)]
        expected = _softmax(logits)

        self.assertAlmostEqual(vec["unusable"], expected[0], places=7)
        self.assertAlmostEqual(vec["truncated"], expected[1], places=7)
        self.assertAlmostEqual(vec["usable_stop"], expected[2], places=7)
        self.assertAlmostEqual(sum(vec[k] for k in ("unusable", "truncated", "usable_stop")), 1.0, places=7)
        self.assertIn(vec["best_guess"], ("unusable", "truncated", "usable_stop"))

    def test_probs_sum_to_one_and_are_order_stable(self):
        from harness.local_fit.infer import StdlibScorer
        from harness.local_fit.features import expected_vector_length

        in_dim = expected_vector_length()
        with tempfile.TemporaryDirectory() as d:
            _write_model(d, in_dim)
            s = StdlibScorer(os.path.join(d, "model_weights.json"),
                             os.path.join(d, "model_meta.json"))
        a = s.score({"task_type": "apply", "seat_role": "apply"})
        b = s.score({"task_type": "apply", "seat_role": "apply"})
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(a[k] for k in ("unusable", "truncated", "usable_stop")), 1.0, places=7)

    def test_dim_mismatch_fails_closed(self):
        from harness.local_fit.infer import StdlibScorer
        with tempfile.TemporaryDirectory() as d:
            _write_model(d, 10)  # wrong in_dim on purpose
            with self.assertRaises(ValueError):
                StdlibScorer(os.path.join(d, "model_weights.json"),
                             os.path.join(d, "model_meta.json"))

    def test_stats_zscore_is_applied(self):
        from harness.local_fit.infer import StdlibScorer
        from harness.local_fit.features import expected_vector_length

        in_dim = expected_vector_length()
        with tempfile.TemporaryDirectory() as d:
            meta_extra = {"stats": {"prompt_chars": {"mean": 100.0, "stdev": 10.0}}}
            _write_model(d, in_dim, meta_extra)
            s = StdlibScorer(os.path.join(d, "model_weights.json"),
                             os.path.join(d, "model_meta.json"))
        # prompt_chars=150 -> z=5.0; verify it changes the output vs 0.
        a = s.score({"prompt_chars": 150})
        b = s.score({"prompt_chars": 0})
        self.assertNotEqual(a, b)


class TestImportIsolation(unittest.TestCase):
    def test_enabled_scoring_path_imports_no_numpy_or_onnxruntime(self):
        """Scoring must not newly import numpy/onnx/onnxruntime or the train module.

        Strategy: snapshot sys.modules before the enabled scoring call, score,
        and assert none of the heavy third-party/train modules were newly added.
        (Deleting already-imported modules to force a fresh import is unreliable
        with numpy's submodule machinery.)
        """
        from harness.local_fit.extract import all_run_files, extract
        from harness.local_fit.train import run_pipeline

        files = all_run_files("audits")
        rows = extract(files)
        self.assertGreater(len(rows), 0)

        with tempfile.TemporaryDirectory() as d:
            run_pipeline(rows, d)  # train-time path; numpy allowed here
            # Simulate a stdlib-only process: the loader must serve StdlibScorer.
            os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
            os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = d
            try:
                import harness.local_fit.config as config
                before = set(sys.modules)
                scorer = config.load_scorer()
                self.assertIsNotNone(scorer)
                from harness.local_fit.infer import StdlibScorer
                self.assertIsInstance(scorer, StdlibScorer)
                scores = scorer.score(rows[0].features)
                self.assertIn("usable_stop", scores)
                newly = set(sys.modules) - before
                for banned in ("numpy", "onnx", "onnxruntime"):
                    hits = [m for m in newly if m == banned or m.startswith(banned + ".")]
                    self.assertEqual(hits, [], f"runtime scoring imported {hits}")
                self.assertNotIn("harness.local_fit.train", newly,
                                 "scoring must not import the train module")
            finally:
                os.environ.pop("HARNESS_LOCAL_FIT_ENABLE", None)
                os.environ.pop("HARNESS_LOCAL_FIT_MODEL_DIR", None)

    def test_loader_prefers_stdlib_artifact(self):
        from harness.local_fit import model_loader
        from harness.local_fit.infer import StdlibScorer

        with tempfile.TemporaryDirectory() as d:
            from harness.local_fit.features import expected_vector_length
            _write_model(d, expected_vector_length())
            s = model_loader.LocalScorer(d)
            self.assertIsInstance(s, StdlibScorer)

    def test_loader_returns_none_for_empty_dir(self):
        from harness.local_fit import model_loader
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(model_loader.LocalScorer(d))

    def test_pipeline_exports_weights_json(self):
        from harness.local_fit.extract import all_run_files, extract
        from harness.local_fit.train import run_pipeline

        rows = extract(all_run_files("audits"))
        with tempfile.TemporaryDirectory() as d:
            run_pipeline(rows, d)
            self.assertTrue(os.path.exists(os.path.join(d, "model_weights.json")))
            # stdlib scorer loads the pipeline's own artifact
            from harness.local_fit.infer import StdlibScorer
            s = StdlibScorer(os.path.join(d, "model_weights.json"),
                             os.path.join(d, "model_meta.json"))
            scores = s.score(rows[0].features)
            self.assertAlmostEqual(
                sum(scores[k] for k in ("unusable", "truncated", "usable_stop")),
                1.0, places=6)


if __name__ == "__main__":
    unittest.main()
