# Hourglass Freebuff lane prompts

These prompts translate the `HV-*` rows in [jev-roadmap.md](jev-roadmap.md) into bounded Freebuff assignments. The roadmap remains canonical for scope, dependencies, acceptance, and status. Use the kickoff order below; verify it against fresh `origin/main` before every lane.

## Safe launch order

1. First land the Hourglass vision and `HV-*` rows through the clean documentation PR. The current canon queue then requires `JEV-AUDIT-GATE` and the HUL-D follow-ups before `HV-0`. Recheck their actual merge state; do not infer completion from a worktree.
2. Run `HV-0` alone from the resulting latest `origin/main`. It defines the JEV assessment contract and fixes the shared price/preflight owner to use the operator-verified account rate. Its single live design-assessment pilot runs only after payload, credential, and budget gates pass. A blocked pilot stays unassessed and must be tracked honestly.
3. After the `HV-0` contract is merged, `HV-1`, `HV-2`, and `HV-3` may develop concurrently in disjoint worktrees. Open and merge **one phase PR at a time**; rebase each remaining branch onto current `origin/main` before its PR.
4. Run `HV-4`, `HV-5`, and `HV-6` sequentially after their dependencies merge, each in its own fresh worktree and PR.

The three concurrent ownership boundaries are: JEV policy and packs (`HV-1`); context condenser and brief (`HV-2`); token allowance/accounting and its config (`HV-3`). If a required shared API is missing, report the exact interface needed and wait for the owner to resolve it. Do not edit another lane's owned files to work around the gap.

## Shared instructions for every lane

Use the repository at its latest `origin/main`; fetch and inspect the canonical [jev-roadmap.md](jev-roadmap.md) first because the operator `main` checkout may be stale. Keep all implementation in a dedicated worktree and one branch for the assigned `HV-*` row; only one phase PR may be active at a time. Do not push directly to `main`, merge your PR, or claim completion from local results alone. Do not redo work already merged; reconcile the current tracker and active worktrees first.

Implement only the assigned lane and named acceptance criteria. Preserve the Hourglass product shape: broad context gathering with relatively generous token allowance; a planning waist that narrows curated context and token allowance as capability increases; then execution where token allowance expands again for capable, cost-effective workers. Stages are optional and composable; a request may start from a supplied brief or plan and omit other stages. Token ceilings and the existing monetary spend ceiling are independent limits.

JEV supplies typed semantic judgments at explicitly selected integration points. Code owns exact facts, authorization, budgets, schema validation, dispatch, and verification. Reuse the single `JevPolicy` and pack owner. Do not create a second JEV client, use `jev-phase` as a design-quality score, treat local fallback as a live JEV result, or add arbitrary executable third-party plugins. Keep keys and other secrets out of source, prompts, logs, and reports. Any live API call must be expressly within the operator's requested task, preflighted through the shared spend owner, bounded to the planned single call where specified, and honestly reported with live/fallback, cost, and usage evidence.

Follow `AGENTS.md`, `CLAUDE.md`, `.claude/` lane rules, and `.agents/skills/typesafe-ai/SKILL.md` where applicable. Use hermetic tests and the exact gates named in the `HV-*` roadmap row; do not add unrelated test infrastructure. Do not refresh coverage baselines to conceal new untested lines. Keep canonical STATUS in `docs/jev-roadmap.md` only, and update it as part of the PR. Stop with a concise blocker report if scope overlaps another active owner, a required contract is unavailable, or a gate fails; include the exact evidence and the smallest needed decision.

When finished or blocked, report the branch/PR title, changed areas, named gates and results, remaining risks, and whether the lane can merge or needs a decision. Never claim work is complete before the repo's required audit, Jev phase bar, and CI conditions pass.

---

## `HV-0` — JEV integration foundation and vision-assessment pilot

**Goal:** Establish a reusable, composable JEV integration contract and a dedicated, category-based assessment for the Hourglass vision and implementation plan. This is a design-quality judgment, distinct from phase completion and implementation readiness.

