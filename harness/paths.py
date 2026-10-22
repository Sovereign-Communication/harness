"""Shared per-user paths for Harness-managed state."""
import os


def get_config_dir():
    """Return the configured state directory, or ``~/.config/harness``."""
    configured = os.environ.get("HARNESS_CONFIG_DIR", "").strip()
    if not configured:
        configured = "~/.config/harness"
    return os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
