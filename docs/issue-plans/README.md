# Issue implementation queue — batch 1

This directory is the execution view of the canon's #295 register. The
authoritative order, dependencies and acceptance exits remain
[`docs/jev-roadmap.md`](../jev-roadmap.md); this queue is generated from that
register plus the live GitHub open-issue set.

**Snapshot 2026-10-24:** 58 open GitHub issues, all mapped to a canon row.
Batch 1 implements **#347** (config/Jev override precedence); the other 57 are
`queued` in [`queue.json`](queue.json). No issue is marked complete until its
changes are merged with green CI.

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
- [`queue.json`](queue.json) — all 58 open issues with owner, prerequisites,
  acceptance and batch status.

## Batch rules

- One phase PR/merge at a time; no mixed all-issue PR.
- Each issue needs its named acceptance test and a green local gate.
- STATUS/queue completion waits for the merge-result CI, not a local preview.
