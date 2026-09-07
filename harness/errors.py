"""Shared exception types.

HarnessError lives here so config.py can raise it without a circular import.
Every layer raises it directly from this module.
"""


class HarnessError(Exception):
    """A harness-level failure with a user-facing message."""
    kind = "harness_error"


class ToolCancelled(Exception):
    """Raised inside a long-running tool when its request id is cancelled
    via notifications/cancelled (#13). Not an error of the work itself."""
