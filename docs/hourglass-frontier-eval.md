# Frontier eval brief — the "Hourglass" end-state for Harness

> **How to use this file:** paste its entire contents into a frontier model
> (no repo access needed — everything material is inline). The model's job is
> to evaluate the proposed end-state against the real code facts below and
> decide the best path forward. If the model *does* have repo/tool access,
> Section 1.2 tells it where to look and what NOT to bother re-reading.
> This document also doubles as the reference design for a future
> `harness brief` feature (generate a context pack for any model on demand).

---

## 0. Role, mission, ground rules

You are a principal-level architect reviewing **Harness**
(`github.com/Sovereign-Communication/harness`, v0.3.3, MIT): a zero-runtime-dependency,
pure-stdlib Python package that does cost-bounded multi-model verification
and coding over OpenRouter, with hard spend guarantees, model consent
("sovereignty"), and a hash-chained autonomy ledger.

**Mission.** The operator wants an ambitious end-state — the **hourglass**:

1. **Wide base (cheap):** the cheapest capable models do as much as possible —
   triage, decomposition, first-pass edits, bulk voting — for near-zero cost.
2. **Narrow waist (expensive, rare):** a *condensed, evidence-backed brief*
   escalates to a frontier model that **confirms or repairs the plan** —
   explicitly allowed to *bypass reading/searching* because the brief already
   carries grounded context — and never sees the raw repo unless it truly must.
3. **Wide base again (cheap, parallel):** the confirmed plan is **pushed back
   down the cost ladder**: cheap models execute work packages, ideally in
   parallel and safely isolated (unique PRs per package) or cooperatively
   where the orchestrator decides they must share state.
4. Consensus and verification stay cheap; only *conflicts and hard nodes*
   re-escalate. Total cost stays a **computable worst-case guarantee**.

Net effect: frontier-grade intelligence and execution at commodity cost.

**Your deliverables** (Section 5 defines the exact format): a verdict, numbered
architecture decisions, a gap analysis against the code facts below, a phased
roadmap with hermetic-test acceptance criteria, a risk register, a red-team of
the pyramid itself, and a spec for the reusable `harness brief` context pack.

**Ground rules.**
- Preserve the repo's non-negotiables: zero runtime deps (stdlib only),
  Python 3.9, worst-case preflight before ANY network call, fail-closed on
  untrusted output, the verify gate as the only authority for "done",
  append-only ledger evidence, hermetic tests, ruff clean.
- Be concrete: every recommendation should name the module/function it
  touches (Section 1.2 is the map). Prefer extending existing one-owner
  modules over new ones; the repo's audit enforces ownership.
- Flag anything in this brief that contradicts the code facts — the brief was
  written by a repo-aware assistant, but you are the check.

---

## 1. Context pack — the repo as it stands (verified 2026-09-17)

### 1.1 Identity and hard discipline

- **Surfaces:** CLI (`harness` / `python -m harness.cli`), MCP stdio server
  (`harness-mcp`), library, and a web UI (`harness serve` / `harness desktop`,
  `harness/server.py` + `harness/ui/`). Pure stdlib runtime; optional extras
  only for dev tooling, local-fit training (numpy/onnx), and the desktop shell.
- **Cost guarantees, not hopes:** no `tools` key in any payload, ever;
  worst-case cost computed against live per-token pricing and refused at the
  ceiling (default $0.02/call ceiling, $0.10 hard max) *before* any network
  call; actual spend re-checked after every call (mid-batch fail-closed);
  BYOK org-prefix denylist + learned prefixes; key must have a finite spend
  limit; `--expect-key-label` identity check.
- **Sovereignty model:** consent is a separate cheap probe (accept/decline/
  defer/redirect), renewed per verify round, revocable mid-task; forced
  self-check (`HARNESS_READY: confident|defer`) rotates defer-only models
  before any write; `HARNESS_DEFER:` mid-task handoff preserves partial work
  as a **continuation**; unparseable consent fails closed to defer; every
  decision lands in the append-only, hash-chained JSONL ledger
  (`harness ledger verify` proves chain integrity; 10 MB rotation, anchors).
- **Verification authority:** rotating panel + JSON judge, rotation on ANY
  imperfect output (incl. the judge seat; truncation detection names it);
  deterministic convergence specialist with per-claim tally; **claims
  grounding lint (R1 ungrounded-absence, R2 out-of-window-ref, R3
  contradicted-by-source) runs hermetically before any spend**; the verify
  gate (a real shell command, `shell=False`, timeout, gate-identity-bound) is
  the only "done".
