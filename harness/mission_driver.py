"""HUL-D until-limits mission driver.

Iterates attempts against a HUL-A mission pack until one of:

* **honest success** — HUL-C scope determination ``complete=True``
  (verifier + success_definition_met + scope hold). Unkeyed fallback
  may NEVER alone mark complete.
* **limits** — working budget exhausted, max attempts, token ceiling,
  or error ceiling.
* **stall** — ``DEFAULT_STALL_LIMIT`` consecutive attempts with no new
  artifact/evidence (default 5).

After every attempt the pack resume state is rewritten (interrupt-safe).
Terminal outcomes write ``FINDINGS.md`` via ``mission_record.mark_terminal``.

Dual-budget enforcement (attempts never eat ``terminal_reserve``) is HUL-B.
Until that lands, this driver uses the honest ``working_remaining`` formula
already stored by ``mission_record``::

    working_remaining = max_cost_usd - spent - terminal_reserve.cost_usd

``attempt_fn`` is injected by the caller (library / tests / CLI). The
function receives one context dict and returns a mapping. No provider
brand strings live in this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set

from .errors import HarnessError
from . import mission_record as mr

DEFAULT_STALL_LIMIT = 5
# HUL-B dual budget not on this tree by default — working_remaining formula
# from mission budget is the reserve-aware ceiling used for cost limits.
HUL_B_DUAL_BUDGET_NOTE = (
    "HUL-B dual-budget enforcement not present; using mission_record "
    "working_remaining (max - spent - terminal_reserve)."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_callable(fn: Any, what: str) -> Callable[..., Any]:
    if not callable(fn):
        raise HarnessError(f"{what} must be callable")
    return fn


def artifact_names(pack: mr.MissionPack) -> Set[str]:
    """Code-owned snapshot of artifact file names under the pack."""
    directory = pack.artifacts_dir
    if not directory.is_dir():
        return set()
    return {p.name for p in directory.iterdir() if p.is_file()}


def normalize_attempt(raw: Any, *, attempt: int) -> Dict[str, Any]:
    """Normalize an attempt_fn return value to the driver's receipt shape."""
    if raw is None:
        body: Dict[str, Any] = {}
    elif isinstance(raw, dict):
        body = dict(raw)
    else:
        raise HarnessError("attempt_fn must return a mapping or None")
    artifacts = body.get("artifacts") or []
    evidence = body.get("evidence") or []
    if not isinstance(artifacts, (list, tuple)):
        artifacts = [str(artifacts)]
    if not isinstance(evidence, (list, tuple)):
        evidence = [str(evidence)]
    cost = body.get("cost_usd", 0.0) or 0.0
    tokens = body.get("tokens", 0) or 0
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise HarnessError("attempt cost_usd must be a non-negative number")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise HarnessError("attempt tokens must be a non-negative integer")
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    return {
        "kind": "attempt",
        "attempt": attempt,
        "ok": bool(body.get("ok", False)),
        "cost_usd": float(cost),
        "tokens": int(tokens),
        "error": error,
        "artifacts": [str(a) for a in artifacts],
        "evidence": [str(e) for e in evidence],
        "verifier_holds": bool(body.get("verifier_holds", False)),
        "evidence_summary": str(body.get("evidence_summary") or ""),
        "claims": list(body.get("claims") or []),
        "state_summary": str(body.get("state_summary") or ""),
        "notes": str(body.get("notes") or ""),
    }


def findings_md(
    pack: mr.MissionPack,
    *,
    outcome: str,
    reason: str,
    attempts: int,
    stall_counter: int,
    spent: float,
    working_remaining: float,
    determination: Optional[Dict[str, Any]] = None,
) -> str:
    """Honest FINDINGS body for a terminal driver outcome."""
    lines = [
        f"# FINDINGS — {pack.id}",
        "",
        f"Terminal outcome: `{outcome}` at {_now_iso()}.",
        "",
        "## Driver (HUL-D until-limits)",
        "",
        f"- reason: {reason}",
        f"- attempts: {attempts}",
        f"- stall_counter: {stall_counter}",
        f"- budget.spent: {spent}",
        f"- budget.working_remaining: {working_remaining}",
        f"- dual_budget: {HUL_B_DUAL_BUDGET_NOTE}",
        "",
    ]
    if determination is not None:
        lines.extend([
            "## Scope determination (HUL-C)",
            "",
            f"- complete: {determination.get('complete')}",
            f"- verifier_holds: {determination.get('verifier_holds')}",
            f"- success_definition_met: {determination.get('success_definition_met')}",
            f"- scope_holds: {determination.get('scope_holds')}",
            f"- is_fallback: {determination.get('is_fallback')}",
            f"- site: {determination.get('site')}",
            "",
        ])
        reasons = determination.get("reasons") or []
        if reasons:
            lines.append("### Determination reasons")
            lines.append("")
            for item in reasons:
                lines.append(f"- {item}")
            lines.append("")
    else:
        lines.extend([
            "## Scope determination (HUL-C)",
            "",
            "- complete: false (no scope gate on this run; unkeyed/absent "
            "policy cannot alone mark complete)",
            "",
        ])
    lines.extend([
        "## Resume",
        "",
        "resume.json remains the continuation source of truth. Re-run "
        "`harness mission run` only after addressing the terminal reason "
        "or resetting the pack deliberately.",
        "",
    ])
    return "\n".join(lines)


