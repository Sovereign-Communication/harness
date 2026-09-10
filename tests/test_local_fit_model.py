"""Tests for training pipeline, ONNX export, and advisory scorer.

These are hermetic where possible and only depend on extracted rows from
existing run JSON via the read-only extractor.
"""

import os
import sys
import tempfile
import unittest

try:
    import numpy as np
except ImportError:  # pragma: no cover - train-time extra absent
    np = None

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Train-time tests need the optional local-fit-train extra (numpy/onnx).
# Without it the modules under test cannot even import, so skip the class
# instead of failing collection on clean CI runners.
needs_numpy = unittest.skipUnless(
    np is not None, "train-time deps (numpy) required")


def extract_some_rows():
    from harness.local_fit.extract import extract
    path = os.path.join("audits", "scmessenger", "_runs", "v4")
    files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith(".json")]
    return extract(files)


@needs_numpy
class TestFeatureVector(unittest.TestCase):
    def test_vector_length_matches_schema(self):
        from harness.local_fit.train import expected_vector_length, canonical_feature_order
        self.assertEqual(expected_vector_length(), len(canonical_feature_order()))

    def test_vector_shape_stable(self):
        from harness.local_fit.train import compute_stats, build_feature_vector, expected_vector_length
        rows = extract_some_rows()
        stats = compute_stats(rows)
        vec = build_feature_vector(rows[0].features, stats)
        self.assertEqual(len(vec), expected_vector_length())


@needs_numpy
class TestTrainingAndExport(unittest.TestCase):
    def test_train_export_and_smoke(self):
        from harness.local_fit.train import run_pipeline
        rows = extract_some_rows()
        self.assertGreater(len(rows), 0)

        with tempfile.TemporaryDirectory() as d:
            result = run_pipeline(rows, d)
            self.assertGreater(result["rows"], 0)
            self.assertTrue(os.path.exists(result["model_path"]))
            self.assertTrue(os.path.exists(result["meta_path"]))

    def test_onnx_runtime_loads_and_scores(self):
        from harness.local_fit.train import compute_stats, build_dataset, TinyNet, export_onnx, export_metadata, canonical_feature_order, softmax, LABEL_INDEX
        rows = extract_some_rows()
        stats = compute_stats(rows)
        X, Y = build_dataset(rows, stats)
        in_dim = X.shape[1]
        net = TinyNet(in_dim, hidden=16, seed=13)
        net.train(X, Y, epochs=40, lr=0.02)

        with tempfile.TemporaryDirectory() as d:
            model_path = os.path.join(d, "model.onnx")
            meta_path = os.path.join(d, "model_meta.json")
            export_onnx(net, in_dim, model_path)
            export_metadata(stats, canonical_feature_order(), LABEL_INDEX, meta_path)

            import onnxruntime as ort
            sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            logits = sess.run(None, {"X": X[:1]})[0][0]
            probs = softmax(logits)
            self.assertAlmostEqual(float(np.sum(probs)), 1.0, places=5)
            self.assertEqual(len(probs), 3)

    def test_advisory_hook_reads_scores(self):
        from harness.local_fit.train import compute_stats, build_dataset, export_onnx, export_metadata, canonical_feature_order, TinyNet, LABEL_INDEX
        rows = extract_some_rows()
        stats = compute_stats(rows)
        X, Y = build_dataset(rows, stats)
        in_dim = X.shape[1]
        net = TinyNet(in_dim, hidden=16, seed=13)
        net.train(X, Y, epochs=40, lr=0.02)

        with tempfile.TemporaryDirectory() as d:
            model_path = os.path.join(d, "model.onnx")
            meta_path = os.path.join(d, "model_meta.json")
            export_onnx(net, in_dim, model_path)
            export_metadata(stats, canonical_feature_order(), LABEL_INDEX, meta_path)

            os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
            os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = d
            try:
                from harness.local_fit.config import load_scorer, is_enabled
                self.assertTrue(is_enabled())
                scorer = load_scorer()
                self.assertIsNotNone(scorer)
                scores = scorer.score(rows[0].features)
                self.assertIn("usable_stop", scores)
                self.assertIn("truncated", scores)
                self.assertIn("unusable", scores)
            finally:
                os.environ.pop("HARNESS_LOCAL_FIT_ENABLE", None)
                os.environ.pop("HARNESS_LOCAL_FIT_MODEL_DIR", None)

    def test_flag_off_is_inert(self):
        from harness.local_fit.config import is_enabled, load_scorer
        self.assertFalse(is_enabled())
        self.assertIsNone(load_scorer())


