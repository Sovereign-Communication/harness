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

    def __init__(self, message="Prompt execution was cancelled by user",
                 *, known_cost=0.0, usage_unknown=False):
        super().__init__(message)
        self.known_cost = max(0.0, float(known_cost or 0.0))
        self.cost_accounted = False
        # A local worker may be terminated after a provider received a request
        # but before its usage response arrived. Keep that distinct from a
        # pre-dispatch stop and from a cancellation during retry backoff.
        self.usage_unknown = bool(usage_unknown)

    def add_known_cost(self, amount):
        """Preserve spend already reported by responses before cancellation."""
        try:
            value = max(0.0, float(amount or 0.0))
        except (TypeError, ValueError):
            value = 0.0
        self.known_cost += value


class ProviderUsageUnknown(HarnessError):
    """A provider request may have been billed, but its usage response was lost."""
    kind = "provider_usage_unknown"

    def __init__(self, message, *, known_cost=0.0, reserved_cost=0.0):
        super().__init__(message)
        self.known_cost = max(0.0, float(known_cost or 0.0))
        self.reserved_cost = max(0.0, float(reserved_cost or 0.0))
        self.cost_accounted = False
        self.usage_unknown = True

    def add_known_cost(self, amount):
        try:
            value = max(0.0, float(amount or 0.0))
        except (TypeError, ValueError):
            value = 0.0
        self.known_cost += value
