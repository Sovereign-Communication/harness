"""
Phase 0 RECONCILE: write the authoritative overnight ledger for this pass.

Scope of this ledger (per prompt): the prompt-named open issues + their
dependency chains + every unlanded branch/worktree on this machine. The live
open-issue count (169) exceeds the prompt's 74; the ledger records that delta
explicitly and does NOT pretend to burn down all 169 this pass.

Citations (verified fresh this session):
  - lane branch/HEAD/main all = 4ccee99 (clean checkout, no harness diff)
  - PR #431 DRAFT head 114e8101, 15/15 CI checks pass (check-runs, not combined)
  - Open issue count live = 169 (de-duped from gh search)
  - worktrees = 7 (see inventory below)
  - gate-v2-implementation head = 67bf24b plans/worldwide-push-2026-10-25
  - Lucas Gate v2 runner ABSENT from this lane
  - MCP catalog = 30, no lucas_gate_v2
  - Lucas v2 profile ABSENT

Status legend:
  LAND     = order-aware landing plan (only gated writes when gates actually pass)
  PARK     = owner decision / answer needed; move on; record need
  BLOCKED  = hard blocker with exact owner action
  LOCAL-ONLY = branch/worktree not yet pulled/landed
  SKIP     = deliberately not done this run (rule-bound)
  VERIFIED = checked this session with evidence
"""
from pathlib import Path
import json
from datetime import datetime

OUT = Path("docs") / "overnight-ledger-2026-10-28.md"

rows = []

def row(label, status, note, owner_action):
    rows.append((label, status, note, owner_action))

# ---------- Scope / verification header ----------
row(
    "SCOPE — overnight ledger pass (2026-10-28)",
    "VERIFIED",
    "Lane == main == 4ccee99 (clean checkout, no harness diff). "
    "PR #431 DRAFT head 114e8101, 15/15 CI checks pass (check-runs). "
    "Live open issues = 169 (prompt said 74 — delta recorded). "
    "Worktrees = 7. gate-v2-implementation head = 67bf24b (NOT 8917b91 as prompt cited). "
    "Lucas Gate v2 runner ABSENT from this lane; MCP catalog = 30, no lucas_gate_v2; "
    "profile ABSENT. Degraded mode: no gate-dependent GitHub writes.",
    ""
)

# ---------- Open issues (prompt-named set + deps) ----------
prompt_open = [
    270,271,272,273,275,277,280,281,283,284,285,286,287,289,290,291,292,293,294,
    295,299,300,301,302,303,304,306,308,309,310,311,313,314,318,319,325,332,334,
    335,336,337,340,342,343,344,345,346,365,377,378,379,380,381,382,
    407,409,411,
    412,413,414,415,416,417,418,419,
    420,421,422,423,424,425,426,427,428,429,430,431,
]
row(
    "OPEN ISSUES — prompt-named set",
    "VERIFIED",
    f"Prompt-named open set = {len(prompt_open)} issues (list above). "
    "Live open count = 169 (delta recorded separately). "
    "Per-issue disposition deferred to BURNDOWN sweep in dependency order; "
    "skip #333 (landed via PR #398 per prompt); #288 closed per prompt (verified absent from open list). "
    "Held items (rule 47) get Jev-flushed briefs, never decisions.",
    ""
)

# ---------- Held items (rule 47) ----------
held = [
    "#294 router flag",
    "#270-273 debut scope",
    "#275 Hourglass philosophy",
    "#291 runner-vs-manual",
    "#325 aggregation rule",
    "#280 strike policy + weights",
    "#281 schedule/venue/bar",
    "#282/#283 rescope",
    "#314 parity bars",
    "#336 never-prune list",
    "#337 checklist criteria",
    "#303 enforce-vs-disclose",
    "public deploy",
]
row(
    "HELD OWNER-DECISION ITEMS (rule 47)",
    "PARK",
    "These get Jev-flushed decision briefs, never decisions. "
    "They are PARKED, not blockers for the rest of the loop. "
    "List: " + "; ".join(held) + ".",
    ""
)

