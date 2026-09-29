# Harness mission control-plane adoption boundary

**Date:** 2026-09-24
**Owner:** Harness
**Change boundary:** Documentation only. This handoff changes no Harness source, tests, hooks, CI, installation, runtime, deployment, or legacy handoff. It is a routing and acceptance proposal, not implementation authorization.

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/Harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

## Decision requested

Adopt the existing Harness mission/orchestrator/receipt/worktree-isolation control plane as the single execution owner. Do not create a second queue, scheduler, session store, retry policy, provider router, or worktree manager in an integrating layer.

The smallest safe implementation boundary is an explicitly versioned adapter over an existing Harness entry point. If the current CLI, MCP, and library surfaces already provide the required contract, the implementation boundary is zero source change: add only owner-approved contract tests and documentation.

This document does **not** authorize implementation, deployment, restart, provider calls, message sending, legacy migration, or PR creation.

## Evidence inspected

The architecture below is pinned to the read-only remote reference `origin/main` at commit `2cf24b569d9d73afaf78d489561ca0b79fc5490a`. The local Harness checkout is behind that reference; no local WIP was treated as canonical.

| Capability | Exact evidence location | What the code already owns |
|---|---|---|
| Until-limits mission driver | `harness/mission_driver.py:1-27,189-222,249-409` | Attempt loop, interrupt-safe progress, dual budget, terminal outcomes, receipt normalization, and resume persistence. |
| Mission state and receipts | `harness/mission_record.py:1-14,400-500,680-710,863-891` | Pack layout, append-only `receipts.jsonl`, `resume.json`, budget, status, and terminal findings. |
| Orchestration policy | `harness/orchestrator.py:1-8,83-94,142-204,238-320` | Completion truth, bounded rounds, artifact checks, and re-planning. |
| One execution assembly | `harness/executor.py:396-405,414-473,518-567` | Shared worker count, reservations, worktree isolation, final gate, and result summary for CLI, MCP, and agent lanes. |
| Worktree lifecycle | `harness/worktree.py:71-188` | Create, audit declared writes, commit, merge, and discard in an isolated checkout. |
| Agent edit lane | `harness/agent.py:892-907,984-1140,1230-1270` | Routes edit requests through the shared planner, executor, final gate, and typed result envelope. |
| MCP faces | `harness/mcp.py:509-705,830-842`; `harness/mcp_schemas.py:135-167,206-235` | Write/verify/continue authorization, shared service assembly, and read-only mission status. |
| Durable worker example | `examples/oc_handoff/worker.py:53-77,629-703,877-949` | SQLite task/outbox state, exact-file commit audit, crash recovery, pending receipts, and acknowledgement. |
| Existing control-plane tests | `tests/test_hul_mission_record.py:184-244,255-336`; `tests/test_worktree.py:28-138`; `tests/test_executor.py:56-180`; `tests/test_hg_final_gate.py:13-104`; `tests/test_oc_handoff_worker.py:85-137,234-269` | Append-only receipts, resume, worktree boundaries, executor truth, final-gate failure, exact commit, and receipt recovery. |

Pinned Git blob identities for the principal reference files are:

```text
harness/mission_driver.py       ddf55289c2a9dc79a8105018e39dd5763c329789
harness/mission_record.py       5c68960334da356b85ffc19f17577947afc61301
harness/orchestrator.py         fb7508a4d87519a374bdd51e2fec3696f29c5353
harness/executor.py             a11de4f05e67e865d2f08be9e0476a10e00765cf
harness/worktree.py             fad8b4f730a3fb50e7986746b4f22dc55624b618
harness/agent.py                697b6cc70dd472474946d6373ea98cb0b2c5042e
harness/mcp.py                  f27b0de16fe85bec98b6a8f4ed597b92a4749a6f
examples/oc_handoff/worker.py   7bf92ae987832bd5eb13d6ae4132a7ea9373e3b5
```

## Required contract for an integrating caller

A caller may request a goal, allowed target files, a verification gate, budget, cancellation, and an optional continuation. The response must identify the run, preserve the existing result/error shape, and expose the existing completion and receipt evidence. A caller must not receive a second history store or infer completion from a model response.

The control plane must make these distinctions explicit:

```text
request accepted
attempt receipted
artifact present
verification gate passed
mission terminal
```

A structural fallback, a live provider response, or a process-health result cannot by itself become a successful mission without the existing verifier policy and a receipt.

## Smallest implementation boundary

1. Select one existing public Harness entry point: the library agent surface, the CLI, or MCP. Do not add a second execution implementation.
2. If an adapter is required, keep it stateless and request-scoped. Pass through caller task/session identifiers; do not create a fixed global session.
3. Return an immutable envelope containing the existing result plus run/receipt references. Do not mutate a caller-owned result dictionary or hide fallback state.
4. Reuse `PlanExecutor`, `WorktreeIsolation`, mission receipts, resume state, and final-gate policy. Do not reimplement any of them in the adapter.
5. Keep deployment, provider credentials, scheduling, and product-specific acceptance outside this Harness boundary.

## Deterministic acceptance commands

These are proposed hermetic acceptance commands for a separately authorized implementation. They are not run by this documentation-only handoff and must not be redirected to a live provider.

```text
python -m unittest tests.test_hul_mission_record tests.test_orchestrator tests.test_worktree tests.test_executor tests.test_hg_final_gate -v
python -m unittest tests.test_oc_handoff_worker -v
python -m unittest tests.test_mcp tests.test_agent -v
python -m compileall -q harness examples/oc_handoff
```

The acceptance review must additionally inspect that the same execution assembly is used by the CLI, MCP, and agent lanes, and that an interrupted run resumes from the existing pack rather than starting a new history.

## Compatibility and rollback

- Preserve existing CLI and MCP response shapes and mission-pack schemas.
- Make any adapter route additive and versioned; do not silently reinterpret an old result.
- Preserve append-only receipts and `resume.json`; do not delete or rewrite prior attempts.
- Roll back by disabling the adapter route or reverting only the adapter in an isolated branch. Existing packs and worktrees remain untouched.
- A missing or incompatible Harness surface is a blocked compatibility result, not permission to create a second control plane.

## Explicit stop conditions

Stop before implementation if any of the following is true:

- the proposed change introduces another scheduler, queue, session/history owner, retry policy, or worktree manager;
- a structural fallback or provider response is treated as mission completion without the existing verifier and receipt;
- a result shape is changed without a versioned compatibility plan;
- the local checkout and `origin/main` have not been reconciled;
- the work requires editing source, tests, hooks, CI, deployment, credentials, or legacy handoffs under this document alone.

## Unresolved prerequisites and limits

- Harness owner review of the selected public entry point and adapter contract.
- A clean, isolated Harness worktree based on a reconciled remote commit.
- Owner-approved hermetic contract tests for any new adapter.
- A separate authorization for live provider or deployment validation.

No product status, foreign evidence, or external conclusion is imported by this handoff. It records only the Harness control-plane boundary and its acceptance requirements.
