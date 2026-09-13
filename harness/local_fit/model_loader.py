"""Scorer loader for the advisory hook.

The runtime default is the pure-stdlib scorer (:class:`infer.StdlibScorer`),
which reads ``model_weights.json`` + ``model_meta.json`` and needs no
third-party packages — preserving the Harness zero-runtime-dependency
promise even when the advisory layer is enabled.

If a model directory only carries the legacy ONNX artifact, the onnxruntime
scorer (``train.LocalScorer``) is attempted as a fallback; that import only
happens on the fallback path, so stdlib-only installs never touch numpy or
onnxruntime.
"""

import os
from typing import Any, Optional

from .infer import StdlibScorer

WEIGHTS_FILE = "model_weights.json"
META_FILE = "model_meta.json"
ONNX_FILE = "model.onnx"


def LocalScorer(model_dir: str) -> Optional[Any]:
    """Load a scorer for a model directory.

    Returns an object with ``.score(features_dict)`` or None when the
    directory holds nothing loadable. Callers treat None as "inert".
    """
    weights_path = os.path.join(model_dir, WEIGHTS_FILE)
    meta_path = os.path.join(model_dir, META_FILE)
    if os.path.isfile(weights_path):
        return StdlibScorer(weights_path, meta_path)

    # Legacy ONNX artifact fallback (train-time deps only; stdlib installs
    # without onnxruntime land here and stay inert).
    onnx_path = os.path.join(model_dir, ONNX_FILE)
    if os.path.isfile(onnx_path) and os.path.isfile(meta_path):
        try:
            from .train import LocalScorer as _OnnxScorer

            return _OnnxScorer(onnx_path, meta_path)
        except Exception:
            return None
    return None
