"""harness — cost-bounded multi-model verification & coding, with AI sovereignty.

Pure Python stdlib, zero runtime dependencies. Exposes a library core, a
back-compatible CLI, and a native MCP server (stdio).
"""
import re
from pathlib import Path

# Only used when neither installed-dist metadata nor pyproject.toml is
# reachable (should not happen in practice).
_FALLBACK_VERSION = "0.3.3"


def _detect_version() -> str:
    # 1) Installed distribution metadata (wheel, sdist, or editable install).
    #    This is the authoritative value once the package is installed.
    try:
        from importlib.metadata import version

        detected = version("sovereign-harness")
        if detected:
            return detected
    except Exception:
        pass
    # 2) Source checkout: read the single source of truth in pyproject.toml.
    try:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        match = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    return _FALLBACK_VERSION


__version__ = _detect_version()
