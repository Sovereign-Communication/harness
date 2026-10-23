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

## Current mission (truth 2026-09-24 — details in canon STATUS)

Claude, Codex, and Freebuff use `docs/jev-roadmap.md` as the shared operational canon. Lane-specific handoffs are seat state, not parallel plans. Every change, including STATUS and docs, goes through a reviewed PR with local gates and green CI; keep one phase PR active at a time and never push directly to `main`.

| Item | Truth |
|---|---|
| Shipped | Every canon `complete` row listed in [docs/jev-roadmap.md](docs/jev-roadmap.md), reconciled against merged `main` on 2026-10-04 (post PR #171 `d78319f`). Every `HV-*` row is complete (`HV-0` PR #93+#97; `HV-1` PR #98 landed with #101 plus calling-lane wiring in PR #153 `aa09a97`; `HV-2`/`HV-3` PR #99/#100 landed with #101; `HV-4` PR #118/#134/#136/#140; `HV-5` PR #146 `f5b53ff`; `HV-6` PR #148 `e5809a6b`), and all five `GAP-*` rows #154 and #156 delivered are now flipped (`GAP-diff-auth`, `GAP-consent-stale`, `GAP-waist-degrade`, `GAP-judge-fallback`, `GAP-token-kinds`), as is `JEV-P4-residual` (PR #130 for the freeze face, PR #157 `563d27e` for the A/B receipt) and the first Exit row. Also merged: PR #157 (`v0.4.2`), #158 (`DYN-ALLOC`), #163+#167 (`EV-0` and the committed-receipt discount gate), #160-#162 and #164-#166 (the recovered-WIP train: canon/DRV-1 correction, driver panes, strict dogfood scoring, repo cards, Jev core hardening, provisioning core), and #169/#170/#171 (`harness/jev_calibration.py`, the chat-lane fixes, and dynamic model ranking wired into rotation). PR #86 Ling rotation (`a2cbb21`), PR #87 media adapter (`8631ecad`), and PR #88 Freebuff answer lifecycle (`65da7b1`) remain merged; PR #88 was only a partial `HV-1` slice and #153 finished it. See the dated ledger and rows in the canon. |
| Open | Rows marked **open** in canon STATUS — the canon's *“Next implementation slice” item 19* is the derived, ordered list; take it from there rather than from any summary here. In short: `PROVISION-CORE` (merged core with **zero** callers or surfaces — PR #166 `24057b1`); `DRV-1` (one blocker left, `dogfood_missing` — a paid-cheap live dogfood receipt; `harness jev-phase --phase DRV-1` returns 84.99 with every CI and merge gate green); `DRV-2` (every synthetic input action is declared and always refused — nothing registers a backend behind `driver_core.osal.register_input_backend`); `OC-HANDOFF`; `GAP-plan-http` (operator ruling); `EV-0` (only the `EV-0a` live probe receipt is missing; `jev-phase --phase EV-0` → 35.0/85.0); `GAP-jury` / `JEV-P2-jury` (deferred by operator ruling — do not invent it); `CIVICSCOPE-COMPLETION` (registered in `PHASE_CONTRACTS`, scores 0); and the open halves of `REPO-CARDS` (no `harness`/CI face) and `JEV-P3-calibration-analysis` (no production caller). **Not open:** `JEV-P4-residual`, `JEV-P6` stage C, the first Exit row, and every `HV-*` row. PR #73 is the in-scope experimental OC handoff lane (OC-HANDOFF); Harness-owned changes may write findings only to HANDOFF/OC_FINDINGS.md. Keep all work inside this Harness repository; do not change external instances or configuration. |
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
coding agent's own delegation (no static model tiers) is in `CLAUDE.md`.

## Rules every agent keeps

1. Canon STATUS only — no parallel plans; update existing docs instead of adding new ones.
2. One phase PR at a time; merge only when **local gates + CI audit** green.
3. Builder ≠ sole grader; fail ≠ approve; no fake complete.
4. No provider brand hardcoding in phase code PRs — resolve via ladders / `MS-*`.
5. Do not game `audits/self/coverage_baseline.json` to hide untested new lines; refresh only after real tests execute those lines.
6. Report blocked with exact command/output — never “will do”.
7. Every change to `main` — STATUS and docs included — lands through a PR with green CI; never push directly to `main` (2026-09-22: direct STATUS pushes left `main` red, `DF-GOV-1`).
