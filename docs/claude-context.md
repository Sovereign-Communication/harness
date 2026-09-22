# Agent-lane context pack — Claude Code (primary)

This file is the **operator/agent context pack** for coding-agent sessions on
this repo. Claude Code is the primary implementer lane (migrated from the
Freebuff lane on 2026-09-22). Canonical STATUS still lives only in
[jev-roadmap.md](jev-roadmap.md).

## Context files checklist

| Path | Status | Purpose |
|---|---|---|
| `CLAUDE.md` (repo root) | **required** — auto-loaded by Claude Code | Imports `AGENTS.md`; model tiering, missions, gates, MCP |
| `AGENTS.md` (repo root) | **required** | Tool-neutral mission truth, model policy, rules |
| `.claude/settings.json` | present | Project allowlist (hermetic gates, read-only git/gh); denies reads of key files |
| `.claude/skills/isolated-mission/` | present | `/isolated-mission` — tiered mission loop; state = HUL mission pack |
| `.claude/skills/isolated-request/` | present | `/isolated-request` — one fresh `claude -p` session, result + cost |
| `.claude/agents/harness-{scout,implementer,verifier}.md` | present | Haiku / Sonnet / Sonnet delegation tiers |
| `.agents/skills/typesafe-ai/SKILL.md` | present | TypeSafe skill (pinned in `skills-lock.json`) |
| `docs/jev-roadmap.md` | **CANON** on `origin/main` | STATUS + next slice |
| `docs/jev-mission-prompt.md` | on `origin/main` | Mission prompts to paste / run headless |

### Workspace trust (one-time, operator)

Claude Code ignores `.claude/settings.json` allow rules in a workspace whose
trust dialog was never accepted (headless runs print
`Ignoring N permissions.allow entries ... this workspace has not been trusted`).
Open Claude Code interactively in the repo once and accept the dialog. Skill
`allowed-tools` grants still apply; headless callers can also pass
`--allowedTools` explicitly.

### Stale local-main trap

The operator `main` worktree can lag `origin/main`. **Always:**

```powershell
git -C C:\Users\SCM\Documents\GitHub\Harness fetch origin
git -C C:\Users\SCM\Documents\GitHub\Harness show origin/main:docs/jev-roadmap.md
```

Do not implement from a stale local STATUS.

## Launch recipes

| Goal | How |
|---|---|
| Interactive mission on the next open row | `/isolated-mission --iterative --bar <ID> <mission>` (see `docs/jev-mission-prompt.md`) |
| Unattended / scheduled mission | `claude -p --model opus --max-budget-usd <N> "/isolated-mission --bar <ID> ..."` from the repo root |
| Clean-context scan or extraction | `/isolated-request --model haiku <prompt>` |
| Whole-board completion check | `python -m harness.cli jev-phase --all --repo-root . --local-only` |
| Harness as tools inside Claude | local-scope MCP server (`CLAUDE.md` § Harness MCP) |

Opus orchestrates and plans; Haiku scouts; Sonnet implements and verifies.
Mission state (receipts, dual budget, Jev bar evals, FINDINGS) lands in
`tmp/claude/missions/<id>/` in the canonical HUL pack format.

## Model tracking (live evidence)

See `AGENTS.md` § Model tracking policy. Summary:

- Unit tests: hermetic doubles — unchanged.
- Live tracking/smoke/dogfood: **cheap paid** first (`deepseek-v4.1-flash`, `z-ai/glm-5.3-flash`, paid ling), not free ling/gemma alone.
- Free pool is evidence of free-tier fallback only; gemma free was **429** in the 2026-09-21 probe.

Key resolution for OpenRouter: `~/.config/scmorc/openrouter_fusion.env` then
`openrouter.env` (harness `resolve_api_key`). Jev/TypeSafe: `resolve_jev_key()`
(see canon Operator notes). Keys never enter the agent's context — the project
settings deny reads of those files; Harness reads them itself.

## Mission loop reminder

1. Read canon STATUS (origin/main).
2. Pick the first open row in "Next implementation slice".
3. Implement in a dedicated worktree off `origin/main`.
4. Before any STATUS `complete`: run
   `python -m harness.cli jev-phase --phase <ID> --repo-root . --local-only`
   and paste `can_mark_complete=true`; work the `improvements` list until it passes.
5. Gates local + CI → PR → merge green only → STATUS → next row.

## Legacy: Freebuff lane (retired 2026-09-22)

Freebuff/Codebuff drove P0–P6 through saved missions and a desktop auto-run
prompt. Retired in favour of Claude Code; nothing here depends on it.

- `.freebuff/project-id` (`079c1c19-eefd-49c1-b243-5cecf83ea4b6`) and
  `.freebuff/worktrees/*` remain gitignored local state; prune merged worktrees at will.
- Its auto-run / `savedMissions` prompts are superseded by `docs/jev-mission-prompt.md`.
- Earlier revisions of this file (as `docs/freebuff-context.md`) are in git history.