# ---------- #306 (hard blocker, untouched) ----------
row(
    "#306 (blocked, blocker intact)",
    "BLOCKED",
    "One-shot Fireworks probe returned HTTP 200 with contains_ok=false; marker prohibits another call. "
    "NEVER rerun probe, NEVER delete marker, NEVER mark complete. "
    "Only unblocker: operator decision on a non-repeating remedy or another already-existing valid receipt.",
    "operator decision on non-repeating remedy (or produce another valid receipt)"
)

# ---------- #407 revocation decision unrecorded ----------
row(
    "#407 revocation decision (0 comments, unrecorded)",
    "PARK",
    "Record decision FIRST per prompt rule 39: verify #407 comments; if absent, gated comment only if gate passes; "
    "otherwise defer. Opt-in only, aggregates only, sanitization documented + hermetic-tested. "
    "Revoking opt-in STOPS future submissions; contributions already in published aggregates STAY.",
    "record revocation decision (or confirm already present in comments)"
)

# ---------- Draft PR #431 ----------
row(
    "#431 DRAFT (docs: reconcile Jev research and full-source audit plans)",
    "PARK",
    "Draft only — NEVER merge while draft. Verified fresh: head 114e8101, base main, mergeable true, "
    "15/15 check-runs PASS (scope, hygiene, UI smoke, audit, typecheck, hermetic x5, installed package x3, package). "
    "If this is the Jev-research reconciliation, fold into ledger; ready ONLY with a gate pass + green CI. "
    "No merge this pass while draft.",
    "gate pass + green CI before ready; do not merge while draft"
)

# ---------- Worktree: gate-v2-implementation ----------
row(
    "worktree gate-v2-implementation (plans/worldwide-push-2026-10-25, head 67bf24b)",
    "LOCAL-ONLY",
    "Prompt cited head b8917b91; LIVE head is 67bf24b. Re-verify the plans branch before any landing. "
    "Check for overlap/duplication with the Lucas Gate runner changes BEFORE landing either. "
    "This lane has NO Lucas runner committed, so there is nothing to duplicate yet — but any future runner PR "
    "must be checked against this worktree's plans branch content.",
    "re-verify plans branch head + content before any landing"
)

# ---------- Worktree: #290 runbook ----------
row(
    "worktree Harness-290-triage-runbook (codex/290-triage-runbook, head d8a5aa9)",
    "LOCAL-ONLY",
    "Only docs/audit-triage.md modified per prompt. R13 timeout cause explicitly unresolved (allowed; must stay documented). "
    "LAND as its own PR: commit any uncommitted pruning first; focused docs tests pass; verify contributor/audit-doc link post-merge. "
    "Mark #290 complete ONLY with a declared close keyword in PR body after acceptance verified.",
    "finalize + PR; declared close only after acceptance verified"
)

# ---------- Worktree: planning reconciliation ----------
row(
    "worktree Harness-issue-290-planning-reconcile (codex/issue-290-planning-reconcile, head 4ccee99)",
    "LOCAL-ONLY",
    "Roadmap + queue + README. LAND ONLY after overlap check vs #288 bookkeeping diff: "
    "if both touch same files, reconcile into one coherent sequence (rebase, never clobber). "
    "Report chosen order with evidence. This lane has no harness diff, so the planning-reconcile content "
    "must be inspected on its own worktree before any merge decision.",
    "inspect + overlap check before landing"
)

# ---------- Dirty worktree: capability fix + comment script ----------
row(
    "dirty capability-fallback-fix + post_session_audit_comments.ps1 (previous session)",
    "LOCAL-ONLY",
    "Prompt: capability fallback fix untouched; post_session_audit_comments.ps1 unrun and MUST STAY UNRUN "
    "(audit comments already posted from tracking thread). LAND capability fix as its own PR: "
    "nonempty stale snapshot survives provider errors and empty catalogs; empty registry retried, never saved as fresh; "
    "cancellation and spend-limit errors propagate; full capability suite + lint + wheel build.",
    "do NOT run post_session_audit_comments.ps1; land capability fix as own PR when its gates pass"
)

