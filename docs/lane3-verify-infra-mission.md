# Lane 3 Mission: verification (builder != grader) and infrastructure

You own this tracker PR. `docs/jev-roadmap.md` stays the only plan; this file is deleted in the final commit.

## Authority (granted by Lucas)
- For your own PRs: the 100%-safe merge rule, one merge at a time across lanes (FIFO). Lane 1 verifies your PRs.
- For other lanes' PRs: verify and comment only. Never approve, merge, push to, or edit their branches.

## Job 1 (always first): verify every Lane 1/2 PR before it merges
- Work in a clean detached worktree at the PR head, with a private `HARNESS_LEDGER`.
- Run ruff, compileall, both suite halves (`-p "test_[a-j]*.py"` and `-p "test_[k-z]*.py"`), `audits/self/audit.py`,
  and a live `jev-phase --phase <ID>` for each claimed row.
- Review against the row's definition of done, the lane file ownership (see `docs/lane2-hourglass-mission.md`),
  scope (no unrelated or generated churn), STATUS honesty, and tests that actually execute the new lines.
- Post one comment: `Lane 3 verification: PASS | PASS-WITH-NITS | FAIL`, with evidence tails and `file:line` findings.
- Queue:
  1. Lane 1's decision-gate v2 PR. It changes the rule that cleared its own merge, so judge the rule on its merits:
     decisive lead -> proceed; near-tie -> escalate; destructive >= 0.5 -> escalate; missing probabilities or unkeyed -> fail closed;
     the gate may only add escalation reasons.
  2. #117.
  3. #118 (HV-4), then later HV PRs.

## Job 2: infra/DF backlog from the 2026-09-27 Jev audit (one PR at a time, when nothing is waiting on Job 1)
1. **Ledger pollution (high).** The unittest suite appends fake `jev_eval` / `plan_verdict` / `brief_built` events to whatever
   `HARNESS_LEDGER` points at. The real ledger holds 42,698 zero-cost fallback `jev_eval` events out of 64,821 entries, against 8,122 live.
   Fix it tests-only: force a temp ledger for the whole suite, add a guard test, and fix `tests/test_cost_cli.py`'s ignored `HARNESS_LEDGER_PATH`.
   Never rewrite the hash chain.
2. **DF-AUDIT-3 (found by Lane 1).** D12 passes silently because baseline commit `f878b01` does not exist.
   - Make D12 print SKIP visibly and fail closed in CI when the baseline commit is unreachable, then re-baseline on current main.
   - The traced refresh takes about 40 min, which rules out a single 600s tool call and setsid. Launch it detached with PowerShell
     `Start-Process -WindowStyle Hidden -RedirectStandardOutput .refresh.out -RedirectStandardError .refresh.err`, or ask Lucas.
   - Commit the baseline and the D12 change together.
3. `harness/ledger.py` warns about its own `ledger.jsonl.lock` on every run.
4. `jev-phase --all` silently ignores live mode. Add a one-line notice; coordinate with Lane 2 once HV-6 touches `cli.py`.
5. The repo-summary pack (JEV-P6) returns 89% ambiguous judgments, and `waist_relevant` is true for 1 of 502 elements.
   Rewrite its criteria with full meaning, and measure ambiguity before and after with `repo-summary --limit 60`.
6. Report to Lucas (do not fix):
   - The shared `.venv` editable install points at `Harness-ui-mcp-parity`, 74 commits behind.
   - The operator tree's `main` is 12+ merges behind, and its untracked `scripts/validate_handoff_scope.py` and
     `tests/test_handoff_scope.py` block a fast-forward pull.
   - About 40 worktrees are registered.
   - Salvage branches `salvage/*` preserve orphaned lane work.

## Rules
- Commit and push at every green checkpoint; Freebuff free sessions end without warning.
- Run from the worktree root with the operator `.venv` python (`-m`); never use `harness.exe`.
- Use a private `HARNESS_LEDGER` and spend nothing on OpenRouter.
- You own test-isolation helpers, `audits/self/*`, `harness/ledger.py` and `packs/repo_summary.pack.json`.
  For anything else, put the request in your PR body.

## Definition of Done
- Every Lane 1/2 PR merged with a Lane 3 verification comment.
- Backlog items 1-5 merged green, or dropped with Jev triage evidence; item 6 reported.
- The final commit deletes this file; then merge as the seal.
