"""Bipolar trust: how much history has earned, on a -11..+11 scale.

Two scores, one policy each:

* **Trust amount** (per principal: host/caller, model, continuation author).
  ``-11`` is extreme distrust, ``0`` is unknown (the only cold-start value),
  ``+11`` is extreme trust. Levels *and* gates: the integer is stored and
  compared, and thresholds on it unlock actions (refuse / preview-only /
  gated-standard / expanded). Trust accrues slowly (3 clean successes per
  +1) and drops fast on safety signals (bounded protocol sloppiness,
  -1 guidance denials, -4 hostile denials): one exploit costs more than
  ten clean runs earn.
* **Correctness vs ceiling.** A separate correctness level (same scale,
  from success evidence only) rations the spend ceiling: low correctness
  gets a small fraction of the hard cap, proven correctness unlocks the
  full cap. Trust never raises *past* ``HARD_MAX_COST``/``HARD_TASK_MAX_COST``.

Both scores are pure functions of the autonomy ledger's participation
report (plus the raw success counts the report carries), so there is no
second trust database to fork from the evidence chain. New principals
start at exactly 0 and build up over prompts: the first runs are
preview-only or tight-ceiling, and each clean verify-passed round earns
toward the next level.
"""

from .errors import HarnessError

MIN_TRUST = -11
MAX_TRUST = 11

# ---- gate thresholds (levels AND gates: the score is stored, the
# ---- thresholds below decide what it unlocks) ----
REFUSE_AT_OR_BELOW = -6   # <= -6: refuse any mutation or gate execution
# -5..-1: preview-only (write/exec refused with guidance to re-send verify_only)
UNKNOWN = 0               # 0: unknown -- tightest ceilings, strong gate required
STANDARD_FROM = 1         # +1..+5: standard policy (today's behavior)
EXPANDED_FROM = 6         # +6..+11: expanded ceilings/rounds within HARD_* caps

# ---- evidence weights: slow up, fast down ----
CLEAN_PER_LEVEL = 3       # clean successes needed per +1
# Ordinary verify failures do NOT move trust: a caught miss is the gate
# working, and round-level misses are normal iteration (a fix on round 2
# would otherwise punish the workflow the harness exists for). Misses
# move *correctness*, which rations ceilings.
HYGIENE_CAP = 3           # protocol sloppiness (reasoning-only /
                          # consent-unusable output) is bounded evidence:
                          # rotation already handles it, and uncapped counts
                          # would let sheer volume swamp real history.
STRIKE_HOSTILE = 4        # a hostile denial (retarget, root escape,
                          # refuse-level write): malice-grade, fast down
STRIKE_SOFT = 1           # a guidance denial (preview-band, over-ceiling,
                          # missing allow-flag): friction, not malice


def _clamp(score):
    return max(MIN_TRUST, min(MAX_TRUST, int(score)))


def _calibration_for(report, model):
    cal = (report or {}).get("calibration", {})
    entry = cal.get(model) if isinstance(cal, dict) else None
    return entry if isinstance(entry, dict) else {}


