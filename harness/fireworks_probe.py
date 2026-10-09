"""EV-8: the single paid Fireworks verification call, and its receipt.

Runs through the existing ``harness economics`` probe face with
``--provider fireworks --confirm-single-paid-call``; there is no new command.
Exactly one paid call is ever sent: a one-shot marker is written before any
network call and the probe refuses while the marker exists, including after a
failed call.

Preconditions (all refused fail-closed): Fireworks enabled in settings, the
model is a confirmed Fireworks path, ``--max-cost`` is at most $0.01, and a
Fireworks key resolves. The request is ``Reply with the single word: ok``
with ``max_tokens = 8``, Fireworks forced, failover off (one POST, no retry).

Output discipline: only the HTTP status, whether the reply contains ``ok``,
token counts, and estimated cost are printed. One ledger row is written with
``route_reason = verify``. The receipt is committed under
``audits/self/dogfood/`` as the DoD's paid-cheap live receipt. On failure the
status and a sanitized body (key redacted, truncated) are reported, then stop.
"""
import json
import os

from .chat import chat_for_route
from .config import CONFIG_DIR
from .discount_gate import REPO_ROOT
from .errors import HarnessError
from .fireworks import resolve_offer, resolve_route

VERIFY_MESSAGE = "Reply with the single word: ok"
VERIFY_MAX_TOKENS = 8
VERIFY_MAX_COST_USD = 0.01
VERIFY_RECEIPT_REL = os.path.join(
    "audits", "self", "dogfood", "FIREWORKS_VERIFY_RECEIPT.json")


def _default_marker_path():
    return os.path.join(CONFIG_DIR, "fireworks_verify.marker")


def _default_receipt_path():
    return os.path.join(REPO_ROOT, VERIFY_RECEIPT_REL)


def _sanitize_body(body, secrets):
    """A failure body safe to print: secrets redacted, truncated."""
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    if len(text) > 500:
        text = text[:500] + "…[truncated]"
    return text


def run_fireworks_probe(transport, openrouter_key, fireworks_key, governor,
                        ledger, *, model, max_cost,
                        fireworks_enabled=False,
                        marker_path=None, receipt_path=None):
    """Send the one paid Fireworks verification call. Returns the report dict.

    ``max_cost`` is the operator's ``--max-cost`` for this call and must be at
    most $0.01. ``marker_path``/``receipt_path`` default to the machine-local
    marker and the committed receipt; tests override both with temp paths.
    """
    marker = marker_path or _default_marker_path()
    receipt = receipt_path or _default_receipt_path()
    if os.path.exists(marker):
        raise HarnessError(
            "the Fireworks verification call already ran (marker exists); "
            "this probe sends exactly one paid call, ever -- refusing.")
    if not fireworks_enabled:
        raise HarnessError(
            "Fireworks is disabled (HARNESS_FIREWORKS_ENABLED); refusing the "
            "paid verification call.")
    try:
        spend = float(max_cost)
    except (TypeError, ValueError):
        raise HarnessError(
            f"--max-cost must be a number at most ${VERIFY_MAX_COST_USD:.2f} "
            "for the verification call.") from None
    if not 0 < spend <= VERIFY_MAX_COST_USD:
        raise HarnessError(
            f"--max-cost ${spend} is outside (0, ${VERIFY_MAX_COST_USD:.2f}] "
            "for the single paid verification call; refusing.")
    # Confirmed path only: raises for unconfirmed or non-Fireworks models.
    offer = resolve_offer(model)
    route = resolve_route(model, openrouter_enabled=True,
                          fireworks_enabled=True)
    if not fireworks_key:
        raise HarnessError(
            "Fireworks key missing: set ~/.config/scmorc/fireworks.env, "
            "~/.config/harness/fireworks.env, or FIREWORKS_API_KEY")
    # The marker goes down before any network call, so even a failed call
    # cannot be retried into a second paid call.
    parent = os.path.dirname(os.path.abspath(marker))
    os.makedirs(parent, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(json.dumps({"model": model, "max_cost": spend}) + "\n")
    messages = [{"role": "user", "content": VERIFY_MESSAGE}]
    status, resp = chat_for_route(route, transport, openrouter_key,
                                  fireworks_key, messages, VERIFY_MAX_TOKENS,
                                  reasoning_effort="none", governor=governor)
    usage = resp.get("usage") if isinstance(resp, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    if status != 200:
        raise HarnessError(
            f"Fireworks verification failed with HTTP {status}: "
            f"{_sanitize_body(resp, (fireworks_key, openrouter_key))}")
    content = ""
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    contains_ok = isinstance(content, str) and "ok" in content.lower()
    cost = usage.get("cost", 0.0)
    report = {
        "status": status,
        "contains_ok": contains_ok,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "estimated_cost": cost,
        "cost_estimated": bool(usage.get("cost_estimated", False)),
        "model": model,
        "offer_model": offer.model,
    }
    if ledger is not None:
        ledger.append("fireworks_verify", model=model, provider="fireworks",
                      wire_model=model, route_reason="verify",
                      cost=cost, cost_estimated=report["cost_estimated"])
    rparent = os.path.dirname(os.path.abspath(receipt))
    os.makedirs(rparent, exist_ok=True)
    with open(receipt, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(report, indent=2) + "\n")
    return report