- **Capability routing:** `harness capability.py` blends declared `/models`
  metadata with ledger-observed evidence: composite reliability =
  0.4·capability + 0.3·calibration + 0.3·verify-success, prior-shrunk by
  sample count; hard gate to 0 when context can't hold the window; one owner
  (`model_reliability()`) shared by CLI, routing, continuation, and MCP.
- **Trust:** bipolar −11..+11 per principal (host/caller, model, continuation
  author), cold-start 0, refuses ≤ −6, preview-only band, ceilings rationed by
  a separate correctness score; every denial ledgered (`trust_gate`).
- **Free tier default:** curated free-model pools with live validation and
  rotation; `openrouter/free` final fallback; saturation policy warns when the
  whole tier is 429-limited (pre-run look-ahead + reactive verdict).

### 1.2 Module map (one owner per concern; the audit enforces this)

| Module | Owns |
|---|---|
| `config.py` | Settings, curated pools, **`effective_lane_policy` = ONE owner of per-lane token budgets + reasoning modes**; `frontier_model` setting (`HARNESS_FRONTIER_MODEL`); paid escalation ladder + `judge_top` |
| `session.py` | Composition root: governor/ledger/router/engine wiring; nothing else constructs engines |
| `spend.py` | `SpendGovernor`: preflight, per-actual enforcement, BYOK learning |
| `router.py` | Cheap-first ladder: pools, rotation, **multi-rung escalation ladder with judge-gated DE-escalation** (`de_escalate_to_rung`), `classify_and_route` → per-task tier spec |
| `sliding_scale.py` | Complexity classification into **tiers 0 (scout/simple) / 1 (standard) / 2 (deep/frontier)**, `tier_model_ladder`, `tier_cost_ceiling`, frontier resolution |
| `dag.py` | **TaskDAG**: atomic nodes (instruction, target_files, dependencies, local_gate, complexity_tier), Kahn cycle validation, `topological_batches()` (parallel stages), `ready_nodes(completed)`; LLM decomposition prompt + strict JSON parser (`build_decomposition_prompt`, `parse_decomposition_response`); heuristic fallback; `plan_task()` = decompose + per-node tier routing + summed cost ceiling |
| `executor.py` | **`ConcurrentExecutor`**: ThreadPoolExecutor; `execute_dag` runs topological batches **in parallel**, propagates `dependency_failed`, per-path `FileLockManager` (sorted acquisition, process-internal); `execute_files` for flat batches |
| `batch.py` | Multi-file apply over one engine; parallel mode via ConcurrentExecutor; batch envelope contract |
| `escalation.py` | `EscalationDriver`: walks rungs cheapest→capable; judge-condensed context prepended to later rungs; a rung only "wins" through the real gate; fail-closed on transport errors |
| `condenser.py` | **`MicroBrief`**: AST-signature extraction (Python), heuristic signatures (other langs), error-log condensation, token-budget pruning (`distill_context`) |
| `agent.py` | Chat/agent lane: `distill_context` → `plan_task` → `execute_dag` (currently `max_workers=1`, serial) |
| `panel.py` / `convergence.py` / `consent.py` / `prompts.py` | Panel+judge with rotation everywhere; specialist tally; consent probes |
| `apply.py` / `apply_policy.py` / `apply_gate.py` / `apply_state.py` | Apply engine lifecycle; per-round machinery (billing, consent, rotation, deferrals); candidate writes, verification, previews, rewind-to-original on failure |
| `continuation.py` | Resumable-state contract: `verify_cmd` + sha256 `verify_gate_id` bind-or-refuse; target-hash tamper check |
| `ledger.py` / `ledger_analytics.py` | Hash-chained evidence; read-only analytics (calibration, defer-stats, participation) |
| `trust.py` | Bipolar trust scores + gates |
| `capability.py` / `rankings.py` | Capability profiles, composite reliability, pool ordering; daily-rankings candidate refresh (CI, probe-gated) |
| `claims.py` | Claims manifest + grounding lint (R1–R3), auto-expansion of called-but-not-defined symbols |
| `mcp.py` / `mcp_lanes.py` / `mcp_schemas.py` | JSON-RPC/MCP framing; **serial lanes (mutation/spendy/observe)**; per-tool deadlines; cancellation; `allow_verify`/write gates |
| `server.py` / `ui/` | Web UI data layer (events JSONL, envelopes); chat lane with honest web tools (allowlist fetch, operator-set search endpoint) |
| `local_fit/` | Opt-in, OFF-by-default local scorer (stdlib ONNX-style runtime); advisory seat-outcome probabilities; OBSERVE→INFLUENCE gating; **measured: no routing lift over ledger observed rates on current corpus** |