def _clean_successes(entry):
    """Ground-truth successes: verify-gate passes + known-answer passes.

    Falls back to confident_passed for reports written before raw counts
    landed (additive ledger change); confident passes are a subset of gate
    passes, so the fallback never double-counts, it only under-counts.
    """
    passed = entry.get("success_pass")
    structured = entry.get("structured_pass")
    if passed is None and structured is None:
        try:
            return max(0, int(entry.get("confident_passed") or 0))
        except (TypeError, ValueError):
            return 0
    total = 0
    for key in ("success_pass", "structured_pass"):
        try:
            total += max(0, int(entry.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _int(entry, key):
    try:
        return max(0, int(entry.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _model_denials(entry):
    """Safety denials attributed to one model: (hostile, soft).

    Older reports carry only a total (no severity split): those count as
    soft, the lenient reading -- severity tagging postdates them.
    """
    hostile = _int(entry, "trust_hostile")
    total = _int(entry, "trust_denials")
    return hostile, max(0, total - hostile)


def model_trust(model, report):
    """Trust earned by one model id: (score, reasons).

    Unknown models score exactly 0 -- declared /models capability never
    substitutes for observed behavior. Trust measures *safety* (will this
    principal's output respect the mutation boundary), not round-level
    accuracy: accuracy lives in correctness_level and rations ceilings.
    """
    entry = _calibration_for(report, model)
    if not entry:
        return 0, ["no history for this model: unknown"]
    clean = _clean_successes(entry)
    hygiene = min(HYGIENE_CAP, _int(entry, "unusable_outputs")
                  + _int(entry, "consent_unusable"))
    hostile, soft = _model_denials(entry)
    levels = min(MAX_TRUST, clean // CLEAN_PER_LEVEL)
    strikes = hygiene + hostile * STRIKE_HOSTILE + soft * STRIKE_SOFT
    score = _clamp(levels - strikes)
    reasons = [f"{clean} clean successes ({levels} levels)",
               f"{strikes} strike points"]
    if score <= REFUSE_AT_OR_BELOW:
        reasons.append("at or below the refuse threshold")
    elif score < UNKNOWN:
        reasons.append("preview-only until clean runs accrue")
    elif score == UNKNOWN:
        reasons.append("unknown: gated standard policy with tight ceilings")
    return score, reasons


def _counts(source, completions_key="completions", gates_key="trust_gates",
            hostile_key="trust_hostile"):
    """Pull (completions, hostile, soft) counts out of a report section."""
    def _int(key):
        try:
            return max(0, int(source.get(key) or 0))
        except (TypeError, ValueError, AttributeError):
            return 0
    total = _int(gates_key)
    hostile = _int(hostile_key)
    return _int(completions_key), hostile, max(0, total - hostile)


def host_trust(report, caller=None):
    """Trust earned by the calling host/session: (score, reasons).

    With a caller id, only that caller's tagged history scores -- one
    abusive peer no longer taints every other caller's standing. Without
    one (or when the caller has no tagged history), the global session
    counts apply, so untagged and mixed history still gate honestly.
    """
    report = report or {}
    if caller is not None:
        entry = (report.get("per_caller") or {}).get(caller)
        if entry is None:
            return 0, [f"no history for caller {caller}: unknown"]
        completions, hostile, soft = _counts(entry)
        levels = min(MAX_TRUST, completions // CLEAN_PER_LEVEL)
        strikes = hostile * STRIKE_HOSTILE + soft * STRIKE_SOFT
        score = _clamp(levels - strikes)
        reasons = [f"caller {caller}: {completions} completions "
                   f"({levels} levels)",
                   f"{hostile} hostile + {soft} guidance denials"]
        if not completions and not (hostile + soft):
            return 0, [f"no history for caller {caller}: unknown"]
        return score, reasons
    completions, hostile, soft = _counts(report)
    levels = min(MAX_TRUST, completions // CLEAN_PER_LEVEL)
    strikes = hostile * STRIKE_HOSTILE + soft * STRIKE_SOFT
    score = _clamp(levels - strikes)
    reasons = [f"{completions} completions ({levels} levels)",
               f"{hostile} hostile + {soft} guidance denials"]
    if not completions and not (hostile + soft):
        return 0, ["no host history: unknown"]
    return score, reasons


def author_trust():
    """Trust of a continuation-state author: v1 is always unknown.

    There is no per-author history yet (state authors are not tagged in
    ledger events), so a resume is always treated as unknown: no file
    retarget, gate identity must match, hash must match when present.
    Returns (0, reasons) so the author participates in the combined
    minimum on resumes without ever capping fresh applies.
    """
    return 0, ["no per-author history yet: unknown"]


def combined_trust(host_score, model_score, author_score=None):
    """Weakest link over the principals that matter for this request.

    The author only matters on a resume (fresh applies have no state
    author); otherwise the minimum of host and model decides.
    """
    scores = [host_score, model_score]
    if author_score is not None:
        scores.append(author_score)
    return min(scores)


def correctness_level(model, report):
    """Correctness evidence for one model: -11..+11 from success only.

    Safety strikes do not enter here (they move trust); correctness is
    purely "does this principal produce right answers": passes earn
    slowly, fails push negative so incorrectness rations the budget down.
    """
    entry = _calibration_for(report, model)
    if not entry:
        return 0
    passes = _clean_successes(entry)
    fails = _int(entry, "success_fail") + _int(entry, "structured_fail")
    if passes == 0 and fails == 0:
        return 0
    # Symmetric and volume-fair: round-level misses are normal iteration,
    # so a miss costs the same one level a pass earns. A 65%-accurate
    # workhorse still rations near-standard budgets; a 0%-accurate probe
    # subject rations to the floor.
    net = passes - fails
    level = min(MAX_TRUST, abs(net) // CLEAN_PER_LEVEL)
    return _clamp(level if net >= 0 else -level)


def ceiling_fraction(correctness):
    """Fraction of a HARD_* cap unlocked by a correctness level.

    Unknown (0) lands exactly on today's defaults: 0.2 of the 10c session
    cap is 2c (DEFAULT_MAX_COST) and 0.2 of the 25c task cap is 5c
    (DEFAULT_TASK_MAX_COST). Proven correctness expands toward the hard
    cap; negative correctness tightens below the defaults.
    """
    if correctness <= 0:
        return 0.2 if correctness == 0 else 0.1
    if correctness < EXPANDED_FROM:
        return 0.5
    return 1.0


def rationed_session_ceiling(requested, correctness, hard_cap):
    """Highest session ceiling this correctness unlocks (never past hard)."""
    allowed = hard_cap * ceiling_fraction(correctness)
    return min(float(requested), hard_cap, allowed)


def rationed_task_ceiling(requested, correctness, hard_cap):
    """Highest per-task ceiling this correctness unlocks (never past hard)."""
    allowed = hard_cap * ceiling_fraction(correctness)
    return min(float(requested), hard_cap, allowed)


def gate_for_write_exec(combined):
    """What a combined trust score permits for mutation/execution."""
    if combined <= REFUSE_AT_OR_BELOW:
        return "refuse"
    if combined < UNKNOWN:
        return "preview-only"
    return "allow"


def check_apply(*, ledger, report, model, resumed, verify_only,
                verify_cmd, task_max_cost, task_id, hard_task_cap,
                caller=None):
    """Enforce the hard gates for one apply request.

    Returns {"combined", "correctness", "allowed_task_ceiling", "notes"}.
    Denials append a trust_gate event (the evidence loop) and raise
    HarnessError with the score and how to proceed. Callers must invoke
    this AFTER the model is known and BEFORE any file write or gate run.
    With a caller id the host leg scores that peer's tagged history.
    """
    host_score, host_reasons = host_trust(report, caller=caller)
    model_score, model_reasons = model_trust(model, report)
    author = author_trust() if resumed else None
    combined = combined_trust(host_score, model_score,
                              author[0] if author else None)
    correctness = correctness_level(model, report)
    notes = [f"host {host_score} ({'; '.join(host_reasons)})",
             f"model {model_score} ({'; '.join(model_reasons)})"]
    if author:
        notes.append(f"author {author[0]} ({'; '.join(author[1])})")
    notes.append(f"correctness {correctness}")

    wants_write = not verify_only
    wants_exec = bool(verify_cmd) and not verify_only

    def _deny(reason, guidance, severity="soft"):
        try:
            ledger.append("trust_gate", task_id=task_id, model=model,
                          reason=reason, severity=severity,
                          combined=combined, correctness=correctness)
        except Exception:
            pass
        raise HarnessError(
            f"trust gate denied this apply (combined trust {combined}, "
            f"correctness {correctness}): {reason} {guidance}")

    if (wants_write or wants_exec):
        gate = gate_for_write_exec(combined)
        if gate == "refuse":
            _deny("score at or below the refuse threshold.",
                  "No mutation or gate execution at this trust. "
                  "Earn trust with preview-only runs first.")
        if gate == "preview-only":
            _deny("preview-only trust band.",
                  "Re-send with verify_only=true for a no-write proposal, "
                  "or earn trust with clean preview runs.")
    # NOTE: unknown trust without a gate is NOT refused here. Consent,
    # readiness, and capability-deferral paths must still run (they never
    # touch the file); the write itself is refused at mutation time by
    # check_mutation, which the gate policy calls before any disk write.
    try:
        requested = float(task_max_cost)
    except (TypeError, ValueError):
        requested = 0.0
    allowed = rationed_task_ceiling(requested, correctness, hard_task_cap)
    if requested - allowed > 1e-12:
        _deny(f"correctness {correctness} unlocks a task ceiling of "
              f"${allowed:.6f} (requested ${requested:.6f}).",
              "Lower task_max_cost to the unlocked ceiling or earn "
              "correctness with verify-passed runs.")
    notes.append(f"task ceiling ${requested:.6f} within unlocked ${allowed:.6f}")
    return {"combined": combined, "correctness": correctness,
            "allowed_task_ceiling": allowed, "notes": notes}


def check_mutation(*, ledger, combined, verify_cmd, task_id, model):
    """Enforce the write-time gate: called before any candidate bytes land.

    Consent/readiness/deferral paths return before this point, so refusing
    here never blocks an honest deferral -- it only stops unreviewed model
    output from reaching disk. Unknown trust (0) with no verification gate
    is refused (supply a gate or use verify_only); extreme distrust refuses
    every write as a backstop to the dispatch-time gate.
    """
    if combined <= REFUSE_AT_OR_BELOW or (combined == UNKNOWN and not verify_cmd):
        hostile = combined != UNKNOWN
        try:
            ledger.append("trust_gate", task_id=task_id, model=model,
                          reason=("gateless write at unknown trust"
                                  if combined == UNKNOWN
                                  else "write at refuse-level trust"),
                          severity=("hostile" if hostile else "soft"),
                          combined=combined, correctness=None)
        except Exception:
            pass
        if combined == UNKNOWN:
            raise HarnessError(
                "trust gate denied this write (combined trust 0, unknown): "
                "a write with no verification gate is not allowed at unknown "
                "trust. Supply verify_cmd (a gate the dispatcher can run) or "
                "re-send with verify_only=true for a proposal.")
        raise HarnessError(
            f"trust gate denied this write (combined trust {combined}): "
            "score at or below the refuse threshold. No file mutation at "
            "this trust. Earn trust with preview-only runs first.")


def trust_status(report, model=None, caller=None):
    """Read-only trust snapshot for CLI/MCP surfaces (no ledger writes).

    With a caller id the host section scores that caller's tagged history
    (global stays the fallback), and the per_caller table carries every
    tagged peer's standing.
    """
    host_score, host_reasons = host_trust(report, caller=caller)
    out = {"host": {"score": host_score, "reasons": host_reasons},
           "scale": {"min": MIN_TRUST, "unknown": UNKNOWN, "max": MAX_TRUST,
                     "refuse_at_or_below": REFUSE_AT_OR_BELOW,
                     "standard_from": STANDARD_FROM,
                     "expanded_from": EXPANDED_FROM},
           "trust_gates": (report or {}).get("trust_gates", 0)}
    if caller is not None:
        out["caller"] = {"id": caller, "score": host_score,
                         "reasons": host_reasons}
    table = {}
    for cid, entry in ((report or {}).get("per_caller") or {}).items():
        completions, hostile, soft = _counts(entry)
        levels = min(MAX_TRUST, completions // CLEAN_PER_LEVEL)
        table[cid] = {
            "score": _clamp(levels - hostile * STRIKE_HOSTILE
                            - soft * STRIKE_SOFT),
            "completions": completions,
            "trust_gates": hostile + soft,
        }
    out["per_caller"] = table
    if model:
        model_score, model_reasons = model_trust(model, report)
        correctness = correctness_level(model, report)
        out["model"] = {"id": model, "score": model_score,
                        "reasons": model_reasons}
        out["correctness"] = {"level": correctness,
                              "session_fraction": ceiling_fraction(correctness),
                              "task_fraction": ceiling_fraction(correctness)}
        out["combined"] = combined_trust(host_score, model_score)
    return out
