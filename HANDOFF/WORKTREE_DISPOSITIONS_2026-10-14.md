# Worktree dispositions — every tree accounted for (2026-10-14, base `6cc8b87`)

Evidence: `git worktree list --porcelain` (64 entries) + `git merge-base --is-ancestor <tip> origin/main` (21 LANDED / 35 UNMERGED / 8 DETACHED) + `git log origin/main..<branch>` unique-commit subjects. Verdicts: **PRUNE-SAFE** (on main, remove anytime) · **COMPARE-PRUNE** (content landed via merged PR under another SHA; diff hunk vs main, then prune) · **PROMOTE** (dirty set feeds a sequence step) · **HOLD** · **KEEP-PARKED** · **DROP**.

## Codex trees (`~/.codex/worktrees/`)

| Tree | Tip / branch | Ancestry | Verdict |
|---|---|---|---|
| canon-reconcile-oct05 | `bedd432` / `codex/canon-reconcile-20261005` (5 docs commits) | UNMERGED | PROVEN-SUPERSEDED (diff: docs-only 12+/3- Oct-05 prose — STATUS stamp, EV-0 30.0 remeasure, DF-CLI-3/4/6 rows; substance covered by merged #178 + pending #220). Prune-safe |
| canonical-plan-reconcile | `d9e2ff5` / `codex/canonical-plan-reconcile` (+1 dirty file) | LANDED | Classify dirty file; PRUNE-SAFE after |
| df-audit5-fix | `bbfa7f3` / `codex/hv1-hourglass-integration` | LANDED | PRUNE-SAFE (historical) |
| df-cli-3-verify-defer | `cd53a7e` / `codex/df-cli-3-verify-defer` (4 commits, #180 lane) | UNMERGED | PROVEN-EQUIVALENT (diff: `deferred` 6/5/6× in main `cli.py`/`cli_report.py`/`results.py`; #180 merged). Prune-safe |
| explicit-no-escalation | `6cc8b87` detached + dirty `cli_parser/test_cli/test_escalation` | DETACHED | PROMOTE as seq 2 (#215): stash → rebase successor on post-#220 `origin/main` → unstash → focused tests → push → PR (one-PR rule), then remove tree |
| final-burndown-plan | `1a4c225` / `codex/final-burndown-plan` (2 docs commits) | UNMERGED | = PR #220 body; lands as seq 0 |
| lane3-backlog | `6269636` / `codex/lane3-jev-phase-local-accounting-notice` (1 commit) | UNMERGED | KEEP-PARKED → DF-CLI-4 slice only (seq 4) |
| oc-handoff-jev-gate | `87a672e` / `codex/oc-handoff-jev-gate` (1 commit, spend-settlement abort) | UNMERGED | KEEP-PARKED → DF-AUDIT-6 slice only, never wholesale (seq 4) |
| pr141-civicscope-contract | `26587c2` / `codex/hv5-sovereignty-20260929` (3 commits) | UNMERGED | DROP — superseded by #146 `f5b53ff` (per #179) |
| pr213-verification | `75a6b71` detached + dirty `round2_scores.json` | DETACHED | Revert dirty file (`git checkout --`, post-merge artifact, no PR), remove tree |
| simple-request-lifecycle | `75a6b71` detached + dirty `agent.py/chat.py/jev_packs.py/jev_policy.py/test_jev_answer_lifecycle.py` + new `simple_request.py/test_simple_request_lifecycle.py` | DETACHED | PROMOTE as seq 1 (#207): first cherry-pick committed `8be844f`/`66546b7`/`403fd3c` fastpath line onto the successor branch, then stash → rebase on post-#220 `origin/main` → unstash dirty set → focused tests → push → PR. Exclude `round2_scores.json`. Then remove tree |

## Freebuff trees (`~/.freebuff/worktrees/`, `Documents/GitHub/Harness/.freebuff/`)

| Tree | Tip / branch | Ancestry | Verdict |
|---|---|---|---|
| 428dba2b (this mission) | `6cc8b87` / `freebuff/unify-codex-work-on-main-…` clean | LANDED | Active; holds the three proposal docs |
| 9b081b37 | `6bd04a2` / `freebuff/repo-wide-jev-audit-…` | LANDED | PRUNE-SAFE (tip is on main history) |
| efd322d6 | `7b0fad6` / `freebuff/harness-burndown-…` | LANDED | PRUNE-SAFE (tip is on main history) |
| 2978d197 | `3f12163` / `feat/hv4-stage-composition` (2 wip commits) | UNMERGED | PROVEN-SUPERSEDED (diff: branch `stages.py` absent from main, but stage-composition symbols live in main `waist.py`/`jev_completion.py` + `test_hourglass_stage_composition.py`; design evolved via #118). Prune-safe |
| 298d2bdf (+nested pr194-review) | detached `5a68229` / `15566fe` | DETACHED ×2 | Remove trees (detached snapshots; #194 landed) |
| 381d470c | `a950bbd` / `freebuff/repo-audit-and-final-burndown-…` (2 WIP commits: weather relevance, provider routing) | UNMERGED | KEEP-PARKED → classify vs #177 acceptance; slice or DROP (do not land WIP wholesale) |
| 4e01c2ef | `e8538f9` / `feat/hv1-redundancy-guard` (1 wip: PR-body preservation) | UNMERGED | DROP (diff: single file `scratch-pr-body.md`, zero code; guard itself landed via #126). Prune after skimming the scratch text |
| 650892ae | `0022cbb` / `freebuff/check-issue-177-…` | LANDED | PRUNE-SAFE (merge `5d1262a` lineage) |
| 66d2d4a2 | `03b3621` / `feat/ev0-economics-evidence` (3 commits) | UNMERGED | PROVEN-SUPERSEDED (diff: branch `economics.py` monolith absent, but `discount_gate.py`/`discount_probe.py` + discount hits in `cli/cli_parser/config` on main; layout evolved via #163/#167). Prune-safe |
| 704d1bdf | `5272dcd` / `feat/hv3-budget-wiring` (1 wip hv-4) | UNMERGED | PROVEN-SUPERSEDED (diff: `TokenBudget` 11× in main `waist.py` + 5× in `token_budget.py`). Prune-safe |
| 86ac1455 | `4e2a548` / `freebuff/find-the-claude-wip-…` + untracked `MCP_ISOLATION_BASELINE…`, `tmp_phase_f/` | LANDED | KEEP-PARKED until owner classifies untracked files (canon requirement) |
| 8e0878f7 | `f332e99` / `freebuff/resume-codex-harness-wip-…` (6 commits incl. #207/#204–206 slices) | UNMERGED | PROVEN-EQUIVALENT for lifecycle content (diff: `IncidentReplayBudgetTests`, `simple_action` tier, `test_lifecycle_guards_204_205_206.py` all on main; D12-baseline/audit commits superseded by later main work). Prune-safe. NOTE: exact-prompt fastpath follow-ups are NOT in this line — they live on `fix/issue-207-jev-fastpath` (NEEDS-KEEP above) |
| ef2fdc7d | `4528819` / `mission/lane3-verify-infra` (3 WIP commits) | UNMERGED | DROP — mission PR #120 closed; WIP unreviewed |
| fc321fcd | `bff19c9` / `feat/hv5-execution-handoffs` (6 commits) | UNMERGED | PROVEN-SUPERSEDED (diff: execution-budget + consent-handoff tests and 16 execution symbols in main `agent.py`; #146 merged). Prune-safe |
| .gui-session | `5c915c2` / `backup/gui-session-mixed-wip-20261009` (1 checkpoint) | UNMERGED | PRUNE after confirming spend-limit content in #213/#214 |
| .issue198-prep / .issue198-rebased | `d53f75b` / `b428cb6` / `backup/issue198-*-unverified-*` | UNMERGED | PRUNE (content landed via #213; marked unverified backups) |
| .pr194-hardening | `6dc6a68` / `fix/jev-audit-followups` (12 commits) | UNMERGED | PROVEN-EQUIVALENT (diff: `76e887d` (#194) on main; `_quote_job_id` with dot-segment rejection verbatim in main `media_client.py`; 7 missions-root hits in main `server.py`). Prune-safe |

## Operator trees (`Documents/GitHub/Harness-*`, `wt-*`, temp)

LANDED → PRUNE-SAFE (each tip verified ancestor of `origin/main`; merged PR in parens):

| Tree | Tip | Branch | Landed via |
|---|---|---|---|
| Harness-canon | `69382a0` | `fix/phase-registry-honesty` | #175 |
| Harness-chat-lane | `164595c` | `chat-lane-fix` | #170 |
| Harness-consolidate | `5d1262a` | `fix/status-vocabulary-single-owner` | #176 |
| Harness-d12-baseline | `84bc8e5` | `fix/d12-baseline-pin` | #180 train |
| Harness-drv159 | `bca730d` | `drv159-fix` | main |
| Harness-ev0-budget | `f889b4c` | `fix/ev0-endpoint-budget` | #167 |
| Harness-gui-driver | `c1c6a7f` | `gui-driver-fix` | #161 |
| Harness-jevcal | `e33a727` | `fix/jev-calibration-analysis` | #171 |
| Harness-p1-fixups | `d740923` | `p1-fixups` | #160 |
| Harness-p2-dogfood | `e0600d6` | `p2-strict-dogfood` | #162 |
| Harness-p3-jev | `8a357b3` | `p3-jev-core` | #165 |
| Harness-p5-provision | `1978ee4` | `p5-provision` | #166 |
| Harness-repo-cards | `697165d` | `feat/repo-cards` | #164 |

| Tree | Tip / branch | Ancestry | Verdict |
|---|---|---|---|
| Harness-jev-d10-d12 | `29c28de` / `jev-d10-d12` (D10+D12 fix) | UNMERGED | Classify content vs main (#105 closed unmerged; #129/#193/#210 may cover); then DROP or slice |
| Harness-d12-repoint | `387228b` / `fix/d12-baseline-repoint` (1 commit) | UNMERGED | PROVEN-EQUIVALENT (diff `387228b` exact on main). Prune-safe |
| Harness-dynamic-models | `ba8943d` / `chore/no-static-claude-models` (2 commits) | UNMERGED | PROVEN-EQUIVALENT (diff: tip-vs-main empty on all 6 code files; free-ling/static-pin strings absent from main). Prune-safe |
| Harness-ev6-fireworks | `47f475e` / `feat/ev7-fireworks-lane` (5 commits) | UNMERGED | HOLD = PR #212 draft lane (seq 5) |
| Harness-fireworks-canon | `8c7e250` / `docs/fireworks-canon` (2 commits) | UNMERGED | PROVEN-EQUIVALENT (diff: tip→main roadmap diff is a single hunk — main-side lifecycle-doc link addition only; tip EV-6 provenance correction on main). Prune-safe |
| Harness-gui-session | `71b203b` detached (old main) | DETACHED | Remove tree |
| Harness-hv-2 | `aae1b1c` / `feat/hv-2-brief` (1 wip) | UNMERGED | PROVEN-SUPERSEDED (diff: branch `context_brief.py` absent, but main has `brief.py` + `condenser.py` + `test_hourglass_brief.py`; brief story evolved via #99/#101/#136). Prune-safe |
| Harness-issue198-cancel | `2c6c9e3` / `fix/issue-198-gui-cancellation` (2 commits) | UNMERGED | PROVEN-EQUIVALENT (diff `2c6c9e3` + tip-vs-main test-def `comm` on `test_http_cancellation.py`/`test_ui_stop_race.py` both empty). Prune-safe |
| Harness-jev-hourglass-driver | `c72e74c` / `freebuff/jev-hourglass-driver-20260923` (1 wip) | UNMERGED | SUPERSEDED-DROP (diff: commit self-declares "Unfinished; not reviewed, not merged"; driver delivered via `driver_core/` #159/#161). Prune-safe |
| Harness-jev-log | `5b254df` / `feat/jev-p2-escalation-lifecycle-canon` (1 wip) | UNMERGED | DROP (diff: single 2-line `round2_scores.json` evidence-string change, zero code — same class as pr213-verification artifact). Prune-safe |
| Harness-l3-117 / l3-118 / l3-118b / l3verify | `eeba0a3` / `49e9c78` / `8445583` / `528fdf5` (lane-3 WIPs) | UNMERGED ×4 | DROP after confirming verification evidence captured in #142/#143; mission closed |
| Harness-settings-redaction | `ea9b280` / `fix/gui-settings-secret-redaction` (2 commits) | UNMERGED | PROVEN-EQUIVALENT (diff: `7b0fad6` (#214) on main, `redact` in main `server.py`; 5 tip-only lines are superseded pre-#213 cancellation wording). Prune-safe |
| Harness-site-query-jev | `403fd3c` / `fix/issue-207-jev-fastpath` (3 commits: `8be844f`/`66546b7`/`403fd3c`) | UNMERGED | NEEDS-KEEP — TO-LAND seq 1. Diff evidence: 15 test defs absent from main (`comm` branch-vs-main on `test_request_lifecycle.py`: exact-freeoffgrid-403, greeting fast-path, keyed-preflight, Noul-threshold tests); `request_tier` symbols absent from main `harness/`; 23 unique files. Main has only the #209 base tier (`IncidentReplayBudgetTests` class + `simple_action` + guards). Promote WITH the dirty set, not instead of it |
| Harness-verify-panel-hg | `6cf05d0` / `fix/verify-panel-hg-preview` (1 commit: ignore `.venv`) | UNMERGED | DROP (trivial, unneeded) |
| wt-harness-canary | `7b0fad6` detached (= main commit) | DETACHED | Remove tree |
| wt-harness-canonical | `ad4a300` detached | DETACHED | Remove tree |
| Temp/pr213-rebase | `29ac97e` / `feat/weather-lookup-fastpath` (weather fast lane bypassing hourglass) | UNMERGED, in `/tmp` (volatile) | Do NOT land as-is (conflicts with #177 general-lifecycle direction). OWNER: mission executor. DEADLINE: next session — `/tmp` may be evicted any time. Action: mine for #219 worker-provenance insight only, then remove |

## Non-worktree session dirs (`~/.codex/worktrees/*/`, no checkout)

Empty (only `.codex-worktree-name`) → delete dirs: `hourglass-vision-plan`, `hv4-review`, `hv5-7529-validation`, `jev-row-identity`, `lane1-canon-nit`, `lane2-mission-cleanup`, `oc-handoff-canon`, `takeover-state`, `handoff-doctrine`, `live-state-checkpoint` (both hold no Harness checkout — removed from scope of this file). `harness-finalize/Harness` and `lane1-rankings-guard/Harness` are plain copies, not git checkouts → diff against `origin/main` for unique files, then delete.
