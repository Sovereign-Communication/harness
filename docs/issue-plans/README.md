# Issue implementation queue — batch 1

This directory is the execution view of the canon's #295 register. The
authoritative order, dependencies and acceptance exits remain
[`docs/jev-roadmap.md`](../jev-roadmap.md); this queue is generated from that
register plus the live GitHub open-issue set.

**Snapshot 2026-10-25:** the queue was generated at 58 open GitHub issues,
all mapped to a canon row. Seven campaign items have landed with receipts:
**#339** (efficiency evidence, `76a95f6`, outside the 58-issue queue),
**#330** (dogfood `cancel_check`, PR #364, `0737e62`), **#347** (config/Jev
override precedence, PR #365, `4d9fea4`), **#327** (checkout-root gate and
route resolution, PR #366, `8c4324a`), **#278** (one external-tool resolver
and preflight, PR #367, `4155b68`), **#276** (runtime merge-base D12 and
the S5 disposition, PR #369, `f54f8aa`), and **#331** (D12 SKIP has no
passed-measurement credit, PR #371, `52f51b9`, outside the queue). `main` is
`52f51b9` with 12/12 merge-result CI green, a 5,091-test hermetic suite and
**53 open issues**; their status and blockers are in [`queue.json`](queue.json).
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
