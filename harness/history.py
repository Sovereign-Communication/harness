"""Persisted conversation history and session listing (single owner).

Moved verbatim from harness/agent.py (get_default_history_dir,
save_chat_turn, load_chat_history) and harness/server.py
(_list_chat_sessions -> list_chat_sessions,
_delete_chat_session -> delete_chat_session).
The bodies are copied verbatim; only the module location and the
public names list_chat_sessions / delete_chat_session changed.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from .errors import HarnessError
from .lifecycle import LifecycleState
from .paths import get_config_dir


CHAT_CHECKPOINT_SCHEMA = "harness-chat-checkpoint-v1"
MAX_CHAT_CHECKPOINT_BYTES = 96_000
_CHAT_SESSION_ID_MAX_CHARS = 512
_CHAT_SAFE_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")


class ChatResumePayload(TypedDict, total=False):
    """Route-specific chat details nested inside the shared typed contract."""

    schema: str
    session_id: str
    updated_at: float
    route: str
    status: str
    stage: str
    prompt: str
    intent: str
    web_requested: bool
    workspace_root: str
    workspace_context_used: bool
    known_cost: float
    unknown_liability: float
    usage_unknown: bool
    next_round: int
    rounds: List[Dict[str, Any]]
    route_preflight: Dict[str, Any]
    context: str
    context_files: List[str]
    context_tokens: int
    context_scope: str
    context_updated_at: float
    history_turns: List[Dict[str, Any]]
    answer: Optional[str]
    answer_model: Optional[str]
    answer_cost: float
    generation_calls: int
    pre: Dict[str, Any]
    prior: Any
    last_jev: Dict[str, Any]
    truncated: bool


# The on-disk checkpoint is the discriminated union. This alias remains for
# consumers that need to annotate the persisted contract itself.
ChatCheckpoint = LifecycleState


def _chat_history_path(session_id: str,
                       history_dir: Optional[Path] = None) -> Path:
    """Map a public session id to a bounded, contained history filename.

    Keep established UI/MCP ids readable on disk for compatibility. Opaque
    ids accepted by the MCP contract use a stable digest instead of becoming
    path components.
    """
    if (not isinstance(session_id, str) or not session_id.strip()
            or len(session_id) > _CHAT_SESSION_ID_MAX_CHARS):
        raise HarnessError("invalid session_id for chat history")
    root = Path(history_dir) if history_dir is not None else get_default_history_dir()
    if (_CHAT_SAFE_SESSION_ID_RE.fullmatch(session_id)
            and session_id not in {".", ".."} and ".." not in session_id):
        name = f"{session_id}.jsonl"
    else:
        try:
            key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        except UnicodeEncodeError as exc:
            raise HarnessError("invalid session_id for chat history") from exc
        name = f"opaque_{key}.jsonl"
    return root / name


def get_default_history_dir() -> Path:
    # Resolve directory for persisted conversation histories
    base = Path(get_config_dir())
    history_dir = base / "chat_history"
    if history_dir.exists() and not history_dir.is_dir():
        # Older installs may have used this path as a single history file.
        # Preserve it and store per-session JSONL files in a sibling directory.
        history_dir = base / "chat_history_sessions"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def save_chat_turn(session_id: str, turn: Dict[str, Any], history_dir: Optional[Path] = None) -> Path:
    # Append a chat turn to the persisted JSONL history file
    hdir = history_dir or get_default_history_dir()
    file_path = _chat_history_path(session_id, hdir)
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn, ensure_ascii=False) + "\n")
    return file_path


def load_chat_history(session_id: str, history_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    # Load past turns for a session
    hdir = history_dir or get_default_history_dir()
    file_path = _chat_history_path(session_id, hdir)
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


def _chat_checkpoint_path(session_id: str,
                          history_dir: Optional[Path] = None, *,
                          create: bool = False) -> Path:
    if (not isinstance(session_id, str) or not session_id.strip()
            or len(session_id) > _CHAT_SESSION_ID_MAX_CHARS):
        raise HarnessError("invalid session_id for chat continuation")
    try:
        session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise HarnessError("invalid session_id for chat continuation") from exc
    root = Path(history_dir) if history_dir is not None else get_default_history_dir()
    checkpoint_dir = root / "chat_checkpoints"
    if create:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
    if checkpoint_dir.is_symlink():
        raise HarnessError("chat continuation directory is invalid")
    return checkpoint_dir / f"{session_key}.json"


def save_chat_checkpoint(session_id: str, checkpoint: ChatResumePayload,
                         history_dir: Optional[Path] = None) -> Path:
    """Store chat route state within the one shared lifecycle checkpoint."""
    if not isinstance(checkpoint, dict):
        raise HarnessError("chat checkpoint must be an object")
    from .lifecycle import LifecycleCoordinator
    coordinator = LifecycleCoordinator(history_dir=history_dir)
    coordinator.adopt_chat_payload(session_id, dict(checkpoint))
    return _chat_checkpoint_path(session_id, history_dir)


def load_chat_checkpoint(session_id: str,
                         history_dir: Optional[Path] = None) -> Optional[ChatResumePayload]:
    """Load the legacy chat view of the shared checkpoint, if it is active."""
    from .lifecycle import LifecycleCoordinator
    coordinator = LifecycleCoordinator(history_dir=history_dir)
    path = _chat_checkpoint_path(session_id, history_dir)
    if path.is_symlink():
        raise HarnessError("request lifecycle state is invalid")
    if path.exists():
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_CHAT_CHECKPOINT_BYTES + 1)
            if len(raw) > MAX_CHAT_CHECKPOINT_BYTES:
                raise HarnessError("request lifecycle exceeds its storage limit")
            legacy = json.loads(raw.decode("utf-8"))
        except HarnessError:
            raise
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise HarnessError("request lifecycle state is unreadable") from exc
        if isinstance(legacy, dict) and legacy.get("schema") == CHAT_CHECKPOINT_SCHEMA:
            if legacy.get("session_id") != session_id:
                raise HarnessError("request lifecycle state is invalid")
            if legacy.get("status") == "complete":
                return None
            coordinator.adopt_chat_payload(session_id, legacy)
    state = coordinator.load(session_id)
    if state is None or state["lane"] != "chat" or state["status"] == "complete":
        return None
    payload = dict(state["data"])
    if not isinstance(payload.get("prompt"), str) or not payload["prompt"].strip():
        raise HarnessError("chat continuation state has no original request")
    return payload


def clear_chat_checkpoint(session_id: str,
                          history_dir: Optional[Path] = None) -> bool:
    """Clear the one lifecycle checkpoint without touching chat history."""
    from .lifecycle import LifecycleCoordinator
    return LifecycleCoordinator(history_dir=history_dir).clear(session_id)


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
    target = _chat_history_path(session_id, hdir)
    deleted = target.exists()
    if deleted:
        target.unlink()
    clear_chat_checkpoint(session_id, hdir)
    return deleted
