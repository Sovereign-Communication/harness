# Architecture

Harness is a dependency-light Python package with three surfaces:

- `harness.cli`: command-line parsing and presentation;
- `harness.mcp`: JSON-RPC/MCP parsing and presentation;
- library modules: policy, orchestration, transport, and evidence.

## Ownership

- `config.py`: settings, model-pool defaults, lane budgets, and Jev endpoint/model/threshold configuration
  (`effective_lane_policy` is the ONE owner of per-lane output budgets and
  reasoning modes; lanes resolve policy through it, never locally).
  `resolve_hourglass(settings, opts)` is the ONE owner of the hourglass
  switch mapping: a per-request flag wins, otherwise the settings file. The
  CLI passes its parsed flags, MCP is seeded from it at startup, and the
  agent's edit lane passes no flags -- every lane therefore runs the same
  hourglass the settings file describes instead of re-deriving it.
- `jev.py`: the Phase 0 TypeSafe System One adapter: official primitive
  packs and answer parsing, input-token cost math, live thresholding, and
  honest local AST/JSON/diff fallback. Lane policy does **not** live here.
- `jev_policy.py`: the ONE Jev policy owner (JEV-P1/P3/P5): when a typed
  call may dispatch, bounded spend preflight, one ledger `jev_eval`, the
  shared `structural` envelope, and utilization packs (route, triage-files,
  context, claims, completion, issue-sort). `session.jev_for` and
  `engine_for` route through `policy_for` — no orphan `JevEvaluator`
  construction outside this owner.
- `jev_packs.py`: typed question packs + local heuristics imported by
  `jev_policy` (still one policy owner, never a second client).
- `jev_completion.py`: owns the phase-completion bar; packs in `jev_packs.py`; policy call via `JevPolicy.evaluate_phase_completion` (site `phase_completion`).
- `site_export.py`: the ONE boundary owner between a private evidence ledger
  and the public Proof Bench site (SITE-*): rebuilds sanitized runs from an
  allowlist of ledger events, requires a consent record and a hash chain
  that verifies, and refuses (never redacts) on secret-shaped content.
  No other module may produce a `site-bundle-v1` payload.
- `service.py`: canonical verify/claims request assembly shared by the CLI
  and web interfaces (prompt/claims reading, cancelled-run envelope,
  cost/meta attachment). Interfaces consume it; they do not re-derive the
  verify lane.
- `validation.py`: shared trust-boundary validation.
- `spend.py`: cost ceilings, pricing, BYOK, model discovery, and the HUL-B dual-budget envelope (ONE owner of `working_remaining = max_cost - spent - terminal_reserve`; attempt preflight/reserve/record never eat the terminal reserve; terminal findings may spend up to the reserve).
- `chat.py`: model transport payloads and output usability (one owner of the
  text-shape verdicts: `assess_output` for lane gates, `looks_truncated` for
  bodies cut off mid-JSON).
- `panel.py`: panel/judge verification. The judge seat is a rotation: one
  predicate (`_judge_fallback_candidates`) names both the preflight reserve
  seats and the runtime fallback candidates, so the worst-case ceiling always
  covers rotation; `_run_judge_attempt` is the single owner of one seat
  attempt (call, classify, bill, ledger, emit) shared by the primary, the
  bounded transient retry, and each fallback. BYOK judges stay
  single-attempt (`record_byok` only -- invisible spend never bills); the
  seat never fabricates a verdict (exhausted seat defers with raw panel
  outputs).
- `convergence.py`: deterministic structured-claim tally and specialist lane.
- `consent.py`: consent probes and renewal.
- `apply.py`: request preparation, model dispatch, rotation, and round orchestration.
- `apply_gate.py`: candidate writes, verification, previews, escalation gates, and failed-run transaction policy.
- `apply_state.py`: frozen request data and mutable per-run state for the apply engine.
- `batch.py`: multi-file orchestration over the apply engine.
- `prompts.py`: prompt and response contracts.
- `filesafety.py`: atomic writes, backups, and verification execution.
- `continuation.py`: persisted continuation authority and gate identity.
- `ledger.py`: hash-chained evidence.
- `results.py`: apply result vocabulary and exit-code policy.
- `rankings.py`: rankings-driven pool-candidate refresh (daily OpenRouter
  rankings -> catalog intersection -> one-vote probe gate). Advisory only:
  it never mutates configuration.
- `dag.py`: DAG data only (`DAGNode`, `TaskDAG`, validation, topological
  batches, serialization).
