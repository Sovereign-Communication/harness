# Hourglass Freebuff lane prompts

These prompts translate the `HV-*` rows in [jev-roadmap.md](jev-roadmap.md) into bounded Freebuff assignments. The roadmap remains canonical for scope, dependencies, acceptance, and status. Use the kickoff order below; do not launch the three parallel prompts until the JEV foundation and vision assessment have been reviewed.

## Safe launch order

1. Run `HV-0` alone. It defines the reusable JEV integration contract and the dedicated design/vision assessment capability. Use its single vision assessment as the Freebuff pilot. Resolve real gaps in the vision or plan and update the canon roadmap before parallel work begins.
2. After `HV-0` is merged and its keyed/free behavior, assessment categories, and pilot are verified, launch `HV-1`, `HV-2`, and `HV-3` concurrently in three separate worktrees based on the current `origin/main`.
3. Integrate and merge the three PRs one at a time after rebase and green gates. Then run `HV-4`, `HV-5`, and `HV-6` sequentially, each in its own fresh worktree and PR.

The three concurrent ownership boundaries are: JEV policy and packs (`HV-1`); context condenser and brief (`HV-2`); token allowance/accounting and its config (`HV-3`). If a required shared API is missing, report the exact interface needed and wait for the owner to resolve it. Do not edit another lane's owned files to work around the gap.

## Shared instructions for every lane

Use the repository at its latest `origin/main`; fetch and inspect the canonical [jev-roadmap.md](jev-roadmap.md) first because the operator `main` checkout may be stale. Keep all implementation in a dedicated worktree and one branch/PR for the assigned `HV-*` row. Do not push directly to `main`, merge your PR, or claim completion from local results alone. Do not redo work already merged; reconcile the current tracker and active worktrees first.

Implement only the assigned lane and named acceptance criteria. Preserve the Hourglass product shape: broad context gathering with relatively generous token allowance; a planning waist that narrows curated context and token allowance as capability increases; then execution where token allowance expands again for capable, cost-effective workers. Stages are optional and composable; a request may start from a supplied brief or plan and omit other stages. Token ceilings and the existing monetary spend ceiling are independent limits.

JEV supplies typed semantic judgments at explicitly selected integration points. Code owns exact facts, authorization, budgets, schema validation, dispatch, and verification. Reuse the single `JevPolicy` and pack owner. Do not create a second JEV client, use `jev-phase` as a design-quality score, treat local fallback as a live JEV result, or add arbitrary executable third-party plugins. Keep keys and other secrets out of source, prompts, logs, and reports. Any live API call must be expressly within the operator's requested task, preflighted through the shared spend owner, bounded to the planned single call where specified, and honestly reported with live/fallback, cost, and usage evidence.

Follow `AGENTS.md`, `CLAUDE.md`, `.claude/` lane rules, and `.agents/skills/typesafe-ai/SKILL.md` where applicable. Use hermetic tests and the exact gates named in the `HV-*` roadmap row; do not add unrelated test infrastructure. Do not refresh coverage baselines to conceal new untested lines. Keep canonical STATUS in `docs/jev-roadmap.md` only, and update it as part of the PR. Stop with a concise blocker report if scope overlaps another active owner, a required contract is unavailable, or a gate fails; include the exact evidence and the smallest needed decision.

When finished or blocked, report the branch/PR title, changed areas, named gates and results, remaining risks, and whether the lane can merge or needs a decision. Never claim work is complete before the repo's required audit, Jev phase bar, and CI conditions pass.

---

## `HV-0` — JEV integration foundation and vision-assessment pilot

**Goal:** Establish a reusable, composable JEV integration contract and a dedicated, category-based assessment for the Hourglass vision and implementation plan. This is a design-quality judgment, distinct from phase completion and implementation readiness.

Read the full vision document, this roadmap's Hourglass realization rows, and all current Claude planning/progress details that are relevant to this scope. The assessment context should include the vision, current implementation foundations and gaps, Claude's original intended scope and current work status, and the proposed `HV-0` through `HV-6` plan. Sanitize out credentials, IP addresses, machine-specific home paths, unrelated personal/session details, and raw logs. Preserve material technical facts, PR state, dependencies, and remaining work. Before the live call, report the sanitized payload outline and estimated input size; send exactly one full-context JEV request only after shared-policy preflight succeeds. Do not retry on transport or parse failure. If the key or safe budget is unavailable, report the live assessment blocked and use only hermetic fakes for implementation tests.

