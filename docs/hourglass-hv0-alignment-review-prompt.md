# Freebuff review prompt: reconcile the Hourglass HV-0 proposal with current Harness canon

This is a **read-only alignment review**. Do not edit files, create branches/worktrees, inspect credential files, make API calls, or start implementation. The goal is to decide whether the proposed `HV-0` JEV design assessment can be safely started under the repository's current source of truth and governance, and to return concrete amendments if it cannot.

## Read these sources from current `origin/main`

Start by fetching `origin` and inspect the current revision of:

- `AGENTS.md` — especially canon, worktree, PR, and phase-order rules.
- `docs/jev-roadmap.md` — the only operational plan and current `Next implementation slice` / STATUS.
- `docs/claude-context.md`, `docs/jev-dogfood.md`, and `docs/jev-mission-prompt.md`.
- `docs/hourglass-vision.md`, `docs/hourglass-freebuff-prompts.md`, and this proposal, if they exist on `origin/main`.
- Current relevant implementation contracts: `harness/jev.py`, `harness/jev_policy.py`, `harness/jev_packs.py`, `harness/session.py`, `harness/config.py`, and tests that establish the existing JEV policy and live-smoke behavior.
- Current GitHub PR/worktree state for the Hourglass docs and any earlier JEV-audit work only if accessible through existing read-only tools; do not infer merge status from a stale local checkout.

The Hourglass documents may still exist only on an unmerged branch. If they are absent from `origin/main`, treat them as proposals, not canon. Do not consume an entire unrelated branch or recommend merging unrelated changes wholesale. Identify the minimal source material and the correct clean-PR/base sequence.

## Proposed HV-0 concept to evaluate

HV-0 proposes a reusable, typed JEV design/vision assessment for the modular token-budget Hourglass. The product flow is broad context intake with generous token allowance → progressively curated context and tighter allowance through a more capable planning waist → a wider execution chamber with more token allowance for capable cost-effective workers. Users may select any stage or subset. Sovereign Harness consent, decline, defer, handoff, and resume apply to assignments. At completion, a JEV check compares the original request and relevant retained source context against the result; if iteration is needed, it may recommend a declared restart phase (context, planning, or execution). Code validates routing and remains authoritative for budgets, permissions, execution, and independent completion verification.

The intended pilot would provide ten typed Score categories in one structured state and one live request. JEV Score outputs do **not** provide generated prose rationales. “Evidence” must therefore mean code-owned references in the payload (for example, `vision.stage_contracts`, `roadmap.HV-5`, or `audit.consent_handoffs`), not model-authored explanations. Each category result would include raw score, deterministic level chosen as the highest-probability criterion with a declared tie rule, code-owned evidence references, and an improvement bucket selected from the declared level when below top. “Perfect” means every category selects its top declared level and no improvement bucket remains. This design score must never return `can_mark_complete`, readiness, or phase status.

The ten proposed categories are: modularity/composability; token-budget hourglass shape; grounding/evidence preservation; planning boundedness; execution boundary; JEV integration coverage/extensibility; sovereignty/consent/defer/handoff/resume; observability/accounting; independent verification/completion alignment; cost/resource limits.

## Validate the blockers and proposed contract

For each item below, independently verify it against current source. Mark it **confirmed**, **partly confirmed**, **not confirmed**, or **cannot verify**, and cite a file/section, code symbol, test, PR title, or current GitHub state. Clearly distinguish repository facts from recommendations.

1. **Canon and branch ordering:** Are the vision and `HV-*` rows on current `origin/main`? What is the actual current first open roadmap item? Does repo governance require completing it before HV-0, or can the roadmap be amended through a PR to reorder work? What is the minimal compliant path, including whether the open vision PR must first be rebased/cleaned/merged? Never recommend using a branch wholesale if it carries unrelated source, tests, changelog, or audit changes.
2. **One phase PR at a time:** What does the exact canon rule say? Does it prohibit parallel Freebuff worktrees, or only serial merge/phase progression? Resolve the apparent conflict using the current rule text, not assumptions.
3. **JEV Score contract:** Verify the TypeSafe Score answer schema in checked-in docs/skill and current adapter code. Confirm whether rationales are available. Assess the typed-only, code-owned-evidence design above and identify required validation for all ten IDs, legends, probability keys, finite scores/probabilities/confidence, allowed ranges, probability sums, missing/extra keys, and invalid/partial responses. Specify whether any current validator already enforces each condition.
4. **One-call semantics:** Trace `JevEvaluator` transport behavior and shared `JevPolicy` preflight, reservation, settlement, fallback, and ledger paths. Determine how to disable automatic retries for the single pilot without bypassing shared ownership. “Preflight” means a local spend-governor reservation with no network dispatch; clearly separate it from the one live call. A preflight refusal or missing credential must leave scores unassessed.
5. **No false score:** Unkeyed, transport-failed, malformed, invalid, and budget-refused paths must leave every category score `None` and mark assessment unassessed. Existing local heuristics must not be presented as JEV design judgment. A partially returned ten-category payload must be rejected as a whole.
6. **Ledger contract:** Propose the minimal metadata-only event fields required for one settled `jev_eval`: stable capability/site, pack version, model, `is_fallback`, usage and whether measured/estimated/unavailable, cost, and result state. Check current ledger schema and PII/secrets policies. Do not log the full assessment payload or key.
7. **Payload limits:** Verify current TypeSafe request size constraints from the checked-in integration docs or primary authoritative documentation only if already available to this read-only task. The proposed constraints are a 64k total request limit and 32k for `state` plus its longest question. Explain exactly how to measure serialized request characters, estimate input tokens using an existing configured method if present, calculate margin to both limits, and stop before preflight/call if full sanitized context does not fit. Never silently truncate context because the task requires full context and exactly one call.
8. **Prior work and current progress:** Audit the actual Claude plan/workflow status and current PR/worktree state. Distinguish merged, open, pending review/CI, uncommitted, and not included. Report remaining work by roadmap item, but do not expose key values, IPs, home-directory paths, raw logs, or irrelevant personal details.
9. **Assessment and iteration semantics:** Confirm that the proposed completion alignment uses the original request plus relevant uncondensed retained source references and that JEV recommends (but cannot perform) a declared restart target. Identify how code validates the target, preserves completed work and provenance, reapplies budgets, and renews consent for materially changed work. Flag any unresolved conflict with Sovereign Harness or existing verification authority.
10. **Modularity and plugin scope:** Check that this is a composable typed integration using existing `JevPolicy`/pack owners, not an arbitrary executable plugin loader, second JEV client, hidden always-on stage, or JEV override of code-owned controls.

## Required output

Return a concise but complete review with:

1. A verdict: **aligned and ready after named prerequisite(s)**, **aligned but needs specific amendments**, or **not aligned**.
2. A source-grounded table for blockers 1–10, with status, evidence, and needed correction.
3. A compliant execution sequence, beginning with required canon/queue reconciliation and ending with implementation, hermetic gates, measured payload sizing, local spend reservation, exactly one no-retry live call (only if all prerequisites pass), response validation, settlement, ledger record, and final report.
4. An exact amendment list for the roadmap and HV-0 prompt. Be explicit about which text belongs in canon versus the vision or lane prompt.
5. Any unanswered question that requires the operator. Do not silently assume authorization to reorder the operational queue, merge a PR, or make the live call.

This review grants no authorization to implement, call JEV, read credentials, or alter the repository. It should help the operator decide whether the proposed work is aligned and what must be amended before a later implementation assignment.