Read the full vision document, this roadmap's Hourglass realization rows, and current Claude planning/progress details relevant to this scope. The assessment context should include the vision, current implementation foundations and gaps, Claude's original intended scope and current work status, and the proposed `HV-0` through `HV-6` plan. Sanitize out credentials, IP addresses, machine-specific home paths, unrelated personal/session details, and raw logs. Preserve material technical facts, PR state, dependencies, and remaining work. The current code does not yet implement the direct-source completion alignment, restart controller, or fully assignment-bound consent described in the vision; assess those as proposed contracts, not shipped behavior.

Build one structured state and **exactly ten** operator-declared Score questions in one request. JEV provides typed scores and distributions, not prose explanations. Code associates each category with payload evidence references and a declared improvement bucket per below-top level; do not request or invent model-written evidence. For each category, report raw score, selected ordinal (highest probability; a tie selects the lower ordinal), code-owned evidence references, exactly one declared improvement bucket when below top, and `review_required` below the pack-declared confidence threshold. `perfect` requires all ten selected top levels, confidence at or above that threshold in every category, and no improvement or review bucket. Never use `JevEvaluationResult.is_passing`, `can_mark_complete`, readiness, or phase status as a design score.

Before any reservation or network call, report the sanitized payload outline, UTF-8 serialized byte count, estimated complete-request input tokens, estimated `state` plus longest-question tokens, estimator name, both margins under TypeSafe's published 64k and 32k **token** limits, pack version, and worst-case monetary reserve. Use the same JSON serialization as dispatch and `harness.tokens.estimate_prompt_tokens`; byte count is diagnostic, while the model limits are measured in estimated tokens. The operator verified the TypeSafe account rate in the billing console as **$0.0042 per million input tokens**; accept that rate without requesting further evidence. The current `harness.jev.jev_cost` constant uses $42 per million; fix or configure the shared pricing owner and its named tests so preflight and settlement use the verified account rate. The account's included credit does not override the per-call limit. Compute the reserve from the corrected shared rate and full request estimate. Do not silently truncate, drop categories, or split the call. If the full request exceeds either model limit or the corrected reserve exceeds the authorized per-call ceiling (`HARD_MAX_COST` is currently $0.10), stop the live pilot and leave every category unassessed. Do not silently raise the ceiling.

Resolve the usual Harness JEV key through `load_settings`/normal resolution, returning only an availability status. The shared `JevPolicy` method must own exactly one local spend reservation, one supported no-retry transport attempt, all-or-nothing validation, one settlement, and one metadata-only `jev_eval` for a dispatched call. Do not manually reserve and then call a method that reserves again. Pre-dispatch refusal records a refusal, with no live call and no settled `jev_eval`. Do not retry transport or parse failures. On any missing/extra/malformed category or invalid model/legend/score/probability/confidence, reject the entire assessment and return all category scores as `None`; settle any reported usage honestly. Unkeyed and failed transport paths are also unassessed, never a heuristic design score.

The ten categories are modularity/composability, token-budget hourglass shape, grounding/evidence preservation, planning boundedness, execution boundary, JEV integration coverage/extensibility, sovereignty/consent/defer/handoff/resume, observability/accounting, independent verification/completion alignment, and cost/resource limits. Declare their ordered levels, evidence references, bucket mapping, and confidence threshold before requesting assessment. Validate exactly the ten answer IDs, exact legends and probability keys, finite/ranged scores and confidence, 0–1 probabilities summing to one, and a non-empty observed model. Partial or extra answers invalidate the whole assessment. The output must distinguish live JEV, an unassessed failure, and any fallback state; include model, usage source, token counts if measured, and cost. A perfect JEV design result is advisory evidence, never a completion gate.

Reuse `JevPolicy` / `jev_packs`; create the smallest typed, versioned assessment contract and one thin supported entry point if needed. Do not build a generic plugin loader or second JEV client. Use a supported one-attempt transport setting and metadata-only ledger fields: site/capability, pack ID/version, observed model, result state, fallback state, usage source, observed token counts, and cost; never store the payload or key. Test keyed success; unkeyed, 429/5xx/transport, malformed, missing/extra answers, bad legends/probabilities/scores/confidence, budget refusal, exact category coverage, one reservation/settlement/event, and absence of completion/readiness fields. Run the repository's named gates and independent review before the live pilot. Record the live result only if it actually ran. Update the vision or canon plan only where evidence warrants it. Do not implement `HV-1` through `HV-6` in this PR.