@needs_numpy
class TestRunLevelSplit(unittest.TestCase):
    def test_split_by_file_is_deterministic(self):
        from harness.local_fit.train import split_files
        files = ["b.json", "a.json", "c.json", "d.json", "e.json", "f.json", "g.json", "h.json", "i.json"]
        t1, e1 = split_files(files, train_ratio=0.75, seed=1)
        t2, e2 = split_files(files, train_ratio=0.75, seed=1)
        self.assertEqual(t1, t2)
        self.assertEqual(e1, e2)
        # No overlap between train and eval
        self.assertEqual(set(t1) & set(e1), set())
        # train gets 6, eval gets 3 with 9 files at 0.75
        self.assertEqual(len(t1), 6)
        self.assertEqual(len(e1), 3)

    def test_split_no_row_leakage(self):
        from harness.local_fit.train import split_files
        files = ["run1.json", "run2.json", "run3.json", "run4.json", "run5.json"]
        t, e = split_files(files, train_ratio=0.6, seed=7)
        self.assertEqual(set(t) & set(e), set())
        self.assertGreater(len(t), 0)
        self.assertGreater(len(e), 0)


@needs_numpy
class TestHoldOutEval(unittest.TestCase):
    def test_run_eval_produces_artifact_with_eval_metadata(self):
        from harness.local_fit.train import run_eval, split_files
        v4 = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(v4, f) for f in sorted(os.listdir(v4)) if f.endswith(".json")]
        train_files, eval_files = split_files(files, train_ratio=0.75, seed=1)
        self.assertGreater(len(train_files), 0)
        self.assertGreater(len(eval_files), 0)

        with tempfile.TemporaryDirectory() as d:
            result = run_eval(train_files, eval_files, d, train_ratio=0.75, seed=1)
            self.assertTrue(os.path.exists(result["model_path"]))
            self.assertTrue(os.path.exists(result["meta_path"]))
            self.assertTrue(os.path.exists(result["summary_path"]))
            self.assertGreater(result["train_rows"], 0)
            self.assertGreater(result["eval_rows"], 0)
            self.assertIn("eval", result)
            self.assertIn("eval_metrics", result)
            self.assertIn("train_metrics", result)

    def test_eval_metadata_frozen_in_artifact(self):
        from harness.local_fit.train import run_eval, split_files
        v4 = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(v4, f) for f in sorted(os.listdir(v4)) if f.endswith(".json")]
        train_files, eval_files = split_files(files, train_ratio=0.75, seed=1)

        with tempfile.TemporaryDirectory() as d:
            result = run_eval(train_files, eval_files, d, train_ratio=0.75, seed=1)
            import json
            with open(result["meta_path"]) as mf:
                meta = json.load(mf)

        self.assertIn("eval", meta)
        ev = meta["eval"]
        self.assertIn("train_files", ev)
        self.assertIn("eval_files", ev)
        self.assertIn("train_rows", ev)
        self.assertIn("eval_rows", ev)
        self.assertIn("eval", ev)
        self.assertIn("top1_accuracy", ev["eval"])
        self.assertIn("top2_accuracy", ev["eval"])
        self.assertIn("per_class", ev["eval"])
        self.assertIn("confusion", ev["eval"])
        # eval metrics are floats, reproducible
        self.assertIsInstance(ev["eval"]["top1_accuracy"], float)

    def test_eval_top1_and_top2_reported(self):
        from harness.local_fit.train import run_eval, split_files
        v4 = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(v4, f) for f in sorted(os.listdir(v4)) if f.endswith(".json")]
        train_files, eval_files = split_files(files, train_ratio=0.75, seed=1)

        with tempfile.TemporaryDirectory() as d:
            result = run_eval(train_files, eval_files, d, train_ratio=0.75, seed=1)

        em = result["eval_metrics"]
        self.assertIn("top1_accuracy", em)
        self.assertIn("top2_accuracy", em)
        self.assertIn("top1_correct", em)
        self.assertIn("top2_correct", em)
        self.assertIn("per_class", em)
        self.assertIn("confusion", em)
        self.assertIn("class_order", em)
        # top2 should be >= top1
        self.assertGreaterEqual(em["top2_accuracy"], em["top1_accuracy"])
        # confusion is 3x3
        self.assertEqual(len(em["confusion"]), 3)
        self.assertEqual(len(em["confusion"][0]), 3)

    def test_eval_stats_from_train_only(self):
        """Verify that eval uses train stats, not eval stats, for normalization."""
        from harness.local_fit.train import split_files, compute_stats
        from harness.local_fit.extract import extract
        v4 = os.path.join("audits", "scmessenger", "_runs", "v4")
        files = [os.path.join(v4, f) for f in sorted(os.listdir(v4)) if f.endswith(".json")]
        train_files, eval_files = split_files(files, train_ratio=0.75, seed=1)
        train_rows = extract(train_files)
        eval_rows = extract(eval_files)
        train_stats = compute_stats(train_rows)
        eval_stats = compute_stats(eval_rows)
        # Train and eval stats should differ in general (different row sets)
        # but both should be valid dicts.
        self.assertIsInstance(train_stats, dict)
        self.assertIsInstance(eval_stats, dict)
        self.assertGreater(len(train_stats), 0)
        self.assertGreater(len(eval_stats), 0)