# ---------- Lucas Gate v2 runner (absent) ----------
row(
    "Lucas Gate v2 runner (lucas_gate.py, mcp_schemas.py update, test_lucas_gate_v2.py) — ABSENT",
    "BLOCKED",
    "Prompt: last session built these but landed NOTHING; this lane has NONE of them. "
    "A fresh stdio launch lists 31 tools incl lucas_gate_v2, but the CONNECTED MCP still lists 30. "
    "SYNTHETIC PASS IS NOT A SEMANTIC GATE PASS — nothing gated until it passes against the real profile. "
    "No driving artifact counts as gated until real-profile semantic judgment passes. "
    "If the prior runner code exists on another worktree/branch, re-derive it here BEFORE any gate-dependent write; "
    "otherwise rebuild it here (tests + ruff + pyright clean).",
    "re-derive or locate runner; load real profile; reload MCP until it lists lucas_gate_v2"
)

# ---------- Lucas v2 profile ----------
row(
    "Lucas v2 private profile — ABSENT",
    "BLOCKED",
    "Locate at its documented config location (find LUCAS_PROFILE_PATH in the gate implementation plan first). "
    "If absent, REQUEST from operator and record in ledger. NEVER commit, paste, or print. Load locally only. "
    "DEGRADED MODE if absent overnight: ledger + local verification + code/tests/docs ONLY; "
    "ZERO gate-dependent GitHub writes (queue, never post ungated); report blocker exactly.",
    "operator provides profile (or confirm location)"
)

# ---------- MCP catalog mismatch ----------
row(
    "MCP connected catalog lists 30 tools (no lucas_gate_v2)",
    "BLOCKED",
    "Prompt: fresh stdio lists 31 incl lucas_gate_v2, connected MCP still 30. Verify BOTH before any gate-dependent write. "
    "This lane's mcp_schemas.py still has 30 schemas (verified this session via verify_tools.py + direct parse). "
    "The gap must close before any gate-dependent write. If only pack-constrained tools available, report tool-fit defer; "
    "do not force gate numbers.",
    "reload MCP catalog until it lists lucas_gate_v2 (and re-verify both stdio + connected)"
)

# ---------- Fresh-state verification artifacts ----------
row(
    "local verification artifacts created this session",
    "VERIFIED",
    "Created: .venv with pytest+pyright; invoke_lucas_semantic.py (deterministic local checks); "
    "lucas_local_scorecard_2026-10-28.json (synthetic, NOT a gate pass); "
    "refresh_ledger.py; verify_fresh.py; verify_tools.py; show_scorecard.py; find_runner.py; find_venv.py; runner_env_status.json. "
    "Results: hermetic rotate-subset 28 passed; ruff all passed; pyright runs (fails because lucas_gate.py absent — truth, not regression); "
    "git status: only my scratch files + .venv untracked; diff main..HEAD: none.",
    ""
)

# ---------- Scope delta (169 vs 74) ----------
row(
    "SCOPE DELTA — live open 169 vs prompt 74",
    "PARK",
    "Hard delta: live open issue count (169) exceeds prompt's 74. This overnight ledger is scoped to the prompt-named "
    "inventory + existing worktrees; the full 169 is NOT being burned down this pass. "
    "If operator wants the full 169 itemized, that is a separate task. "
    "The delta is reported in the overnight report rather than silently ignored or faked.",
    "operator decision: scope to prompt-named set only, or expand to full 169"
)