def _budget_view(pack: mr.MissionPack) -> Dict[str, float]:
    budget = mr.load_budget(pack)
    max_cost = float(budget.get("max_cost_usd") or 0.0)
    reserve = float(budget.get("terminal_reserve_cost_usd") or 0.0)
    spent = float(budget.get("spent") or 0.0)
    remaining = mr.working_remaining(max_cost, spent, reserve)
    return {
        "max_cost_usd": max_cost,
        "terminal_reserve_cost_usd": reserve,
        "spent": spent,
        "working_remaining": remaining,
    }


def run_mission(
    pack: mr.MissionPack,
    *,
    attempt_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
    scope_policy: Any = None,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    max_attempts: Optional[int] = None,
    max_tokens: Optional[int] = None,
    max_errors: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Until-limits driver. See module docstring for the contract.

    Returns a machine-readable driver summary. Does not raise for ordinary
    terminal outcomes (stall / limits / complete); schema errors raise
    ``HarnessError``.
    """
    if not pack.exists():
        raise HarnessError(f"mission pack not found: {pack.dir}")
    if stall_limit < 1:
        raise HarnessError("stall_limit must be >= 1")
    if max_attempts is not None and max_attempts < 0:
        raise HarnessError("max_attempts must be >= 0 when set")
    if attempt_fn is None:
        raise HarnessError(
            "run_mission requires attempt_fn (library injects the "
            "attempt seat; CLI supplies the pack probe)")
    _require_callable(attempt_fn, "attempt_fn")

    spec = pack.spec()
    if not pack.resume_path.is_file():
        mr.write_resume(pack, mr.build_resume_state(pack))
    resume = mr.load_resume(pack)
    if mr.is_terminal(pack):
        summary = mr.pack_summary(pack)
        summary["driver"] = {
            "status": resume.get("status"),
            "terminal": True,
            "reason": "pack already terminal",
            "attempts": resume.get("attempts", 0),
            "dual_budget_note": HUL_B_DUAL_BUDGET_NOTE,
        }
        return summary

    budget = _budget_view(pack)
    cost_ceiling = (
        float(max_cost_usd) if max_cost_usd is not None
        else budget["working_remaining"]
    )
    stall_counter = int(resume.get("stall_counter") or 0)
    errors = int(resume.get("errors") or 0)
    tokens_total = int(resume.get("tokens") or 0)
    attempts = int(resume.get("attempts") or 0)
    seen_evidence = set(resume.get("seen_evidence") or [])
    known_artifacts = artifact_names(pack)
    last_determination: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    outcome: Optional[str] = None

    def _persist(status: str, **extra: Any) -> Dict[str, Any]:
        payload = {
            "status": status,
            "stall_counter": stall_counter,
            "errors": errors,
            "tokens": tokens_total,
            "seen_evidence": sorted(seen_evidence),
            "attempts": max(attempts, int(
                sum(1 for r in mr.load_receipts(pack) if r.get("kind") == "attempt"))),
        }
        payload.update(extra)
        state = mr.build_resume_state(pack, **payload)
        # Preserve the in-flight attempt counter even before its receipt lands.
        if "attempts" in extra:
            state["attempts"] = extra["attempts"]
        else:
            state["attempts"] = attempts
        return mr.write_resume(pack, state)

    def _terminal(outcome_name: str, reason_text: str) -> Dict[str, Any]:
        body = findings_md(
            pack,
            outcome=outcome_name,
            reason=reason_text,
            attempts=attempts,
            stall_counter=stall_counter,
            spent=budget["spent"],
            working_remaining=budget["working_remaining"],
            determination=last_determination,
        )
        resume_after = mr.mark_terminal(
            pack, outcome=outcome_name, findings=body)
        mr.write_index(pack)
        summary = mr.pack_summary(pack)
        summary["driver"] = {
            "status": outcome_name,
            "terminal": True,
            "reason": reason_text,
            "attempts": attempts,
            "stall_counter": stall_counter,
            "errors": errors,
            "tokens": tokens_total,
            "spent": budget["spent"],
            "working_remaining": budget["working_remaining"],
            "determination": last_determination,
            "dual_budget_note": HUL_B_DUAL_BUDGET_NOTE,
            "resume": resume_after,
        }
        return summary

    # Pre-attempt budget check (interrupt-safe start state).
    if budget["working_remaining"] <= 0 and cost_ceiling <= 0:
        outcome, reason = "blocked", (
            f"working budget exhausted before any attempt "
            f"(spent={budget['spent']}, remaining={budget['working_remaining']})")
        return _terminal(outcome, reason)

    while True:
        budget = _budget_view(pack)
        if cost_ceiling is not None and budget["spent"] >= cost_ceiling:
            outcome, reason = "blocked", (
                f"cost limit reached (spent={budget['spent']} "
                f">= ceiling={cost_ceiling})")
            break
        if budget["working_remaining"] <= 0:
            outcome, reason = "blocked", (
                f"working_remaining exhausted (spent={budget['spent']})")
            break
        if max_attempts is not None and attempts >= max_attempts:
            outcome, reason = "blocked", (
                f"max attempts reached ({attempts} >= {max_attempts})")
            break
        if max_tokens is not None and tokens_total >= max_tokens:
            outcome, reason = "blocked", (
                f"token limit reached ({tokens_total} >= {max_tokens})")
            break
        if max_errors is not None and errors >= max_errors:
            outcome, reason = "failed", (
                f"error limit reached ({errors} >= {max_errors})")
            break
        if stall_counter >= stall_limit:
            outcome, reason = "stalled", (
                f"{stall_limit} consecutive attempts with no new "
                f"artifact/evidence")
            break

        # Interrupt-safe: persist in_progress state before the attempt.
        attempts += 1
        _persist("in_progress", attempt_in_flight=attempts)

        context = {
            "pack": pack,
            "spec": spec,
            "mission_id": pack.id,
            "attempt": attempts,
            "budget": dict(budget),
            "known_artifacts": set(known_artifacts),
            "stall_counter": stall_counter,
            "resume": mr.load_resume(pack),
        }
        try:
            raw_outcome = attempt_fn(context)
            receipt = normalize_attempt(raw_outcome, attempt=attempts)
            error = receipt["error"]
        except HarnessError:
            raise
        except Exception as exc:  # attempt seat blew up — count as error
            receipt = normalize_attempt(
                {"ok": False, "error": str(exc)}, attempt=attempts)
            error = str(exc)

        after = artifact_names(pack)
        new_on_disk = sorted(after - known_artifacts)
        known_artifacts |= after
        claimed = set(receipt["artifacts"]) | set(receipt["evidence"])
        new_claimed = {name for name in claimed if name and name not in seen_evidence}
        new_evidence = set(new_on_disk) | new_claimed
        # Stall: consecutive attempts that introduce no new artifact/evidence.
        if new_evidence:
            stall_counter = 0
        else:
            stall_counter += 1

        if error:
            errors += 1
        tokens_total += int(receipt["tokens"])
        mr.append_receipt(pack, receipt)
        if receipt["cost_usd"] > 0:
            mr.record_spend(pack, receipt["cost_usd"])
            budget = _budget_view(pack)

        seen_evidence |= claimed | set(new_on_disk)
        resume = _persist("in_progress")

        # HUL-C scope gate when a policy owner is attached.
        if scope_policy is not None:
            try:
                _result, _structural, determination = mr.evaluate_scope_on_pack(
                    pack,
                    scope_policy,
                    evidence_summary=receipt["evidence_summary"],
                    verifier_holds=receipt["verifier_holds"],
                    state_summary=receipt["state_summary"],
                    task_id=task_id or pack.id,
                )
                last_determination = determination
            except HarnessError as exc:
                last_determination = {
                    "complete": False,
                    "is_fallback": True,
                    "reasons": [str(exc)],
                    "site": "hul_scope",
                }
            if last_determination.get("complete"):
                outcome = "complete"
                reason = (
                    "HUL-C scope determination complete "
                    "(verifier + success_definition_met + scope hold)")
                break
            # False-done blocked: attempt claimed verifier_holds but scope
            # determination is not complete — continue until limits/stall.
        else:
            # No scope policy: unkeyed/absent gate cannot alone complete.
            last_determination = {
                "complete": False,
                "verifier_holds": receipt["verifier_holds"],
                "success_definition_met": False,
                "scope_holds": False,
                "is_fallback": True,
                "site": "hul_scope",
                "reasons": [
                    "no scope policy attached — unkeyed fallback cannot "
                    "alone mark mission complete"],
            }

        mr.write_status(pack)
        mr.write_index(pack)

        if stall_counter >= stall_limit:
            outcome, reason = "stalled", (
                f"{stall_limit} consecutive attempts with no new "
                f"artifact/evidence")
            break

    assert outcome is not None and reason is not None
    return _terminal(outcome, reason)


def pack_probe_attempt(context: Dict[str, Any]) -> Dict[str, Any]:
    """CLI default attempt seat: code-owned pack probe, no network spend.

    Reports artifacts already present under ``pack/artifacts/``. Produces
    no new work of its own — without an injected attempt_fn the driver will
    honestly stall unless external work writes into the pack.
    """
    pack = context.get("pack")
    if pack is None:
        return {"ok": False, "error": "pack probe missing pack handle"}
    try:
        names = sorted(artifact_names(pack))
    except OSError as exc:
        return {"ok": False, "error": f"artifact probe failed: {exc}"}
    if not names:
        return {
            "ok": False,
            "error": "no artifacts in pack; no attempt runner wired",
            "evidence_summary": "pack artifacts directory empty",
        }
    return {
        "ok": True,
        "artifacts": names,
        "evidence_summary": "observed pack artifacts: " + ", ".join(names),
        "notes": "pack probe only — verifier not executed",
    }
