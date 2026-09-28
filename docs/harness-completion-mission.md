# Harness Completion Mission

You are the owner of this PR and of all the work below. Read this whole file, then do everything it says. When every item is complete and Jev-confirmed, merge this PR yourself — you are authorized to.

## Your authority (granted by Lucas, the repo owner)

- You own this PR (`mission/harness-completion`) and every work item below.
- You may create implementation PRs, drive their CI green, and **merge them** when they satisfy the 100%-safe rule: latest-head CI fully green, clean merge state, not draft, no changes requested, no unresolved substantive objections. If behind main, rebase onto fresh main and re-verify before merging.
- You may **merge this mission PR** when the Definition of Done below is met.
- You may delete only a merged PR's own branch, then verify main + CI before proceeding.
- You may NOT close other PRs, delete unmerged branches, or take destructive actions without Lucas's explicit approval — report and wait instead.
- Never ask Lucas technical questions (merge order, rebase tactics, how to fix CI). Make those calls yourself. Escalate only: authorization needs (spend, keys, permissions), genuine end-state choices, or real ramifications.

## Verified starting state (snapshot 2026-09-27 ~19:40 HST — re-verify fresh with `git ls-remote` and the API before acting)

- Repo: Sovereign-Communication/harness. You commit as Treystu. Main HEAD: `04992777`.
- **Zero open PRs.** Today's drive merged #109 (evaluate_decision Jev primitive, closes #106), #112 + #113 (test pins, genuine residuals of #105), #114 (advisory warn-only handoff ownership gate + waiver register), #115 (RSI dogfood iteration-1 fix: gate interpreters must resolve via `sys.executable`, not bare `python -m`). #105 was closed unmerged as SUPERSEDED (its genuine work landed via #112/#113). #75 closed as resolved.
- Open issues: **#107** (PyPI trusted publishing), **#108** (handoff ownership gate follow-ups).

## Work items, in order

### 1. Issue #107 — PyPI trusted publishing (partially owner-blocked)

The implementation already exists, local-only: worktree `~/workspace/worktrees/harness-publish`, branch `publish/trusted-pypi-107`, commit `7ef4d5c` — adds `.github/workflows/publish.yml` (tag push -> build -> twine check -> version parity -> pypa/gh-action-pypi-publish with OIDC, no secrets) and `docs/releasing.md` (runbook). If you can access that worktree, verify its contents; if not, reimplement from the issue spec.

**Blockers (owner-side, cannot be worked around):**
- (a) The GitHub token lacks the **Workflows** permission: any push containing `.github/workflows/*` gets 403. The remote branch `publish/trusted-pypi-107` exists but is stale (it does not contain the workflow). Do everything up to the push: verify the work, open the PR the moment pushing is possible. If Lucas re-grants the token (or pushes the branch himself), open the PR immediately, drive CI green, merge under the 100%-safe rule.
- (b) PyPI trusted-publisher registration at pypi.org must be done by Lucas before any real publish can succeed (until then the workflow fails 403 at upload).

Post both asks precisely (what Lucas must do, in what order) as a comment on issue #107. Do not silently skip this item.

### 2. Issue #108 follow-ups (implementation merged via #114)

- (a) **Stamp/waive the 9 docs** missing scope metadata in `handoff_scope_waivers.json`. Not blocked — do it now: get each doc stamped by its owning lane or record a waiver with reason. Then re-run `scripts/validate_handoff_scope.py` and confirm zero findings.
- (b) **Wire the warn-mode CI job** for the handoff gate (draft YAML was left in PR #114's body). Blocked on the same Workflows token permission as item 1 — prep the workflow file fully, land it the moment pushing is possible.

### 3. HV-1 — verify, then complete

Per `docs/jev-roadmap.md`, HV-1 requires `context_intake`, `plan_soundness`, `execution`, `consent`, and `restart_target` actually wired into the context/planning/execution lanes, followed by a live dogfood run. Verify what #114 and today's work already covered; implement whatever remains as focused PRs; then dogfood live and record the outcome. If the roadmap shows HV-1 complete, verify the claim with a live run rather than trusting the checkbox.

### 4. RSI dogfood iterations (bounded)

Iteration 1 is done (found and fixed the `sys.executable` bug via #115). Keep iterating: each iteration runs the harness's full self-audit battery against itself, classifies every finding with Jev **before** implementing (KEEP/DROP + severity), implements only KEEP findings as focused PRs. **Stop after 2 consecutive iterations produce zero KEEP findings** — no churn for churn's sake. The F3 parked item (clearer final-gate failure message, escalated @ 0.28) may be reconsidered in a later iteration with better evidence.

### 5. Hygiene

- `~/workspace/harness-105` holds stale scratch from the closed #105 era (uncommitted `CONTRIBUTING.md`, `harness/cli.py`, `harness/cli_parser.py` changes). Verify none of it is unpreserved unique work (the genuine residuals landed via #112/#113), then remove the worktree.
- Report any other stale branches/worktrees you find; delete only what you are certain is fully preserved elsewhere, otherwise list them for Lucas.

## Jev protocol (mandatory)

- Use Jev (TypeSafe System One) for: triaging every candidate finding (is_genuine_work / category / severity / well_scoped -> KEEP/DROP), and gating every merge decision (PROCEED only at high calibrated confidence; anything under the bar gets fixed or escalated, never merged on a shrug).
- Key: `TYPESAFE_API_KEY` / `HARNESS_JEV_KEY` env, or the `custom.typesafe` connector. If Jev is unreachable, say so explicitly in the relevant PR and do not silently skip the gate.
- Jev advises; it never bypasses authorization. Merges still need the 100%-safe rule satisfied.

## Definition of Done — merge this PR only when ALL of these hold

1. Items 1–5 are complete, or for owner-blocked parts: fully prepped with the precise ask posted to Lucas (issue #107 comment) and nothing actionable remaining on your side.
2. Every implementation PR you created is merged, CI green at merge, branches deleted, main verified green after each.
3. A final Jev decision-gate judgment confirms the mission complete (PROCEED at high confidence).
4. This PR's branch is rebased onto fresh main, CI green at latest head.
5. Your merge commit message summarizes what landed and what remains owner-blocked, if anything.

Then merge this PR. That merge is the seal: harness is comprehensively complete and confirmed.

## Standing rules

- `docs/jev-roadmap.md` is the canon — no parallel plans. This mission executes the canon's open items; update the canon's STATUS through your PRs as items land.
- One PR merging at a time; rebase when behind; verify main/CI between merges.
- Narrate before any network/upload/push action: what, where, why.
- No emoji in repo content. One worktree per lane; never edit another lane's files.
- Trust `origin/main` over any cached state, including this file's snapshot — re-verify at every step.
