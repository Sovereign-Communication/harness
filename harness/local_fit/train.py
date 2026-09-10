"""Train a small local model-fit classifier from extracted seat rows.

This is intentionally plain:
- numeric feature vector from canonical feature order
- stable categorical encoding
- small feedforward net
- exports ONNX + metadata JSON for local inference
"""

import json
import math
import os
import random
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import onnx
import onnxruntime as ort
from onnxruntime import InferenceSession


# ---------------------------------------------------------------------------
# Feature canonicalization (canonical implementation lives in features.py)
# ---------------------------------------------------------------------------

# Vector canonicalization lives in features.py (pure stdlib) so training and
# inference share one implementation and cannot drift. These wrappers keep
# the historical train.py API for callers and tests.
from .features import (  # noqa: F401
    build_feature_vector,
    expected_vector_length,
    NUMERIC_FEATURES,
)


def feature_order() -> List[str]:
    from .schema import FEATURE_ORDER
    return list(FEATURE_ORDER)


def canonical_feature_order() -> List[str]:
    """Return the exact ordered list of scalar feature names that
    build_feature_vector emits. This is what the ONNX input_dim and
    metadata feature_order should refer to.
    """
    from .schema import TASK_TYPE_VOCAB, SEAT_ROLE_VOCAB, REASONING_EFFORT_VOCAB
    order: List[str] = []
    for v in TASK_TYPE_VOCAB:
        order.append(f"task_type={v}")
    for v in SEAT_ROLE_VOCAB:
        order.append(f"seat_role={v}")
    for v in REASONING_EFFORT_VOCAB:
        order.append(f"reasoning_effort={v}")
    order.extend(NUMERIC_FEATURES)
    return order


# NOTE: build_feature_vector and expected_vector_length are imported from
# features.py above. The previous train.py-local implementations were removed
# so there is exactly one canonical vector builder.


# ---------------------------------------------------------------------------
# Dataset build
# ---------------------------------------------------------------------------

LABEL_INDEX = {
    "unusable": 0,
    "truncated": 1,
    "usable_stop": 2,
}

def build_dataset(rows: List[Any], stats: Dict[str, Dict[str, float]]) -> Tuple[np.ndarray, np.ndarray]:
    """Return (X, Y) with one-hot labels for the 3 classes."""
    X_rows: List[List[float]] = []
    Y_rows: List[List[float]] = []
    for r in rows:
        feat = r.features
        vec = build_feature_vector(feat, stats)
        X_rows.append(vec)
        li = LABEL_INDEX.get(r.label, 0)
        y = [0.0, 0.0, 0.0]
        y[li] = 1.0
        Y_rows.append(y)
    X = np.array(X_rows, dtype=np.float32)
    Y = np.array(Y_rows, dtype=np.float32)
    return X, Y


def compute_stats(rows: List[Any]) -> Dict[str, Dict[str, float]]:
    """Compute means/stdevs for numeric features across extracted rows."""
    from .schema import (
        TASK_TYPE_VOCAB, SEAT_ROLE_VOCAB, REASONING_EFFORT_VOCAB,
    )
    numerics = [
        "structured_output_required", "max_tokens_requested", "prompt_chars",
        "source_window_attached", "claims_count", "convergence_expected", "is_iterative",
        "model_id_hash", "free_tier", "declared_context_length",
        "declared_structured_json", "declared_reasoning",
        "observed_usable_rate", "observed_truncation_rate", "observed_unusable_rate",
        "observed_mean_resp_chars", "observed_median_resp_chars", "observed_max_resp_chars",
        "observed_sample_count",
        "prompt_tokens_est_over_context", "max_tokens_over_mean_resp",
        "structured_need_vs_declared_json", "iterative_vs_truncation_rate",
    ]
    acc: Dict[str, List[float]] = {k: [] for k in numerics}
    for r in rows:
        feat = r.features
        for k in numerics:
            v = feat.get(k, 0.0)
            try:
                acc[k].append(float(v))
            except (TypeError, ValueError):
                acc[k].append(0.0)

    out: Dict[str, Dict[str, float]] = {}
    for k, vals in acc.items():
        if not vals:
            out[k] = {"mean": 0.0, "stdev": 1.0}
            continue
        m = float(np.mean(vals))
        s = float(np.std(vals))
        if s == 0:
            s = 1.0
        out[k] = {"mean": m, "stdev": s}
    return out


