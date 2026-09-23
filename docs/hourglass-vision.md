# Harness Hourglass — high-level vision

**Status:** vision document; not an implementation plan or source of operational STATUS  
**Purpose:** keep product design and future roadmap slices aligned with the operator’s intended token-budget flow.  
**Canon:** implementation status, sequencing, and acceptance gates remain in [jev-roadmap.md](jev-roadmap.md).
so1. The product idea

Harness coordinates model work under explicit limits and with evidence a human can inspect. Its Hourglass is a modular workflow for turning broad context into a sound plan and then carrying that plan out. The shape describes how token allowance changes across the work, not a requirement to use every model tier or every stage on every request.

The complete flow has three regions:

1. **Context intake — broad allowance.** Lower-cost capable models can inspect more source context and perform broad extraction, scouting, and condensation. This stage turns raw material into grounded, progressively smaller briefs.
2. **Planning waist — narrow allowance.** Increasingly capable models receive curated briefs and smaller token allowances. They resolve uncertainty, answer the question, or create and refine a bounded plan. The waist is where the plan is made; it is constrained by token policy as well as the independent dollar ceiling.
3. **Execution — allowance opens again.** If action is needed, the strongest model required to produce or validate the plan hands off well-defined work packages. Capable, lower-cost models can execute those packages with a larger token allowance appropriate to implementation, while remaining within the run’s cost and safety limits.

```text
Broad source context
        ▼
   ╭────────────────────────────────────────╮
   │ CONTEXT INTAKE                         │
   │ lower-cost capable models               │
   │ broad source coverage · broad allowance │
   ╰──────────────────╮  ╭──────────────────╯
                       ╲╱
                       ╱╲
                  ╭───╯  ╰───╮
                  │ PLANNING  │  increasingly capable models
                  │   WAIST   │  curated context · tighter tokens
                  ╰───╮  ╭───╯
                       ╲╱
                       ╱╲
   ╭──────────────────╯  ╰──────────────────╮
   │ EXECUTION                               │
   │ capable, cost-effective worker pool    │
   │ work-package context · allowance widens │
   ╰──────────────────┬─────────────────────╯
                      ▼
          JEV completion alignment check
          full request + relevant source context
                      │
             complete ─┴─ needs iteration
                │                 │
                ▼                 ▼
             report      JEV selects phase target
                          │       │       │
                    context   planning  execution
                          ╲       │       ╱
                           ╰── restart at target
```

The diagram is conceptual: token limits, context size, capability, and price are distinct dimensions. A model’s token allowance is not inferred from its price, and a dollar ceiling is never replaced by a token limit. The upper chamber widens after the planning waist: it represents an expansion of available execution tokens and parallel worker capacity, not a reversal of the information flow or a return to planning. Verification returns a decision to a named stage only when the original request is not yet satisfied.

## 2. Modularity is a product requirement

The complete flow is available when useful, but callers may select an individual capability or a subset of stages. Examples include:

- Condense supplied files into a grounded brief without planning or execution.
- Create or review a plan from an existing brief without gathering repository context.
- Execute a supplied, already-approved plan without rerunning broad intake.
- Run the full context → planning → execution flow for work that benefits from it.

Each stage must have a clear input and output contract so it can be used independently or composed. Selecting a partial flow must not secretly invoke omitted stages. A composed flow must preserve provenance and budgets across stage boundaries. Shared primitives should have one owner; independent product surfaces should adapt to those primitives rather than fork their policy.

## 3. Stage contracts

### Context intake and condensation

**Input:** a user goal plus explicitly available files, records, or other authorized context.  
**Work:** gather and inspect context within an intake allowance; extract code-owned facts where possible; produce a concise brief that preserves evidence, source references, coverage, uncertainty, and unresolved questions. Cheap models may assist with semantic extraction, but may not invent facts or silently promote unsupported claims.  
**Output:** a versioned brief with source identities, included/excluded scope, freshness information, estimated token size, and known gaps.

