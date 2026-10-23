"""Persisted conversation history and session listing (single owner).

Moved verbatim from harness/agent.py (get_default_history_dir,
save_chat_turn, load_chat_history) and harness/server.py
(_list_chat_sessions -> list_chat_sessions,
_delete_chat_session -> delete_chat_session).
The bodies are copied verbatim; only the module location and the
public names list_chat_sessions / delete_chat_session changed.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import HarnessError


def get_default_history_dir() -> Path:
    # Resolve directory for persisted conversation histories
    base = Path(os.environ.get("HARNESS_CONFIG_DIR", Path.home() / ".config" / "harness"))
    history_dir = base / "chat_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def save_chat_turn(session_id: str, turn: Dict[str, Any], history_dir: Optional[Path] = None) -> Path:
    # Append a chat turn to the persisted JSONL history file
    hdir = history_dir or get_default_history_dir()
    file_path = hdir / f"{session_id}.jsonl"
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn, ensure_ascii=False) + "\n")
    return file_path


def load_chat_history(session_id: str, history_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    # Load past turns for a session
    hdir = history_dir or get_default_history_dir()
    file_path = hdir / f"{session_id}.jsonl"
    if not file_path.exists():
        return []
    history: List[Dict[str, Any]] = []
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return history


def list_chat_sessions():
    """Return all persisted sessions sorted newest-first with a preview title.

    Each entry: {"id": str, "preview": str, "updated_at": float}
    The preview is the first prompt from the session file (truncated to 60 chars).
    """
    hdir = get_default_history_dir()
    sessions = []
    for p in hdir.glob("sess_*.jsonl"):
        try:
            mtime = p.stat().st_mtime
            preview = ""
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        turn = json.loads(line)
                        preview = (turn.get("prompt") or "")[:60]
                        break
                    except json.JSONDecodeError:
                        continue
            sessions.append({
                "id": p.stem,
                "preview": preview or "(empty)",
                "updated_at": mtime,
            })
        except OSError:
            continue
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def delete_chat_session(session_id: str) -> bool:
    """Delete a session JSONL file. Returns True if deleted, False if not found.

    Only deletes files matching the sess_* pattern. The id must not contain
    path separators or '..' (defense-in-depth: on some platforms Path
    collapses traversal segments, but the boundary refuses them outright
    instead of relying on resolution semantics).
    """
    if not session_id or not session_id.startswith("sess_"):
        raise HarnessError("invalid session_id: must start with 'sess_'")
    if ("/" in session_id or "\\" in session_id or ".." in session_id
            or os.sep in session_id):
        raise HarnessError("invalid session_id: path separators and '..' are not allowed")
    hdir = get_default_history_dir()
    target = hdir / f"{session_id}.jsonl"
    if not target.exists():
        return False
    target.unlink()
    return True