- `waist.py`: the plan lane (M1/M2) -- decomposition prompt/parse, tier
  classification (`plan_task`), single-pass fitting
  (`chunk_oversized_nodes`: a target past the engine's rewrite cap or
  output budget becomes ONE node with the `backend: "diff"` hint, while an
  instruction past `MAX_INSTRUCTION_CHARS` or a target larger than the
  rung's declared read budget -- `capability.source_budget_for` -- splits
  into ordered chunks), per-node routing kwargs (`node_apply_kwargs`), the
  confirmation gate (`confirm_plan`), and `compose_plan`. Callers plan by
  calling `compose_plan`; the confirmation gate resolves its own frontier
  rung (never a decomposition seam) and fail-closes on a refusal or an
  unreachable rung.
- `worktree.py`: `WorktreeIsolation` -- create/audit/**commit**/merge/discard
  per-node worktrees. `merge(handle, paths)` commits the node's declared work
  before merging the branch: a worker writes files but a branch carries only
  what the worktree committed, so merging an uncommitted worktree is
  "Already up to date" and the edit silently never lands while the node
  reports ok. Declared targets may be repo-relative (agent lane) or absolute
  (MCP lane); both are normalized to worktree-relative paths for the audit
  and the commit.
- `executor.py`: `FileLockManager`, `ConcurrentExecutor` (reserver/isolator
  seams), and `PlanExecutor` -- the ONE plan-lane execution assembly: worker
  count, per-node cost reservations, git-worktree isolation rooted at the
  tree being edited, write attestation, and per-node routing kwargs. The
  CLI, MCP, and the agent's edit lane all build this object, so how a plan
  runs is derived once, and each passes `run_ceiling` (the session budget it
  is really running under) so no lane bounds a reservation by a nominal
  default. The reservation itself is `spend.NodeReserver`'s: a node's own
  route ceiling (a free tier's $0.00 included) or a fallback capped by the
  run ceiling, with `SpendGovernor.remaining()` as the single answer to
  "what can this run still commit".
- `orchestrator.py`: the autonomous edit driver. It owns bounded
  plan -> execute -> completion-judge rounds, re-planning remaining scope,
  artifact-truth checks, and round/result state. `assess_completion`,
  `triage_files`, `keyword_fallback`, and `build_state_summary` are its
  injectable decision helpers; execution is supplied through one
  `execute_plan` callback backed by `executor.PlanExecutor`.
- `repo_scope.py`: repo file discovery and verification-gate discovery.
- `history.py`: chat-turn persistence and session listing (CLI/ui-server
  seam included).
- `web.py`: web context gathering for the chat lane.
- `agent.py`: the chat/GUI lane's intent classifier and dispatch façade; its
  edit path composes `repo_scope` -> `waist.compose_plan` ->
  `orchestrator.drive` -> `executor.PlanExecutor` -> `history`. It owns only
  lane-specific apply callbacks and presentation/event assembly, not round
  progression, completion policy, or plan state.
- `session.py`: dependency composition.
- `mcp.py`: MCP JSON-RPC framing, request lifecycle, tool contracts, boundary validation, and engine dispatch.

Interfaces translate input into shared request policy; they do not reimplement
engine or safety behavior. MCP and library callers pass through the shared
validators in `validation.py` before engine dispatch. Every network call is
governed, every model response is untrusted, and a gated edit is only successful
after the real verification command passes.

## Data flow and state ownership

```
CLI/MCP input -> validation.py -> session composition -> ApplyEngine/PanelEngine
                         |                 |                    |
                         |                 |                    +-> chat.py -> SpendGovernor
                         |                 +-> Router, Ledger, filesystem ports
                         +-> plain request data                         |
                                                          result dict <- results.py
```

Plan-lane direction (one owner per stage): interfaces validate -> `waist.compose_plan`
plans (decompose, classify, confirm) -> `executor.PlanExecutor` executes
(parallel workers, reservations, isolation, attestation) -> `spend`/`ledger`
record -> interfaces render. The agent's edit lane is a caller of that same
chain, with `orchestrator.drive` owning its judge loop; the hourglass switches
it reads come from `config.resolve_hourglass`, exactly as the CLI's do.

`ApplyRequest` is an immutable value object for one apply, including its bound continuation gate. `RunState` is the sole mutable transaction record and is passed explicitly through orchestration and gate operations. `GatePolicy` owns only filesystem/gate effects and result events; `AutonomyLedger` owns persisted evidence; and `Router` owns configured pools but is never mutated per request. `McpServer` owns JSON-RPC framing, tool contracts, shared boundary validation, lane scheduling (one serial worker each for mutation / spendy / observe), cooperative cancellation and per-tool deadlines, engine dispatch, and response lifecycle. Identified MCP request IDs remain reserved through response serialization, while notifications never emit responses. Rendering and exit-code policy remain at the CLI/MCP boundary (`cli.py`, `mcp.py`, `output.py`).