The allowance here is intentionally wider than at the planning waist. Large raw inputs should be processed in bounded chunks and summarized, not passed wholesale to each later model.

### Planning waist

**Input:** the grounded brief, goal, budget policy, and explicit unknowns.  
**Work:** route only as much capability and token allowance as needed. Each escalation receives the accumulated evidence and open questions, not an unbounded transcript. The stage may answer directly, ask for bounded additional evidence, produce a plan, or defer/refuse.  
**Output:** an answer or a schema-validated plan with bounded steps, dependencies, expected artifacts, execution constraints, verification gates, and remaining uncertainty.

Token allowances tighten toward the capable planner. The waist has explicit per-call and aggregate token limits. Its model may not increase those limits itself. If the answer is already sufficient, stop; reaching the most capable model is a fallback, not a success criterion.

### Execution and verification

**Input:** a validated plan or well-defined work package, the relevant evidence, the applicable consent decision, and its assigned limits.  
**Work:** open token allowance relative to the waist so execution can include necessary local detail. Route work to the least costly capable executor. Parallelize only where the plan and isolation rules make it safe. Each executor may accept, decline, redirect, or defer. Verification remains independent of the builder and remains the authority for completion.  
**Output:** artifacts or changes, per-package verification evidence, spend/token accounting, and resumable handoffs for incomplete work.

The intended completion contract asks JEV to check alignment and completeness against the original user request and the relevant source context needed to interpret it. This verification input is not passed through another generative condensation step: code supplies the original request and retained source/context references, with any bounded excerpting or chunking made explicit. Existing Harness completion checks use a truncated state summary; this direct-reference contract remains to be implemented. JEV can flag unmet requirements through declared typed outcomes, while independent code-owned tests and verification continue to decide whether the work is actually complete.

If the result is incomplete, the proposed JEV judgment may select a declared restart target: **context intake** when source coverage or grounding is missing; **planning waist** when the evidence is sufficient but the plan or acceptance criteria need revision; or **execution** when the plan remains valid and a bounded work package needs correction. The proposed controller validates this enum target against preserved evidence, policy, and current limits; it preserves completed work, re-preflights budgets, and renews consent when the assignment identity changes. This controller and full assignment-bound consent are future work, not current Harness behavior. JEV cannot directly dispatch, rewrite state, or bypass a stage contract. A sufficient result exits with its alignment and independent-verification evidence.

## 4. Policy boundaries and invariants

- **Token budgets and money ceilings are separate.** Enforce input/output token limits for each stage and call; independently preflight and enforce the composed dollar ceiling before network calls.
- **No hidden stage activation.** Optional stages are opt-in or governed by explicit resolved settings. The envelope reports which stages ran and why.
- **Grounding survives condensation.** Brief claims link back to source material or are labeled as inference/unknown. Condensation must not erase material disagreement or uncertainty.
- **Consent follows the work.** Before dispatch, show the model the actual assignment and applicable context. Accept, decline, redirect, and defer are valid outcomes; malformed or missing consent fails closed. A mid-task deferral stops that assignment and records a handoff that can be resumed without pretending it completed.
- **Plan authority is bounded.** Execution workers receive only their approved package and necessary context. Any material scope or dependency change returns through the planning contract.
- **Verification decides completion.** A model’s confidence, consent, or completion claim cannot substitute for code-owned gates and independent verification.
- **Evidence is inspectable.** Record stage, model/rung, requested and actual token use where available, estimated tokens otherwise, cost, brief identity, consent outcome, fallback/escalation, and verification result in the run envelope and ledger.
- **Failure is explicit.** If a stage cannot meet its evidence, budget, consent, or verification contract, stop or defer with a useful reason; do not silently treat fallback output as equivalent to a successful live judgment.
- **Composable by default.** CLI, MCP, agent, and other surfaces use the same stage owners and resolved policy; they do not implement independent versions of the Hourglass.

## 5. Vocabulary