Declare the assessment categories and their ordered levels before asking JEV. At minimum cover modularity/composability, token-budget shape, grounding/context evidence, planning quality, execution boundary, JEV integration coverage, sovereignty/consent/defer, observability, independent verification, and cost/resource bounds. For each category return the score/level, evidence, and improvement bucket(s); declare what constitutes a perfect result. Report a category below perfect with the gap and a specific candidate improvement. Do not reduce the response to one aggregate score. The output must distinguish live JEV from fallback and include model, usage, and cost when provided. A perfect JEV result is advisory evidence, not a completion gate.

Reuse `JevPolicy` / `jev_packs`; create the smallest typed, versioned assessment contract and a single supported entry point that fits existing architecture. Preserve key preflight, bounded cost reservation/settlement, confidence/abstention, explicit fallback, and one ledger event. Test keyed behavior with hermetic transport doubles, plus unkeyed, malformed, transport-failure, budget-refusal, category coverage, and “not a phase score” behavior. Record the live pilot result only if it actually ran. Update the vision or plan only where the evidence warrants it. Do not implement the other `HV-*` lanes in this PR.

**Done when:** the reusable contract and category assessment are implemented and documented; named hermetic tests and repo gates pass; the assessment has either one honest live pilot result or a precise live blocker; non-perfect categories are addressed in vision/plan or explicitly tracked; and the PR is independently reviewed and passes the repository's merge requirements.

## `HV-1` — Stage-specific JEV integration coverage

**Goal:** Apply the shared `HV-0` JEV contract to independently selectable Hourglass judgments.

Cover context relevance/coverage/conflict; planning sufficiency, soundness, and evidence requests; execution package suitability/checkpoints; consent freshness, defer, and escalation signals; and verification-claim support. For every capability, state what code owns versus JEV judges, define the versioned typed pack and declared outcomes, explicit confidence/abstention/fallback behavior, budget/preflight, and ledger evidence. Do not call JEV where no decision is needed. Do not let JEV grant consent, alter budgets, dispatch work, or certify completion.

Own JEV policy/packs and the integration matrix only. Do not edit condenser/brief, token accounting/config, waist composition, or CLI/MCP surfaces. Publish stable APIs for later lanes. Tests: `tests/test_hourglass_jev_integrations.py` plus relevant JEV pack/policy tests and repo gates.

**Done when:** each named capability can be selected independently through the shared owner; keyed and degraded behavior is truthful and bounded; invalid/unavailable results cannot authorize a protected action; event evidence identifies capability and outcome; and the named gates pass.

## `HV-2` — Evidence-bearing context brief

**Goal:** Produce a bounded, reusable brief that retains grounding and makes missing or pruned context visible.

Extend the existing condenser/brief path. The brief should carry goal, source identity/freshness, scope coverage, evidence references, included and excluded material, uncertainty/conflicts, and an honest token estimate. Preserve decision-critical evidence under pruning and identify omissions/truncation. Provide independent create, validate, and render/use behavior so a caller can supply a brief without running context intake. Treat `MicroBrief` as a starting point rather than presuming it already satisfies the contract.

Own `harness/condenser.py` and only a focused brief module if needed. Do not edit Jev policy/packs, spend or token-budget config, waist composition, or surfaces. Publish schema/helpers for downstream work. Tests: `tests/test_hourglass_brief.py` plus relevant condenser tests and repo gates.

**Done when:** a brief is portable, bounded, validated, evidence-bearing, and explicit about uncertainty and omissions; supplied briefs can be used without intake; and the named gates pass.

## `HV-3` — Token allowance and accounting owner

**Goal:** Add one composable token-budget owner that is independent from monetary spend governance.

