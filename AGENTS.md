# Harness — agent context

**Read this first.** Then read the canon. Tool-neutral: Claude Code (primary
lane) loads this via `CLAUDE.md`; any other agent reads it directly.

## Single source of truth

| File | Role |
|---|---|
| **`docs/jev-roadmap.md`** | **CANON** — STATUS, tracker, next slice, DoD. The only operational plan. |
| `CLAUDE.md` | Claude Code operating notes (model tiering, skills, gates, MCP) |
| `docs/claude-context.md` | Agent-lane context pack: files, trust/permissions, launch recipes, legacy Freebuff notes |
| `docs/jev-mission-prompt.md` | Mission prompts (`/isolated-mission`, headless `claude -p`) |
| `docs/system-one-integration.md` | Architecture rationale only (not STATUS) |
| `docs/MODEL_SELECTION_HANDOFF_2026-09-13.md` | Model policy evidence |
| `.agents/skills/typesafe-ai/SKILL.md` | TypeSafe / System One skill (read before touching `jev*.py`) |
| `.claude/skills/`, `.claude/agents/` | `/isolated-mission`, `/isolated-request`, seats `/cto` `/ceo` `/bod`; scout / implementer / verifier tiers |
| `HANDOFF/{CTO,CEO,BOD}_STATE.md` | Seat state (read at seat resume; updated at session close) — not plans |

If a local `docs/jev-roadmap.md` disagrees with `origin/main`, **fetch origin** —
origin wins. The operator tree (`main`) may lag; reconcile before trusting STATUS.

## Current mission (truth 2026-09-22 — details in canon STATUS)

| Item | Truth |
|---|---|
| Shipped | JEV-P0..P6, JEV-COMPLETION, JEV-P5, JEV-LOG-*, HUL-A..D, HG-*, SITE-* — all merged (HG reopened by dogfood: `DF-HG-1/2`) (latest PR #65 `1936ed4`); `main` CI + local audit BAR MET |
| Open | rows marked **open** in canon STATUS (MS-*, Exit rows, the Claude-lane/Jev-bar rows, dogfood follow-ups) — take the first one in "Next implementation slice" |
| Completion rule | STATUS may say complete only when `harness jev-phase --phase <ID>` passes the bar (hard gates + sentiment buckets) **and** CI is green on the merge |
| `JEV-P2-jury` | **deferred** by operator ruling — do not invent it to look complete |
| Do not | redo shipped phases; mark complete while audit/CI red; merge red CI; hardcode provider brands in phase code |

## Worktrees

`git worktree list` is authoritative. Convention: one worktree per phase PR at
`C:\Users\SCM\Documents\GitHub\Harness-<slug>` on its feature branch, created
off `origin/main`; remove it after the PR merges. The operator tree
`C:\Users\SCM\Documents\GitHub\Harness` stays on `main`. Worktrees whose tip is
already an ancestor of `origin/main` are historical — leave them alone or prune.
`.freebuff/worktrees/*` are legacy Freebuff session trees (gitignored).

## Model tracking policy (operator ruling 2026-09-21)

Hermetic unit tests stay hermetic (`test/model` doubles) — they prove contracts, not provider quality.

**Live tracking / dogfood / smoke** must not be free-tier-only (`*:free`, especially ling). Use **cheap paid** rungs first so cost, parseability, and escalation are real:

| Seat | Prefer (paid cheap) | Avoid as sole live evidence |
|---|---|---|
| Apply primary | `deepseek/deepseek-v4.1-flash` | `inclusionai/ling-3.0-flash-fin:free` |
| Judge | `z-ai/glm-5.3-flash` | free gemma-only when rate-limited |
| Paid ling (if used) | `inclusionai/ling-3.0-flash` | free `ling-…:free` |
| Escalation | `z-ai/glm-5.3-flash` → `deepseek/deepseek-v4-pro` → `qwen/qwen3.8-max-0902` | free-only ladder |

Live probe snapshot (2026-09-21, OpenRouter fusion key, small “OK” chat):

| Model | Status | Notes |
|---|---|---|
| `inclusionai/ling-3.0-flash-fin:free` | 200 / $0 | Free baseline only |
| `google/gemma-4-31b-it:free` | **429** | Free pool unstable — do not gate tracking on it |
| `inclusionai/ling-3.0-flash` | 200 / ~$0.0000007 | Cheapest paid ling |
| `deepseek/deepseek-v4.1-flash` | 200 / ~$0.000002 | Default paid apply |
| `z-ai/glm-5.3-flash` | 200 / ~$0.00001 (reasoning-off retry) | Default paid judge |
| `qwen/qwen3.8-max-0902` | 200 / ~$0.00029 | Frontier rung — use sparingly |

Operator harness config (`~/.config/harness/config.json`) already arms paid escalation. For tracking runs prefer paid apply/judge via env or config (`HARNESS_USE_FREE=false`, paid pools in `harness/config.py`). The OpenRouter key has a small daily limit (≈$0.75 on 2026-09-22) — keep probes tiny.

The model policy above governs **Harness's own lanes**. The policy for the
coding agent's own tiers (Opus orchestrates; Haiku/Sonnet do the work) is in `CLAUDE.md`.

## Rules every agent keeps

1. Canon STATUS only — no parallel plans; update existing docs instead of adding new ones.
2. One phase PR at a time; merge only when **local gates + CI audit** green.
3. Builder ≠ sole grader; fail ≠ approve; no fake complete.
4. No provider brand hardcoding in phase code PRs — resolve via ladders / `MS-*`.
5. Do not game `audits/self/coverage_baseline.json` to hide untested new lines; refresh only after real tests execute those lines.
6. Report blocked with exact command/output — never “will do”.