### 1.3 Quality machinery

- ~60 hermetic test modules (no network, no key), run under
  `-W error::ResourceWarning`; ruff clean; CI enforces all of it.
- **Self-audit**: `audits/self/audit.py` encodes 44 checks as code (AST
  inspections, hermetic scenarios, doc-consistency greps); 9.5+/10 bar per
  dimension (Security, Reliability, Structure, Docs) fails the build; audit
  evidence is hash-pinned via `corpus_manifest.json`.
- Coverage baseline (stdlib-trace): **77% of executable lines overall** —
  honest gaps: `server.py` 26%, `mcp.py` 62%; the 95% bar binds *changed*
  lines only.

### 1.4 Honest residuals (documented, not hidden)

- `docs/system-one-integration.md`: fast-path triage + calibrated-confidence
  deferral designed; **Milestones 2–4 unimplemented** (confidence extraction
  into ledger, `HARNESS_MIN_CONFIDENCE` gating, Jev/System-One adapter).
- `local_fit`: safe, fail-closed, but proven to add no routing lift on the
  current small corpus (it recapitulates ledger observed rates); INFLUENCE
  mode stays off pending profile-enriched retraining.
- MCP `tools/call` streaming deferred; ledger is tamper-*evident* not
  tamper-proof; verify commands run with host privileges (no sandbox) —
  documented in THREAT_MODEL.md.
- Judge-less fallback: a converged panel whose every judge candidate fails
  still defers honestly (optional deterministic-tally last resort is an open
  follow-up, not built).

---

## 2. The hourglass proposal (evaluate this)

> **Update (2026-09-17, post-brief draft):** gap **1** below is now
> **implemented in the working tree** — `dag.node_apply_kwargs` (one pure
> mapping) threads each planned node's tier ladder into `apply_edit` as the
> per-request `apply_pool` (engine-ordered via `capability.ordered_pool`;
> interfaces never pre-order) and binds the tier cost ceiling as the
> per-task ceiling on paid tiers. Deliberate policies to review: a $0
> free-tier ceiling is *not* passed (a zero task budget would refuse the
> escalation ladder for hard nodes; the governor owns free-tier cost), an
> explicit `--model` still pins routing outright, and an explicit
> `--task-max-cost` is never tightened by tier policy. Wired identically
> into the CLI plan lane, MCP `plan_and_execute`, and the agent lane
> (including its healing retry); hermetically pinned in
> `tests/test_planning_surface.py`. Separately, a **pre-existing R13 leak**
> was fixed: `tests/test_web.py` closed the refused-redirect HTTPError whose
> unraisable GC warning failed the audit's leak scan on CPython 3.14
> (summary stayed OK, so the battery looked green while leaking). Review
> both diffs as part of this eval; gaps 2-8 stand as written.
>
> **New open finding from this session:** the self-audit's R13 gate
> (full-suite green hermetically) **flaked once under audit conditions** —
> a teardown race in the MCP frame loop (`serve_forever`'s
> `stdin.readline()` traceback at child-suite end) that does not reproduce
> standalone or on a re-run. A repo whose brand is deterministic evidence
> should not have a nondeterministic own-gate: consider whether lane-pool
> shutdown needs a join/drain contract and whether the audit should
> classify teardown-traceback signatures separately from summary failures.

### 2.1 Shape and stages

