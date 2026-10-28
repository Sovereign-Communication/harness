# CANONICAL INDEX — harness lane docs (same branch)

## CANONICAL (follow this; where docs disagree, this wins)

- `docs/issue-plans/unified-main-prompt.md` — THE driving prompt: mega
  transformer goal, literal full self-audit win, world-class PyPI bar,
  approved hybrid gate, step order, standing constraints, end-state.

## GATE SPEC (read this to build the runner and the shadow job)

- `docs/issue-plans/lucas-gate-v2-harness-implementation.md` — Lucas Gate
  v2 implementation plan, INCLUDING the approved hybrid amendment
  (2026-10-27). Branch copy: references to the private v2 profile file
  mean "the v2 profile held privately by the operator" — the profile
  itself is NEVER published to this public repo. The plan's bars, tracks,
  bias list, and amendment are complete without it.

- `docs/issue-plans/jev-use-instructions.md` — when and how to use Jev for driving decisions, how to read results under v1, and how to save Jev context to memory.

## SHADOW GATE (staged; advisory first, enforce only when verified)

- `docs/issue-plans/shadow-gate/driving-hygiene.py` — staged scanner
  (stdlib only, no model calls, no secrets).
- `docs/issue-plans/shadow-gate/driving-hygiene-job.yml` — job snippet to
  merge into `.github/workflows/ci.yml` (NOT live until a rollout PR
  lands it; advisory via `continue-on-error: true` first).
- `docs/issue-plans/shadow-gate/shadow-gate-rollout-prompt.md` — the
  rollout prompt: advisory → verify → enforce, with a negative control.

## BOOKKEEPING (launch prompt for the #288 receipt-only PR)

- `docs/issue-plans/bookkeeping-pr-launch-prompt.md` — open, fully
  verify, and land the #288 bookkeeping PR. Receipt-only; #306 untouched.
- `docs/issue-plans/bookkeeping-pr-launch-mission.md` — condensed
  mission version of the launch prompt, under 4,000 chars, for mission
  inputs with a character cap. Points to the full prompt above.

## SUPERSEDED (history only — DO NOT FOLLOW)

- `docs/issue-plans/worldwide-push-plan.md` — superseded by the canonical
  prompt. Stale where it disagrees: still instructs building #333 and
  its #306 phase predates #306's block.
- `docs/issue-plans/worldwide-push-mission.md` — superseded; same
  staleness on #333 and #306.
- `docs/issue-plans/burndown-prompt.md` — superseded; corrected
  2026-10-27 for #333 DONE, #306 BLOCKED, #288 DONE, but the canonical
  prompt still wins on any disagreement.
- `docs/issue-plans/burndown-mission.md` — superseded; same corrections.
- `docs/issue-plans/burndown-resume-prompt.md` — superseded point-in-time
  resume doc (2026-10-26 baseline); its steps 1–2 live on as canonical
  Steps 1–2.

## RULES

1. Where any two docs disagree, `unified-main-prompt.md` wins.
2. The v2 voice profile is NEVER published to this repo. Lane scoring
   uses this index's gate spec (bars, tracks, bias list) plus the
   operator-held profile when the operator supplies it privately.
3. No doc in this directory authorizes a merge, a deploy, a spend, or a
   probe rerun on its own — the canonical prompt's constraints and gates
   govern every action.

- `docs/issue-plans/burndown-verify-polish-prompt.md` + `burndown-verify-polish-mission.md` (2026-10-27, FRESH session prompt): burndown/verify/polish run order - bookkeeping, #407 record, #293/#301, capability fix PR, dependency burndown incl. #418/#419 and #412-417 packet, shadow gate, #306 untouched, #277 LAST, #407 build, polish. Mission 2,897 chars, ASCII, under the 4,000 cap. Supersedes prior session prompts for this run; unified-main-prompt.md remains canonical reference.

- `docs/issue-plans/comprehensive-sweep-prompt.md` + `comprehensive-sweep-mission.md` (2026-10-27, FRESH completion sweep): phased sweep - ledger first, profile+MCP unblock (with degraded mode), land in order (#288, runner, #293, #301, capability fix, #290 runbook, planning reconciliation), packet + R13 comparison + #418/#419 drive-down, #277 last, polish. Mission 3,930 chars, ASCII, under the 4,000 cap. Supersedes the burndown-verify-polish prompt for this run; unified-main-prompt.md remains canonical reference.