# ---------------------------------------------------------------------------
# Small trainable net
# ---------------------------------------------------------------------------

def init_weights(shape: Tuple[int, int], scale: float = 0.1) -> np.ndarray:
    return np.random.randn(*shape).astype(np.float32) * scale


class TinyNet:
    """2-layer feedforward net with softmax output for 3 classes."""

    def __init__(self, in_dim: int, hidden: int = 16, seed: int = 1):
        rng = np.random.default_rng(seed)
        self.w1 = rng.normal(0, 0.1, (in_dim, hidden)).astype(np.float32)
        self.b1 = np.zeros((hidden,), dtype=np.float32)
        self.w2 = rng.normal(0, 0.1, (hidden, 3)).astype(np.float32)
        self.b2 = np.zeros((3,), dtype=np.float32)

    def forward(self, x: np.ndarray) -> np.ndarray:
        h = x @ self.w1 + self.b1
        h = np.maximum(h, 0.0)  # relu
        logits = h @ self.w2 + self.b2
        return logits

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        logits = self.forward(x)
        exp_logits = np.exp(logits - np.max(logits, axis=1, keepdims=True))
        probs = exp_logits / np.sum(exp_logits, axis=1, keepdims=True)
        return probs

    def train_step(self, x: np.ndarray, y: np.ndarray, lr: float = 0.01) -> float:
        # forward
        h = x @ self.w1 + self.b1
        h_relu = np.maximum(h, 0.0)
        logits = h_relu @ self.w2 + self.b2

        # softmax cross-entropy gradient
        exp_logits = np.exp(logits - np.max(logits, axis=1, keepdims=True))
        probs = exp_logits / np.sum(exp_logits, axis=1, keepdims=True)
        dlogits = (probs - y) / max(x.shape[0], 1)

        # backprop
        dw2 = h_relu.T @ dlogits
        db2 = np.sum(dlogits, axis=0)
        dh = dlogits @ self.w2.T
        dh_relu = dh * (h > 0).astype(np.float32)
        dw1 = x.T @ dh_relu
        db1 = np.sum(dh_relu, axis=0)

        # sgd
        self.w1 -= lr * dw1
        self.b1 -= lr * db1
        self.w2 -= lr * dw2
        self.b2 -= lr * db2

        # loss
        n = max(x.shape[0], 1)
        loss = -np.sum(y * np.log(probs + 1e-9)) / n
        return float(loss)

    def train(self, x: np.ndarray, y: np.ndarray, epochs: int = 80, lr: float = 0.02) -> List[float]:
        losses: List[float] = []
        for i in range(epochs):
            idx = np.arange(x.shape[0])
            np.random.shuffle(idx)
            xb = x[idx]
            yb = y[idx]
            loss = self.train_step(xb, yb, lr=lr)
            losses.append(loss)
            if (i + 1) % 20 == 0:
                lr *= 0.5
        return losses


# ---------------------------------------------------------------------------
# Stdlib weights export (runtime artifact for the zero-dependency scorer)
# ---------------------------------------------------------------------------