Define per-call input/output maxima and optional stage/run aggregate limits. Support reservations so concurrent calls cannot overcommit a budget; reconcile provider-reported actual usage, and label estimates, missing usage, and actuals distinctly. Expose remaining/used allowances. Handle refusal, error, cancellation, and partial provider responses without releasing already-used budget or inventing token counts. Compose with the existing `SpendGovernor` monetary preflight; neither limit can silently replace the other.

Own `harness/spend.py` or one narrowly scoped token-budget module and its settings. Do not edit JEV policy/packs, condenser/brief, waist, or CLI/MCP surfaces. Publish a stable API and integration notes. Tests: `tests/test_hourglass_token_budget.py` plus spend/config tests and repo gates.

**Done when:** call and aggregate ceilings are enforced before dispatch, reservations are safe under concurrency, actual/estimated/unknown usage is distinguished, failure accounting is sound, and monetary governance remains intact.

## `HV-4` — Stage composition and planning waist

**Goal:** Compose optional stages and implement the progressively narrowing planning waist using the contracts merged from `HV-1` through `HV-3`.

Resolve selected stage sets and supplied artifacts explicitly. Planning should use increasingly curated briefs and decreasing declared token allowances across its chosen model rungs; stop when the question is answered, or emit a validated bounded plan, bounded evidence request, or honest defer. It cannot raise its own limits. A supplied brief or plan must let callers bypass omitted upstream stages. Preserve existing behavior where compatibility requires it, and document actual defaults versus operator overrides.

Own `harness/waist.py`, stage resolution in config, and plan/DAG contracts. Consume shared JEV, brief, token-budget, and spend APIs; do not duplicate their validation or accounting. Tests: `tests/test_hourglass_stage_composition.py` and `tests/test_hourglass_planning_budget.py` plus repo gates.

**Done when:** each stage can run alone and supported subsets compose without activating omitted stages; waist limits narrow by explicit policy; answer, plan, evidence request, and defer outcomes validate; and supplied artifacts bypass the intended stages.

## `HV-5` — Expanded-token execution and Sovereign Harness handoffs

**Goal:** Execute validated bounded plans with expanded token allowance while preserving consent and resumable deferral.

Dispatch work packages to the least-cost capable workers that meet task requirements, with execution token allowances wider than the planning waist but still bounded by run and monetary limits. Bind consent to the exact package, relevant brief/context identity, selected model/pool, and current limits. A decline stops dispatch. A defer, including one during work, stops that assignment and writes a resumable handoff that preserves completed evidence without replaying completed work. Material plan changes require a bounded planning amendment. Independent verification remains the completion authority.

Own agent/apply/consent continuation seams identified by the roadmap. Consume the shared APIs; do not edit shared owners from `HV-1` through `HV-4`. Tests: `tests/test_hourglass_execution_budget.py` and `tests/test_hourglass_consent_handoff.py` plus repo gates.

**Done when:** every dispatch is authorized for the exact current work and limits; decline/defer prevents continued work; resumption is evidence-preserving and does not duplicate completed actions; and completion requires independent verification.

## `HV-6` — Surface parity, observability, and acceptance

**Goal:** Make the shared stage/budget behavior usable and inspectable through supported interfaces, then demonstrate the full flow and modular subsets.

Expose stage selection, supplied artifacts, and explicit budgets through CLI, MCP, and agent/API surfaces using shared policy. Report selected and skipped stages; requested, actual, estimated, or unavailable tokens; monetary spend; JEV capability and live/fallback state; consent; handoffs; and verification. Keep adapters thin. Use hermetic fakes to prove surface parity, modular subsets, and the complete flow. Run a small operator-authorized paid-cheap dogfood comparison for cost, token profile, fallbacks, defer/resume, and verified outcomes; do not use free-only live evidence as the sole model-quality basis.

Own thin adapters, result envelopes, and observability as the roadmap identifies after prior contracts have merged. Tests: `tests/test_hourglass_surface_parity.py` and `tests/test_hourglass_end_to_end.py` plus repo gates.

**Done when:** CLI, MCP, and agent/API surfaces expose the same shared behavior and honest evidence; partial and complete flows pass named gates; dogfood reports measured versus unknown quantities and verified outcome; and docs describe defaults, limits, and known gaps.
