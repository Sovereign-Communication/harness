# P1 — Harness Jev: completion dogfood gate + P2 repair

> **SUPERSEDED 2026-09-22** — historical Freebuff-lane handoff; every item here has merged (PRs #36–#65). Current truth: `docs/jev-roadmap.md` (canon STATUS). Agent lane: Claude Code (`CLAUDE.md`, `docs/claude-context.md`).

**Status:** OPEN  
**Priority:** P1 (mission accountability; blocks P3/HUL)  
**Filed:** 2026-09-21  
**Repo:** `C:\Users\SCM\Documents\GitHub\Harness`  
**Authority:** `HANDOFF/CTO_HANDOFF_HARNESS_JEV_FREEBUFF_AUDIT_2026-09-21.md`  
**Canon:** `docs/jev-roadmap.md` on **origin/main** (not dirty local STATUS)

## Order (do not reorder)

### 1) JEV-COMPLETION — PR #39

- Worktree: `C:\Users\SCM\Documents\GitHub\Harness-jev-completion`  
- Branch: `feat/jev-phase-completion-score`  
- Feature: `harness jev-phase` — 0–100 score; STATUS complete only if hard gates + score ≥ 85  
- CI: unittest **pass**; **audit FAIL** D12 changed-line coverage  
- Also: make `tests/test_jev_lane_parity.py` hermetic (operator suite FAIL ×2 on settings bleed)  
- Then official `python audits/self/refresh_coverage_baseline.py` **after** suite green — do not game baseline  
- Merge only when **audit BAR MET** + tests green  

### 2) JEV-P2 repair — PR #36

- Worktree: `C:\Users\SCM\Documents\GitHub\Harness-jev-p2`  
- Branch: `feat/jev-p2-system-one-pillars` tip `15b98da`  
- STATUS stays **in progress/repair** until merge  
- Hermetic lane-parity; cover `harness/jev_policy.py` lines **235,239,241,242**  
- Before STATUS complete:  
  `python -m harness.cli jev-phase --phase JEV-P2 --repo-root . --local-only` → **can_mark_complete=true**  
- `JEV-P2-jury` remains **deferred** unless fail-closed implementation lands  

### 3) P3 / HUL — only after 1+2 merge green

## Forbidden

- Redo P1 / touch `Harness-jev-p1`  
- Mark complete without `jev-phase` green  
- Merge red CI  
- Free-tier-only live tracking  
- Trust local dirty `docs/jev-roadmap.md` on operator main  

## Context (origin/main)

- `AGENTS.md`  
- `docs/jev-mission-prompt.md` (RELAUNCH P2 repair)  
- `docs/jev-roadmap.md`  
- `docs/freebuff-context.md`  
- `docs/jev-completion-dogfood.md`  

## Freebuff

Thread `7a783017…` auto-run = **P2 repair only**. Confirm before long auto-runs.

## Dogfood snapshot (2026-09-21)

| Phase | Score | can_mark_complete |
|---|---|---|
| JEV-P1 | 100 | true |
| JEV-P2 | 30 | false |
| JEV-COMPLETION | 30 | false |
