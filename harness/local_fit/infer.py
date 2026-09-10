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

# Input z-scores beyond this magnitude are clipped before the forward pass.
# Dispatch-time features can legitimately carry values the training corpus
# never saw (e.g. a declared context length when training rows lacked one);
# unclipped, a single such feature produces logits in the thousands, the
# softmax pins to 0/0/1, and every candidate becomes indistinguishable — the
# saturation the dispatch guard exists to catch. Clipping is applied
# identically to every candidate so ordering within a pool stays honest.
Z_CLIP = 8.0


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
        # Export-time calibration temperature: the runtime softmax divides
        # logits by it so dispatch-time probabilities keep the separation the
        # net trained to (see train.export_temperature). Unset/invalid -> 1.0
        # (artifact written before the calibration field existed must keep
        # loading; nothing is silently reshaped).
        self.temperature = 1.0
        try:
            t = float(self.weights.get("temperature",
                                       self.metadata.get("temperature", 1.0)))
            if math.isfinite(t) and t > 0.0:
                self.temperature = min(20.0, max(0.1, t))
        except (TypeError, ValueError):
            pass
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
        vec = [max(-Z_CLIP, min(Z_CLIP, v)) for v in vec]
        logits = self._forward(vec)
        if self.temperature != 1.0:
            logits = [v / self.temperature for v in logits]
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
