# Harness fastdriver compatibility adapter boundary

**Date:** 2026-09-24
**Owner:** Harness
**Change boundary:** Documentation only. This handoff changes no Harness source, tests, hooks, CI, installation, runtime, deployment, or consumer WIP. It is not implementation authorization.

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/Harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

## Finding

Harness already has a public, owner-controlled execution surface. A compatibility adapter should translate a narrowly defined caller request into that surface; it should not become a second agent runtime.

The evidence supports the following boundary:

- `harness/agent.py:150-190` exposes `AutonomousAgent.run_prompt(...)` as the public prompt entry point and routes edit intent into the existing orchestrator.
- `harness/agent.py:892-907,984-1140` composes the shared planner, `PlanExecutor`, worktree isolation, budget reservation, and final gate.
- `harness/executor.py:396-405,414-473,518-567` states that `PlanExecutor` is the one execution assembly shared by the CLI, MCP, and agent lanes.
- `harness/mcp.py:566-705` already provides write-gated `apply_edit` and `continue_work`; `harness/mcp.py:830-842` exposes mission status without mutating progress.
- `harness/service.py:1-7,188-205` identifies the service composition as the owner used by CLI, web, and MCP callers.
- `HANDOFF/review/OC_FASTDRIVER_CONTRACT_DEFECT_2026-09-24.md:15-19,44-53` is the existing Harness-owned record of the consumer API mismatch and its evidence limits. Its SHA-256 is `94b5afcc0e1f64c422b904eb1edf033257c05c3d9711f6f490ed9b29a8e51db0`.

The current remote reference is `origin/main` commit `2cf24b569d9d73afaf78d489561ca0b79fc5490a`. The compatibility proposal is pinned to that reference; the local checkout is behind it and its WIP is not canonical.

## Smallest implementation boundary

Implement, only after separate owner authorization, one thin and versioned adapter over an existing public Harness entry point. The adapter may:

- validate a request identifier, goal/prompt, allowed root, verification command, budget, and cancellation token;
- call the existing agent/CLI/MCP service composition;
- pass through caller-supplied task/session identity;
- return the existing result envelope plus stable run/receipt references;
- expose an explicit unavailable/refused state without silently selecting another execution path.

The adapter must not:

- create a scheduler, queue, worker pool, retry policy, provider router, or worktree manager;
- use a fixed shared session or mutate global history;
- call providers or verification commands directly outside Harness policy;
- turn a model response, health check, or structural fallback into completion;
- add a new result-dict shape that requires consumers to guess field ownership;
- become a second implementation of the mission driver or `PlanExecutor`.

The removal target is duplicate **consumer-side execution ownership**, not Harness functionality. Consumer source/test WIP remains outside this handoff and must not be copied, staged, committed, or treated as Harness implementation.

## Compatibility contract

A caller must be able to distinguish at least these outcomes without interpreting prose:

```text
accepted
running
verified
refused
deferred
blocked
failed
```

Every result must retain the underlying Harness status and any receipt/run reference. A compatibility failure must be explicit and non-destructive. A fallback must be named in the result and must not masquerade as the primary path.

## Deterministic acceptance commands

These commands are proposed for a later authorized implementation and were not run for this documentation-only handoff:

```text
python -m unittest tests.test_agent tests.test_mcp tests.test_executor -v
python -m unittest tests.test_cli tests.test_service_closeout -v
python -m compileall -q harness
```

The owner must add a focused contract test for the adapter. The test must use fakes and prove:

1. request identity and allowed-root validation are passed through;
2. the existing Harness execution assembly is called once;
3. a verification failure cannot become a successful envelope;
4. cancellation, refusal, and unavailable states are distinct;
5. no fixed global session or caller-result mutation is introduced.

The test should assert the public result shape and receipt reference, not provider output or message content.

## Compatibility and rollback notes

- Keep `run_prompt`, CLI, and MCP behavior backward compatible.
- Make the adapter route additive and versioned; do not silently reinterpret old result dictionaries.
- Do not migrate or delete Harness history, mission packs, receipts, or worktrees.
- Roll back by removing the adapter route or reverting only its isolated change. A failed or blocked adapter must leave existing state byte-for-byte unchanged.
- If the requested behavior cannot be expressed through the existing API, stop and return a compatibility finding rather than adding a parallel runtime.

## Explicit stop conditions

Stop before implementation if any of the following is requested or discovered:

- a second scheduler, queue, session/history owner, retry policy, provider router, or worktree manager;
- a fixed `fastdriver` session or global mutable history;
- direct provider calls outside Harness policy;
- silent fallback from the primary path;
- a success claim based only on a model response, health check, or structural check;
- edits to source, tests, hooks, CI, deployment state, or consumer WIP under this handoff alone;
- an unresolved mismatch between the selected public entry point and `origin/main`.

## Unresolved prerequisites and evidence limits

- Harness owner must select and version the public compatibility entry point.
- The local checkout must be reconciled with `origin/main` before implementation.
- A clean isolated worktree and owner-approved hermetic contract test are required.
- Live provider behavior, native verification, deployment, and production availability are outside this record and require separate authorization.

This document is a Harness-owned compatibility and ownership handoff only. It is not authorization to implement, deploy, restart, send, migrate, or publish anything.
