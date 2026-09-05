"""Shared exception types.

HarnessError lives here (not core.py) so config.py can raise it without a
circular import; core.py re-exports it, so `from harness.core import
HarnessError` keeps working.
"""


class HarnessError(Exception):
    """A harness-level failure with a user-facing message."""
