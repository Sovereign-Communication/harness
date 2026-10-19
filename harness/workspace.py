"""Explicit filesystem scope for authenticated Harness surfaces.

The interactive server must never infer a project root from its own process
directory. A workspace is an operator-selected directory; all file paths are
canonicalized against it before they reach a reader or writer.
"""
import os
from pathlib import Path

from .errors import HarnessError
from . import osal


_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SENSITIVE_DIRS = {
    ".ssh", ".aws", ".azure", ".gcloud", ".gnupg", ".kube",
    "credentials", "secrets", ".git", ".hg", ".svn",
}
_SENSITIVE_NAMES = {
    ".netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "terraform.tfstate",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".p8", ".keystore", ".kdbx"}


def is_sensitive_path(path) -> bool:
    """Return whether a path is an obvious credential or private-key file."""
    candidate = Path(path)
    parts = [part.casefold() for part in candidate.parts]
    name = candidate.name.casefold()
    if any(part in _SENSITIVE_DIRS for part in parts):
        return True
    if name in _SENSITIVE_NAMES:
        return True
    if name.startswith(".env") and name not in _SAFE_ENV_TEMPLATES:
        return True
    if candidate.suffix.casefold() in _SENSITIVE_SUFFIXES:
        return True
    stem = candidate.stem.casefold()
    return any(token in stem for token in ("secret", "credential", "password", "private_key", "private-key"))


def require_workspace(raw) -> Path:
    """Resolve an explicit existing absolute workspace directory."""
    if raw is None or not str(raw).strip():
        raise HarnessError("Select a workspace folder before using files")
    path = Path(os.fspath(raw)).expanduser()
    if not path.is_absolute():
        raise HarnessError("Workspace folder must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HarnessError(f"Workspace folder is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise HarnessError(f"Workspace folder is not a directory: {resolved}")
    return resolved


def resolve_workspace_path(root, raw, *, must_exist=False,
                           reject_sensitive=True) -> Path:
    """Resolve a file path inside ``root``, refusing traversal and symlink escapes."""
    workspace = require_workspace(root)
    if raw is None or not str(raw).strip():
        raise HarnessError("File path is required")
    candidate = Path(os.fspath(raw)).expanduser()
    if not candidate.is_absolute():
        if ".." in candidate.parts:
            raise HarnessError("File path may not traverse outside the workspace")
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise HarnessError(f"File path is unavailable: {raw}") from exc
    if not osal.is_within(str(resolved), str(workspace)):
        raise HarnessError("File path is outside the selected workspace")
    if reject_sensitive and is_sensitive_path(resolved):
        raise HarnessError("Credential and private-key paths are excluded from workspace file access")
    if must_exist and not resolved.exists():
        raise HarnessError(f"File path does not exist: {raw}")
    return resolved


def choose_allowed_workspace(raw, allowed_roots):
    """Choose one configured MCP root (or a child), never the server CWD."""
    roots = [require_workspace(root) for root in (allowed_roots or [])]
    if raw is None or not str(raw).strip():
        if len(roots) == 1:
            return roots[0]
        if not roots:
            raise HarnessError("This MCP file operation requires a configured allowed root")
        raise HarnessError("Specify root when more than one MCP allowed root is configured")
    supplied = Path(os.fspath(raw)).expanduser()
    if not supplied.is_absolute():
        if len(roots) != 1:
            raise HarnessError("Specify an absolute root when multiple MCP roots are configured")
        supplied = roots[0] / supplied
    try:
        workspace = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HarnessError(f"Workspace root is unavailable: {raw}") from exc
    if not workspace.is_dir() or not any(
            osal.is_within(str(workspace), str(root)) for root in roots):
        raise HarnessError("Workspace root is outside configured MCP allowed roots")
    return workspace


def resolve_allowed_path(raw, allowed_roots, *, workspace=None, must_exist=False,
                         reject_sensitive=True) -> Path:
    """Resolve a file path under one configured MCP root without CWD lookup."""
    roots = [require_workspace(root) for root in (allowed_roots or [])]
    if not roots:
        raise HarnessError("This MCP file operation requires a configured allowed root")
    if workspace is not None:
        selected = choose_allowed_workspace(workspace, roots)
        return resolve_workspace_path(selected, raw, must_exist=must_exist,
                                      reject_sensitive=reject_sensitive)

    supplied = Path(os.fspath(raw)).expanduser()
    matches = []
    for root in roots:
        try:
            resolved = resolve_workspace_path(root, supplied, must_exist=must_exist,
                                              reject_sensitive=reject_sensitive)
        except HarnessError:
            continue
        if resolved not in matches:
            matches.append(resolved)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HarnessError("Relative file path matches more than one allowed root; specify root")
    raise HarnessError("File path is outside every configured allowed root or is unavailable")
