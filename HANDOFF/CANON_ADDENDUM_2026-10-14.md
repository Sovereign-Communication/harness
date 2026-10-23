# Canonical addendum — consolidation (APPLY-READY, post-#220 only)

**How to apply:** after PR #220 (`codex/final-burndown-plan`) merges, open the next serial phase PR from fresh `origin/main` and apply the edits below to `docs/jev-roadmap.md`. ANCHORS ARE SEARCH STRINGS, not line numbers (numbers are as-of hints at base `6cc8b87`, 833 lines — #220's +83/−112 shifts them; locate each row by its bold ID text). Do NOT apply while #220 is open (one-phase-PR rule). No STATUS row flips to `complete` here; every flip still needs its `harness jev-phase` bar + green CI.

## EDIT 1 — order row 1 (as-of line 71; anchor: row containing `**#180 / DF-CLI-3**`): #180 landed, #220 is the active PR

Old (anchor row):
`| 1 | **#180 / DF-CLI-3** | Resolve the D12 trace discrepancy on the actual merge base; ... Then merge #180 and confirm green post-merge `main`. |`
New:
`| 1 | **#220 canon refresh (then next slice)** | #180 / DF-CLI-3 is landed. Merge #220 first and confirm green post-merge `main`; then take the next row below as its own reviewed PR from fresh `origin/main`. |`

## EDIT 2 — new rows after row 4 (anchor: row containing `**#179 / pack limits and JEV-P6 prose**`): #207 lifecycle + #215 escalation surface

Insert:
`| 2a | **#207 request lifecycle** | Shared Choice→probe→Noul fast path on GUI/CLI/MCP (retain Jev per 2026-10-13 ruling, ≤2 calls; Jev-outage deterministic fallback). Acceptance: exact-prompt replays, `IncidentReplayBudgetTests`, live dogfood receipt with cost/latency/provenance. Dirty set staged in `simple-request-lifecycle` tree. |`
`| 2b | **#215 CLI escalation surface** | Add `--no-escalation`; document `max_rotations` bounds in-round rotation only, escalation governed by `allow_escalation`. Acceptance: ceiling replay with zero over-budget paid calls + `test_cli`/`test_escalation` green. Dirty set staged in `explicit-no-escalation` tree. |`
Renumber old rows 2–4 (`#179` slices) to 2c–2e; keep their text unchanged.

## EDIT 3 — order row 12 (anchor: row containing `**EV-0 through EV-5**`): record #212 hold

Append to the EV-0..EV-5 cell: `PR #212 (EV-7 draft) is HELD: D10 entry missing, D12 85%<95%, EV-7 must follow EV-4, EV-6 evidence must split from EV-7 routing, 5-item written confirmation required (issue #216). Do not merge #212 until #216 clears.`

## EDIT 4 — order row 15 (anchor: row containing `**#177 hygiene**`): refresh #177 hygiene

Replace the row-15 cell with: `Issue #177 body refreshed post-#213/#214/#220: #180 and #168 closed as landed; list only validated residual hygiene items, then close when resolved.`

## EDIT 5 — branches section (anchor: heading `### Branches and worktrees — disposition`): replace body with pointer

Replace the existing per-branch bullets (stale: references #180 as sole open PR, base `0676d64`) with: `Per-worktree dispositions live in HANDOFF/WORKTREE_DISPOSITIONS_2026-10-14.md (64 worktrees + 14 session dirs, each LANDED/SUPERSEDED/HELD/TO-LAND with SHAs). Prune only the rows marked prune-safe there; keep parked rows until their owning sequence step lands.`
