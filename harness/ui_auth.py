"""Shared local-UI token storage for the desktop and serve entrypoints."""
import os
import secrets


def read_ui_token(path):
    """Read a persisted UI token without following a symlink."""
    if os.path.islink(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return ""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def write_ui_token(path, token):
    """Atomically persist a token, replacing rather than following a file."""
    tmp = None
    try:
        if os.path.islink(path):
            return
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".desktop_token.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load_or_create_ui_token(config_dir=None):
    """Return the configured user's persistent UI token or create one."""
    if config_dir is None:
        from .config import CONFIG_DIR
        config_dir = CONFIG_DIR
    token_path = os.path.join(config_dir, "desktop_token")
    token = read_ui_token(token_path)
    if not token:
        token = secrets.token_urlsafe(24)
        write_ui_token(token_path, token)
    return token
