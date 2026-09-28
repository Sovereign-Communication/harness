# Lane 2 Mission: Hourglass chain (HV-4 -> HV-5 -> HV-6)

You own this tracker PR and the work below. `docs/jev-roadmap.md` stays the only plan. This file executes its
HV-4/5/6 rows and is deleted in the final commit.

## Authority (granted by Lucas, same terms as PR #116)
- Create implementation PRs, drive CI green, and merge under the 100%-safe rule: latest-head CI green, clean, not draft,
  no changes requested, no unresolved objections, and a Lane 3 verification comment present.
- One merge at a time across all lanes, FIFO by ready time. Rebase and re-verify if behind.
- Never edit another lane's files, close its PRs, or delete unmerged branches. Escalate only spend, keys, permissions and rulings.

## Lanes and file ownership
| Lane | Tracker | Owns |
|---|---|---|
| 1 | PR #116 (Freebuff mission) | `harness/jev_packs.py`, `harness/jev_policy.py` (+ tests), `.github/workflows/*`, handoff-scope files, the canon header / Next slice / non-HV rows, `AGENTS.md` |
| 2 | this PR | `harness/stages.py` (single composition owner), `harness/waist.py` (call site), `harness/jev_completion.py` (status-row matching + HV-4..6 contracts), `harness/config.py` (stage resolution), plan/DAG modules; HV-5: `harness/agent.py`, `apply.py`, `consent.py`; HV-6: CLI/MCP/server adapters; HV-4..6 canon rows and `jev-phase` contracts |
| 3 | Lane 3 mission PR | test-isolation helpers, `audits/self/*`, `harness/ledger.py`, `packs/repo_summary.pack.json` |
If you need a change in another lane's file, write the exact request in your PR body; do not make the change.

## Work, in order
0. **Status-row identity fix first** (operator ruling 2026-09-28), as its own small PR off origin/main before #118:
   `jev_completion.py` `_row_id_cell` + exact-ID bonus + word-boundary merged/PR matching from the salvage, with the six StatusRowIdentityTests.
   A tie between candidate rows must fail closed ("ambiguous STATUS row", no can_mark_complete), never break on document order.
   Without this fix, `jev-phase --phase HV-4` reads HV-3's merged row.
1. **HV-4: draft PR #118** (`feat/hv4-waist-composition` @ `7d77907` = the never-pushed `459bd87` rebased onto `0499277`):
   `harness/stages.py` +646, the two HV-4 test modules (49 tests), and the HV-4 contract.
   Operator rulings: `stages.py` is the single composition owner. Port the salvage's `compose_plan` kwargs and composition envelope
   as a thin `waist.py` call site, plus the `agent.py` plan-path hunk. Port semantics, not duplicate functions.
   Execution-side `agent.py` stays HV-5.
   Reconcile the newer wiring on `salvage/704d1bdf-hv4-waist-wiring` (`waist.py` +279, `config.py`, `jev_completion.py` +56,
   plus alternate test versions). Keep the stronger assertions, and cite each hunk's source.
   Before you extend it, check it against the HV-4 row: the waist consumes curated briefs with decreasing token allowances,
   cannot raise its own limits, bypasses omitted stages, and keeps token and dollar ceilings independent.
   (Jev's stage check on the stated plan gave plan_soundness 0.32 and asked for evidence at 0.79.)
2. **HV-5:** expanded-token execution and sovereign handoffs. Wire Lane 1's HV-1 integrations at the call sites:
   consent freshness, execution checkpoints, and the restart-target enum.
   HV-1 completes when these land with a live dogfood.
3. **HV-6:** CLI/MCP/agent surface parity and observability, the DF-UI-2 dogfood face, the JEV-P4 freeze face and the JEV-P6 stage C residuals,
   plus the paid-cheap dogfood comparison. The dogfood needs Lucas's spend approval, and it clears `dogfood_missing` on many phases.

## Rules learned the hard way (2026-09-27)
- **Commit and push at every green checkpoint.** A Freebuff free session ended Lane 1 mid-turn after 159 tool calls with all work uncommitted.
- Run from the worktree root: `C:/Users/SCM/Documents/GitHub/Harness/.venv/Scripts/python.exe -m ...`. Never use `harness.exe`:
  the shared editable install points at a stale worktree.
- Set `HARNESS_LEDGER=<worktree>/tmp/lane2-ledger.jsonl` before tests, audit or CLI. The unit suite writes fake events into the configured ledger.
- The full suite takes more than 600s, and `setsid` does not exist here. Run it in two halves:
  `-W error::ResourceWarning -m unittest discover -s tests -p "test_[a-j]*.py"`, then the same with `-p "test_[k-z]*.py"`.
  Together they cover all 141 files.
- Stay hermetic until HV-6. The OpenRouter cap is about $0.75/day, shared. Jev (TypeSafe) is cheap and advisory.
- D12 is inert (DF-AUDIT-3). Name the tests that execute new lines instead of citing "audit BAR MET".
- Gates in every PR (raw tails): ruff, compileall, both suite halves, `audits/self/audit.py`, and `jev-phase --phase <ID>`
  (a single phase is live Jev; `--all` is local only).

## Definition of Done (the mission file lives only on this tracker branch; delete it here, not in #118)
HV-4, HV-5 and HV-6 are merged with CI green, each with its `jev-phase` contract passing and its row honestly complete.
Consumed salvage branches are noted in the PRs. The final commit deletes this file; then merge this PR as the seal.