row(
    "JEV CLUSTER #420-430 — triage read this session",
    "VERIFIED",
    "Read #422 (2 comments) and #423 (1 comment) in full; read bodies of #420,421,424,425,426,427,428,429,430. "
    "Summary written to docs/jev_cluster_triage_2026-10-28.md. "
    "Live state cache written to docs/status_batch_2026-10-28.md. "
    "Key facts: (1) #422 has an attached science-review report (reports/ai-for-science-review-2026-10.md) but the "
    "product citation capability is NOT shipped — retain issue as implementation owner for metadata identity + "
    "exact-passage verification. (2) #423: the prior two-pass audit (711 files / 2,180 chunks; findings #420-429) "
    "manifest/receipt is ABSENT and must stay absent — a new sweep cannot be represented as recovery of that old "
    "receipt. (3) #420-430 are tightly coupled to PR #431's docs plan; the exact-plan Jev gate for this request is "
    "still below threshold and BLOCKED (per #423 comment + docs/jev-roadmap.md). (4) PR #431 is still DRAFT — no "
    "merge this pass.",
    "resolve #431 to ready+merged OR fold Jev-cluster work into a non-draft PR before BURNDOWN of #420-430"
)

row(
    "PROMPT-SCOPED ISSUE LIVE STATUS — 80 issues fetched this session",
    "VERIFIED",
    "Fetched title+state for all 80 prompt-named issues (docs/status_batch_2026-10-28.md). "
    "Unexpected closures found vs prompt assumption: #282 is CLOSED (not open); #409 is MERGED (not open); "
    "#365 is MERGED (PR #365, consistent with prompt's '#347 merged via PR #365' note). "
    "#422 and #431 returned state '???' from gh (title readable, state field absent) — treat as OPEN unless proven otherwise, "
    "and re-verify #422/#431 state via a direct state fetch before relying on it for a merge/close decision. "
    "The prompt's snapshot said #288 closed (consistent), #290 open (consistent), #412-430 present (consistent), "
    "#277 open (consistent), #407 open with 0 comments (consistent). "
    "All remaining prompt-named issues are OPEN as of this fetch.",
    "re-verify #422 and #431 state directly before any close/merge decision; otherwise state is captured"
)

# ---------- Write ----------
OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8") as f:
    f.write("# Overnight burndown ledger — 2026-10-28 (this pass)\n\n")
    f.write("Lane branch: `freebuff/harness-overnight-burndown-full-polish-continuous--4676c2d5-a427-43a6-a820-c28dd0f78979`\n")
    f.write("Lane HEAD == main HEAD: `4ccee99` (clean checkout, no harness diff)\n\n")
    f.write("## Ledger rows\n\n")
    f.write("| # | Label | Status | Note | Owner action |\n")
    f.write("|---|-------|--------|------|--------------|\n")
    for i, (label, status, note, action) in enumerate(rows, 1):
        note_clean = note.replace("\n", " ").replace("|", "\\|")
        action_clean = action.replace("\n", " ").replace("|", "\\|")
        f.write(f"| {i} | {label} | {status} | {note_clean} | {action_clean} |\n")
    f.write("\n")
    f.write("## Status legend\n\n")
    f.write("- **VERIFIED** — checked this session with evidence\n")
    f.write("- **LAND** — order-aware landing plan (gated writes only when gates pass)\n")
    f.write("- **PARK** — owner decision/answer needed; move on; record need\n")
    f.write("- **BLOCKED** — hard blocker with exact owner action\n")
    f.write("- **LOCAL-ONLY** — branch/worktree not yet pulled/landed\n")
    f.write("- **SKIP** — deliberately not done this run (rule-bound)\n\n")
    f.write("## Stop conditions\n\n")
    f.write("- Full completion, OR\n")
    f.write("- Full pass with zero progress (every remaining item needs an answer, "
            "an owner action, or is hard-blocked).\n\n")
    f.write("End-of-night report to the harness tracking thread with this ledger + "
            "Jev audit summary. Never claim completion for parked/blocked items.\n")

print("Wrote", OUT)
print("Rows:", len(rows))