def export_weights(net: "TinyNet", path: str) -> None:
    """Export the trained net's weights to model_weights.json.

    This JSON artifact is what the pure-stdlib runtime scorer (infer.py)
    consumes, so the enabled advisory path never needs onnxruntime. Layout
    matches the ONNX graph: W1 (in_dim, hidden), b1 (hidden,), W2 (hidden, 3),
    b2 (3,), all row-major nested lists of floats.
    """
    payload = {
        "format": "harness-local-fit-stdlib-weights",
        "version": 1,
        "in_dim": int(net.w1.shape[0]),
        "hidden": int(net.w1.shape[1]),
        "outputs": 3,
        "activation": "relu",
        "w1": [[float(v) for v in row] for row in net.w1.tolist()],
        "b1": [float(v) for v in net.b1.tolist()],
        "w2": [[float(v) for v in row] for row in net.w2.tolist()],
        "b2": [float(v) for v in net.b2.tolist()],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# ONNX export
# ---------------------------------------------------------------------------

def export_onnx(net: TinyNet, in_dim: int, path: str) -> None:
    import onnx
    from onnx import helper, TensorProto

    X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [None, in_dim])
    Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [None, 3])

    # ReLU node
    w1 = helper.make_tensor("w1", TensorProto.FLOAT, [in_dim, 16], net.w1.flatten().tolist())
    b1 = helper.make_tensor("b1", TensorProto.FLOAT, [16], net.b1.tolist())
    matmul1 = helper.make_node("MatMul", ["X", "w1"], ["matmul1"])
    add1 = helper.make_node("Add", ["matmul1", "b1"], ["add1"])
    relu = helper.make_node("Relu", ["add1"], ["relu"])

    w2 = helper.make_tensor("w2", TensorProto.FLOAT, [16, 3], net.w2.flatten().tolist())
    b2 = helper.make_tensor("b2", TensorProto.FLOAT, [3], net.b2.tolist())
    matmul2 = helper.make_node("MatMul", ["relu", "w2"], ["matmul2"])
    add2 = helper.make_node("Add", ["matmul2", "b2"], ["Y"])

    graph = helper.make_graph(
        [matmul1, add1, relu, matmul2, add2],
        "tiny_net",
        [X],
        [Y],
        [w1, b1, w2, b2],
    )
    model = helper.make_model(graph, producer_name="harness_local_fit")
    model.opset_import[0].version = 26
    onnx.checker.check_model(model)
    onnx.save(model, path)


def export_metadata(
    stats: Dict[str, Dict[str, float]],
    feature_order: List[str],
    label_index: Dict[str, int],
    path: str,
    eval_result: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "model_version": "0.1.0-draft",
        "producer": "harness_local_fit",
        "runtime": "stdlib-json",
        "provider": "pure-stdlib",
        "input_dim": len(feature_order),
        "output_classes": ["unusable", "truncated", "usable_stop"],
        "feature_order": feature_order,
        "stats": stats,
        "label_index": label_index,
        "explanation": "Advisory-only local model-fit classifier. Scores seat usability and truncation risk.",
    }
    if eval_result is not None:
        payload["eval"] = eval_result
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def softmax(logits: np.ndarray) -> np.ndarray:
    exp_logits = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
    return exp_logits / np.sum(exp_logits, axis=-1, keepdims=True)


# ---------------------------------------------------------------------------
# Run-level split (deterministic, no row-level leakage)
# ---------------------------------------------------------------------------

def split_files(files: List[str], train_ratio: float = 0.75, seed: int = 1) -> Tuple[List[str], List[str]]:
    """Split run files into train and eval groups by file, deterministically.

    Seats from the same run file never appear in both groups.
    """
    rng = np.random.default_rng(seed)
    ordered = sorted(files)
    idx = np.arange(len(ordered))
    rng.shuffle(idx)
    cut = max(1, int(len(ordered) * train_ratio))
    train_idx = idx[:cut]
    eval_idx = idx[cut:]
    train = [ordered[i] for i in train_idx]
    eval = [ordered[i] for i in eval_idx]
    return train, eval


