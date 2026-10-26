"""Shared local-UI token storage for the desktop and serve entrypoints."""
import os
import secrets

from . import osal


def _atomic_private_write(path, contents):
    """Atomically write a user-owned file without following its destination."""
    if os.path.islink(path):
        raise OSError("refusing to replace a symlink")
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
        os.replace(tmp, path)
        tmp = None
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows permissions are inherited from the per-user config
            # directory; POSIX permissions are enforced by the file mode.
            if osal.IS_POSIX:
                raise
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
    try:
        _atomic_private_write(path, token)
    except OSError:
        return


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
        if read_ui_token(token_path) != token:
            raise OSError(
                f"could not persist the Harness UI owner credential at {token_path}")
    return token


def write_ui_handoff(config_dir=None, url=None, token=None):
    """Write a private browser shortcut and a non-secret recovery note."""
    if config_dir is None:
        from .config import CONFIG_DIR
        config_dir = CONFIG_DIR
    shortcut_path = os.path.join(config_dir, "gui-open.url")
    handoff_path = os.path.join(config_dir, "gui-auth-handoff.txt")
    _atomic_private_write(
        handoff_path,
        "Harness browser recovery\n"
        "=========================\n\n"
        f"Open this shortcut to reconnect your browser:\n{shortcut_path}\n\n"
        "It contains private local access. Keep it in your user account and "
        "do not share it.\n"
        "If Harness is stopped, run: harness serve --open. It reuses this "
        "owner identity and refreshes the shortcut.\n"
        "To connect another browser, use the signed-in Harness window to "
        "create a one-time pairing code.\n",
    )
    _atomic_private_write(
        shortcut_path,
        "[InternetShortcut]\nURL=" + url + "#" + token + "\n",
    )
    return shortcut_path
