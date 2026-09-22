# Pointer — use the canon

**Canonical plan (only):** [jev-roadmap.md](jev-roadmap.md) — `Harness Mission Canon — Jev + Until-Limits`

**Mission prompt (Claude Code `/isolated-mission`):** [jev-mission-prompt.md](jev-mission-prompt.md)

**Architecture only (non-operational):** [system-one-integration.md](system-one-integration.md)

This file is **not** a plan. It holds no STATUS, DoD, or schedule.  
If anything here ever disagrees with `docs/jev-roadmap.md`, **the canon wins**.

---

## Freebuff Mission facts (legacy lane, retired 2026-09-22 — history only)

Observed 2026-09-20 on this machine:

| Mechanism | Role |
|---|---|
| `~/.config/freebuff-desktop/state.json` → `uiPrefs.savedMissions` | Saved long mission prompts |
| `uiPrefs.missionEffort` | Default **5** |
| Project DB `threads.auto_run*` | Live campaign (prompt, effort, pass/decision counts) |
| `auto_run_decision_receipts` | Manager-generated next steps (`/merge-pr`, `/test`, …) |
| `queue_items` source `mission-required` | Injected audits / recovery |
| `thread_deliveries` | PR merge outcomes |

**Why Jev stopped after P0:** mission text lacked an explicit multi-phase STATUS target; thread showed `auto_run=1` but `pass_count=0` / `decision_count=0` after P0 merged.

**Mitigation:** short prompt → **canon** `docs/jev-roadmap.md` STATUS + “do not stop after Phase 0 / until-limits + FINDINGS.”

Successful historical missions ran many decision passes until a numeric goal; HUL/Jev missions must keep relaunching until **canon Exit rows** are complete or an open-problem pack hits an honest terminal with findings.