- **Allowance:** maximum tokens made available to a call or stage. Input and output limits should be distinguishable when the provider supports them.
- **Cost ceiling:** maximum monetary spend authorized for a stage or composed run, including worst-case reservations.
- **Brief:** a compact, provenance-bearing representation of relevant context, evidence, and unknowns.
- **Rung:** a configured model/capability option in a routing ladder. A rung does not imply a token budget by itself.
- **Waist:** the constrained planning region where evidence is turned into an answer or plan.
- **Work package:** a bounded execution assignment derived from a validated plan.
- **Handoff:** a durable record of deferred, redirected, or interrupted work, its completed evidence, and the next safe action.

## 6. Relationship to current Harness work

Existing components provide useful foundations, not proof that the full vision is already implemented:

| Vision capability | Existing foundation | Remaining vision-level question |
|---|---|---|
| Condense source context | `harness/condenser.py` (`MicroBrief`, `distill_context`) | How are intake allowances, provenance, coverage, freshness, and unknowns represented end to end? |
| Resolve stage settings | `config.resolve_hourglass` | Can callers select each stage independently with explicit, composable contracts? |
| Build and confirm a plan | `harness/waist.py`, `TaskDAG`, `--decompose-llm`, `--confirm` | Are planning token limits progressively tighter and independently enforced from cost ceilings? |
| Route and execute work | router ladders, per-node routing, DAG executor | Does execution explicitly reopen its token allowance while retaining bounded work packages and composed spend? |
| Consent and deferral | `harness/consent.py`, apply lifecycle, continuation/handoff records | Is consent tied to the exact package/context at every dispatch and renewed safely after material changes? |
| Spend and evidence | `SpendGovernor`, ledger, envelopes | Are token budgets, actual usage, stage transitions, handoffs, and composed cost visible consistently on every surface? |

The roadmap remains authoritative for which gaps are open and what should be implemented next. In particular, existing roadmap items concerning condensed state and a grounded waist brief should be evaluated against the whole flow here; completing one local condensation seam alone does not establish the complete modular token-budget Hourglass.

## 7. Jev as a modular judgment layer

Jev should be available at multiple decision points across Harness, including Hourglass, without becoming a second orchestrator or replacing code-owned facts. The intended integration design is plug-in-like in composition: a caller selects a named judgment capability and supplies its typed, bounded state; shared policy handles keying, preflight, confidence, fallback, spend settlement, and ledger evidence. A capability that is not selected should not run implicitly.

The current architecture already has a useful foundation: `JevPolicy` is the shared owner, and `jev_packs` contains typed question-pack patterns with declared choices and score levels. Existing entry points span apply candidate/diff evaluation, plan and route judgments, file triage, claim support, completion, scope, issue sorting, log analysis, repository summary, model routing, and phase completion. This breadth is real, but it is implemented as individually named policy methods and pack schemas; it is not yet a general extension protocol for adding an integration type.

For the Hourglass vision, assess Jev integrations by stage and decision. Candidate capabilities include:

| Stage | Judgment opportunity | Authority boundary |
|---|---|---|
| Context intake | Relevance, treatment, coverage gaps, source conflicts, and whether a brief preserves decision-critical evidence | Jev may classify or flag; code owns inventory, source identity, hashes, counts, and budget enforcement |
| Planning waist | Whether evidence is sufficient, whether the plan answers the request, whether dependencies/scope/risks are missing, and whether more bounded evidence is needed | Jev may assess declared criteria and recommend amend/request/refuse; code validates schema, targets, DAG, limits, and allowed actions |
| Execution dispatch | Whether a package is clear, appropriately scoped, and suitable for the selected capability | Jev may advise route/complexity; code owns available models, authorization, spend, and dispatch eligibility |
| Execution checkpoints | Whether a worker’s result matches the assigned package, whether a material change invalidates consent, and whether to defer/escalate | Jev may flag semantic concerns; code owns diff hashes, consent state, gates, and stop/continue mechanics |
| Verification/closeout | Whether claims are supported by artifacts and whether evidence supports the requested outcome | Jev may assess bounded semantic claims; independent code-owned verification decides completion |