def eval_metrics(
    probs: np.ndarray,
    y: np.ndarray,
    label_index: Dict[str, int],
    class_names: List[str],
) -> Dict[str, Any]:
    """Compute per-class precision/recall/f1 and overall metrics.

    probs: (N, 3) softmax probabilities.
    y: (N, 3) one-hot labels.
    """
    pred_idx = np.argmax(probs, axis=1)
    true_idx = np.argmax(y, axis=1)

    n = int(y.shape[0])
    correct = int(np.sum(pred_idx == true_idx))

    per_class: Dict[str, Dict[str, Any]] = {}
    for name in class_names:
        ci = label_index[name]
        tp = int(np.sum((pred_idx == ci) & (true_idx == ci)))
        fp = int(np.sum((pred_idx == ci) & (true_idx != ci)))
        fn = int(np.sum((pred_idx != ci) & (true_idx == ci)))
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        if precision + recall > 0:
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = 0.0
        per_class[name] = {
            "support": int(np.sum(true_idx == ci)),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    top1_correct = int(correct)
    top2_correct = 0
    if probs.shape[1] >= 2:
        top2 = np.argsort(probs, axis=1)[:, -2:]
        for i in range(n):
            if true_idx[i] in top2[i]:
                top2_correct += 1

    # Confusion matrix: rows = true, cols = pred
    n_classes = len(class_names)
    confusion = np.zeros((n_classes, n_classes), dtype=int)
    for i in range(n):
        confusion[true_idx[i], pred_idx[i]] += 1

    return {
        "n": n,
        "top1_accuracy": top1_correct / max(n, 1),
        "top1_correct": top1_correct,
        "top2_accuracy": top2_correct / max(n, 1),
        "top2_correct": top2_correct,
        "per_class": per_class,
        "confusion": confusion.tolist(),
        "class_order": class_names,
    }


# ---------------------------------------------------------------------------
# Inference helper
# ---------------------------------------------------------------------------

class LocalScorer:
    """Load exported ONNX model and score candidate seats."""

    def __init__(self, model_path: str, metadata_path: str):
        self.sess = InferenceSession(model_path, providers=["CPUExecutionProvider"])
        with open(metadata_path, "r", encoding="utf-8") as f:
            self.metadata = json.load(f)

    def score(self, row_features: Dict[str, Any]) -> Dict[str, Any]:
        stats = self.metadata["stats"]
        vec = np.array([build_feature_vector(row_features, stats)], dtype=np.float32)
        logits = self.sess.run(None, {"X": vec})[0][0]
        probs = softmax(logits)
        return {
            "unusable": float(probs[0]),
            "truncated": float(probs[1]),
            "usable_stop": float(probs[2]),
            "best_guess": self.metadata["output_classes"][int(np.argmax(probs))],
        }


# ---------------------------------------------------------------------------
# CLI-ish runner for dataset + train + export
# ---------------------------------------------------------------------------

def run_pipeline(
    rows: List[Any],
    out_dir: str,
    force: bool = False,
) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)

    stats = compute_stats(rows)
    X, Y = build_dataset(rows, stats)

    in_dim = X.shape[1]
    net = TinyNet(in_dim, hidden=16, seed=13)
    losses = net.train(X, Y, epochs=80, lr=0.02)

    model_path = os.path.join(out_dir, "model.onnx")
    meta_path = os.path.join(out_dir, "model_meta.json")
    weights_path = os.path.join(out_dir, "model_weights.json")
    export_onnx(net, in_dim, model_path)
    export_weights(net, weights_path)
    export_metadata(stats, canonical_feature_order(), LABEL_INDEX, meta_path)

    # quick smoke inference inside this runtime
    probs = net.predict_proba(X[:1])
    scorer = LocalScorer(model_path, meta_path)
    ref = scorer.score(rows[0].features)

    # The stdlib scorer must agree with the numpy/ONNX graph on the same row.
    from .infer import StdlibScorer
    std_scores = StdlibScorer(weights_path, meta_path).score(rows[0].features)
    for k in ("unusable", "truncated", "usable_stop"):
        if abs(std_scores[k] - ref[k]) > 1e-4:
            raise AssertionError(
                f"stdlib/ONNX scorer mismatch on {k}: {std_scores[k]} vs {ref[k]}"
            )

    return {
        "rows": len(rows),
        "input_dim": in_dim,
        "final_loss": losses[-1],
        "train_losses": losses,
        "model_path": model_path,
        "meta_path": meta_path,
        "smoke_prob_first_row": probs[0].tolist(),
        "smoke_score_first_row": ref,
    }


