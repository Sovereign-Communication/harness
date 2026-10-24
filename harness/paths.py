"""Shared filesystem anchors for Harness-managed state and its checkout."""
import os

#: The directory containing this package's checkout. An installed wheel
#: resolves to site-packages; repo-file gates must refuse there rather than
#: report a green run against files that are not present.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config_dir():
    """Return the configured state directory, or ``~/.config/harness``."""
    configured = os.environ.get("HARNESS_CONFIG_DIR", "").strip()
    if not configured:
        configured = "~/.config/harness"
    return os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