These are design coverage areas, not an instruction to call Jev at every point. Each capability should have a unique stable name/site, a versioned operator-declared pack or question contract, explicit state fields and evidence references, typed outputs with declared values only, fallback semantics, confidence/abstention behavior, cost/token preflight, one ledger event per judgment, and hermetic keyed/unkeyed/invalid/transport-failure tests. The result envelope should state the capability, pack version, live versus fallback status, confidence where meaningful, actual/estimated usage, and the effect on downstream policy.

Prefer extending the existing `JevPolicy` + `jev_packs` owner over adding a parallel plug-in framework now. First identify repeated contract structure and missing shared lifecycle behavior; then define a small registry/adapter protocol only if it reduces duplication without permitting arbitrary code or model output to bypass pack validation, fail-closed behavior, spend governance, or auditability. “Plug-in modular” here means capabilities can be composed and omitted through explicit interfaces; it does not imply loading untrusted third-party executable plug-ins.

### JEV vision-assessment result

The existing `harness jev-phase --phase HG --local-only` path returned **96/100** with `can_mark_complete=true` for the shipped Hourglass implementation’s phase-completion evidence. Its category results included `residual_scope = at_risk` (`residual_untracked`); this is a phase-bar result, not an assessment of this vision document. The command’s contract requires repository phase evidence such as merged PR, named tests, CI, and open blockers. It cannot fairly score a standalone design vision.

No live JEV judgment of this vision has been run yet. The current phase-completion axes also do not evaluate product qualities such as modularity, token-budget shape, composable integrations, stage contracts, consent handoffs, or completeness of JEV integration coverage. A purpose-built, operator-declared **vision/design assessment integration** is therefore a required Harness improvement. Score answers should supply typed values only; supporting evidence should be code-owned references to the assessed material, not model-generated rationales. A category below its declared top level should map to a code-declared improvement bucket. The assessment must not reuse `can_mark_complete` or imply implementation readiness. Historical keyed JEV runs are recorded in the roadmap and dogfood receipts; credential availability and pricing for a future pilot are operational checks. Do not treat the 96/100 HG phase score as validation of this vision.

## 8. Not implied by this vision

- Every request must use the full Hourglass.
- Every request must reach the most capable or most expensive model.
- “Cheapest” alone is sufficient to select an executor; capability and task requirements still matter.
- More input tokens always improve context or correctness.
- A plan can expand its own authority, cost ceiling, or token allowance.
- Multiple model opinions constitute verification by themselves.
- The system may continue after a model declines or defers its assignment.

## 9. Design questions to settle before implementation slices

These questions are intentionally left open rather than disguised as settled policy:

1. Are stage token allowances configured as per-call maxima, stage totals, or both? Which defaults and operator overrides are required?
2. Should limits distinguish input tokens from output tokens, reasoning tokens, and cached tokens, and how should providers with incomplete usage reporting be handled?
3. What is the canonical brief schema, including source hashes, line/symbol references, coverage, freshness, trust labels, and unknowns?
4. Which planning outcomes may request more source windows, and what bounds apply to those requests?
5. How much execution autonomy is allowed after a planner hands off: fixed packages only, or bounded replanning within a package?
6. What exact events make prior consent stale and require renewal: changed files, changed instructions, changed model, changed token/cost limits, or all of these?
7. How do partial stage selections compose budgets and provenance when the caller supplies a brief or plan created elsewhere?
8. Which measurements demonstrate that token spending is decreasing through the waist and opening for execution without reducing verified outcomes?

## 10. How to use this document

Use this as the product-vision reference when evaluating Hourglass proposals and future work. Before implementation, translate a chosen gap into an identified row and acceptance criteria in `docs/jev-roadmap.md`. Do not use this document to mark work complete or create a competing operational plan. Update it only when the product vision itself changes.
