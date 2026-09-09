"""Local ONNX scorer loader for the advisory hook.

Kept separate so the advisory path can import it without pulling in training code.
"""

from typing import Any, Dict

from .train import LocalScorer as _LocalScorer


def LocalScorer(model_path: str, metadata_path: str) -> _LocalScorer:
    return _LocalScorer(model_path, metadata_path)
