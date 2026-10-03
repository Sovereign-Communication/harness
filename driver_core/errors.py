"""Typed failures for driver-core.

Every degraded path in this package must be *nameable*: a caller that sees an
exception knows exactly which contract was broken, and a caller that sees a
successful return knows the success was real. The project-wide rule is that
partial work is never promoted to a verdict, so the error types here exist to
separate "the model said something odd" from "the system could not ask".
"""


class DriverError(Exception):
    """Base class for every driver-core failure."""


class SchemaError(DriverError):
    """A declared schema was malformed, or a state did not satisfy it."""


class VocabularyError(DriverError):
    """An action name is not in the declared vocabulary.

    Raised rather than falling back to a default: an action the code did not
    declare is an action the code cannot consent to, log, or bound. This is
    the single most important refusal in the package.
    """


class AgreementError(DriverError):
    """The extractors did not reach a usable verdict.

    Carries the reason (``insufficient`` or ``disagreed``) so a caller can
    distinguish "nobody answered" from "they answered differently" -- two
    failures with completely different remedies.
    """

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class DecisionError(DriverError):
    """The decision tier could not produce a usable recommendation."""


class ConsentError(DriverError):
    """An action was attempted without valid, current, sufficient consent."""


class BudgetRefused(DriverError):
    """A pre-flight ceiling refused the call. No spend occurred."""

    def __init__(self, estimated, ceiling, label=""):
        detail = f"{label} " if label else ""
        super().__init__(
            f"{detail}refused before dispatch: estimated ${estimated:.6f} "
            f"exceeds ceiling ${ceiling:.6f}"
        )
        self.estimated = estimated
        self.ceiling = ceiling


class PerceptionUnavailable(DriverError):
    """The perception tier could not be reached or produced nothing usable.

    Mirrors the honest-failure discipline of the sibling media adapter: a
    service that is down, unsigned-in, or refusing produces a named reason
    and zero spend, never a fabricated empty state.
    """


class ExecutorError(DriverError):
    """A declared executor failed while performing an action."""


class OsalError(DriverError):
    """The operating-system layer could not or would not carry out a request.

    Named separately from :class:`ExecutorError` because the two call for
    different responses. An executor failure is "the action did not happen";
    an osal failure is usually "the request was malformed, or this platform
    cannot do it, or the host withheld permission" -- conditions the caller
    can correct, as opposed to retrying.
    """