```
            +-----------------------------------------------------+
  user -->  | A PREP (cheap, parallel): decompose -> DAG, tier    |
            |    every node, scout tier-0 nodes immediately       |
            +------------------------+----------------------------+
                                     |
            +------------------------v----------------------------+
            | B CONDENSE (cheap): MicroBrief per stage + grounded  |
            |    claims + failure evidence -> ONE waist brief with |
            |    an explicit coverage contract + unknowns list     |
            +------------------------+----------------------------+
                                     |
            +------------------------v----------------------------+
            | C WAIST (frontier, bounded round-trips): confirm /   |
            |    repair / split the plan; may request up to K file |
            |    windows instead of open-ended reading; structured |
            |    verdict contract (approve|amend|split|refuse)     |
            +------------------------+----------------------------+
                                     |
            +------------------------v----------------------------+
            | D DISPATCH (cheap, parallel): per-node tier routing; |
            |    isolation mode per node: shared-tree+locks |      |
            |    worktree/branch -> unique PR | cooperative        |
            |    (serialized) when files overlap; per-node gate    |
            +------------------------+----------------------------+
                                     |
            +------------------------v----------------------------+
            | E ADJUDICATE (cheap; escalate only on conflict):     |
            |    panel consensus on results; stage-boundary +      |
            |    final FULL-suite gate; conflicts re-condense ->   |
            |    waist; failures resume via continuations          |
            +------------------------------------------------------+
```

Every arrow is ledgered; every network call is preflighted against ONE
composed ceiling for the whole pyramid run; consent is taken per dispatch
(exists today) plus once at the waist for the plan itself.

### 2.2 Primitive mapping — most of the hourglass already exists

| Hourglass need | Existing primitive (owner) | Status |
|---|---|---|
| Decompose goal into DAG | `dag.plan_task` + `TaskDAG` | live via `harness plan` — but **heuristic-only** |
| LLM-authored decomposition | `build_decomposition_prompt` / `parse_decomposition_response` | **built + tested, unwired** (no live caller) |
| Tier each node, pick ladder + ceiling | `sliding_scale.classify_task_tier`, `router.classify_and_route` | live in `plan_task` output |
| Parallel stage execution | `executor.execute_dag` (thread pool, `dependency_failed` propagation, file locks) | live via `harness plan --execute --parallel` |
| Escalate hard nodes | `escalation.EscalationDriver` + `router` rungs + `judge_top` | live, per-apply-run, opt-in |
| Condense context for escalation | `condenser.distill_context` → `MicroBrief` | live in agent lane; NOT used by the plan lane |
| Judge-gated de-escalation | `router.de_escalate_to_rung` | live |
| Cheap consensus on results | `panel.run_panel` + `convergence` specialist + claims lint | live (verify lane) |
| Resume partial work | `continuation` contract + `harness continue` | live, per-file |
| Safe writes | `apply_gate` atomic writes, symlink refusal, rewind, root containment | live |
| Per-caller trust for MCP drivers | `trust.py` bipolar scores | live |
| Frontier model setting | `config.frontier_model` (`HARNESS_FRONTIER_MODEL`) | live |
| Tier-down early warning | `saturation.py` | live |

### 2.3 Gaps and disconnects found in the code today (verify these)

1. **Plan/execute tier disconnect (the big one).** `cli._cmd_plan` computes
   per-node `complexity_tier`, `recommended_model`, `route.ladder`, and a
   summed `total_cost_ceiling` — then `run_node` calls `engine.apply_edit`
   with `model=opts.model` (or the engine default apply pool) and **never
   threads the node's tier/routing into execution**. The DAG's intelligence
   is computed and discarded. (MCP's plan tool at `mcp.py:~681-703` looks
   similar — confirm.) **[RESOLVED 2026-09-17 — see the update note at the
   top of Section 2; review the implementation rather than re-diagnosing.]**
2. **Decomposition is heuristic-only in the live path.** Numbered-steps or
   per-file splitting; the LLM decomposition contract exists but no lane
   calls it. There is no "cheap model decomposes, schema-validated" step.
   **[RESOLVED 2026-09-17: `harness plan --decompose-llm` wires the contract
   via `dag.decompose_via_llm` + `chat.governed_text`; tier-0-ladder-head
   model; heuristic fallback on `--execute`, loud failure on preview.]**
3. **No waist.** Nothing assembles a frontier brief (MicroBrief + grounded
   claims + failure evidence + coverage contract) for plan *confirmation*;
   no structured plan-verdict contract; no bounded file-request round-trip.
   Escalation today is per-apply-run file completion, not plan adjudication.
   **[RESOLVED 2026-09-17: `harness plan --confirm` (harness/waist.py) —
   approve/amend/refuse/request_windows verdict contract, ≤2 window rounds,
   ledgered `plan_verdict` events, refusals fail closed on every surface.]**