# ---------------------------------------------------------------------------
# Hold-out evaluation (run-level split, no leakage)
# ---------------------------------------------------------------------------

def run_eval(
    train_files: List[str],
    eval_files: List[str],
    out_dir: str,
    train_ratio: Optional[float] = None,
    seed: int = 1,
    epochs: int = 80,
    lr: float = 0.02,
    hidden: int = 16,
    net_seed: int = 13,
) -> Dict[str, Any]:
    """Train on train_files runs, evaluate on held-out eval_files runs.

    Returns a result dict with train + eval metrics and exported artifact
    paths. The exported metadata includes frozen eval stats.

    Statistics (means/stdevs) are computed from train rows only and applied
    to both train and eval rows, so eval cannot leak through normalization.
    """
    from .extract import extract

    os.makedirs(out_dir, exist_ok=True)

    train_rows = extract(train_files)
    eval_rows = extract(eval_files)

    if not train_rows:
        raise ValueError("no train rows extracted")
    if not eval_rows:
        raise ValueError("no eval rows extracted")

    train_stats = compute_stats(train_rows)
    eval_stats = compute_stats(eval_rows)

    X_train, Y_train = build_dataset(train_rows, train_stats)
    X_eval, Y_eval = build_dataset(eval_rows, train_stats)

    in_dim = X_train.shape[1]
    net = TinyNet(in_dim, hidden=hidden, seed=net_seed)
    train_losses = net.train(X_train, Y_train, epochs=epochs, lr=lr)

    # Predict
    train_proba = net.predict_proba(X_train)
    eval_proba = net.predict_proba(X_eval)

    class_names = ["unusable", "truncated", "usable_stop"]
    train_metrics = eval_metrics(train_proba, Y_train, LABEL_INDEX, class_names)
    eval_metrics_result = eval_metrics(eval_proba, Y_eval, LABEL_INDEX, class_names)

    model_path = os.path.join(out_dir, "model.onnx")
    meta_path = os.path.join(out_dir, "model_meta.json")
    export_onnx(net, in_dim, model_path)
    export_weights(net, os.path.join(out_dir, "model_weights.json"))

    eval_payload = {
        "train_files": [os.path.basename(f) for f in train_files],
        "eval_files": [os.path.basename(f) for f in eval_files],
        "train_rows": len(train_rows),
        "eval_rows": len(eval_rows),
        "seed": seed,
        "split": {"train_ratio": train_ratio if train_ratio is not None else 0.75},
        "train": train_metrics,
        "eval": eval_metrics_result,
    }
    export_metadata(train_stats, canonical_feature_order(), LABEL_INDEX, meta_path, eval_result=eval_payload)

    # Also export a human-readable eval summary alongside the artifact.
    summary_path = os.path.join(out_dir, "eval_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(_eval_summary_text(eval_payload))

    return {
        "model_path": model_path,
        "meta_path": meta_path,
        "summary_path": summary_path,
        "train_rows": len(train_rows),
        "eval_rows": len(eval_rows),
        "train_metrics": train_metrics,
        "eval_metrics": eval_metrics_result,
        "eval": eval_metrics_result,
        "train_losses": train_losses,
        "final_train_loss": train_losses[-1],
        "input_dim": in_dim,
    }


def eval_over_seeds(
    all_files: List[str],
    out_dir: str,
    train_ratio: float = 0.75,
    seeds: List[int] = (1, 2, 3),
    epochs: int = 80,
    lr: float = 0.02,
    hidden: int = 16,
    net_seed: int = 13,
) -> Dict[str, Any]:
    """Run hold-out eval over multiple seeds and return a compact range report.

    For each seed this trains on a deterministic train_ratio split of all_files
    and evaluates on the held-out remainder, then aggregates.

    Returns a dict with per-seed results plus union row counts by task_type and
    label across all_files.
    """
    from .extract import extract

    os.makedirs(out_dir, exist_ok=True)

    seeds = list(seeds)
    per_seed: List[Dict[str, Any]] = []
    for s in seeds:
        train_files, eval_files = split_files(all_files, train_ratio=train_ratio, seed=s)
        with tempfile.TemporaryDirectory() as d:
            result = run_eval(
                train_files, eval_files, d,
                train_ratio=train_ratio, seed=s,
                epochs=epochs, lr=lr, hidden=hidden, net_seed=net_seed,
            )
        per_seed.append({
            "seed": s,
            "train_files": [os.path.basename(f) for f in train_files],
            "eval_files": [os.path.join(os.path.basename(os.path.dirname(f)), os.path.basename(f)) for f in eval_files],
            "train_rows": result["train_rows"],
            "eval_rows": result["eval_rows"],
            "train": result["train_metrics"],
            "eval": result["eval_metrics"],
        })

    union_rows = extract(all_files)
    by_task: Dict[str, int] = {}
    by_label: Dict[str, int] = {}
    for r in union_rows:
        by_task[r.task_type] = by_task.get(r.task_type, 0) + 1
        by_label[r.label] = by_label.get(r.label, 0) + 1

    return {
        "train_ratio": train_ratio,
        "seeds": seeds,
        "all_files_count": len(all_files),
        "per_seed": per_seed,
        "union_rows": len(union_rows),
        "union_by_task_type": by_task,
        "union_by_label": by_label,
    }


def _eval_summary_text(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("Local model-fit hold-out evaluation summary")
    lines.append("=" * 50)
    lines.append(f"model_version: 0.1.0-draft")
    lines.append(f"seed: {payload['seed']}")
    lines.append(f"train_ratio: {payload['split']['train_ratio']}")
    lines.append("")
    lines.append("Train runs:", )
    for f in payload["train_files"]:
        lines.append(f"  - {f}")
    lines.append("")
    lines.append("Eval (held-out) runs:", )
    for f in payload["eval_files"]:
        lines.append(f"  - {f}")
    lines.append("")
    lines.append(f"train rows: {payload['train_rows']}")
    lines.append(f"eval rows: {payload['eval_rows']}")
    lines.append("")
    tr = payload["train"]
    ev = payload["eval"]
    lines.append("TRAIN metrics:")
    lines.append(f"  n: {tr['n']}")
    lines.append(f"  top1_accuracy: {tr['top1_accuracy']:.3f}  ({tr['top1_correct']}/{tr['n']})")
    lines.append(f"  top2_accuracy: {tr['top2_accuracy']:.3f}  ({tr['top2_correct']}/{tr['n']})")
    lines.append("  per_class:")
    for name, m in tr["per_class"].items():
        lines.append(f"    {name:12s} support={m['support']:3d} P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f}")
    lines.append("")
    lines.append("EVAL (held-out) metrics:")
    lines.append(f"  n: {ev['n']}")
    lines.append(f"  top1_accuracy: {ev['top1_accuracy']:.3f}  ({ev['top1_correct']}/{ev['n']})")
    lines.append(f"  top2_accuracy: {ev['top2_accuracy']:.3f}  ({ev['top2_correct']}/{ev['n']})")
    lines.append("  per_class:")
    for name, m in ev["per_class"].items():
        lines.append(f"    {name:12s} support={m['support']:3d} P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f}")
    lines.append("")
    lines.append("Confusion matrix (rows=true, cols=pred):")
    lines.append(f"           {'  '.join(f'{c:>9s}' for c in ev['class_order'])}")
    for i, row in enumerate(ev["confusion"]):
        lines.append(f"  {ev['class_order'][i]:>9s}: {'  '.join(f'{v:>9d}' for v in row)}")
    lines.append("")
    lines.append("Note: statistics were computed from train rows only and applied")
    lines.append("to eval rows, so eval results do not leak through normalization.")
    return "\n".join(lines)
