"""Phase 0 RECONCILE: derive the overnight ledger from a fresh-ish snapshot.

Fresh snapshot from the prompt:
  main   = 4ccee99  (PR #411 merge)
  open   = 74 issues (63 + new #420-430)
  PR     = 1 open: #431 DRAFT, head 114e8101, branch
             freebuff/audit-this-harness-request-in-full-331d44a9-d477-4530-924b-ce019bc59e3d
  closed = #288  (closed); #290 open
  unrecorded = #407 revocation decision absent
  local-only branches = codex/290-triage-runbook, codex/issue-290-planning-reconcile
  local-only dirty     = capability fallback fix + post_session_audit_comments.ps1
  gate-v2-implementation worktree = plans branch PR #397 head b8917b91
  runner changes UNCOMMITTED in last session (lucas_gate.py, mcp.py,
             mcp_schemas.py, tests/test_lucas_gate_v2.py) -- confirmed ABSENT here.

This lane's tree (verified):
  branch : freebuff/harness-overnight-burndown-full-polish-continuous--4676c2d5-...
  dirty  : check_tools.py (mine), docs/ledger-reconcil-2026-10-28.md (mine)
  nothing else committed or uncommitted here.

Order: reconcile first, then ledger, then MOVE ON. Do not block the night
on the profile yet -- record its request in the ledger and keep going."""
import subprocess, re, json, os, datetime

def gh(path, silent=False):
    try:
        out = subprocess.check_output(["gh", *path], text=True, stderr=subprocess.STDOUT)
        return out
    except subprocess.CalledProcessError as e:
        if not silent:
            print("gh failed for", path, "->", e.returncode)
            print(e.stdout or e.stderr)
        return ""

def branch_tip(br):
    out = gh(["rev-parse", "--verify", br + "^{commit}"], silent=True)
    return out.strip().split("\n")[-1] if out else "NOT-FOUND"

def branch_remote_tip(br):
    out = gh(["ls-remote", "origin", br], silent=True)
    m = re.search(rb"([0-9a-f]{40})\s+HEAD", out.encode() or b"")
    return m.group(1).decode() if m else "NOT-FOUND"

def commit_subject(sha):
    out = gh(["log", "-1", "--format=%s", sha], silent=True)
    return out.strip().split("\n")[-1] if out.strip() else "???"

def worktree_summary():
    out = gh(["worktree", "list"], silent=True)
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*([^ ]+)\s+([^ ]+)(?:\s+\(bare\))?(?:\s+\(detached HEAD ([0-9a-f]{7,40})\))?\s*$", line)
        if m:
            rows.append(dict(path=m.group(1), branch=m.group(2),
                             detached_head=m.group(3) or "attached"))
    return rows

def current_branch():
    return gh(["branch", "--show-current"], silent=True).strip()

def head_commit(br=None):
    b = br or current_branch()
    out = gh(["rev-parse", "HEAD"], silent=True)
    return out.strip()

def files_on_head_vs_main():
    """Files that differ between HEAD and main (intent of 'what's new here')."""
    out = gh(["diff", "main..HEAD", "--name-only"], silent=True)
    return [l.strip() for l in out.splitlines() if l.strip()]

# ---- Gather facts ----
now = datetime.datetime.now().strftime("%Y-%m-%d")
ledger_rows = []  # each: dict(label, status, note, owner_action)

tip = head_commit()
branch = current_branch()
print("Lane branch:", branch)
print("Lane HEAD:", tip[:12], "subject:", commit_subject(tip)[:80])
print("Files vs main:", files_on_head_vs_main() or "(no diff)")
print("Worktrees:", json.dumps(worktree_summary(), indent=1))
print()

# Open PRs
prs = gh(["pr", "list", "--state", "open", "--json", "number,title,headRefName,headRefName,url,state"])
print("Open PRs (raw):")
print(prs[:1500])
print()

# ---- Build ledger (harmonised with the prompt's inventory) ----

