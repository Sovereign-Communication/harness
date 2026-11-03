# Jev-cluster issue triage — #420-430 (2026-10-28)

Read this session (no profile, no harness writes). Comments for #422 and #423 were read separately and are summarized below.

Sources: gh issue view per number; bodies truncated to ~1200 chars.


## #420–#430 — cluster state (re-read this pass)

All nine issues are OPEN, no labels, titles captured from a fresh `gh issue view --json number,title,state,labels` this pass (the earlier triage file recorded a JSON decode artifact for these rows; that artifact is corrected here).

- #420 | OPEN | [site] Use the TypeSafe System One contract for route judgments
- #421 | OPEN | [consent] Emit one rotation event for each unusable model
- #424 | OPEN | [docs] Align System One feature claims with shipped status
- #425 | OPEN | [driver-core] Refuse append when an existing audit chain is invalid
- #426 | OPEN | [security] Enforce public-network policy for browser evidence rendering
- #427 | OPEN | [mission] Surface malformed receipt and Jev-evaluation records
- #428 | OPEN | [economics] Reject negative endpoint override and cache prices
- #429 | OPEN | [audit] Refuse or recover truncated JEV judgment state before append
- #430 | OPEN | [lifecycle] Stop repeated retries after validated restart or consent refusal

Bodies and comments were read in the prior pass and survived in triage form; the only correction this pass makes is the state field above (OPEN for all nine, confirmed from the API). See the #422/#423 summary below for the coupled context.

## #422 and #423 — summary from earlier read


### #422 citation metadata / claim-to-source

- Title: [research] Verify citation metadata and claim-to-source support

- 2 comments (Treystu, 2026-10-28): a selected science-review draft was produced in reports/ai-for-science-review-2026-10.md; it corrects several material overclaims and labels 2026 AlphaProof Nexus work as a preprint. It is a MANUAL review, not an automated DOI/passage audit. The canonical plan now specifies a claim ledger with source/version identity, DOI resolution outcome, exact passage locator, claim-to-source support state, check date and reviewer/tool provenance. Unsupported/unresolved claims remain unverified. Issue is to be RETAINED as the implementation owner for metadata identity and exact-passage verification; the report does not close it.


### #423 resumable full-source Jev audit command

- Title: [audit] Add a resumable full-source Jev audit command

- 1 comment (Treystu, 2026-10-28): the previously completed two-pass audit (711 files / 2,180 chunks; findings #420 through #429) was NOT repeated. Its historical per-file/chunk manifest and raw Jev/usage receipt are ABSENT and must remain explicitly unavailable; a new sweep cannot be represented as recovery of that old receipt. The canonical plan specifies the future audit-manifest contract (separate code/docs passes; commit + included/excluded paths; file/chunk hashes + chunking params; per-file counts + resume cursor; raw typed judgments/model/pack; deduplicated dispositions; usage/cost totals). Partial passes cannot claim full coverage. Ten prior leads were source-checked without rerunning the sweep. The final exact-plan Jev gate is still below threshold and BLOCKED; see docs/jev-roadmap.md and PR #431.


## Notes for disposition

- #420-430 are tightly coupled to PR #431 (docs: reconcile Jev research and full-source audit plans).

- The prompt says the exact-plan Jev gate for this request is still below threshold and blocked.

- #422 has an attached report but the product capability (citation IDs, DOI resolution, passage locators, deterministic claim-support status) is NOT shipped; issue retained as owner.

- #423 tells us the old two-pass audit manifest/receipt is absent and must stay absent; do NOT represent a new sweep as recovery of that old receipt.

- This pass re-confirmed that #422 and #431 are both OPEN (the earlier triage file recorded a `???` state artifact for those two; that artifact is corrected here). #431 is a DRAFT pull request (`freebuff/audit-this-harness-request-in-full-…`) against main, mergeable=MERGEABLE, mergeStateStatus=CLEAN, 15/15 CI check-runs all SUCCESS; per rule it is not mergeable while it is a draft, so GREEN CI does not make it mergeable this pass.
