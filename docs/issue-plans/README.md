# Issue implementation queue — batch 1

This directory is the execution view of the canon's #295 register. The
authoritative order, dependencies and acceptance exits remain
[`docs/jev-roadmap.md`](../jev-roadmap.md); this queue is generated from that
register plus the live GitHub open-issue set.

**Snapshot 2026-10-25:** the queue was generated at 58 open GitHub issues,
all mapped to a canon row. Six later tracking issues (#377-#382, opened
2026-10-25) were appended after generation, so the queue now holds 65 rows and
all **56** live open issues are represented. Nine queue items have landed with
receipts: **#330** (dogfood `cancel_check`, PR #364, `0737e62`), **#347**
(config/Jev override precedence, PR #365, `4d9fea4`), **#327** (checkout-root
gate and route resolution, PR #366, `8c4324a`), **#278** (one external-tool
resolver and preflight, PR #367, `4155b68`), **#276** (runtime merge-base D12
and S5 disposition, PR #369, `f54f8aa`), **#333** (ledger-backed task-ID
checks, PR #398, `919d2ac`), **#287** (CI cancellation/docs-only path, PR #402,
`82f9381`), **#279** (doctor preflight, PR #403, `707c017`), and **#282**
(Playwright smoke and safe browser handoff, PR #405, `ef21e23`). **#339**
(`76a95f6`) and **#331** (PR #371, `52f51b9`) landed outside the queue. The
full-surface jev/dogfood sweep (PR #373, `ce3efa0`) then fixed two
ceiling-forwarding defects in `apply` and dogfood's apply phase; its
exit-code inventory is
[`audits/self/dogfood/SWEEP_2026-10-25.md`](../../audits/self/dogfood/SWEEP_2026-10-25.md).
The sweep's budget gap then closed in PR #375 (`28bdaf0`): the nine bundled
bench fixtures pin their own `task_max_cost`, bench accepts
`--task-max-cost`, and README documents both the funded bench invocation and
the deliberate apply refusal with its unblock flags. The seam sweep that
followed drove the HTTP server API and the MCP stdio tool face for real and
fixed three defects: MCP verify gates inherited the JSON-RPC stdin pipe and
hung to the 300 s timeout, every HTTP plan failed before the composer, and
HTTP/MCP dogfood crashed on a `None` line limit. Its evidence is
[`audits/self/dogfood/SWEEP_2026-10-25_SEAMS.md`](../../audits/self/dogfood/SWEEP_2026-10-25_SEAMS.md).
The inventory was verified at `main` `ba38b62` (the merge of PR #394) with
12/12 merge-result CI green (run `40243573836`), a 5,102-test hermetic suite
and **59 open issues**; their status and blockers are in
[`queue.json`](queue.json). `v0.5.1` is tagged at `f6ebf6b` and published at
`https://pypi.org/project/sovereign-harness/`; its PyPI description carries the
corrected install story. Main branch protection was enabled after the release:
11 required CI checks and no required approving reviews (API evidence in
[`docs/releasing.md`](../releasing.md)).
The [closing report](closing-report-2026-10-25.md) is the 54-open snapshot
before #276; the current inventory is on #295. No issue is marked complete
until its changes are merged with green CI.

**2026-10-26 tracker update:** PR #406 merged the #282 bookkeeping at
`84e646e`; the source results page is current at 56 open issues. The visibility
issue [#407](https://github.com/Sovereign-Communication/harness/issues/407) is
now in the queue. Its implementation is stopped pending a decision on whether
revocation is future-only or must retract past aggregate contributions.

**2026-10-26 #306 blocker receipt update:** PR #409 merged as `baf6fe1`.
PR-head CI run `40379030379` and post-merge main CI run `40380776860` both
passed all 14 jobs. The post-merge site workflow `40380776750` passed
build/tests and skipped deployment because `CF_API_TOKEN` was not configured;
no public update is claimed. The inventory remains 65 queue rows, 9 complete,
55 queued, 1 blocked, and 56 open issues. #306 remains open and blocked with
its one-shot evidence and named operator-owned unblocker in [`306.md`](306.md).
The #295 tracker comment records the same counts and deployment status.

### Closing verification - #277 (reserved for last)

[#277](https://github.com/Sovereign-Communication/harness/issues/277) remains
**open and not started**; it is reserved for the final cross-surface closing
round after the other work. Its named prerequisites (#276, #331, #327, #330)
are all closed as completed, and each merge commit is an ancestor of
`origin/main`:

| Prerequisite | Receipt | State |
|---|---|---|
| #330 dogfood verify seam | PR #364, `0737e62` | closed completed |
| #327 checkout-root gates | PR #366, `8c4324a` | closed completed |
| #276 runtime merge-base D12 | PR #369, `f54f8aa` | closed completed |
| #331 D12 SKIP accounting | PR #371, `52f51b9` | closed completed |

The earlier baseline mitigation, PR #349 (`f3cac6c`), is also merged; #276 is
the permanent measurement fix. No PR implements #277.

#277 is one structured round, not the old open-ended dogfood-until-zero form:
a checklist pass with a filed report covering CLI, GUI, MCP and website, each
with what was exercised, what was found, and what was fixed or filed. Every
failure must be fixed with a test-bearing PR or filed as its own issue with a
severity note; the report ends with one verdict per surface. Bug-fix and
polish only, no new features. The recorded residual gaps belong in that
evidence: the native `pywebview` shell is absent (GUI access is the loopback
browser path only), the installed MCP runtime identity still needs a real
client check, and no live model/Jev semantic approvals have been demonstrated.

The **#333** ledger-backed task-ID decision landed in PR #398 (`919d2ac`).
**#306** is explicitly blocked: the key is installed and the harness resolves
it (presence-only checks); the one-shot Fireworks probe returned HTTP 200
without the required reply, and its marker prohibits another call. Its local
receipt is now recorded in [`306.md`](306.md) and
`audits/self/dogfood/FIREWORKS_VERIFY_RECEIPT.json`. The named unblocker is an
operator decision on a non-repeating remedy or another already-existing valid
receipt. Neither is a #277 prerequisite.

## Harness planner note

`harness plan` was run on #347 with live LLM decomposition. Both attempts
returned a non-JSON response, so the planner correctly reported the loud
heuristic fallback and wrote a single-node preview to
[`347.harness-plan.json`](347.harness-plan.json). That preview is evidence of
the planner's actual behavior, not a substitute for the canon's multi-step
register. The LLM-decomposition parse failure is a planner-quality finding; it
is not being hidden by relabeling the heuristic plan as a confirmed design.

The canon's #347 contract was implemented and verified directly:

- [`347.md`](347.md) — acceptance matrix, changes and test receipts.
- [`327.md`](327.md) — gate/route root resolution, changes and test receipts.
- [`278.md`](278.md) — tool resolution, preflight and real-process receipts.
- [`276.md`](276.md) — runtime merge-base D12 measurement, real-diff proof
  and the S5 disposition.
- [`331.md`](331.md) — D12 SKIP status/accounting, text and JSON receipts, and
- [`282.md`](282.md) — Playwright UI journeys, persistent owner recovery, the gated merge/disposition receipt, and the merge record.
- [`306.md`](306.md) — presence-only key check, the existing one-shot receipt, failed EV-8 gate and named unblocker; no key material.
- [`407.md`](407.md) — visibility-site filing and the revocation/aggregate-only ambiguity; implementation not started.
- [`closing-report-2026-10-25.md`](closing-report-2026-10-25.md) — landed
  receipts and the per-issue blocker/unblocker accounting for the 54 open
  issues.
- [`queue.json`](queue.json) — all 65 queue rows with owner,
  prerequisites, acceptance, batch status and explicit blocker state.

## Batch rules

- One phase PR/merge at a time; no mixed all-issue PR.
- Each issue needs its named acceptance test and a green local gate.
- STATUS/queue completion waits for the merge-result CI, not a local preview.
