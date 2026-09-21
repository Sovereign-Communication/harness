# Harness — agent / Freebuff context

**Read this first.** Then read the canon.

## Single source of truth

| File | Role |
|---|---|
| **`docs/jev-roadmap.md`** | **CANON** — STATUS, DoD, playbooks. Only operational plan. |
| `docs/jev-mission-prompt.md` | Paste prompts for Freebuff relaunch (current = **P2 repair**) |
| `docs/freebuff-context.md` | Worktrees, model tracking policy, operator notes |
| `docs/system-one-integration.md` | Architecture rationale only (not STATUS) |
| `docs/MODEL_SELECTION_HANDOFF_2026-09-13.md` | Model policy evidence |
| `.agents/skills/typesafe-ai/SKILL.md` | TypeSafe / System One skill |

If a local dirty `docs/jev-roadmap.md` disagrees with `origin/main`, **fetch origin** — origin wins. A stale STATUS that still says “P1 incomplete / no PR” is **wrong**: P1 merged as PR #35 (`9d5ff14`).

## Current mission (do not guess)

| Item | Truth |
|---|---|
| P0 + P1 + P2 | **complete** — PR #34 / #35 / **#36 merged `405bbc1`**; post-merge `main` CI green |
| Next code | **P3 utilization** (`Harness-jev-p3`) and/or **HUL-A** (`Harness-hul-a`) and/or **HG composition** (`Harness-hg-remain`) |
| Also planned | `JEV-P5` issue-sort buckets; `JEV-P4` ops/exit; HUL-B/C/D; MS cheapest-capable + context condensation |
| Do not | Redo P0–P2; edit `Harness-jev-p1`/`Harness-jev-p2` (locked); mark complete while audit/CI red; merge red CI; brand hardcode in phase code |
| `JEV-P2-jury` | **deferred** — not blocking anything above |

**Next work:** implement first incomplete STATUS row with a worktree — P3 patterns, HUL-A mission pack, or HG composition per operator priority. Dogfood every user-facing lane. Paid cheap rungs for live evidence.

## Worktrees

| Path | Branch | Use |
|---|---|---|
| `C:\Users\SCM\Documents\GitHub\Harness` | `main` | Operator tree — may lag origin; reconcile before trusting STATUS |
| `...\Harness-jev-p1` | `feat/jev-p1-policy-and-lanes` | Merged P1 — **leave alone** |
| `...\Harness-jev-p2` | `feat/jev-p2-system-one-pillars` | Merged P2 (#36) — **leave alone** |
| `...\Harness-jev-p0` | `feat/jev-p0` | Historical P0 — leave alone |
| `...\Harness-jev-p3` | `feat/jev-p3-utilization` | **P3 utilization WIP** |
| `...\Harness-hul-a` | `feat/hul-a-mission-pack` | **HUL-A WIP** |
| `...\Harness-hg-remain` | `feat/hourglass-composition` | **Hourglass composition WIP** |
| `...\Harness-jev-next` | `feat/jev-mission-next` | Docs/STATUS promote + next slices |

Freebuff project id: `.freebuff/project-id` → `079c1c19-eefd-49c1-b243-5cecf83ea4b6` (desktop project binds to this repo path).

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

Operator harness config (`~/.config/harness/config.json`) already arms paid escalation. For tracking runs prefer paid apply/judge via env or config (`HARNESS_USE_FREE=false`, paid pools in `harness/config.py`).

## Rules Freebuff must keep

1. Canon STATUS only — no parallel plans.
2. One phase PR at a time; merge only when **local gates + CI audit** green.
3. Builder ≠ sole grader; fail ≠ approve; no fake complete.
4. No provider brand hardcoding in phase code PRs — resolve via ladders / `MS-*`.
5. Do not game `audits/self/coverage_baseline.json` to hide untested new lines; refresh only after real tests execute those lines.
6. Report blocked with exact command/output — never “will do”.
