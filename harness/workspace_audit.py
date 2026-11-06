"""Read-only workspace audit lane: scope, preflight, handoff (issue #448).

A read-only workspace audit must never be answered by the ledger-chain lane,
and it must never dispatch a paid request on a guess. This module owns the
three questions that can be answered without spending:

* what the run would cover (enumerated paths, exclusions, truncation),
* what the complete run would cost (input estimate plus a stated buffer),
* whether the configured cap allows it -- and, if not, the exact raise that
  would. Nothing here raises a cap and nothing here sends a provider request.

The paid engine is the shipped resumable source audit (#423:
``harness source-audit`` and the MCP ``source_audit`` tool), which needs
operator-supplied packs. That handoff is reported explicitly, so the lane
never implies an audit happened when it only preflighted one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from .config import DEFAULT_MAX_COST
from .jev import jev_cost
from .repo_scope import audit_scope

CHARS_PER_TOKEN = 4.0
SAFE_BUFFER = 0.25
CHUNK_OVERHEAD_TOKENS = 64
SOURCE_AUDIT_COMMAND = "harness source-audit --code-pack <code.json> " \
                       "--docs-pack <docs.json> --evidence-pack <evidence.json>"


def estimate_audit_run(scope: Mapping[str, Any]) -> Dict[str, Any]:
    """Conservative cost estimate for a complete audit over ``scope``.

    Included bytes plus per-chunk prompt overhead, then a stated 25% buffer for
    packs, retries and output. The basis is recorded so a reader can see this
    is an estimate with a named safety margin, not a tokenizer manifest.
    """
    chars = int(scope.get("included_chars") or 0)
    chunks = int(scope.get("included_chunks_estimate") or 0)
    tokens = int(chars / CHARS_PER_TOKEN) + chunks * CHUNK_OVERHEAD_TOKENS
    worst = int(round(tokens * (1.0 + SAFE_BUFFER)))
    return {
        "chars_per_token": CHARS_PER_TOKEN,
        "safe_buffer": SAFE_BUFFER,
        "estimated_input_tokens": tokens,
        "input_tokens_with_buffer": worst,
        "worst_case_cost_usd": round(jev_cost(worst), 6),
        "basis": ("included source bytes only; pack, retry and output cost "
                  "is covered by the stated buffer"),
    }


def _summary(scope: Mapping[str, Any], estimate: Mapping[str, Any],
             status: str) -> str:
    coverage = (f"{scope['included_count']} files included, "
                f"{scope['excluded_count']} excluded")
    if scope.get("truncated"):
        coverage += " (listing capped - NOT a complete workspace audit)"
    return (f"Read-only workspace audit preflight: {coverage}, "
            f"~{estimate['input_tokens_with_buffer']} input tokens with buffer, "
            f"worst case ${estimate['worst_case_cost_usd']:.6f} ({status}). "
            "No paid request was sent and no cap was changed.")


def audit_workspace(root: Any, settings: Any) -> Dict[str, Any]:
    """Scope and price a workspace audit; never dispatch, never raise a cap."""
    root_path = Path(root).resolve()
    scope = audit_scope(root_path)
    estimate = estimate_audit_run(scope)
    cap = float(getattr(settings, "max_cost", DEFAULT_MAX_COST) or 0.0)
    expected = float(estimate["worst_case_cost_usd"])
    preflight: Dict[str, Any] = {
        "dispatch_attempts": 0,
        "effective_max_cost_usd": cap,
        "worst_case_cost_usd": expected,
        "approval_required_usd": None,
    }
    if getattr(settings, "jev_disabled", False):
        status = "blocked"
        next_step = ("Jev is disabled for this lane; configure the Jev key or "
                     "run " + SOURCE_AUDIT_COMMAND)
    elif cap <= 0.0 or expected > cap:
        status = "approval_required"
        raise_by = round(expected - cap, 6) if cap > 0.0 else expected
        preflight["approval_required_usd"] = raise_by
        next_step = (f"Approve raising the local run cap by ${raise_by:.6f} "
                     f"(estimate ${expected:.6f} - cap ${cap:.6f}) plus the "
                     f"{int(SAFE_BUFFER * 100)}% buffer already included, then "
                     "run " + SOURCE_AUDIT_COMMAND)
    else:
        status = "ready"
        next_step = ("Run the full-source audit engine (issue #423) with the "
                     "operator's packs: " + SOURCE_AUDIT_COMMAND)
    return {
        "status": status,
        "scope": scope,
        "estimate": estimate,
        "preflight": preflight,
        "next_step": next_step,
        "summary": _summary(scope, estimate, status),
    }