4. **No plan-consensus step.** Panels verify results; nothing asks a second
   (or the) model "is this decomposition sound?" before spending execution
   budget — the cheapest place to catch a bad plan is before stage 0.
5. **Isolation is single-tree.** `FileLockManager` is process-internal; parallel
   workers mutate one working tree. No worktree/branch/PR isolation, no merge
   ordering across parallel nodes, no global gate between stages.
6. **No composed ceiling for a pyramid run.** `plan_task` sums per-node
   ceilings; execution uses one session `max_cost`. Parallel threads share one
   governor — reservation semantics under concurrency are unproven.
7. **No DAG-level continuation.** Continuations are per-file; a failed stage
   marks dependents `dependency_failed` but there is no persisted, resumable
   pyramid state (bench.py's manifest pattern is the closest precedent).
8. **No global/final gate.** Per-node `local_gate` only; nothing runs the
   repo's full suite at stage boundaries or at the end, so parallel green
   nodes can still compose into a red tree.

---

## 3. Difficulty inventory — where this gets hard

1. **Composed worst-case ceiling under parallelism.** The guarantee "exactly
   computable before any network call" must hold for N concurrent cheap calls
   + reserved waist round-trips + escalation reserves. `SpendGovernor.preflight`
   reserves per serial call today; parallel threads sharing one governor need
   reservation slots per in-flight worker (the panel's `_chat_reservation_slots`
   pattern is precedent) and a locking story proven by tests, not asserted.
2. **Brief fidelity vs token budget.** The waist model must get everything it
   needs to NOT read the repo: signatures + focused windows + grounded claims
   + honest "unknowns/not-included" list — and the brief itself should pass the
   R1–R3 grounding lint (no ungrounded "there is no cap"-style assurances to
   the planner). Under-budget pruning must prefer failures over structure.
3. **Bounded interactivity.** "Bypass searching/reading unless necessary" needs
   a contract: up to K (e.g. 1–2) round-trips where the waist model names exact
   file windows to attach; everything else must be decided from the brief.
   Unbounded tool-loops are the cost hole this architecture exists to close.
4. **Parallel safety.** Shared-tree locks prevent concurrent same-file writes
   but not semantic interference (two green nodes, red composition). Worktree
   isolation per node (or per stage) turns this into a merge problem; the
   repo is stdlib-only, so `git worktree` via subprocess is host tooling (git
   is already required to develop), while forge/PR creation (gh/API) should be
   opt-in. Decide per node from file-overlap analysis in the DAG.
5. **Plan drift.** After stage k executes, the brief for stage k+1 (and any
   re-escalation) must reflect the mutated tree — re-condense policy needed
   (per-stage? on gate failure? on file-overlap with executed nodes?).
6. **Consensus semantics.** When does cheap consensus suffice? Working rule to
   evaluate: gate-pass + panel agreement=high → accept; anything else
   (low/unknown/split, gate-fail loops, cross-node conflict) → re-condense to
   the waist. Watch the known degeneracies: unanimous wrong panels
   (grounding lint + gate are the counterweights) and overconfident models
   (ledger calibration already tracks this per model).
7. **Sovereignty across tiers.** Consent probes exist per apply dispatch;
   add one plan-acceptance probe at the waist (the frontier model may
   decline/redirect too — and its decline is evidence, ledgered). Per-node
   `HARNESS_READY` + `HARNESS_DEFER` semantics carry over unchanged.
   Degenerate-consent detection already runs in the participation report.
8. **Concurrency surface.** Threads are fine (stdlib, precedent in
   `executor.py`); but MCP lanes are deliberately serial per class — a pyramid
   run driven over MCP needs either a batch-style job tool (bench precedent)
   or an explicit lane-scheduling decision. Don't quietly break the
   "status queries never head-of-line-block" guarantee.
9. **Failure taxonomy & replay.** `dependency_failed` exists; needs a persisted
   pyramid state (goal, DAG, per-node results, briefs, waist verdicts,
   ceilings spent) so any stage can resume under a new model — continuation
   per node, bench-style manifest for the whole run.
10. **Audit/coverage bar.** New engine modules must land with hermetic tests
    and one-owner separation from day one (the self-audit will fail the build
    otherwise). Design the roadmap so each phase ships with its pins.

## 4. Decision points (answer each; current lean is a strawman, not a verdict)

- **D1 — Waist scope:** per-pyramid (one plan confirmation) vs per-stage vs
  conflict-only. *Lean:* one plan confirmation + conflict/hard-node-only
  re-escalation (already the EscalationDriver shape).
- **D2 — Isolation model:** shared tree + file locks (today) vs `git worktree`
  per node with unique branches/PRs vs hybrid (partition independent nodes by
  file overlap; worktree only where overlap-free). *Lean:* hybrid; PRs opt-in.
- **D3 — Decomposition owner:** keep heuristic vs wire the existing LLM
  decomposition (cheap model, schema-validated, heuristic fallback) vs
  frontier-authored. *Lean:* cheap-LLM + schema validation, frontier *confirms/
  repairs* at the waist (D1), heuristic stays as the offline/degraded path.
- **D4 — Plan-verdict contract:** JSON schema for the waist response
  (`approve|amend|split|refuse`, amended DAG, file-window requests, reasoning
  bounds). Reuse consent/claims contract patterns; hermetic parser tests.
- **D5 — Global gate policy:** full-suite gate at stage boundaries vs
  final-only vs per-node + final. What IS the gate in foreign repos (no
  suite? `compileall`? operator-specified)? *Lean:* per-node + stage-boundary
  + final, all operator-overridable.
- **D6 — Surface:** extend `harness plan --execute` vs new `harness pyramid`
  command vs agent lane; what does MCP expose (a single `plan/run` tool
  mirroring bench)? *Lean:* extend the plan lane; MCP gets one batch-style
  tool; keep `agent.py` consuming the same engine.
- **D7 — Ceiling UX:** one `--max-cost` for the whole pyramid (composed
  preflight) vs per-tier budgets (`HARNESS_*` env ladder). *Lean:* composed
  single ceiling with visible per-stage reserves in the envelope.
- **D8 — System One fast path:** implement Milestones 2–3 (confidence
  extraction + `HARNESS_MIN_CONFIDENCE` gating) as the tier-0 triage gate, or
  defer? *Lean:* defer until the pyramid works with explicit signals only.
- **D9 — PR mechanism:** patch-bundle artifacts (forge-agnostic, stdlib) vs
  subprocess `git worktree` + branch vs opt-in `gh`. *Lean:* worktree+branch
  core, gh optional.
- **D10 — Explicit anti-scope:** what should we NOT build (multi-agent chat,
  unbounded tool loops, always-on frontier review of green runs, sandboxing
  claims beyond THREAT_MODEL's honest residuals)?

---

## 5. Required output format (what we want back)

Answer in this order, tersely, with module-level specificity:

1. **Verdict** — build the hourglass as proposed / build with amendments
   (list them) / don't build (say what to build instead). One paragraph.
2. **Decisions D1–D10** — one each: choice + 2–3 sentence rationale grounded
   in the code facts above.
3. **Gap analysis** — confirm/correct the eight disconnects in Section 2.3;
   add anything the brief missed; name dead or duplicated seams you'd delete.
4. **Phased roadmap M0–Mn** — each phase: goal, touched modules, new
   contracts, hermetic acceptance tests (name the test file), ceiling math
   (worst-case composed cost), and what stays OUT of the phase. Phases must
   be independently shippable and CI-green.
5. **Risk register** — top risks with mitigations; mark any risk you consider
   disqualifying.
6. **Red-team the pyramid** — how an attacker (or a merely-confused model)
   abuses THIS architecture: brief poisoning via fetched web content,
   consensus gaming across cheap models, ceiling fraud via decomposition
   inflation, PR poisoning, plan-verdict marker smuggling, worktree escape.
   Map each to a control (existing or to-add) or an accepted residual risk.
7. **`harness brief` spec** — the reusable context pack this document
   prototypes: required sections, grounding rules, size budget, freshness
   contract, and the CLI/MCP shape (`harness brief --goal ... --out ...`?).
8. **Highest-value open questions** — the ≤10 questions whose answers would
   most change the design, for the operator to rule on.

## 6. Appendix — operator question tracks (the reason for this eval)

- **T1 Red-team (highest value):** the repo's own audit is introspective and
  scores 10/10 on its four dimensions; what it cannot see is an outside
  adversary. Attack the guarantees: cost ceilings, consent fail-closedness,
  ledger trust, marker stripping, trust-score gaming, bench sandbox escapes.
- **T2 Architecture:** the hourglass above — is the waist the right place to
  spend frontier tokens (plan confirmation) vs alternatives (frontier as
  judge-only, frontier as decomposer-only, frontier on-demand per node)?
- **T3 Product:** does "frontier intelligence at free-tier cost" survive
  contact with real free-tier saturation (429s, reasoning-only bodies)?
  What is the honest failure story to users when the waist model is
  unreachable mid-pyramid?
- **T4 Meta:** the repo's superpower is evidence discipline (ledger, audit,
  calibration). What feature would most *compound* that advantage, and what
  fashionable feature would most *dilute* it?

### 6.1 Questions rebalanced after the 2026-09-17 implementation

Gap 1 no longer needs diagnosis — it needs **review of its policy choices**,
and two questions got sharper:

- **R1 (was gap 1):** Is "a $0 free-tier ceiling is never passed as
  `task_max_cost`" the right call (it would silently refuse the escalation
  ladder via `spent - start < 0.0`), or should the free tier get an explicit
  escalation budget knob instead of relying on the governor's $0-billing
  discipline? Should the tier ceiling *compose* into a pyramid-level
  reservation instead of binding per task?
