# Consolidation index — unify all work on main, verified (proposal, 2026-10-14)

**Status: PROPOSAL — not canon.** `docs/jev-roadmap.md` is the sole canon; one-phase-PR rule holds. Canon changes: [CANON_ADDENDUM](HANDOFF/CANON_ADDENDUM_2026-10-14.md) (apply post-#220). Tree verdicts: [WORKTREE_DISPOSITIONS](HANDOFF/WORKTREE_DISPOSITIONS_2026-10-14.md). Details live once, in those files — this index only orders.
**Base:** `origin/main` = `6cc8b87` (PR #213). PR #220 OPEN/MERGEABLE (active); PR #212 DRAFT/HELD.

| Seq | Lane | Sources | Acceptance gate (prove before merge) |
|---|---|---|---|
| 0 | Merge #220 | `codex/final-burndown-plan` (2 docs commits) | Review + green CI, green post-merge `main` |
| 1 | #207 lifecycle | PROMOTE rows (committed `8be844f`/`66546b7`/`403fd3c` fastpath line + dirty `simple_request.py` set) + #200–#203 | GUI+CLI+MCP replays: 1 probe, 1 Noul, `Yes.`, zero model calls; Jev retained (≤2 calls) + outage fallback; `IncidentReplayBudgetTests` + live receipt |
| 2 | #215 no-escalation | PROMOTE rows (dirty `cli_parser.py` set) + canon EDIT 2 | `--no-escalation` flag; ceiling replay zero over-budget; `test_cli`/`test_escalation` green |
| 3 | #219 weather P0 | `/tmp/pr213-rebase` insight only (owner: mission executor, deadline: next session — `/tmp` is volatile) | Worker provenance + removed-checkout/Windows regression; exact-prompt replay |
| 4 | #179 slices | KEEP-PARKED `6269636` (DF-CLI-4), `87a672e` (DF-AUDIT-6, never wholesale) | Phase contract + `max_tokens>0` proof tests; JSON stdout unchanged |
| 5 | EV chain | HOLD `feat/ev7-fireworks-lane` (= #212) + #216 | Gates per canon EDIT 3 |
| 6 | Audit program | #181 serial (#182 → #183 → #184–186 → #187/188 → #190/191 → #192) | Per-row gates; never Jev-score-gate code changes |
| 7 | Close-out | #177, #211, #195–#199/#202 | Per canon EDIT 4 + dogfood matrix + green `main` |

Global: local gates + required PR checks green; `harness jev-phase` bar for any STATUS flip; no baseline hand-edits; no provider hardcoding.

## Self-gate (heuristic, not live certification): 9/10 overall

Completeness 9 (long tail now per-tree rows) · Correctness 9 (states/anchors live-probed; COMPARE-PRUNE needs hunk diffs at execution) · Alignment 9 (one-PR rule kept) · Verifiability 9 (live receipts pending) · No-orphan 9 (`86ac1455` untracked files await owner classification).