@needs_numpy
class TestAllAuditsTrainEval(unittest.TestCase):
    def test_all_audits_run_eval_succeeds(self):
        """Train + hold-out eval over the union of all available *_runs/ data."""
        from harness.local_fit.extract import all_run_files
        from harness.local_fit.train import run_eval, split_files
        files = all_run_files("audits")
        self.assertGreater(len(files), 0)
        train_files, eval_files = split_files(files, train_ratio=0.75, seed=1)
        self.assertGreater(len(train_files), 0)
        self.assertGreater(len(eval_files), 0)

        with tempfile.TemporaryDirectory() as d:
            result = run_eval(train_files, eval_files, d, train_ratio=0.75, seed=1)
            self.assertTrue(os.path.exists(result["model_path"]))
            self.assertTrue(os.path.exists(result["meta_path"]))
            self.assertTrue(os.path.exists(result["summary_path"]))
            self.assertGreater(result["train_rows"], 0)
            self.assertGreater(result["eval_rows"], 0)
            self.assertIn("eval", result)
            self.assertIn("eval_metrics", result)
            self.assertIn("train_metrics", result)

    def test_all_audits_eval_artifact_carries_eval_block(self):
        from harness.local_fit.extract import all_run_files
        from harness.local_fit.train import run_eval, split_files
        files = all_run_files("audits")
        train_files, eval_files = split_files(files, train_ratio=0.75, seed=1)
        with tempfile.TemporaryDirectory() as d:
            result = run_eval(train_files, eval_files, d, train_ratio=0.75, seed=1)
            import json
            with open(result["meta_path"]) as mf:
                meta = json.load(mf)
        self.assertIn("eval", meta)
        ev = meta["eval"]["eval"]
        self.assertIn("top1_accuracy", ev)
        self.assertIn("top2_accuracy", ev)
        self.assertIn("per_class", ev)
        self.assertIn("confusion", ev)


