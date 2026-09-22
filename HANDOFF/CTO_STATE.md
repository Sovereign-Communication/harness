# CTO_STATE — Harness

**Updated:** 2026-09-22 (Claude Code session; operator API limit at ~20%, session parked safely)
**Canon:** `docs/jev-roadmap.md` — STATUS, "Next implementation slice", `DF-*` rows. This file is seat state, not a plan.

## Live state at park

| Item | State |
|---|---|
| `origin/main` | `a9b58ab` (PR #66 CLAUDE-LANE & PR #67 JEV-BAR merged); all tests OK, ruff clean, audit BAR MET 10/10/10/10 |
| Shipped | **PR #66** (`CLAUDE-LANE`) merged `6f00d38`; **PR #67** (`JEV-BAR-*`) merged `a9b58ab` |
| Current slice | Item 3: **HG repair PR** (`DF-HG-1`, `DF-HG-2` HIGH, `DF-HG-3`) |
| Local MCP | `harness` (local scope) protocol negotiation fixed in PR #66; remove dev worktree pointer if present: `claude mcp remove harness-dev -s local` |
| Operator one-time | accept the Claude Code workspace trust dialog in the repo (project allow rules are ignored until then) |

## Next actions (in order)

1. Drive HG repair PR: `DF-HG-1` (composed ceiling `--task-max-cost`), `DF-HG-2` (cold-start pyramid resume), `DF-HG-3` (decompose retry & preview fallback).
2. Run gates and dogfood: `tests/test_hg_*.py`, audit BAR MET, live plan dogfood, `harness jev-phase --phase HG`.
3. Continue "Next implementation slice" items 4–10.

## Blockers

None hard. Soft: OpenRouter daily key limit ~$0.75 (probes must stay tiny); workspace trust not yet accepted.