# 1. Open issues (the prompt lists 74; we cannot fetch all without pagination,
#    so we record the count and the explicitly-named ones, and mark the rest
#    as needing per-issue disposition during BURNDOWN).
open_issue_count = 74
named_open = [
    "#277", "#290", "#292", "#293", "#294", "#299", "#301", "#302", "#303",
    "#304", "#308", "#309", "#313", "#314", "#318", "#319", "#325", "#333",
    "#335", "#336", "#337", "#340", "#342", "#343", "#344", "#345", "#346",
    "#377", "#378", "#379", "#380", "#381", "#382",
    "#407", "#409", "#411",
    "#412", "#413", "#414", "#415", "#416", "#417", "#418", "#419",
    "#420", "#421", "#422", "#423", "#424", "#425", "#426", "#427", "#428",
    "#429", "#430", "#431",
]

ledger_rows.append(dict(
    label="OPEN ISSUES (total {})".format(open_issue_count),
    status="LEDGER",
    note="prompt states 74 open. Per-issue disposition deferred to BURNDOWN sweep "
         "(Phase 2); skip #333 (landed via #398); verify #288 closed before #290.",
    owner_action=""))

# 2. Open PR #431
ledger_rows.append(dict(
    label="#431 DRAFT (freebuff/audit-this-harness-request-in-full-331d44a9...)",
    status="PARK",
    note="Draft only; never merge while draft. Scope + head + CI to be verified "
         "fresh (check-runs, not combined status). If it is the Jev-research "
         "reconciliation, fold into ledger; otherwise ready ONLY with gate pass "
         "+ green CI.",
    owner_action=""))

# 3. Unrecorded revocation decision on #407
ledger_rows.append(dict(
    label="#407 revocation decision (0 comments, unrecorded)",
    status="PARK",
    note="Decision FIRST per prompt #39: verify #407 comments; if still absent, "
         "gated comment only if gate passes; otherwise defer. Opt-in only, aggregates "
         "only, sanitization documented + hermetic-tested. Revoking opt-in STOPS "
         "future submissions; contributions already in published aggregates STAY.",
    owner_action="record revocation decision (or confirm already present)"))

# 4. Local-only #290 runbook branch
b290 = "codex/290-triage-runbook"
t290 = branch_tip(b290)
r290 = branch_remote_tip(b290)
ledger_rows.append(dict(
    label="local branch codex/290-triage-runbook (tip {} vs remote {})".format(t290[:12], r290[:12]),
    status="LOCAL-ONLY",
    note="Only docs/audit-triage.md modified. R13 timeout cause explicitly unresolved "
         "(allowed, must stay documented). Commit any uncommitted pruning first; "
         "focused docs tests pass; verify contributor/audit-doc link post-merge. "
         "Mark #290 complete ONLY with a declared close keyword in PR body after "
         "acceptance verified.",
    owner_action=""))

# 5. Local-only planning reconciliation branch
bp = "codex/issue-290-planning-reconcile"
tp = branch_tip(bp)
rp = branch_remote_tip(bp)
ledger_rows.append(dict(
    label="local branch codex/issue-290-planning-reconcile (tip {} vs remote {})".format(tp[:12], rp[:12]),
    status="LOCAL-ONLY",
    note="Roadmap + queue + README. LAND ONLY after overlap check vs #288 bookkeeping "
         "diff: if same files, reconcile into one coherent sequence (rebase, never "
         "clobber). Report chosen order with evidence.",
    owner_action=""))

# 6. Gate-v2-implementation worktree (plans branch PR #397)
ledger_rows.append(dict(
    label="worktree: gate-v2-implementation (plans branch PR #397 head b8917b91)",
    status="INVENTORY",
    note="Check for overlap/duplication with the Lucas Gate runner changes BEFORE "
         "landing either. Last session lane had runner changes UNCOMMITTED; this lane "
         "does NOT have lucas_gate.py / mcp_schemas.py update / test_lucas_gate_v2.py.",
    owner_action="verify overlap; do not land duplicated work"))

# 7. Capability fallback fix (untouched per prompt)
ledger_rows.append(dict(
    label="capability fallback fix (untouched)",
    status="NOT-CHANGED",
    note="Must land as its own PR: nonempty stale snapshot survives provider errors "
         "and empty catalogs; empty registry retried, never saved as fresh; "
         "cancellation and spend-limit errors propagate; full capability suite + lint "
         "+ wheel build.",
    owner_action=""))