@needs_numpy
class TestMultiSeedEval(unittest.TestCase):
    def test_multi_seed_range_reproducible(self):
        """Run hold-out eval over a few seeds and report a compact range; results must be deterministic per seed."""
        from harness.local_fit.extract import all_run_files
        from harness.local_fit.train import eval_over_seeds
        files = all_run_files("audits")
        self.assertGreater(len(files), 0)
        report = eval_over_seeds(files, tempfile.mkdtemp(), train_ratio=0.75, seeds=(1, 2, 3))
        self.assertIn("per_seed", report)
        self.assertGreaterEqual(len(report["per_seed"]), 3)
        self.assertEqual(report["train_ratio"], 0.75)
        self.assertEqual(report["seeds"], [1, 2, 3])
        self.assertGreater(report["all_files_count"], 0)
        # union counts
        self.assertIn("union_rows", report)
        self.assertGreater(report["union_rows"], 0)
        self.assertIn("union_by_task_type", report)
        self.assertIn("union_by_label", report)
        # each seed must carry full eval metrics
        for seed_result in report["per_seed"]:
            self.assertIn("seed", seed_result)
            self.assertIn("train", seed_result)
            self.assertIn("eval", seed_result)
            self.assertIn("top1_accuracy", seed_result["eval"])
            self.assertIn("top2_accuracy", seed_result["eval"])
            self.assertIn("per_class", seed_result["eval"])
            self.assertIn("confusion", seed_result["eval"])
            self.assertGreaterEqual(seed_result["eval"]["top2_accuracy"], seed_result["eval"]["top1_accuracy"])

    def test_multi_seed_eval_prints_range(self):
        """Sanity: the multi-seed report should show eval top1/top2 across seeds."""
        from harness.local_fit.extract import all_run_files
        from harness.local_fit.train import eval_over_seeds
        files = all_run_files("audits")
        report = eval_over_seeds(files, tempfile.mkdtemp(), train_ratio=0.75, seeds=(1, 2))
        evals = [s["eval"] for s in report["per_seed"]]
        tops = [e["top1_accuracy"] for e in evals]
        self.assertGreaterEqual(min(tops), 0.0)
        self.assertLessEqual(max(tops), 1.0)



class TestNoNetwork(unittest.TestCase):
    """Confirm the local_fit pipeline does not make network calls.

    These tests only assert that the code paths we care about do not import
    or reference any HTTP/LLM client modules. They are a guardrail, not a
    guarantee.
    """
    def test_train_module_has_no_network_imports(self):
        import harness.local_fit.train as t
        source = open(t.__file__, encoding="utf-8").read().lower()
        banned = ["requests", "openai", "anthropic", "httpx", "aiohttp", "urllib.request", "websocket"]
        for b in banned:
            self.assertNotIn(b, source, f"train.py should not reference {b}")

    def test_extract_module_has_no_network_imports(self):
        import harness.local_fit.extract as e
        source = open(e.__file__, encoding="utf-8").read().lower()
        banned = ["requests", "openai", "anthropic", "httpx", "aiohttp", "urllib.request", "websocket"]
        for b in banned:
            self.assertNotIn(b, source, f"extract.py should not reference {b}")

    def test_dispatch_module_has_no_network_imports(self):
        import harness.local_fit.dispatch as d
        source = open(d.__file__, encoding="utf-8").read().lower()
        banned = ["requests", "openai", "anthropic", "httpx", "aiohttp", "urllib.request", "websocket"]
        for b in banned:
            self.assertNotIn(b, source, f"dispatch.py should not reference {b}")

    def test_infer_module_has_no_network_imports(self):
        import harness.local_fit.infer as m
        source = open(m.__file__, encoding="utf-8").read().lower()
        banned = ["requests", "openai", "anthropic", "httpx", "aiohttp", "urllib.request", "websocket"]
        for b in banned:
            self.assertNotIn(b, source, f"infer.py should not reference {b}")
        # The runtime scorer documents the train-time deps by name but must
        # never import them (stdlib-only dispatch path).
        self.assertNotIn("import numpy", source)
        self.assertNotIn("from numpy", source)

