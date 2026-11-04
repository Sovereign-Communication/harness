"""Structured, file-only logging for MCP tool calls (issue #286).

The stdio transport owns stdout and stderr, so a tool-call log may never be
written there: one stray line corrupts the JSON-RPC stream. This module writes
one JSON object per call to an operator-selected file, and refuses to write at
all when no path is configured.

Arguments are scrubbed before serialization. The credential families below are
the same ones the repository's export scan refuses; a planted secret must not
reach disk even though the surrounding request is logged for diagnosis.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-or-v1-[0-9a-zA-Z]{16,}"),
    re.compile(r"apikey_[0-9a-zA-Z]{8,}"),
    re.compile(r"ghp_[0-9a-zA-Z]{20,}"),
    re.compile(r"github_pat_[0-9a-zA-Z_]{20,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

_SECRET_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "secret", "token",
})

REDACTED = "[REDACTED]"


def _redact(text):
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def scrub(value):
    """Return a JSON-safe copy with credential-shaped values redacted."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in _SECRET_KEYS and item not in (None, ""):
                out[key] = REDACTED
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def log_tool_call(path, *, tool, args, ok, duration_ms, error=None):
    """Append one structured call record; never write to stdout/stderr.

    Logging is diagnostic: an unwritable path must not turn a completed tool
    call into a failed one, so OSError is swallowed here. The caller keeps the
    real response either way.
    """
    if not path:
        return
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": tool if isinstance(tool, str) else None,
        "args": scrub(args if isinstance(args, dict) else {}),
        "ok": bool(ok),
        "duration_ms": int(duration_ms),
    }
    if error:
        record["error"] = scrub(str(error))
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    try:
        target = Path(path)
        if target.parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return
