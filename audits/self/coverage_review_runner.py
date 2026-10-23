#!/usr/bin/env python3
"""Single-owner shim: runs the parent checkout's runner with ``__file__``
bound here, so HERE/ROOT stay in this tree (a plain import would retarget
ROOT at the owner's checkout and trace the wrong tree)."""

from pathlib import Path

_SHIM = Path(__file__).resolve()
_OWNER = _SHIM.parents[5] / "audits" / "self" / "refresh_coverage_baseline.py"

_namespace = {"__file__": str(_SHIM), "__name__": "__main__"}
exec(compile(_OWNER.read_bytes(), str(_OWNER), "exec"), _namespace)
