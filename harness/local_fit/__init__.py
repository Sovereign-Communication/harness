"""Local model-fit advisory layer (opt-in, off-by-default, advisory-only).

Importing :mod:`harness.local_fit` binds its public submodules. The layer is
inert unless the HARNESS_LOCAL_FIT_* flags are set; see README.md in this
directory for the full flag contract.

The live integration is :mod:`dispatch` (called from
``capability.order_pool``). ``train`` is intentionally NOT bound here: it
needs numpy/onnx at import time, and stdlib-only installs must import this
package cleanly -- import ``harness.local_fit.train`` directly where the
train-time extra is present.

A failed submodule import degrades to an inert package rather than raising:
the layer must never break ``import harness`` for callers that never opted in.
Individual imports are attempted separately so one broken submodule cannot
prevent the others from binding.
"""

__all__ = ["schema", "extract", "config", "model_loader", "infer", "features", "dispatch"]

for _mod in __all__:
    try:
        globals()[_mod] = __import__(f"{__name__}.{_mod}", fromlist=[_mod])
    except Exception:  # pragma: no cover - degraded import is informational only
        # The layer is optional; a broken optional dependency must not break
        # the host package import. config.py keeps its own guard too.
        pass
