"""Typed classification for terminal upstream provider account failures."""
from typing import Any, Optional

from .errors import HarnessError


class ProviderSpendLimitError(HarnessError):
    """The active provider key/account cannot fund another model request."""

    kind = "provider_spend_limit"

    def __init__(self, detail: str, *, http_status: Optional[int] = None,
                 known_cost: float = 0.0):
        self.http_status = http_status
        try:
            self.known_cost = max(0.0, float(known_cost or 0.0))
        except (TypeError, ValueError):
            self.known_cost = 0.0
        self.cost_accounted = False
        # The mutable state is shared with sibling executor workers, so a
        # queued request can stop before it reaches the upstream provider.
        from .events import mark_provider_spend_limit
        mark_provider_spend_limit(self)
        status = f" (HTTP {http_status})" if http_status is not None else ""
        message = (
            f"Upstream provider spend limit exhausted{status}. "
            "The active Harness operation was stopped; add credits or raise "
            "the API key/account spend limit before retrying."
        )
        super().__init__(message)


_SPEND_CODES = frozenset({
    "billing_hard_limit_reached",
    "billing_not_active",
    "budget_exceeded",
    "credit_balance_too_low",
    "insufficient_credits",
    "insufficient_quota",
    "payment_required",
    "spend_limit_exceeded",
})

_SPEND_HINTS = (
    "account balance is too low",
    "add more credits",
    "billing hard limit",
    "credit balance is too low",
    "exceeded your current quota, please check your plan and billing",
    "insufficient credit",
    "insufficient balance",
    "key spend limit",
    "monthly spend limit",
    "out of credits",
    "payment required",
    "spend limit",
    "spending limit",
)


def _error_fields(response: Any):
    error = response.get("error", response) if isinstance(response, dict) else response
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("detail") or error)
        code = error.get("code") or error.get("type")
    else:
        detail = str(error)
        code = None
    return detail.strip() or "provider reported no remaining credit", str(code or "").lower()


def _response_fingerprint(value: Any, depth: int = 0) -> str:
    """Flatten bounded provider metadata for classification only."""
    if depth > 4:
        return ""
    if isinstance(value, dict):
        return " ".join(_response_fingerprint(item, depth + 1)
                        for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_response_fingerprint(item, depth + 1)
                        for item in value)
    return str(value or "")


def provider_spend_limit_error(status: Any, response: Any, *, known_cost=0.0):
    """Return a typed terminal error for known billing exhaustion responses.

    Generic 429 quota/rate-limit responses deliberately do not match. They
    remain transient and may follow the existing bounded retry/rotation path.
    """
    try:
        http_status = int(status)
    except (TypeError, ValueError):
        http_status = None
    detail, code = _error_fields(response)
    if http_status == 402:
        return ProviderSpendLimitError(detail, http_status=http_status,
                                       known_cost=known_cost)
    # Error phrases can appear in ordinary model text. Only inspect an actual
    # error envelope on a failed HTTP response; a 200 assistant answer that
    # discusses billing is not evidence that the caller's key is exhausted.
    error = response.get("error") if isinstance(response, dict) else None
    if http_status is None or http_status < 400 or not error:
        return None
    lowered = _response_fingerprint(error).lower()
    if code in _SPEND_CODES or any(hint in lowered for hint in _SPEND_HINTS):
        return ProviderSpendLimitError(detail, http_status=http_status,
                                       known_cost=known_cost)
    return None


def raise_for_provider_spend_limit(status: Any, response: Any, *,
                                   known_cost=0.0) -> None:
    error = provider_spend_limit_error(status, response, known_cost=known_cost)
    if error is not None:
        raise error


def account_provider_spend_limit(error, governor, model) -> None:
    """Settle and persist known billed usage without replacing the error.

    Generative calls share this owner across CLI, MCP, GUI, and probe paths.
    Jev has a typed ``jev_eval`` receipt of its own and is excluded here so
    its charge is never represented twice.
    """
    cost = max(0.0, float(getattr(error, "known_cost", 0.0) or 0.0))
    if cost <= 0.0:
        return

    model_id = str(model or "unknown")
    if (not model_id.startswith("jev-")
            and not getattr(error, "ledger_recorded", False)):
        try:
            from .config import load_settings
            from .events import current_task_id
            from .ledger import AutonomyLedger

            settings = load_settings()
            ledger = AutonomyLedger(settings.ledger_path, caller="harness")
            task_id = current_task_id()
            fields = {
                "event_note": "provider_spend_limit",
                "model": model_id,
                "category": "provider",
                "status": "error",
                "error_kind": "provider_spend_limit",
                "http_status": getattr(error, "http_status", None),
                "cost": cost,
                "tracked_cost": cost,
                "billable_cost": cost,
                "reported_cost": cost,
                "reason": str(error),
            }
            if task_id:
                fields["task_id"] = task_id
            ledger.append("model_result", **fields)
            error.ledger_recorded = True
        except Exception as exc:
            # A ledger outage must not turn a known terminal provider error
            # into a retry or replace its actionable diagnosis.
            from .output import eprint
            eprint(f"[ledger] provider spend receipt was not written: {exc}")

    if governor is None or getattr(error, "cost_accounted", False):
        return
    try:
        governor.record_actual(cost, model_id)
    except HarnessError:
        book_overrun = getattr(governor, "record_overrun", None)
        if callable(book_overrun):
            book_overrun(cost, model_id)
    error.cost_accounted = True
