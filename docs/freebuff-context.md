# Freebuff context pack — Harness Jev mission

This file is the **operator/agent context pack** for Freebuff Codebuff sessions on this repo. Canonical STATUS still lives only in [jev-roadmap.md](jev-roadmap.md).

## Context files checklist

| Path | Status | Purpose |
|---|---|---|
| `AGENTS.md` (repo root) | **required** — injected when Freebuff `injectAgentsMd=true` | Mission truth, worktrees, model policy |
| `.freebuff/project-id` | present | Desktop project binding |
| `docs/jev-roadmap.md` | **CANON** on `origin/main` | STATUS + playbooks |
| `docs/jev-mission-prompt.md` | on `origin/main` | RE-LAUNCH prompts |
| `docs/system-one-integration.md` | on `origin/main` | Architecture; milestones map to JEV-Pn |
| `.agents/skills/typesafe-ai/SKILL.md` | present | TypeSafe skill |
| Freebuff desktop `auto_run_prompt` | **must match** current phase | See below |

### Stale local-main trap

Operator `main` worktree may still have **uncommitted** old STATUS text claiming P1 incomplete. Freebuff cwd is often that main tree. **Always:**

```powershell
git -C C:\Users\SCM\Documents\GitHub\Harness fetch origin
git -C C:\Users\SCM\Documents\GitHub\Harness show origin/main:docs/jev-roadmap.md
```

Do not implement from dirty local STATUS.

## Freebuff desktop auto-run prompt (update when phase changes)

Current intended prompt is **P2 repair only** (see `docs/jev-mission-prompt.md` → `RELAUNCH P2 repair`).  
Older auto-run text that still says “P1 in progress — finish feat/jev-p1-policy-and-lanes” is **obsolete** and will re-steer the implementer into shipped work.

When editing Freebuff saved missions / auto-run:

1. Open the Harness Freebuff thread (project path = this repo).
2. Replace auto-run / mission prompt with the P2 repair paste from `docs/jev-mission-prompt.md`.
3. Keep `model` on a **cheap paid** rung for implementer turns if the session spends (e.g. `z-ai/glm-5.3-flash` / gpt-5-mini class) — not free-only ling if tracking quality matters.
4. Do **not** bind the thread to `Harness-jev-p1`. Target worktree: `Harness-jev-p2`.

## Model tracking (live evidence)

See `AGENTS.md` § Model tracking policy. Summary:

- Unit tests: hermetic doubles — unchanged.
- Live tracking/smoke/dogfood: **cheap paid** first (`deepseek-v4.1-flash`, `z-ai/glm-5.3-flash`, paid ling), not free ling/gemma alone.
- Free pool is evidence of free-tier fallback only; gemma free was **429** in the 2026-09-21 probe.

Key resolution for OpenRouter: `~/.config/scmorc/openrouter_fusion.env` then `openrouter.env` (harness `resolve_api_key`). Daily key limit may be low — keep probes tiny.

## Session audit notes (2026-09-21)

| Item | Finding |
|---|---|
| Freebuff thread | `7a783017…` “Jev TypeSafe System One Phase 0 Implementation” — still titled Phase 0 but work has moved to P2 |
| Prompt delivery | Operator P2 repair paste landed (msg 902) |
| Work evidence | `Harness-jev-p2` tip advanced to `449a1ce test(jev): refresh P2 coverage baseline` (pushed) |
| STATUS on P2 branch | Updated to **in progress / repair** (good) |
| Coverage baseline | **Repaired with PR #36** — D12 lines executed by real tests; baseline refreshed after green tests; audit BAR MET on merge tip |
| Lane parity | Still expected red on operator machine until hermetic fixtures land |
| PR #36 | Open; do not merge until audit BAR MET + local gates green |
| Context gap | Root `AGENTS.md` was missing until this pack — required for injectAgentsMd |

## Mission loop reminder

1. Read canon STATUS (origin/main).
2. Pick first incomplete row with a playbook (priority: **JEV-COMPLETION** gate, then P2 repair).
3. Implement in the **named worktree only**.
4. Before any STATUS `complete`: run
   `python -m harness.cli jev-phase --phase <JEV-Pn> --repo-root . --local-only`
   and paste `can_mark_complete=true` + score ≥ 85.
5. Gates local + CI → PR → merge green only → STATUS → next phase.
