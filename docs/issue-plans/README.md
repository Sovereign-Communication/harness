# Issue implementation queue — batch 1

This directory is the execution view of the canon's #295 register. The
authoritative order, dependencies and acceptance exits remain
[`docs/jev-roadmap.md`](../jev-roadmap.md); this queue is generated from that
register plus the live GitHub open-issue set.

**Snapshot 2026-10-25:** the queue was generated at 58 open GitHub issues,
all mapped to a canon row. Six later tracking issues (#377–#382, opened
2026-10-25) were appended after generation, so the queue now holds 64 rows and
all **59** live open issues are represented. Seven campaign items have landed with receipts:
**#339** (efficiency evidence, `76a95f6`, outside the 58-issue queue),
**#330** (dogfood `cancel_check`, PR #364, `0737e62`), **#347** (config/Jev
override precedence, PR #365, `4d9fea4`), **#327** (checkout-root gate and
route resolution, PR #366, `8c4324a`), **#278** (one external-tool resolver
and preflight, PR #367, `4155b68`), **#276** (runtime merge-base D12 and
the S5 disposition, PR #369, `f54f8aa`), and **#331** (D12 SKIP has no
passed-measurement credit, PR #371, `52f51b9`, outside the queue). The
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
`main` is `9facb42` (the `v0.5.0` tag) with 12/12 merge-result CI green, a
5,102-test hermetic suite and **59 open issues**; their status and blockers are
in [`queue.json`](queue.json). The `0.5.0` distribution is published at
`https://pypi.org/project/sovereign-harness/`.
The [closing report](closing-report-2026-10-25.md) is the 54-open snapshot
before #276; the current inventory is on #295. No issue is marked complete
until its changes are merged with green CI.

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
  the merge record.
- [`closing-report-2026-10-25.md`](closing-report-2026-10-25.md) — landed
  receipts and the per-issue blocker/unblocker accounting for the 54 open
  issues.
- [`queue.json`](queue.json) — all 58 generated issues with owner,
  prerequisites, acceptance and batch status.

## Batch rules

- One phase PR/merge at a time; no mixed all-issue PR.
- Each issue needs its named acceptance test and a green local gate.
- STATUS/queue completion waits for the merge-result CI, not a local preview.
