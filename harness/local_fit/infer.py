"""Pure-stdlib inference for the local-fit advisory layer.

The Harness package promises zero runtime dependencies. Training (train.py)
may use numpy/onnx/onnxruntime, but the enabled advisory path must not import
any of them. This module implements the same forward pass as the exported
ONNX graph using only the stdlib:

    logits = relu(X @ W1 + b1) @ W2 + b2
    probs  = softmax(logits)

Weights come from ``model_weights.json`` (exported alongside model.onnx by
train.py). The artifact layout matches ONNX exactly: W1 is (in_dim, hidden),
W2 is (hidden, 3), row-major.
"""

import json
import math
import os
from typing import Any, Dict, List

from .features import build_feature_vector, expected_vector_length

WEIGHTS_FILE = "model_weights.json"
META_FILE = "model_meta.json"


class StdlibScorer:
    """Score seat feature dicts with the exported weights, stdlib only."""

    def __init__(self, weights_path: str, metadata_path: str):
        with open(weights_path, "r", encoding="utf-8") as f:
            self.weights = json.load(f)
        with open(metadata_path, "r", encoding="utf-8") as f:
            self.metadata = json.load(f)

        w1 = self.weights["w1"]
        b1 = self.weights["b1"]
        w2 = self.weights["w2"]
        b2 = self.weights["b2"]
        if not b1:
            raise ValueError("b1 must be non-empty")

        self.in_dim = len(w1)
        self.hidden = len(b1)
        n_out = len(b2)
        if n_out != 3:
            raise ValueError(f"expected 3 output classes, got {n_out}")
        if len(w2) != self.hidden or any(len(row) != n_out for row in w2):
            raise ValueError("w2 shape does not match b1/b2 dims")
        declared = self.metadata.get("input_dim")
        if declared is not None and int(declared) != self.in_dim:
            raise ValueError(
                f"weights in_dim {self.in_dim} != metadata input_dim {declared}"
            )
        expected = expected_vector_length()
        if self.in_dim != expected:
            raise ValueError(
                f"weights in_dim {self.in_dim} != canonical vector length {expected}"
            )
        self.w1 = w1
        self.b1 = b1
        self.w2 = w2
        self.b2 = b2
        self.output_classes = self.metadata.get(
            "output_classes", ["unusable", "truncated", "usable_stop"]
        )

    def score(self, row_features: Dict[str, Any]) -> Dict[str, Any]:
        stats = self.metadata.get("stats", {})
        vec = build_feature_vector(row_features, stats)
        if len(vec) != self.in_dim:
            raise ValueError(
                f"feature vector length {len(vec)} != model in_dim {self.in_dim}"
            )
        logits = self._forward(vec)
        probs = _softmax(logits)
        best = max(range(len(probs)), key=lambda i: probs[i])
        return {
            "unusable": probs[0],
            "truncated": probs[1],
            "usable_stop": probs[2],
            "best_guess": self.output_classes[best],
        }

    def _forward(self, vec: List[float]) -> List[float]:
        # h = relu(vec @ W1 + b1)  — W1 rows are hidden-unit columns.
        hidden_out: List[float] = []
        w1 = self.w1  # local alias for speed
        for j in range(self.hidden):
            acc = self.b1[j]
            for i in range(self.in_dim):
                acc += vec[i] * w1[i][j]
            if acc > 0.0:
                hidden_out.append(acc)
            else:
                hidden_out.append(0.0)
        # logits = hidden_out @ W2 + b2
        logits = list(self.b2)
        for j, h in enumerate(hidden_out):
            if h == 0.0:
                continue
            row2 = self.w2[j]
            for k in range(3):
                logits[k] += h * row2[k]
        return logits


def _softmax(logits: List[float]) -> List[float]:
    m = max(logits)
    exps = [math.exp(v - m) for v in logits]
    total = sum(exps)
    if total <= 0:
        raise ValueError("softmax denominator underflow")
    return [e / total for e in exps]


def load_scorer(model_dir: str) -> StdlibScorer:
    """Load a StdlibScorer from a model directory.

    Prefers model_weights.json; falls back to converting an ONNX artifact is
    NOT supported stdlib-only, so a missing weights file raises FileNotFoundError.
    """
    weights_path = os.path.join(model_dir, WEIGHTS_FILE)
    meta_path = os.path.join(model_dir, META_FILE)
    return StdlibScorer(weights_path, meta_path)