**Done when:** the narrow contract and category assessment are implemented and documented; named hermetic tests and repo gates pass; the assessment has one valid live pilot result or a precise recorded live blocker with all categories unassessed; non-perfect categories are addressed in vision/plan or explicitly tracked; and the PR is independently reviewed and passes the repository's merge requirements. A blocked pilot does not count as a completed live assessment.

## `HV-1` — Stage-specific JEV integration coverage

**Goal:** Apply the shared `HV-0` JEV contract to independently selectable Hourglass judgments.

Cover context relevance/coverage/conflict; planning sufficiency, soundness, and evidence requests; execution package suitability/checkpoints; consent freshness, defer, and escalation signals; final completeness/alignment against the original request and relevant retained source context; and verification-claim support. Do not run another generative condensation pass before the final alignment judgment. Where payload bounds require chunking or excerpts, make that transformation explicit and preserve source references. Add a typed JEV restart-target judgment with only declared choices: context intake, planning waist, execution, or no iteration. For every capability, state what code owns versus JEV judges, define the versioned typed pack and declared outcomes, explicit confidence/abstention/fallback behavior, budget/preflight, and ledger evidence. Do not call JEV where no decision is needed. Do not let JEV grant consent, alter budgets, dispatch work, or certify completion.

The final JEV judgment may recommend a phase target and typed unmet-requirement choices; code maps those choices to retained request and evidence references, validates the transition, and re-enters the corresponding stage through its normal contract. Preserve completed work, source/evidence identity, budget history, and handoff state; do not repeat completed packages. Re-preflight limits and renew Sovereign Harness consent if the target requires a materially changed assignment or limits. Independent verification remains the completion authority.

Own JEV policy/packs and the integration matrix only. Do not edit condenser/brief, token accounting/config, waist composition, or CLI/MCP surfaces. Publish stable APIs for later lanes. Tests: `tests/test_hourglass_jev_integrations.py` plus relevant JEV pack/policy tests and repo gates.

**Done when:** each named capability, including final alignment and restart-target selection, can be selected independently through the shared owner; keyed and degraded behavior is truthful and bounded; invalid/unavailable results cannot authorize a protected action; event evidence identifies capability and outcome; and the named gates pass.

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

**Done when:** every dispatch is authorized for the exact current work and limits; decline/defer prevents continued work; resumption is evidence-preserving and does not duplicate completed actions; final alignment compares against the original request and relevant retained source context without a new generative condensation pass; any requested iteration re-enters only a declared stage and preserves completed work; and completion requires independent verification.

## `HV-6` — Surface parity, observability, and acceptance

**Goal:** Make the shared stage/budget behavior usable and inspectable through supported interfaces, then demonstrate the full flow and modular subsets.

Expose stage selection, supplied artifacts, and explicit budgets through CLI, MCP, and agent/API surfaces using shared policy. Report selected and skipped stages; requested, actual, estimated, or unavailable tokens; monetary spend; JEV capability and live/fallback state; consent; handoffs; and verification. Keep adapters thin. Use hermetic fakes to prove surface parity, modular subsets, and the complete flow. Run a small operator-authorized paid-cheap dogfood comparison for cost, token profile, fallbacks, defer/resume, and verified outcomes; do not use free-only live evidence as the sole model-quality basis.

Own thin adapters, result envelopes, and observability as the roadmap identifies after prior contracts have merged. Tests: `tests/test_hourglass_surface_parity.py` and `tests/test_hourglass_end_to_end.py` plus repo gates.

**Done when:** CLI, MCP, and agent/API surfaces expose the same shared behavior and honest evidence; partial and complete flows pass named gates; dogfood reports measured versus unknown quantities and verified outcome; and docs describe defaults, limits, and known gaps.