# 8. post_session_audit_comments.ps1 (unrun, must stay unrun)
ledger_rows.append(dict(
    label="post_session_audit_comments.ps1 (unrun)",
    status="UNRUN",
    note="MUST STAY UNRUN -- audit comments already posted from the tracking thread. "
         "Never re-post or re-run.",
    owner_action="do not run"))

# 9. Issue #306 (blocked, untouched)
ledger_rows.append(dict(
    label="#306 (blocked, blocker intact)",
    status="BLOCKED",
    note="One-shot Fireworks probe returned HTTP 200 with contains_ok=false; marker "
         "prohibits another call. NEVER rerun probe, NEVER delete marker, NEVER mark "
         "complete. Only unblocker: operator decision on non-repeating remedy or "
         "another already-existing valid receipt.",
    owner_action="operator decision on non-repeating remedy"))

# 10. Profile absent
ledger_rows.append(dict(
    label="Lucas v2 profile ABSENT from this lane",
    status="BLOCKED",
    note="Locate at its documented config location (find LUCAS_PROFILE_PATH in the "
         "gate implementation plan). If absent, REQUEST from operator and record in "
         "ledger. NEVER commit, paste, or print. Load locally only. Synthetic scorecard "
         "pass is NOT a semantic gate pass. DEGRADED MODE if absent overnight: ledger + "
         "local verification + code/tests/docs ONLY; ZERO gate-dependent GitHub writes "
         "(queue, never post ungated).",
    owner_action=""))

# 11. MCP catalog mismatch
ledger_rows.append(dict(
    label="MCP connected catalog lists 30 tools (no lucas_gate_v2)",
    status="BLOCKED",
    note="Prompt: fresh stdio lists 31 incl lucas_gate_v2, connected MCP still 30. "
         "Verify BOTH before any gate-dependent write. Must close the gap. If only "
         "pack-constrained tools available, report tool-fit defer; do not force numbers.",
    owner_action=""))

# 12. Ledger file created by previous session (mine to verify / refresh)
ledger_rows.append(dict(
    label="docs/ledger-reconcil-2026-10-28.md (previous session)",
    status="VERIFY",
    note="Previous session wrote this file. Verify against fresh state above; do not "
         "trust blindly; do not rebuild blindly. If stale, refresh.",
    owner_action="verify / refresh"))

# Write the ledger
out_path = "docs/overnight-ledger-2026-10-28.md"
with open(out_path, "w") as f:
    f.write("# Overnight burndown ledger — 2026-10-28\n\n")
    f.write("Lane branch: `{}`\n".format(branch))
    f.write("Lane HEAD: `{}` — {}\n\n".format(tip[:12], commit_subject(tip)))
    f.write("Fresh snapshot reconciled from prompt (2026-10-28).\n\n")
    f.write("## Ledger rows\n\n")
    f.write("| Label | Status | Note | Owner action |\n")
    f.write("|---|---|---|---|\n")
    for r in ledger_rows:
        note = r["note"].replace("\n", " ").replace("|", "\\|")
        f.write("| {} | {} | {} | {} |\n".format(
            r["label"], r["status"], note, r["owner_action"]))
    f.write("\n## Status legend\n\n")
    f.write("- **LEDGER** — counted; disposition deferred to BURNDOWN sweep\n")
    f.write("- **PARK** — owner decision or answer needed; move on\n")
    f.write("- **BLOCKED** — hard blocker; exact owner action named\n")
    f.write("- **LOCAL-ONLY** — branch/worktree not yet pulled/landed\n")
    f.write("- **INVENTORY** — needs overlap check before landing\n")
    f.write("- **NOT-CHANGED** — untouched this session, preserved as-is\n")
    f.write("- **UNRUN** — must stay unrun (audit comments already posted)\n")
    f.write("- **VERIFY** — needs verification against fresh state\n\n")
    f.write("## Stop conditions\n\n")
    f.write("- Full completion, OR\n")
    f.write("- Full pass with zero progress (every remaining item needs an answer, "
            "an owner action, or is hard-blocked).\n\n")
    f.write("End-of-night report to the harness tracking thread with this ledger + "
            "Jev audit summary. Never claim completion for parked/blocked items.\n")

print("Wrote", out_path)