- **R2 (sharper D3 ordering):** per-node routing now *moves money* — the
  heuristic decomposition's tier guess decides which ladder spends. Does
  that make wiring the schema-validated LLM decomposition (M1) a
  **prerequisite** for the waist (M2) rather than a parallel track — i.e.
  should frontier review confirm-or-repair the *heuristic* plan knowing the
  classifier's keyword bias (e.g. "refactor" pins tier 1; a heuristic-missed
  tier-2 node runs on a tier-1 budget)?
- **R3 (new, from the R13 flake):** hermeticity under thread teardown — the
  audit's own reliability gate can flake on the MCP frame-loop shutdown.
  Red-team the *test determinism* itself: where else do lane pools, event
  sinks, or ledger rotation leave nondeterministic teardown evidence that a
  stricter CI interpreter (3.14) will surface as a flaky gate?

### 6.2 Addendum 2026-09-18 — MR-7/MR-8 outcomes (run through Harness itself)

- **R1 RESOLVED (MR-2 consensus): "omit is correct."** Grok + Astra,
  independent families: a $0.0 per-task ceiling would block the escalation
  ladder without adding protection (free-tier attempts are preflighted and
  billed $0.0 against the governor's key-level ceiling); a paid-escalation
  knob is a separate paid-fallback policy, warranted only if free-tier
  nodes should ever invoke paid models by intent. No code change.
- **MR-7a (ceilings/BYOK): the named attack — concurrent preflights each
  passing against the full remaining ceiling — is exactly what M3's
  reservations close** (`SpendGovernor.reserve`/`reconcile`/`outstanding`,
  pinned by `test_concurrent_dispatch_cannot_overcommit_ceiling`). The
  attack validated that the pre-M3 guarantee set was insufficient; the
  fix shipped in the same cycle.
- **NEW MILESTONE CANDIDATE (M4) — diff-bound independent authorization
  before write.** MR-7b and MR-7c converged independently on the same
  control: consent today is intent-level (the sovereign accepts path +
  instruction + content-so-far; renewal re-probes BEFORE the round's
  model call, so the final bytes are never sovereign-seen), and brief
  poisoning defeats any brief-only review via semantic laundering (a
  poisoned input becomes a cheap model's "established requirement"
  summary, cited while the contradicting contract sits outside the
  window). The control that survives both attacks: a verifier OUTSIDE
  the brief pipeline authorizes the exact proposed diff, fail-closed,
  with the attestation bound to the diff hash; missing evidence =
  rejection. Contract drafting is MR-9 (see
  docs/hourglass-micro-requests.md).
- **MR-8 RESOLVED — grounding rules for the pack's own claims.** Minimal
  shape for any generated brief: a required `grounding` object —
  `sources: [{id, path, sha, span|quote}]`, `claims: [{text,
  source_ids}]`, `unknowns: [str]`; uncited assertions are invalid (drop
  or list as unknowns); models may use only cited windows. This is the
  spec seed for the `harness brief` builder and the R1–R3 grounding lint.

---

*Context pack prepared 2026-09-17 from the working tree at commit `8221bfc`
(main). Facts above were verified against the code and docs as of that date;
where the brief says "confirm", the reviewing model should treat the point as
open rather than settled.*
