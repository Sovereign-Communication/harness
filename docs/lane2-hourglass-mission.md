# Lane 2 Mission: Hourglass chain (HV-4 -> HV-5 -> HV-6)

This file is a temporary execution tracker. `docs/jev-roadmap.md` is the only operational plan and the source of truth for current phase status, acceptance tests, and next work. This file is deleted in the final seal commit.

## Authority and merge discipline

- Own Lane 2 work and its tracker PR. Create implementation PRs, keep each PR to one phase slice, run the required local gates, obtain Lane 3 verification, and merge only after latest-head CI is green.
- Keep one implementation PR active at a time across lanes. Rebase and re-verify when `main` advances.
- Do not modify files owned by another lane. Put a precise request in the affected PR body when work crosses ownership.
- Escalate only owner-side spend, credentials, permissions, or genuine policy rulings.

## File ownership

| Lane | Tracker | Owns |
|---|---|---|
| 1 | PR #116 | `harness/jev_packs.py`, `harness/jev_policy.py` and tests, `.github/workflows/*`, handoff-scope files, canon header / Next slice / non-HV rows, `AGENTS.md` |
| 2 | this PR | `harness/waist.py` (single composition owner), `harness/jev_completion.py` (phase contracts and status-row matching), `harness/config.py` (stage resolution), plan/DAG modules; HV-5 `agent.py`, apply and consent flow; HV-6 CLI/MCP/server adapters; HV-4..6 canon rows and contracts |
| 3 | Lane 3 mission PR | test isolation, `audits/self/*`, `harness/ledger.py`, `packs/repo_summary.pack.json` |

## Current scope

Use the current `HV-5`, `HV-6`, `HV-1`, `HV-2-use`, and `HV-3-use` rows in `docs/jev-roadmap.md` for the exact remaining work and gates. Do not use this file's old snapshots, salvage notes, or historical branch instructions as evidence of current status.

On current `main`, HV-4 composition and the composed-run caller/intake slices have merged (#118, #134, #136). Their remaining consumer obligations are tracked in the canon. HV-5 dispatch and HV-6 surface work remain open; HV-1 still needs its typed dimensions called from the relevant lanes and live dogfood. Update the canon only through reviewed PRs after each slice lands.

## Operating rules

- Commit each verified checkpoint; never push directly to `main`.
- Run commands from the phase worktree root and use an isolated `HARNESS_LEDGER` under that worktree.
- Keep unit tests hermetic. Do not spend on OpenRouter without explicit owner approval; the configured daily cap is small.
- Run Ruff, compileall, both unittest halves (`test_[a-j]*.py` and `test_[k-z]*.py`), `audits/self/audit.py`, and the applicable phase gate. A single-phase gate is live Jev; `--all --local-only` is a separate repo-wide mechanical audit.
- Name tests that execute changed lines; never claim completion from a skip or fallback.

## Definition of Done

Complete the current Lane 2 scope from the canon: every owned HV-4/HV-5/HV-6 clause and consumer obligation is honestly closed with its named tests, required live evidence, and passing phase gate; CI is green after each merge; then delete this file in the final seal PR and merge it only after its checks and the required Lane 3 verification are green.
