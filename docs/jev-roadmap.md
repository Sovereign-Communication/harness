# Jev Full-Functionality Roadmap

**Status:** active tracking doc  
**Implementer lanes:** Claude Code (`CLAUDE.md`, `/isolated-mission`), Codex, and Freebuff share this canon; use lane-specific handoffs as seat state, not parallel plans.
**Historical audit date:** 2026-09-22 (Claude dogfood audit: 9 components, 31 confirmed findings — see `DF-*` below); current repository sweep: 2026-10-05.
**Live probe:** TypeSafe `POST https://api.typesafe.ai/v1/systemone` succeeded with key at `~/.config/harness/jev.env`

This file is the **single source of truth** for Harness mission work (Jev / System One, HUL, hourglass, site, and follow-ups). Every future PR must carry an ID from the tracker below and reference an acceptance test named here. Do not open parallel ad-hoc plans — add or update rows here.

**Mission prompts:** [jev-mission-prompt.md](jev-mission-prompt.md) (`/isolated-mission --bar <ID>`). Trust `origin/main`; a stale local STATUS is not evidence.

**Worktrees:** one per phase PR, `Harness-<slug>` off `origin/main` (convention in `AGENTS.md`); `git worktree list` is authoritative.

Related design context (not the tracker): [system-one-integration.md](system-one-integration.md), [hourglass-frontier-eval.md](hourglass-frontier-eval.md) (D8), skill at `.agents/skills/typesafe-ai/SKILL.md`, live API at <https://docs.typesafe.ai/api.md>.

---

## Canonical current status — comprehensive sweep 2026-10-05

This section supersedes older present-tense status statements and the previous
“Next implementation slice.” Detailed dated audit narratives below are history;
their old “open,” “next,” and “in progress” language is not current unless
repeated here or in a live tracker row. This is the only operational plan.
Reconcile it against remote state before each merge.

### Verified remote and review state

- Canonical repository: `Sovereign-Communication/harness`. The old
  `Treystu/Harness` identity is a moved pointer and is not a reliable source.
- At sweep time, `origin/main` was
  `0676d6423d15b8f71c26c61099fbb8ccefcef715`. The operator checkout at
  `4e2a5480849c870801cdb79178fe64c50f83f9d7` is one commit behind; do not use
  it as current STATUS evidence.
- PR **#180** is the sole open PR, on
  `codex/df-cli-3-verify-defer` at
  `6da467e8975358d56c70391ece2ca51f92c17adb` (this roadmap update), targeting
  `main`; its preceding code commit is `cd53a7e4006e96f80f51d11743ef338426a4686c`.
  It closes #144 once merged. At the code commit, 11/12 required checks passed;
  required audit/D12 failed at **43/46 = 93%** against baseline `3ac7bc2b9b`
  (bar 95%). Reported changed lines are `cli.py:225`, `cli_report.py:53`, and
  `routing_table.py:171`. The newest review comment accepts the code/test
  direction but flags a mismatch: the new CLI test should execute
  `cli.py:225`, while D12 reports it uncovered. Resolve this using execution
  and trace evidence before changing tests or the baseline. `sd_coverage_changed`
  reads committed trace data; it does not itself execute tests.
- Pushing the roadmap commit restarted PR checks. At the time of this update,
  Ubuntu installed-package, package, and checkout-hygiene checks passed; the
  audit and platform/test matrix were still running. This document update does
  not resolve the D12 failure; re-read current checks before merge.
- The coverage refresh runner inherits its process environment and home. A
  prior attempted full battery unexpectedly reached live provider paths; the
  approval review rejected another run because of that risk. Do not rerun it
  or use an indirect workaround without explicit operator authorization. First
  make runner isolation and trace provenance reviewable, then request
  authorization if a live-capable run is still required. Never hand-edit the
  coverage baseline.
- No submitted PR review or unresolved inline review thread was found on #180.
  Its substantive conversation comment is tracked above. Re-read PR checks,
  comments, and base SHA immediately before merge.

### Current backlog and dependency order

Only one phase PR may be active. Keep all work in the existing #180 PR until
it lands; then take the next item below as its own reviewed PR from fresh
`origin/main`. Each row stays open until its named acceptance evidence and
required local gates are present.

| Order | Canonical ID / source | Remaining work and acceptance evidence |
|---|---|---|
| 1 | **#180 / DF-CLI-3** | Resolve the D12 trace discrepancy on the actual merge base; make the coverage runner safe and its test execution/trace lineage auditable; obtain required changed-line coverage without falsifying or editing the baseline; local gates and all required PR checks green. Then merge #180 and confirm green post-merge `main`. |
| 2 | **#179 / DF-CLI-4** | Recover only the CLI stderr notice and regression tests from `6269636c9b`; rebase on current `main` and fix its current `max_cost` ceiling failure. Do not carry unrelated patchsets. |
| 3 | **#179 / DF-AUDIT-6 candidate** | Isolate and review the AST preflight strengthening from current branch evidence; establish a named phase contract and regression tests before implementation. An issue comment alone is not acceptance. |
| 4 | **#179 / pack limits and JEV-P6 prose** | Reconcile the claimed 1,900-character seed cap / 1,886-character measurement with `tests/test_jev_repo_pack.py` setting `SEED_MAX_QUESTION_CHARS = 3400`. Decide and test one actual bound, then align the pack and roadmap prose. |
| 5 | **DF-AUDIT-5** | Recheck completion clauses against merged #146 and #165: bounded iteration, visible fallback provenance, fallback-cause tests, and independent completion authority. Narrow this row to any production/test residual still missing; do not repeat delivered work. |
| 6 | **GAP-plan-http / DF-UI-2** | Implementation is present from #155: `RUNNERS["plan"]`, `run_plan_task`, shared `compose_plan`/`PlanExecutor`, and `tests/test_plan_http_batch.py` cover HTTP dispatch, plan-only, and execution. Replace the stale operator-ruling request with a closure review: confirm registration in `PHASE_CONTRACTS` (or document why this GAP does not use a phase contract), run named gates, and close only on evidence. Dogfood face shipped with #148. |
| 7 | **REPO-CARDS** | Generator landed in #164; add and verify its user-facing CLI/MCP face and CI drift check. Preserve source-grounded, deterministic card rules. |
| 8 | **PROVISION-CORE** | Core landed in #166; add separately scoped production entry points (CLI, MCP, agent intent, GUI only as required), phase contract, consent/dry-run tests, and a safe live receipt. Do not widen the existing core or imply its unit tests alone ship the feature. |
| 9 | **JEV-P3-calibration-analysis** | Analysis module landed in #169; wire it to real ledger evidence and a bounded operator threshold-application workflow, register phase contract/tests, and retain `jev-freeze` as settings owner. |
| 10 | **DRV-1** | Code and CI are complete (#159/#161); only the paid-cheap live dogfood receipt is missing. Run through operator-gated model policy, record cost/fallback/verification evidence, and flip only when `harness jev-phase --phase DRV-1` passes. |
| 11 | **DRV-2** | Implement a real consent/policy-gated input backend and live vision extraction behind declared driver actions; add backend conformance tests and prove `harness driver step` performs a permitted action. Synthetic inputs correctly refuse today. |
| 12 | **EV-0 through EV-5** | Keep the dependency chain: resolve EV-0a discount semantics with its committed measurement receipt; EV-1 endpoint cost/capability index; EV-2 committed tier snapshot and freshness gate; EV-3 advisory Jev pool resolver; EV-4 bounded automatic movement (off by default); EV-5 CLI/MCP/UI/report and committed refresh workflow. Preserve economic invariants and use paid-cheap receipts. |
| 13 | **OC-HANDOFF** | Worker safety implementation is merged (#90); remain gated until an operator supplies a valid signed candidate and findings-only output/receipt audit passes. Do not merge stale `87a672e8` wholesale: it is 246 commits behind and changes spend settlement from main’s book+flag+return to book+abort. Only findings may be written to `HANDOFF/OC_FINDINGS.md`; no external configuration or seat-state writes. |
| 14 | **Exit: HUL ruling** | HUL A–D and HV consumers are complete. The old exit row’s “HUL A–D shipped **or** open-problem packs” condition is already satisfied by HUL A–D. Do not hold it open for an uncited extra ruling; verify originating authority, then close with existing evidence or amend the criterion through this reviewed plan PR. |
| 15 | **#177 hygiene** | Its body is stale (references #173 as sole open PR, #144 as unimplemented, and old Phase 3 work). After #180 lands, update its disposition to cite landed fixes and list only validated residual hygiene items, then close when resolved. #144 is implemented by #180 pending merge; #168 was closed by owner on 2026-10-04. |
| 16 | **Final landing and dogfood** | After selected backlog rows land, re-read canon and issue/PR state from remote; verify each merged phase gate and green `main` CI. Dogfood user-facing CLI/MCP/API/GUI paths with cheap paid models first where live judgment is required; record receipts, actual cost, fallback provenance, refusal/defer behavior, and regressions. Fix regressions in reviewed PRs, rerun affected gates, and update this canon only through a reviewed PR. |

### Branches and worktrees — disposition

The sweep found **93 local branch refs not ancestors of the then-current
`origin/main`**, plus numerous worktrees. This is an inventory count, not 93
backlog items: many are backups, WIP snapshots, duplicate patches, or historical
Freebuff sessions. Compare patch identity and live PR/issue state before
promoting any ref.

- `codex/hv5-sovereignty-20260929` contains work already represented by merged
  #146; do not replay those duplicate commits.
- `codex/lane3-jev-phase-local-accounting-notice` at `6269636c9b` has one
  potentially useful CLI notice commit; recover only that slice as described
  above.
- `codex/oc-handoff-jev-gate` at `87a672e8ad` must not land wholesale because
  of the spend-settlement regression; extract only independently reviewed safe
  improvements if still needed.
- `codex/canon-reconcile-20261005` and the active #180 branch are review
  candidates; reconcile distinct hunks against remote canon before cherry-pick.
  The `canonical-plan-reconcile` worktree is already an ancestor of remote main
  and is historical.
- Preserve the dirty legacy Freebuff worktree at
  `.freebuff/worktrees/86ac1455-199f-4d4d-96b4-46b235cd4bfb` until its untracked
  `HANDOFF/MCP_ISOLATION_BASELINE_2026-10-03.md` and `tmp_phase_f/` are
  classified and intentionally captured/discarded by their owner. Do not prune
  or clean worktrees during backlog reconciliation.

### Scope and status corrections

- Replace the stale numbered “Next implementation slice” with the order above;
  archival narratives must not define current status.
- `DF-UI-2` dogfood is delivered by #148 and HTTP plan dispatch by #155; only
  its explicit closure-contract review remains.
- `JEV-P4-residual`, `JEV-P6`, `HV-0` through `HV-6`, `HV-2-use`, `HV-3-use`,
  HUL A–D, Jev P0–P4, release publishing, #173, #178 and #171 are landed per
  recorded evidence; do not reopen or repeat them.
- The comment that the HV-5 sovereignty branch was rewritten is not evidence
  that all current changes are unique or needed. Compare patches before recovery.
- This sweep cannot honestly promise 99% certainty about every defect across
  every historical ref. It records evidence, known limits, and exact remaining
  closure conditions so new remote comments and CI changes can be incorporated
  before each merge.

---

## North star

> **Jev is Harness’s tier-0 System One decision layer.** Every material plan, consent, write, and verify-adjacent judgment can be gated by typed Jev questions, with calibrated confidence used as a *second axis* (not free-text voting). Local AST/JSON/diff fallback remains the unkeyed degraded path. Costs are token-priced, preflighted, billed, and ledgered. CLI, MCP, agent, and plan lanes share **one policy owner**. The verification gate remains the authority for code correctness — Jev triages, escalates, and refuses *before* expensive generative work.

---

## What the Freebuff lane landed (pre-P0 baseline, historical)

| Commit | Shipped |
|---|---|
| `b3960e7` | Cost foundation + first jev structural client |
| `a591739` | Waist amend-first + Jev pre-planning (`evaluate_plan_requirements`) |
| `b6fa945` | `#PR-Jev-Live`: live client, primitives, skill, tests |

**Present today**

- `harness/jev.py` — live System One client + local structural fallback
- Config: `jev_api_key`, `jev_endpoint`, `min_confidence`, `resolve_jev_key()`
- `session.jev_for()`
- Agent lane only: pre-plan iteration guideline + post-apply `verify_diff_mechanics` + structural retry
- Skill: `.agents/skills/typesafe-ai/SKILL.md`
- Tests: `tests/test_jev.py` (13/13 hermetic green at audit)

---

## Audit snapshot (why utilization is incomplete)

### Working
- Auth, endpoint `…/v1/systemone`, Bearer header, `model: jev-latest`
- Typed answers (`noul` / `score` / `choice`) parse from live responses
- Pre-plan `requires_iteration` answers correctly on live probes

### P0 — contract defects
1. **Pricing reconciled and verified.** TypeSafe `usage` is `{input_tokens, output_tokens}` only; the original client used an incorrect **$42/Mtok** input rate, and HV-0 temporarily shifted this to **$0.0042/Mtok**. The verified account rate across 13.66M tokens ($0.5738 billed of the $5.00 monthly credit) is **$0.042/Mtok** ($42 per billion tokens); output tokens remain free ($0.00). Jev spend and monthly credit tracking are surfaced across CLI, server, and UI.
2. **`verify_diff_mechanics` questions ill-posed.** State is `{diff, instruction, file_path}` but questions ask about “context support.” Live probe: `supported=0.21`, `syntax_clean=0.92`, verdict fail. Local fallback passes any valid-looking diff — keyed path is stricter and wrong; unkeyed path is a no-op.
3. **Confidence semantics conflated.** TypeSafe: Noul = yes-probability (no confidence); Choice/Score confidence = distribution shape. Code mixes Noul probability, Score level, and distribution confidence. **Partially closed 2026-09-28 (`DF-JEV-5`):** the decision gate no longer thresholds a Choice's distribution concentration, and its calibration fixtures can no longer contradict their own distribution. The Score-side conflation (`JEV-P0-threshold`, `DF-JEV-2`'s tolerance) is a separate, still-open slice.
4. **Live verdict hardcodes 0.70** and ignores `settings.min_confidence`.
5. **Docs drift:** README `…/v1/eval` vs real `…/v1/systemone`; `skills-lock.json` path vs `.agents/skills/…`; no CHANGELOG/architecture ownership for `jev.py`.

### P1 — lane / pillar gaps
- Jev only in agent `apply_node` — not CLI apply, MCP apply, batch, `PlanExecutor`, or waist confirmation
- Consent does not parse optional `confidence` (system-one Milestone 2 open)
- `sliding_scale.decide_probe_verify_escalate` / `should_abstain` tested but unwired
- Pillar 1 complexity triage and Pillar 3 lean structural jury not implemented
- `structural_eval` is telemetry only (not ledger); jev cost often missing on success path
- Questions not written per TypeSafe jaggedness guidance
- Alias `jev-latest` unpinned; system-one M2–M4 still open; hourglass D8 still deferred

---

## Phased plan (all work tracks here)

### Phase 0 — Contract truth — `JEV-P0-*`
**Goal:** Shipped client matches TypeSafe API and can be trusted when keyed.

| ID | Work |
|---|---|
| `JEV-P0-cost` | Historical P0 math used `input_tokens * 42 / 1e6` ($42/Mtok); HV-0 temporarily shifted this to `$0.0042/Mtok`. Reconciled across 13.66M tokens ($0.5738 billed of $5.00 credit) to the verified rate of `$0.042/Mtok` ($42/Btok); free output tokens. Never invent `usage.cost`; expose observed tokens on `JevEvaluationResult`. |
| `JEV-P0-parse` | Official answer shapes only; no default-pass on missing fields |
| `JEV-P0-threshold` | Live verdict uses `settings.min_confidence`; separate action confidence vs Noul probability |
| `JEV-P0-questions` | Per call-site packs: literal instructions, real state, aligned criteria; reject non-primitives |
| `JEV-P0-diff-pack` | Redesign `verify_diff_mechanics`: code-owned facts (path, instruction, syntax AST, hunk shape) + answerable nouls/scores; drop “supported from context” when context is absent |
| `JEV-P0-config-docs` | README endpoint → `/v1/systemone`; `architecture.md` owns `jev.py`; CHANGELOG; skills-lock path; `jev_model` setting (default `jev-latest`, pin allowed) |
| `JEV-P0-tests` | Hermetic fixtures from live answer shapes; cost math; no-fabrication parse |
| `JEV-P0-smoke` | Operator-gated live smoke for key changes |

**Acceptance:** `tests/test_jev.py` + parse/cost tests green; live smoke returns non-fallback with honest cost; structural fail only on real defects. **P0 status:** implemented in this PR; P1 remains untouched.

**Modules:** `harness/jev.py`, `harness/config.py`, `tests/test_jev.py`, README, `docs/architecture.md`, CHANGELOG.

---

### Phase 1 — One owner, all lanes — `JEV-P1-*`
**Goal:** Jev is not agent-private.

| ID | Work |
|---|---|
| `JEV-P1-policy` | ONE owner (`harness/jev_policy.py` or `sliding_scale`): when to call, packs, thresholds, fallback, cost ceiling |
| `JEV-P1-apply` | Post-candidate / pre-gate hook on the shared apply path (CLI, MCP, agent, batch) |
| `JEV-P1-waist` | Pre-planning through `waist.compose_plan` / confirmation |
| `JEV-P1-agent` | Agent `apply_node` calls the owner — no private policy |
| `JEV-P1-ledger` | Hash-chained `jev_eval` events (verdict, scores, cost, model, fallback) |
| `JEV-P1-spend` | Preflight worst-case jev tokens; always `record_actual` |
| `JEV-P1-envelope` | `structural: {verdict, confidence, cost, fallback}` on CLI/MCP/agent results |

**Acceptance:** CLI + MCP + agent hermetic parity tests; ledger shows jev events; governor sees jev spend. Closes system-one Milestone 4 ownership.

**Modules:** new policy module, `apply*.py`, `waist.py`, `session.py`, `agent.py`, `cli.py`, `mcp.py`, `spend.py`, `ledger.py`.

---

### Phase 2 — System One pillars — `JEV-P2-*`
**Goal:** [system-one-integration.md](system-one-integration.md) milestones become code.

| ID | Work |
|---|---|
| `JEV-P2-consent-confidence` | Parse optional `confidence` from consent decisions; ledger it (M2) |
| `JEV-P2-min-confidence` | `HARNESS_MIN_CONFIDENCE` + abstain/escalate before write/gate spend (M3) |
| `JEV-P2-triage` | Pillar 1: Choice complexity + `requires_iteration` + scope → router/tier/waist |
| `JEV-P2-jury` | Pillar 3: optional lean typed pre-gate on verify-only / panel claims |
| `JEV-P2-dead-code` | Wire `decide_probe_verify_escalate` to real jev signals **or delete it** |

**Acceptance:** M2–M4 checked in system-one doc with named tests; low-confidence path defers without writing files.

**Modules:** `consent.py`, `sliding_scale.py`, `panel.py`, `convergence.py`, routing.

---

### Phase 3 — Full utilization — `JEV-P3-*`
Use TypeSafe skill patterns *inside* Harness (code owns exact work; Jev owns bounded judgment):

| ID | Pattern |
|---|---|
| `JEV-P3-route` | Intent/apply-route Choice (free-distill / diff / frontier) |
| `JEV-P3-triage-files` | Noul relevance over orchestrator file candidates |
| `JEV-P3-context-pack` | Filter state to decision-relevant fields before generative lanes |
| `JEV-P3-claims` | Panel structured-claim support checks before judge synthesis |
| `JEV-P3-completion` | Artifact/goal nouls before LLM completion judge |
| `JEV-P3-calibration` | `ledger_analytics`: jev confidence vs real verify outcomes; retune thresholds |

**Policy rule:** counting, arithmetic, paths, gates stay in code; Jev = semantic judgment; generative models = open-ended synthesis.

---

### Phase 4 — Ops & exit criteria — `JEV-P4-*`

- [ ] Key present → default lanes run live Jev on plan + write paths (fallback only on transport failure)
- [ ] Unkeyed → explicit `is_fallback` + honest local structural checks
- [ ] Envelope cost matches token math; preflight blocks over-ceiling jev
- [ ] Ledger + analytics show jev events and calibration
- [ ] Consent confidence gating live (M2–M3)
- [ ] Docs: README, architecture, system-one milestones, hourglass D8 resolved
- [ ] Dogfood: free-tier apply/plan with vs without jev — pass rate + cost delta recorded
- [ ] Model pin + threshold freeze after first calibration

---

## Canonical STATUS (update here only — truth as of 2026-10-05 post PR #176)

**2026-10-05 landing audit (updated after PR #173 merge).** The current remote-tracking `origin/main` is `b1e7f06` (squash merge PR #173), immediately following `0022cbb` (PR #176). PRs #174, #175, and #176 registered already-shipped canon rows, fixed phase-gate status omissions, and consolidated completion registration. PR #173 then removed fixed Claude model pins and IDs while preserving the BYOK safety denylist; it passed all 12 required PR checks and merged as `b1e7f06`. This does **not** close `DF-EV-4`: `EV-2` still owns generation and freshness-gating of the committed tier snapshot, including explicit fallback telemetry for unlisted IDs. The only pre-existing open code PR found at audit start was #173; its merge and the remaining open work are tracked in [#177](https://github.com/Sovereign-Communication/harness/issues/177). This roadmap reconciliation is proposed separately in PR #178. The checked-out operator tree remains on `main` at `4e2a548`; its roadmap copy is stale and must not be treated as current status.

**2026-10-06 follow-up after PR #178 landed.** `origin/main` is now `0676d64`, with PRs #173 and #178 merged; PR #180 is the sole open PR and carries `DF-CLI-3` / issue #144. Its head `06b9232` passes the package, five hermetic-suite, three installed-package, handoff-scope, and checkout-hygiene checks. The audit is still red: D12 reports **43/46 changed lines (93%; required 95%)**, with `harness/cli.py:225`, `harness/cli_report.py:53`, and `harness/routing_table.py:171` uncovered against baseline `3ac7bc2b9b`. Do not mark `DF-CLI-3` complete or merge #180 until a successful full traced battery refreshes the baseline and the audit passes; never hand-edit the baseline. The verify regression also fails closed for malformed consensus objects, covered by `tests.test_cli.VerifyTerminalExitTests`.

**Merge ledger since the 2026-09-24 audit above.** PR #109 (`evaluate-decision-primitive`), #110, #111 (`HV-2` condensation, `fe3113f`), #112 (`apply`-resume scope test), #113 (`media`-intercept test), #114 (advisory warn-only ownership gate + waiver register, `a545ac5`), and #115 (`gate-interpreter-sys-executable`) are all merged; `main` CI is green at `b2c87e5`. PR #117 (PyPI trusted publishing), #121 (`DF-JEV-5` decision-gate-v2) and #122 (handoff-scope stamp) merged 2026-09-28 in that order, `main` verified green after each. **`HV-4` is merged.** PR #118 (`feat/hv4-waist-composition`) first reconciled onto `main` in `55440c4` — the two independent implementations of the status-row identity fix (this branch's identity filter vs `main`'s rank/ambiguity rule from #125) were reconciled rather than one dropped — then merged as `2711cc5`. No `HV-5`/`HV-6` work has merged. In the same drain, #126 (`HV-1` redundancy guard), #125 (status-row identity), #123 (this canon reconcile) and #124 (`rankings` CLI guard) merged first, then #128 (Lane 3 audit/ledger isolation, `ce66c48`). `HV-1` remains in progress — its published dimensions still have no calling lane, and `HV-4`'s consumer wiring is `HV-5` — and `HV-0` is complete. The "HV-0 remains next" line in `AGENTS.md` was stale and is corrected there. **2026-09-29:** #129 (`fix/l3-d12-fail-closed`, `87a3ac0`) merged first — first-ready first-merged, per the three-lane ruling — then #130 (`feat/gap-freeze-face`, `ede24262`); `main` CI is green after each (run `36503298570` for #129). #129's own row notes R13 returned 9.00/10 purely from the operator's environment before its fix; #130 carries the `DF-AUDIT-5` finding that the *local* audit verdict is a live Jev judgment and can therefore flip on an unchanged tree. The operator ruled 2026-09-29 to retain live judging and revise reproducibility claims; the row remains open while default iteration, fallback provenance, and completion authority are implemented. Then #131 (`docs/gap-freeze-status-flip`, `d1f7c85`) flipped `GAP-freeze-face` to complete on its own merge, post-merge `main` CI green (run `36505145608`). **#132 (`feat/gap-recondense`, `c62410c7`) closed the `GAP-recondense` row** — the re-condense/plan-drift trigger for composed runs — with all 12 CI checks green on the PR tip (run `36507098903`) and post-merge `main` CI green (run `36507427322` on `c62410c7`). `HV-1`/`HV-2` are unchanged. **#134 (`feat/composed-run-caller`, `9ef8ce39`) delivered the composed-run caller** — the half of HV-4's residual that said a caller must name the run's allowance — with all 12 CI checks green on the PR tip (run `36510934554`, five-OS/version suite matrix + installed-wheel smoke + audit) and post-merge `main` CI green (run `36511203396` on `9ef8ce39`). The composed path now executes from the CLI, MCP and agent lanes, and `HV-5`'s remaining first piece is invoking `run_planning()` when `planning` is selected. **#136 (`feat/hv2-use-intake-brief`, `cd7424b6`) delivered the intake half of the `HV-2-use` row** -- the curated brief (`waist.intake_brief`, reusing `build_brief`/`estimate_brief_tokens`/`validate_brief`) is now the intake artifact of the composed plan path on all three production lanes through one owner (`waist.compose_arguments`), so `supplied_brief` is true there and the `context` bypass is decided against the brief's own pins; all 12 CI checks were green on the PR tip and post-merge `main` CI is green (run `36514376580` on `cd7424b6`). `HV-2-use` stays **open** for its `HV-5`/`HV-6` clauses; the `HV-2` producer row is unchanged and `HV-4` narrows to that `HV-5` invocation. **#140 (`codex/hv5-planning-invocation-rebased`, `10f14e0`) invoked the selected composed planning stage**, executing `run_planning()` when planning is selected with the composed child budget and curated brief; all parts of `HV-4` are merged on `main` (#118, #134, #136, #140) and `HV-4` is flipped to **complete**. **#146 (`feat/hv5-execution-handoffs-rebased`, `f5b53ff`) delivered expanded-token execution allowances and sovereign consent handoffs, completing `HV-5`** with 12/12 CI checks green on PR tip and post-merge `main` (run `36708802205`); `HV-5` is flipped to **complete**. **#148 (`feat/hv6-surfaces`, `e5809a6`) delivered surface parity across CLI, MCP, and Server/API faces with token budgets, intake brief, and canonical dogfood loop, completing `HV-6`** with 12/12 CI checks green on PR tip and post-merge `main` (run `36773975947`); `HV-6` is flipped to **complete**, `HV-2-use` and `HV-3-use` consumer rows are flipped to **complete**, and `HV-3` is flipped to **complete**. **PR #151 MERGED** `d7df0cf` (Phase 1: test suite isolation and flake hardening); **PR #153 MERGED** `aa09a97` (Phase 2: HV-1 calling-lane wiring & dogfood receipt); **PR #154 MERGED** `3538c0a` (Phase 3: consent staleness reasons & diff authorization); **PR #155 MERGED** `36d4782` (Phase 4: brief CLI/MCP generator, HTTP batch planning runner, and web UI panes); **PR #156 MERGED** `53d2b45` (Phase 5: waist degradation envelope & deterministic judge fallback); and **Phase 6 delivers GAP-token-kinds and JEV-P4-residual dogfood A/B**.
**2026-10-04 reconcile — the first full drain audit of this ledger.** The Phase-6 sentence above was still written as a plan after it had merged: **PR #157 MERGED `563d27e`** (`v0.4.2`, post-merge `main` CI run `36924391113`) shipped the token-kind categories — `reasoning_tokens`, `cached_tokens` and a `token_kinds` block in `harness/token_budget.py` (`git log -S reasoning_tokens -- harness/token_budget.py` names `563d27e`), 68 lines of `tests/test_hourglass_token_budget.py`, and the `audits/self/dogfood/JEV_P4_AB_EVIDENCE.md` A/B receipt, which it also pinned in `audits/self/corpus_manifest.json` for D11. It flipped `JEV-P4-residual` and the Jev-P4 Exit row but left `GAP-token-kinds` reading **open**. **#158 MERGED** delivered `DYN-ALLOC`. **#150 MERGED** (dynamic answer-token allocation and truncation recovery), **#151 MERGED** `d7df0cf` (test isolation), **#152 MERGED** `b56029f` (`feat/drv`), **#153 MERGED** `aa09a97`, **#154 MERGED** `3538c0a`, **#155 MERGED** `36d4782`, **#156 MERGED** `53d2b45`, and **#149 MERGED** `97c46c7` (the `HV-6` flip) are merged; #150, #152 and #158 had no ledger line at all until now. Then the recovered-WIP train merged in this order, **every one with post-merge `main` CI `success`** (run ids bracketed): **#160 MERGED `dbfd5e7`** [`37167866295`] corrected the `DRV-1` row and made local gates reproducible; **#161 MERGED `98f077c`** [`37172346812`] repaired the five pane defects the live dogfood found and enforced `panel_verify`'s per-task cap; **#162 MERGED `fc7e2f0`** [`37174809418`] made dogfood matrix scoring strict and read Jev cost off the full ledger at the verified rate; **#164 MERGED `bbe7f6e`** [`37177899628`] added the repo philosophy-card scanner; **#165 MERGED `ede5d0f`** [`37178933884`] landed the recovered `p3-jev-core` hardening branch; **#166 MERGED `24057b1`** [`37181904007`] landed the recovered `p5-provision` core; **#163 MERGED `c8da12e`** with its five follow-up commits (`15f9150`, `bb43861`, `6df472c`, `23a6921`, `a13458a`) and **#167 MERGED `998e50f`** [`37233264445`] delivered `EV-0` and the fail-closed committed-receipt discount gate, correcting its own canon rows; **#170 MERGED `deb4859`** [`37231496382`] fixed three chat-lane defects; **#169 MERGED `c031dc5`** [`37236617617`] added the Jev calibration-analysis module; **#171 MERGED `d78319f`** [`37239043291`] wired dynamic model ranking into rotation. **Eleven of those merges flipped no STATUS row at all**, so the canon under-stated `main` until this reconcile — which is the `DF-CANON-1` defect class this repo already has a row for. On the phase gate, `harness jev-phase` on the merged tree returns `can_mark_complete=True` for `JEV-P3` (99.25) and `JEV-P4` (95.25), which confirms their existing **complete** rows; `DRV-1` returns 84.99 with `can_mark_complete=False` on `open_blockers` alone; `EV-0` was first recorded as 35.0 here, but the 2026-10-05 remeasurement on the same `0022cbb` tree is **30.0/85.0**, so `EV-0` stays **in progress**. Each new row below cites the green merge CI it rests on, which is the same pattern the `HV-5`, `HV-6`, `GAP-freeze-face` and `JEV-P4-residual` flips used (PRs #145, #147, #157). **What this reconcile does not do:** it invents no completion, opens no new gate, and leaves `OC-HANDOFF`, `DRV-2`, `GAP-plan-http` and `GAP-jury` exactly as gated as they were.
### Lane-unification integration audit — 2026-09-24

The original wf_1c82997e-f19 lane inventory has 23 items: 19 superseded and four non-superseded. Of those four, OC-HANDOFF remains in scope and gated; the safe local worker landed in PR #90 (6aea14b0d4aefd64a59190ad16378040775f3fbd), while PR #73 is closed as superseded. operator_tree_main has only its media portion integrated by PR #87 (8631ecad17d72ec62b1fae5f26ed31ed9fd5c210); worktree_ui_faces / DF-UI-2 remains open; and PR #88 (65da7b13dbe546238268bd986457b0dc96af4766) partially integrates the answer-lifecycle portion of worktree_jev_hourglass. PR #86 Ling rotation (a2cbb21859dcc7c95f1bc8deef168ccb246cede2) is separate from this inventory. PR #88 is only a partial HV-1 slice; no HV row is complete, and HV-0 is in progress.

**Reconciliation 2026-09-26 (origin wins).** Since the audit above, PR #85 (`DF-UI-2` read faces for `jev-phase`/`cost`/`missions` + MCP `jev_phase`) and PR #93 (`HV-0` bounded vision assessment, `2cf24b5`) both **merged**, and `main` CI is green (run 36109210066). So the two "remains open" claims above are stale as written: `worktree_ui_faces` landed a partial `DF-UI-2` slice, and HV-0's implementation merged even though its phase is not complete — the live pilot returned `status=unassessed` because of `DF-JEV-2` below. The dated 2026-09-24 audit is left as written for the record; the operative STATUS rows are the corrected ones below.

| Track / phase | Status | PR / evidence |
|---|---|---|
| Preflight sync `main`↔`origin` P0 | **complete** | `origin/main` includes P0 `d042d70` |
| `JEV-P0-*` contract truth | **complete** | **PR #34 MERGED** `d042d70`; live smoke OK |
| `JEV-P1-*` one owner + lanes | **complete** | **PR #35 MERGED** → `origin/main` `9d5ff14` |
| `JEV-P2-*` System One pillars | **complete** | **PR #36 MERGED** → `origin/main` `405bbc1`; hermetic lane-parity; D12 executed; local audit BAR MET; CI green on PR tip **and** post-merge `main`; live Jev smoke OK; **`JEV-P2-jury` deferred** by operator ruling (follow-up, not blocking P2 complete, tracked in operator ruling) |
| `JEV-P3-*` utilization | **complete** | **PR #43 MERGED** `7eb18ea`; route/triage/context/claims/completion/calibration |
| `JEV-P4-*` ops / exit | **complete** | **PR #46 MERGED** `a021cfc`; jury deferred by operator ruling. Residual dogfood A/B pass-rate + freeze persistence closed and complete on the `JEV-P4-residual` row below. Live dogfood receipt verified in `audits/self/dogfood/JEV_P4_AB_EVIDENCE.md` (cost $0.000002, 0% fallback rate, native jev-1.13.0 verified clean). |
| JEV-COMPLETION | **complete** | **PR #39 MERGED** `5e15f8d`; `harness jev-phase` gate on main |
| `JEV-P5-*` issue-sort buckets | **complete** | **PR #42 MERGED** `c9e1c67`; operator bucket packs, 0 hallucination; local audit 10.00/10. |
| `HUL-A` mission pack | **complete** | **PR #41 MERGED** `64e63a3`; mission pack + CLI + gate tests; CI green |
| `HUL-B` dual budget | **complete** | **PR #47 MERGED** `536e75c`; dual envelope + tests |
| `HUL-C` Jev scope gate | **complete** | **PR #48 MERGED** `469f34f`; scope packs via `jev_policy.evaluate_scope` (site=`hul_scope`); unkeyed cannot alone complete |
| `HUL-D` until-limits driver | **complete** | **PR #48 MERGED** `469f34f`; `mission run` until limits/stall + FINDINGS.md + interrupt-safe resume; D12 coverage honestly refreshed (`a3afc16`); dogfood follow-ups `DF-HUL-1..3` **closed** (PR #78 MERGED `dd431c3`). |
| `HG-*` hourglass composition | **complete** | **PR #44 MERGED** `f22accb`; follow-up **PR #68 MERGED** `4889776` (`DF-HG-1` composed ceiling, `DF-HG-2` cold-start pyramid resume, `DF-HG-3` preview decompose fallback; local audit BAR MET 10/10/10/10, CI green) |
| `SITE-*` proof bench site | **complete** | **PR #60 MERGED** `a9ae53f`; `harness site-export` → bundle-v1 → aggregate/Worker; tiers page + router; sanitized opt-in only; `DF-SITE-1` fixed in PR #70 `412f7e1`; 12/12 CI checks green on `main` (run `36773975947`); local audit 10.00/10. |
| `JEV-P6-*` repo summary A/B | **complete** | **PR #65 MERGED** `1936ed4`; 746/746 elements, $29.345862 exact, ledger chain verified (receipts in the JEV-P6 section); stage C = `JEV-P6-waist-brief` |
| `CLAUDE-LANE` Claude Code migration | **complete** | **PR #66 MERGED** `6f00d38`; `CLAUDE.md` + tool-neutral `AGENTS.md`, `.claude/` skills (`/isolated-mission`, `/isolated-request`) + scout/implementer/verifier tiers + settings, docs migrated (`docs/claude-context.md`, `docs/jev-mission-prompt.md`), MCP version negotiation fix. |
| `JEV-BAR-*` sentiment-bucket bar | **complete** | **PR #67 MERGED** `a9b58ab`; per-axis sentiment + declared improvement buckets drive bar pass; bar calls routed through `JevPolicy`; `packs/phase_completion.pack.json` |
| `HV-4` stage composition and planning waist | **complete** | **PR #118 MERGED** `2711cc5` (waist composition owner, planning ladder, stage selection/bypasses/allowances), **PR #134 MERGED** `9ef8ce39` (composed-run caller on CLI, MCP, and agent lanes), **PR #136 MERGED** `cd7424b6` (intake brief consumption via `waist.intake_brief`), and **PR #140 MERGED** `10f14e0` (composed planning stage invocation via `run_planning()` when selected). **One owner:** `harness/waist.py` owns stage composition (`resolve_stages`, `compose_stages`, `stage_states`, `composition_envelope`) and the planning ladder (`planning_ladder`, `run_planning`, `plan_outcome`). **Selection:** `resolve_stages()` validates subsets of `context`/`planning`/`execution`/`verification` in pipeline order. **Bypasses:** fresh brief bypasses context; supplied plan bypasses planning. **Allowances:** each stage is a child of the run's `TokenBudget`, strictly smaller and capped by the measured brief. **Planning ladder:** curates each round's brief against measured `estimate_brief_tokens` by bounded bisection. Exactly one terminal outcome per run (`sufficient`, `plan`, `evidence_request`, `defer`). 84 hermetic tests across composition and planning ladder (`tests/test_hourglass_stage_composition.py`, `tests/test_hourglass_planning_budget.py`) plus 15 gate tests in `tests/test_jev_completion.py`; `PHASE_CONTRACTS` names `harness/waist.py` and `harness/config.py`. Execution dispatch and sovereign handoffs tracked on `HV-5`. |
| `HV-5` execution dispatch and sovereign handoffs | **complete** | **PR #146 MERGED** `f5b53ff` delivered expanded-token execution allowances and sovereign consent handoffs. **Execution allowances & stage composition:** waist composed stages connect directly into execution dispatch, narrowing allowances from run budget and inheriting intake brief constraints; `AutonomousAgent._compose_run_stages` and `run_hourglass_request` manage per-stage allowances and provide web capability disclosure. **Sovereign consent handoffs:** consent binds to exact package limits; mid-task deferrals persist completed stage artifacts and structured handoffs (`jev-work-package-handoff-v1`) with `validate_restart_request` walk-back verification and consent renewal requirements. **Jev fallback provenance:** captures explicit fallback reasons (`explicit_disable`, `missing_key`, `transport_failure`, `http_fallback`) threaded through evaluator, `JevPolicy`, agent envelopes, and mission records, enforcing completion authority without losing error attribution. 37 hermetic tests in `tests/test_hourglass_execution_budget.py` and `tests/test_hourglass_consent_handoff.py`, plus tests across agent, consent, and orchestrator; local audit 10.00/10 across all 4 dimensions; CI green on PR tip and post-merge `main` (run `36708802205`). |
| `HV-6` surface parity, token budgets, and dogfood loop | **complete** | **PR #148 MERGED** `e5809a6` delivered surface parity across CLI, MCP, and Server/API faces with token budgets, intake brief, and canonical dogfood loop. **Stage selection & budgets:** shared policy owners (`resolve_stages`, `TokenBudget`, `intake_brief`) exposed across `harness plan`, MCP `plan_and_execute`, and server worker tasks. **Dogfood loop:** `harness.service.run_dogfood` is the canonical owner of the 3-phase dogfood lifecycle (ground -> verify -> apply), consumed by CLI (`harness dogfood`), MCP (`dogfood` tool in mutation lane), and Server/API (`run_dogfood_task`). Preflights verify target file and verify command before live spend (A7). Envelopes preserve token budget, brief, stage states, and stage judgments across CLI and MCP. 27 hermetic tests in `tests/test_hourglass_surface_parity.py` and `tests/test_hourglass_end_to_end.py`; local audit 10.00/10 across all 4 dimensions (A: 10/10, R: 10/10, SM: 10/10, SD: 10/10); 12/12 CI checks green on PR tip and post-merge `main` (run `36773975947`). |
| `HV-1` stage-specific JEV integration coverage | **complete** | Three slices landed. **PR #88** delivered the answer/reiterate lifecycle (`answer-sufficiency-v1`, `JevPolicy.evaluate_answer`). **PR #98 MERGED** `69039f1` (landed with PR #101) delivered `hourglass-stage-v1` declaring five dimensions — `context_intake` (relevance/coverage/conflict), `plan_soundness` (soundness/evidence-requested), `execution` (suitable/checkpoint), `consent` (freshness/defer/escalation), and `restart_target` — behind `JevPolicy.evaluate_hourglass_stage` with restart-target enum validation (`validate_restart_request`). Calling-lane wiring and dogfood completed: `harness/waist.py:intake_brief` evaluates `context_intake`; `compose_plan` evaluates and preserves `context` and `planning` stage judgments; `harness/agent.py:_compose_run_stages` and `_restart_decision` evaluate `context_intake`, `execution`, `consent`, and `restart_target` with code-owned consent renewal enforcement. 38 hermetic tests in `tests/test_hourglass_jev_integrations.py`. Local audit 10.00/10 across all 4 dimensions; 12/12 CI checks green on PR tip and post-merge `main` (run `36773975947`). Live dogfood receipt verified in `audits/self/dogfood/HV1_LIVE_PILOT_2026-10-01.md` (cost $0.000010, 0% fallback rate, native jev-1.13.0 verified clean). |
| `HV-2` evidence-bearing context brief | **complete** | **PR #99 MERGED** (stacked; landed on `main` with PR #101) shipped `harness/brief.py` **schema v2** (additive; v1 packs stay readable) and now carries the four evidence families the Hourglass contract requires, all code-owned and hermetic — no network, no model, no Jev call from this module. **Freshness/source identity:** each cited source records `bytes` and `observed_at`, the pack records `built_at`, and the new `freshness_report()` re-checks every pin, reporting drifted or absent sources as findings rather than raising; a brief with no sources is explicitly *not* fresh rather than vacuously so. **Scope coverage:** `scope.included`/`scope.excluded` plus code-owned `coverage` counters. **Omission made visible:** a source the budget cannot represent is listed in `omitted` and mirrored in `coverage.gaps`, and both appear in the rendered artifact. **Uncertainty/conflicts:** `conflicts` must cite the sources it came from; an uncited or unknown-source conflict is refused. **Size:** `estimated_tokens` is measured over the bytes the pack actually ships. Independent `render_brief()` consumes a supplied pack with **zero** I/O, so a caller can use a brief produced elsewhere without re-running intake, while `validate_brief()` closes by default on coverage-counter disagreement, a bad token estimate, or a wrong schema version. Also fixes a real bound bug found while doing this: `_window` bounded a truncated excerpt only by the per-window cap, so a small leftover budget was overrun and `max_total_chars` was decorative on exactly the inputs that needed it. D12 then showed that the two `return None` paths guarding the excerpt were unreachable (a source with any budget left always shows at least one character), so they and the caller's `None` branch were deleted instead of tested, and a sub-character budget now floors to an honest omission. **Reconciliation slice (2026-09-27, this lane):** `condenser.MicroBrief` now carries that same v2 pack via `to_brief_pack()` — built from the bytes the condenser already holds through `brief.build_brief`'s reader seam, so there is no second schema and no extra I/O — and every condensation site (`agent._hourglass_request_context`, the edit lane, `waist._decompose_repo_context`) appends a `brief_built` ledger event (`site=hourglass`, `schema=2`), so condensation is durable evidence rather than a private step. That live run found a real ambiguity: the event announced `schema=2` while carrying only the condensed prompt estimate (320 tokens) and not the pack's own measured size (2629), leaving a budget owner no way to tell which figure it was preflighting against. `MicroBrief.ledger_fields()` now reports both, each named for what it is, and is the one owner of that payload. `MicroBriefReconciliationTests` (9 hermetic tests in `tests/test_hourglass_brief.py`) and `Hv2LedgerEvidenceTests` (2 in `tests/test_hg_condense_decompose.py`) cover the reconciliation and its ledger evidence; `PHASE_CONTRACTS` names the three test modules and both owned files. **Live dogfood 2026-09-27** (real repo, real settings, ledger sandboxed through `HARNESS_LEDGER`, no model call, no network): 1 source cited with sha256/bytes/`observed_at`, `validate_brief` clean, `render_brief` 9,304 chars with zero I/O, `freshness_report` fresh against the live filesystem, `brief_built` event written. Closes `MS-context` (folded in by operator ruling 2026-09-26). The composed and dispatch lanes that consume the brief are tracked on their own row below. |
| `HV-2-use` brief consumption by composed/dispatch lanes | **complete** | Owner: `HV-4` (composition), `HV-5` (execution dispatch), `HV-6` (surfaces). **PR #134 MERGED** `9ef8ce39`, **PR #136 MERGED** `cd7424b6`, **PR #146 MERGED** `f5b53ff`, and **PR #148 MERGED** `e5809a6`: the composition, intake brief, execution dispatch, and surface parity (CLI/MCP/API faces) are delivered. |
| `HV-3-use` budget consumption by composed/dispatch lanes | **complete** | Owner: `HV-4` (composition through `TokenBudget`), `HV-5` (execution packages carrying allowances), `HV-6` (budgets on CLI/MCP/API faces). **PR #134 MERGED** `9ef8ce39`, **PR #146 MERGED** `f5b53ff`, and **PR #148 MERGED** `e5809a6`: composition through `TokenBudget`, execution packages carrying narrowed child allowances under root ceilings, and surface exposure on CLI/MCP/API faces are delivered. |
| `HV-3` token allowance and accounting owner | **complete** | **PR #100 MERGED** (stacked; landed on `main` with PR #101). New `harness/token_budget.py` is the ONE owner of per-call input/output maxima and of composable stage/run reservations, enforced **independently** of `SpendGovernor`'s dollars (`harness/spend.py` keeps the dollar formula; neither can raise the other). Shape: `TokenBudget` holds an input and an output allowance, `budget.stage(label, max_input_tokens=…, max_output_tokens=…)` returns a child that may only **narrow** its parent (a stage cannot widen its own limits by nesting), and one call's worst case is reserved from every budget in the chain at once under the root's single lock. `allowance()` is the reservation object itself, so it settles or cancels exactly once; a second settle, or settling an allowance another budget owns, is refused. `settle(..., source=…)` records `actual`/`estimated`/`unavailable` honestly and charges the **full** reservation when usage is unreadable rather than zero; a provider that reports more than was reserved is recorded with the overrun counted in `over_input_tokens`, never clamped. `cancel()` is a pre-dispatch refusal that charges nothing. `snapshot()` exposes used/reserved/remaining, per-label usage with its source, and `open_allowances` so a run that leaks a reservation is visible. Concurrency is covered with real threads (30 racing workers admit exactly 10, 40 racing settlements never double-count). Config: `token_budget_input`/`token_budget_output` with absolute hard caps (`HARD_TOKEN_BUDGET_INPUT`/`_OUTPUT`), loaded by `budget_from_settings`. 22 hermetic tests in `tests/test_hourglass_token_budget.py`; consumer and surface exposure delivered by **PR #134**, **PR #146**, and **PR #148**. |
| `HV-0` JEV integration foundation + vision-assessment pilot | **complete** | **PR #93 MERGED** `2cf24b5` (implementation) + **PR #97 MERGED** `0e6f537` (close-out; landed on `main` with PR #101) (`DF-JEV-2`/`DF-JEV-3` fixes and the live pilot). Assessment pack **v1.1.0** (declares `score_probability_precision`), `JevPolicy.evaluate_vision_assessment`, all-or-nothing typed/unassessed envelope, shared pricing at `$0.0042/Mtok` (**SUPERSEDED, see CORRECTION below**), `harness jev-vision-assessment` face. **Live pilot passed 2026-09-26:** `status=assessed`, native `jev-1.13.0`, `fallback_state=not_used`, `usage_source=actual`, 15,301 in / 154 out tokens, **$0.0000642642**, one reservation / one no-retry attempt / one settlement / one metadata-only `jev_eval`, ledger chain clean. All four pre-dispatch gates passed with margin (15,896 est. tokens vs 64,000; reserve $0.0000668 vs the $0.10 hard cap). Receipt: `audits/self/dogfood/HV0_LIVE_PILOT_2026-09-26.md`. Named gaps it reported (`jev_selection_gap`, `usage_truth_gap`) corroborate the not-yet-delivered `HV-1` remainder and `HV-3` **CORRECTION (supersedes every `$0.0042/Mtok` figure in this row and in the receipts `audits/self/dogfood/HV0_LIVE_PILOT_2026-09-26.md` and `audits/self/dogfood/JEV_P4_AB_EVIDENCE.md`):** the verified Jev input rate is **`$0.042/Mtok`** ($42 per billion tokens), confirmed by an operator usage export (13,661,429 input tokens = **$0.5738** billed). The pilot cost above is therefore understated 10x: 15,301 input tokens at `$0.042/Mtok` = **$0.000642642** (not `$0.0000642642`), and the worst-case reserve and the A/B cost figures scale the same way. The receipt files are hash-pinned evidence and are left unedited; this note is the correction of record. |
| `JEV-P4-residual` dogfood A/B + Jev settings freeze | **complete** | **PR #130 MERGED** `ede24262` (`GAP-freeze-face` / `harness jev-freeze` + MCP `jev_freeze`) delivered the settings freeze face; dogfood A/B empirical pass-rate, token consumption, and cost delta benchmark executed and archived in `audits/self/dogfood/JEV_P4_AB_EVIDENCE.md`. |
| `DF-*` dogfood follow-ups | **open** | 2026-09-22 audit (9 components, 32 findings, 31 confirmed by independent re-verification, $0.29 live spend) — rows in "Dogfood audit follow-ups". **`DF-#108` handoff-scope follow-ups closed 2026-09-28** (PR #122 MERGED `b2c87e5`): `scripts/validate_handoff_scope.py` now reports **zero findings** across the 11 in-scope documents (6 stamped, 2 owner-case corrected, 3 waived in `handoff_scope_waivers.json`), plus an advisory `handoff-scope` CI job that annotates `::warning` and always prints the register. Fixing the stamps exposed a masked finding — `JEV_LOG_PROMOTION_DRAFT.md`'s body never named its owning repository. |
| `MS-*` cheapest-capable routing + failover | **complete** | **PR #69 MERGED** `e47001a`; cheapest capable routing (no Ling defaults, Scout defaults to Gemma 4 31b, planning markers to Distiller Tier 1, router Jev route evaluation), auto paid failover (cheap paid pools appended, sorted cheapest first), hermetic Jev fake transport, `DF-MS-1` + `DF-MS-3` closed (`DF-MS-2` documentation-only, see `DF-MS-2b`); local audit BAR MET 10/10/10/10, CI green. Scope note (Claude audit 2026-09-22): the routing/failover slice is complete; the "context" half moved to `MS-context` |
| `MS-context` condensed state for expensive seats | **complete** | split from `MS-*` on 2026-09-22; PR #69 did not touch context condensation. Operator ruling 2026-09-26: two rows over one owner surface is a parallel plan, so this row is satisfied by `HV-2`'s evidence-bearing brief (`harness/brief.py` schema v2: provenance, freshness, scope coverage, visible omission, uncertainty/conflicts, token estimate) and closes with it — no separate condensation track. Both halves are in: v2 shipped, and the `condenser.py` `MicroBrief` reconciliation plus its `brief_built` ledger evidence landed 2026-09-27, so this row closes with `HV-2` above; consumption of the brief is tracked on the `HV-2-use` row. |
| `JEV-AUDIT-GATE` Jev 4-dimension self-audit gate | **complete** | **PR #79 MERGED** `d3994e9`; `audits/self/audit.py` gate + `JevPolicy.evaluate_audit_dimensions` + `tests/test_audit_dimensions.py`; hermetic keyed-path test (D12) added, partial-run "not evaluated" scoring fixed; local audit BAR MET 10/10/10/10, CI green |
| `MEDIA-1` `harness media` adapter | **complete** | **PR #87 MERGED** `8631ecad17d72ec62b1fae5f26ed31ed9fd5c210`; owner `harness/media_client.py` (`MediaAdapter` + `run_cli`, thin stdlib client for the sovereign-media sibling service — image/video generation, honest `MediaUnavailable` failures, budget-refusal envelopes, config/env-resolved endpoint, no provider brand strings); hermetic tests `tests/test_media_client.py`; documented in `docs/media.md` + README CLI section |
| `DYN-ALLOC` dynamic resource allocation engine | **complete** | `harness/dynamic_allocation.py` delivers dynamic context window scaling, adaptive window lines, dynamic terminal reserve, model-rated tier spend ceilings, task-scoped evidence sizing, and model ranking. Raised `HARD_MAX_COST` / `HARD_TASK_MAX_COST` to $1.00 and added proportional child token budgeting. 11 hermetic unit tests in `tests/test_dynamic_allocation.py`; local audit 10.00/10 across all 4 dimensions (A: 10/10, R: 10/10, SM: 10/10, SD: 10/10). **Corrected 2026-10-04:** this row read complete while the engine's model half was inert — `rank_models_dynamically` had **zero production callers** and no producer anywhere in the repo for its `health_status` input. That gap is now closed on the `DYN-ROT` row below (PR #171 MERGED `d78319f`; `harness/router.py:106` is the production call site). The clauses above were genuinely live when this row closed, so only the model-ranking claim needed the correction. |
| `JEV-CORE-HARDENING` Jev spend, reservation, flush, cache, and breaker invariants | **complete** | **PR #165 MERGED `ede5d0f`** (post-merge `main` CI run `37178933884`, all 12 legs). Recovers the abandoned `p3-jev-core` WIP (18 commits, ~3,940 lines) onto current `main`. **Delivered:** atomic reservations and spend invariants in `harness/spend.py`; a crash-proof ledger flush plus `chain_status()` segmentation reporting in `harness/ledger_analytics.py`; a single-flight answer cache; site breakers with generations; and a dedupe-and-cap fallback ledger. Gate batteries: `tests/test_jev_chaos.py` (bounded chaos) and `tests/test_jev_core_hardening.py` (2,321 lines). **Two real conflicts, both in `ledger_analytics.py`, settled at the cause rather than by dropping a side:** the WIP branch proposed *“billed is billed”* (prefer the cost the governor settled), while `p2-strict-dogfood` had already fixed the opposite bug — entries written when the client priced input at `$42/Mtok` carry a stored figure **1000x** the verified `$0.042/Mtok` — so any report reading that field overstates spend by three orders of magnitude. The stored cost is untrustworthy, so the settled-cost rule was **dropped**: both report paths now price through one helper (`_jev_row_cost`), which also removes the drift that let the month-scoped Jev credit and the window-scoped totals disagree about the same row; a fallback claims no credit even when it settled, because the credit measures *keyed* usage; and a tokenless row claims nothing. Three tests that encoded the superseded rule were **rewritten, not deleted**, to pin the verified-rate invariant. **No row was flipped by that PR** — this row is created here, which is why it says *complete* against the merge's own CI rather than a phase gate that has no contract entry for it. Gates re-run on the merge tip: hermetic suite 3521 OK (56 skipped), ruff clean, keyed audit BAR MET 10/10/10/10 (`is_fallback=False`, `model=jev-1.13.0`), D11 and D12 pass, coverage baseline regenerated (87 modules, 26,957 lines) and re-pointed to the merge-base with `main`. **Not claimed:** this row owns no live dogfood receipt; the one that the branch's plan asked for was a metric fix, and the branch correctly reported that keyed Jev was already the default path (10-04 ledger: 148 evals, **0** fallbacks). |
| `DYN-ROT` dynamic model ranking wired into rotation | **complete** | **PR #171 MERGED `d78319f`** (post-merge `main` CI run `37239043291`). `rank_models_dynamically` had been on `main` since #158 with **zero production callers** and **no producer anywhere** for its `health_status` input, so the Dynamic Resource Allocation Engine's model half was inert while the `DYN-ALLOC` row above read as complete. **Delivered:** `harness/router.py` keeps a per-model health registry (`note_model_result` / `model_health`) and serves its four rotation pools (`panel_pool`, `apply_pool`, `specialist_pool`, `escalation_pool`) through properties whose setters are retained (three tests assign them directly) and whose getters **delegate ordering to the ranker** rather than reimplementing it; `harness/apply_policy.py` records **every** terminal attempt outcome — HTTP error, paid BYOK, reasoning-only, readiness defer, success — so a model that keeps erroring sinks in the next round's order instead of being retried in its declared position. **Two safety rules, both pinned:** the **declared head is never displaced** (`DF-LING-2` and cheap-first both depend on the default pick staying put, so only the tail is ranked), and **a pool with no observation in its own tail is returned exactly as declared**, so a health record on some unrelated model cannot silently re-rank anything. A success walks the error count down rather than clearing it. The call site sits behind a `getattr`/`callable` guard so a test double or an embedding that supplies its own router cannot break a run over an ordering refinement. Gate tests `tests/test_router_dynamic_rotation.py` (16 tests, 4 classes: `UnobservedPoolsTests`, `HealthRegistryTests`, `RotationOrderTests`, `RankerIsTheOneInUseTests`). |
| `CHAT-LANE` response shapes, reasoning memory, and bounded run wall time | **complete** | **PR #170 MERGED `deb4859`** (post-merge `main` CI run `37231496382`). Three defects the chat lane hits in normal use, each fixed at the cause. **`harness/waist.py`:** `_parse_json_object` searched for the first `{` and parsed the remainder of the body, so a model that narrates before its answer — or adds a sentence after the closed object — was read as unparseable and the caller **retried, paying twice for a good response**. It is now a string-aware balanced-brace scanner (`_balanced_object_spans`, `_json_object_candidates`): fenced block first, then the first balanced object, then the whole body; braces inside JSON strings do not end a span, an unbalanced `{` in a trace is skipped instead of swallowing the rest, and trailing commentary no longer breaks the parse. **Refusal semantics are unchanged:** `refuse` still owes reason+evidence, `amend`/`split` still owe nodes, `request_windows` still owes its list, and a reasoning-only response still raises — that is what lets the caller treat it as retryable rather than as a verdict. This is the deeper fix behind `DF-LING-1` (see that row). **`harness/chat.py`:** a provider's rejection of a reasoning parameter is now remembered process-wide (`note_reasoning_param_rejection` / `reasoning_param_rejected` / `reset_reasoning_param_memory`), learned from the provider's own rejection text and keyed on `strip_variant_suffix` — **no brand hardcoding** — so a parameter a model rejects is not re-offered on every subsequent call. **`harness/agent.py`:** `DEFAULT_RUN_WALL_SECONDS = 120.0` with a per-call `max_wall_seconds`, a deadline checked before every answer attempt, `stop_reason` surfaced in the result dict, and `is_research_question()` (research subject plus lookup verb, or an interrogative opening) forcing the web lane, so a question that needs a lookup cannot be answered from the model's memory. Gate tests `tests/test_waist_response_shapes.py` (26), `tests/test_chat_reasoning_memory.py` (7), `tests/test_agent_run_limits.py` (11), with a per-test reasoning-memory reset added to `tests/__init__.py`. D12 went 104/105 → **103/103 = 100%** because a provably unreachable `raise` in the parse loop was deleted rather than tested (the whole body is always appended to `candidates`, so `last_error` was always set and the guard could never run). |
| `REPO-CARDS` repo philosophy cards for the canonical sibling repos | **partial** (generator delivered; faces **open**) | **PR #164 MERGED `bbe7f6e`** (post-merge `main` CI run `37177899628`). `harness/repo_cards.py` (375 lines) deterministically scans committed repo config into `.harness/cards/*.md` for the **8** canonical sibling repos — `Harness-*` and `wt-*` are worktrees and `test*`/`scratch` are not repos, which leaves nine, and `SCMessenger-v040-harness-plan` was then dropped because it produced a row-identical card to `SCMessenger`. **The schema is three cells — `axis`, `norm`, `origin` — and a row whose origin cannot be resolved from committed config is not emitted at all:** cards are meant to be passed to `build_brief` as ordinary citable windows, so a norm asserted without a source file would hand a model a pre-fabricated unsourced claim to cite — the exact failure `brief.py`'s `GROUNDING_RULES` exist to prevent. Cards record that a convention file *exists* and never summarise what it says (extracting a claim from prose would be fabrication with extra steps), so rewriting `AGENTS.md` produces no card diff and only a new origin-bearing file does. Determinism — sorted, no timestamps, no absolute paths, explicit LF — is pinned by `test_regeneration_is_byte_identical`; `--check` reports staleness and exits non-zero. Gate tests `tests/test_repo_cards.py` (370), including a corpus pin that no row may cite a file that is not on disk. **Withheld from the complete claim, verified by grep on the merged tree:** the scanner has its own argparse entry point but **no `harness` CLI or MCP face** and **no CI drift job** — `harness/cli.py`, `harness/cli_parser.py` and `.github/workflows/ci.yml` contain no reference to cards, and no `harness/*.py` imports `repo_cards`. That is the open half of this row. |
| `PROVISION-CORE` host-aware provisioning: probe, plan, approve, execute | **open** | **PR #166 MERGED `24057b1`** (post-merge `main` CI run `37181904007`) lands the recovered `p5-provision` core: `harness/provision.py` (2,221 lines) split into four separately testable stages. **probe** — read-only host facts (OS, arch, interpreter, GPU, free disk, package managers) through the `osal` subprocess seam. **plan** — an ordered typed `Plan` of `Step`s where a step is an argv **list**, never a shell string, carrying a class (READ / MUTATING / IRREVERSIBLE, the same vocabulary as `driver_core.actions`), its expected effect, a rollback hint, and whether it needs the network; a Jev choice pack may pick among recipes the code already proved applicable, and unkeyed or invalid answers fall back to a deterministic order and say so via `is_fallback`. **approve** — `ApprovalGate`: nothing mutating runs without an approval record bound to the plan's **content digest**; decline and defer are recorded outcomes, not errors; an IRREVERSIBLE step needs its **own** approval and a plan-level yes never covers it. **execute** — dry-run by default; real runs go through `osal` with timeouts, stop on first failure, report rollback hints, and append one hash-chained ledger entry per plan, approval and step. `classify_argv` is the single admission point and **the allowlist is the gate**. Gate tests `tests/test_provision.py` (1,410), `tests/test_provision_hardening.py` (474), `tests/test_provision_structural.py` (645), `tests/test_provision_wiring.py` (456); posture documented in `docs/security.md`. **This row stays open and must not be widened:** the module docstring marks the GUI card, the MCP tool and the agent intent as separate, later wiring, and a grep over `harness/cli.py`, `harness/cli_parser.py`, `harness/mcp.py`, `harness/mcp_lanes.py`, `harness/mcp_schemas.py`, `harness/server.py` and `harness/agent.py` finds **zero** references to `provision` on `main`. There is no caller, no surface, no pack registered for `harness jev-phase`, and no live probe receipt — a core with no surface is not a shipped feature, and closing this row on the strength of its own unit tests is the exact move `HV-2`/`HV-3` were split to prevent. |
| `JEV-P3-calibration-analysis` judgment-distribution calibration analysis | **partial** (module delivered; threshold application **open**) | **PR #169 MERGED `c031dc5`** (post-merge `main` CI run `37236617617`). New `harness/jev_calibration.py` (215 lines) is the analysis owner for the `JEV-P3-calibration` pattern: `_percentile`, `_distribution`, `_validate_record` (a malformed record is refused, never silently dropped), `analyze_judgments`, `recommend_threshold`, `format_report`. It imports nothing from the rest of Harness — pure analysis over judgment records. Documented in `docs/jev-calibration.md`. Gate tests `tests/test_jev_calibration_analysis.py` (23 tests) cover every validation branch, which is what moved D12 from 77/82 (94%, below the 95% bar) to **82/82 = 100%**; the same PR added the module to the CONTRIBUTING table so D10 would not drop for a module with no table row. **Withheld, verified by grep on the merged tree:** no `harness/*.py` imports `jev_calibration` and nothing calls `analyze_judgments`, so the analysis runs in its own tests only — `ledger_analytics.jev_calibration_report` is a pre-existing, unrelated function. The Phase 4 checklist item *“model pin + threshold freeze after first calibration”* remains an **operator** step (`harness jev-freeze --persist`, delivered on `GAP-freeze-face`); this module informs it and does not perform it. Note the module does **not** own `compute_dynamic_tier_ceiling`, `dynamic_run_token_budget` or `TokenBudget.proportional_stage` — those are `harness/dynamic_allocation.py` and `harness/token_budget.py` from PR #158. |
| `DRV-1` `harness driver` adapter | **open** | **PR #159 MERGED `4e2a548` (2026-10-03) — landed on `main`, awaiting its CI-green flip.** The earlier wording ("not landed; CI is green on `bca730d`, the row stays open until the PR merges") was stale on both counts. The merge is on `main`; the local gate is re-run on the merge tip (hermetic suite 3313 tests OK, self-audit BAR MET 10/10/10/10, D12 changed-line coverage 40/40 = 100% against a baseline naming `4e2a548` on `main`). Per the completion rule this row flips only once CI is green on the merge and a paid-cheap live dogfood receipt exists for the driver lane; `harness jev-phase --phase DRV-1` currently returns `can_mark_complete=False` with `no_open_blockers` and `dogfood` unmet, so it stays here. Scope in the PR: the adapter `harness/perception_client.py` (`PerceptionAdapter` + `run_cli`), the Harness face of the driver, plus the driver itself as a native, in-repo, segmented module (`driver_core/`: stdlib only, no Harness imports, wire-compatible with the standalone driver-core project so either can sit behind `DRIVER_BASE_URL`). Thin stdlib client in the same shape as `MEDIA-1`: endpoint resolved from config/env/args, no provider brand strings, injectable `opener` seam so the suite is hermetic. **A refusal is a successful call**: the service answers HTTP 200 with `ok: false` and a closed-set `reason` (`STOP_REASONS`), `step()` returns that envelope, and only transport failures and contract violations raise `PerceptionUnavailable`; `_check_contract` rejects a refusal with no reason or an undeclared one. **Consent is never synthesised** by the adapter. Also in the PR: the `driver-core` entry point, loopback REST `/api/driver/*` (random per-start token, no fixed fallback), the Driver pane, and MCP tools `driver_step`/`driver_health`/`driver_vocabulary`/`driver_verify`. Documented in `docs/driver.md`. Mark it complete only after it lands, with CI passing on the PR tip and on `main`. **2026-10-04 update.** **PR #161 MERGED `98f077c`** (post-merge `main` CI run `37172346812`) repaired five defects the live dogfood found against merged `main`, each reproduced first and fixed at the cause: the driver pane appended an **empty `<form>`** because every field was constructed and then never attached; the Action Vocabulary tab **crashed** because `normalisers` is a name→canonical-value *mapping* the pane called `.join()` on, and the tier was read from `act.mutating`/`act.irreversible`, fields the wire shape never carries, so every row also rendered `READ_ONLY`; **every multi-row table rendered `[object HTMLTableRowElement],…`** because `$` appended a mapped list as a *single* child, which silently broke the Proof Bench and Insights panes too; the header **spend badge always read `$0.00`** because `pollSpend` caught and discarded every error, making an unreadable envelope indistinguishable from zero spend; and **`panel_verify`'s per-task cap was never enforced** — MCP validated `task_max_cost` against the session budget and then never passed it through, so one call could spend the entire session budget. That last one is fixed by `SpendGovernor.task_scope()` (`harness/spend.py`), which **extends the one governor rather than adding a second class** (`tests/test_hul_budget_reserve.py::test_no_second_governor_class` still pins that). **The row still stays open, and now for exactly one reason.** `harness jev-phase --phase DRV-1` on the merged tree returns score 84.99/85.0, `can_mark_complete=False`, with `pr_merged`, `origin_evidence`, `local_gates_green` and `ci_green` **all true** and a single failing gate, `open_blockers`, carrying `[dogfood_missing]`: *“Run paid-cheap live dogfood on the user-facing lane; record receipt, cost, and fallback rate.”* The CI clause of the completion rule is satisfied; the paid-cheap live dogfood receipt for the driver lane is the whole of what is left, and nothing in a Harness PR can produce it for the operator. |
| `DRV-2` driver-core extraction providers | **open** | Tracked here because `DRV-1`'s value depends on it. The adapter and the extraction *tier* are in `DRV-1`; the providers underneath are not functional. Every synthetic input action (`click`, `type_text`, `press_key`, `focus`, `scroll`, `submit_irreversible`) is declared and **always refused** (`ok: false`, `reason: execution_refused`) until an input backend is registered through `driver_core.osal.register_input_backend`, and nothing in this repository registers one; no live vision extractor is wired either. The module is native and in-repo, so this is Harness work (Phase 4 of the takeover plan), built self-contained inside `driver_core/` and interoperable with the standalone driver-core project. Vision stays the *last* tier by design (CLI -> MCP -> DOM -> pixels). Close only when a real backend sits behind a declared action under consent/policy gating, with conformance coverage and a live `harness driver step` that performs the action. See `docs/driver.md`. |
| OC-HANDOFF findings-only lane | open / gated | Safe local worker PR #90 MERGED `6aea14b`; CI run 296 passed all five jobs. PR #73 is closed as superseded. The worker in `examples/oc_handoff/worker.py` accepts signed findings, reviews a bounded semantic summary, and can write only HANDOFF/OC_FINDINGS.md in its executing Harness checkout. A 2026-09-24 local-only phase preflight exited 1 at score 84.99 and correctly failed the required-output and verified-receipt gates. The isolated local self-audit passed 10.00/10 in A/R/SM/SD with R13, Ruff, and coverage green; its Jev judgment used the documented offline fallback, and a live judgment remains pending. The actual signed-candidate Jev intake and receipt/output audit remain pending because this checkout has no candidate or findings file. No external runtime/config changes are in scope; never target seat-owned HANDOFF/*_STATE.md files. See the gate contract below. |
| Dogfood / paid smoke | **ongoing** | every phase: hermetic gates + operator live smoke when client/lane changes; paid cheap rungs (`HARNESS_USE_FREE=false`) |
| Exit | Jev P4 checklist all true on `origin/main` | **complete** — all P4 checklist items are complete and verified on `origin/main`: live Jev default, unkeyed explicit fallback, envelope cost truth, ledger/analytics calibration, consent gating, and JEV-P4-residual dogfood A/B receipt. |
| Exit | HUL A–D shipped **or** open-problem packs on HUL contract | **open** — HUL A–D are all finished per the rows above; what remains is the operator ruling on which open-problem pack (if any) discharges this row, and the two `HV-*` consumers **Corrected 2026-10-04:** the two `HV-*` consumers this row named as the remaining condition are both **complete** — `HV-4` (PR #118/#134/#136/#140), `HV-5` (PR #146), `HV-6` (PR #148), and with them `HV-2-use` and `HV-3-use`. HUL A–D were already finished. **What actually remains is the operator ruling alone**: which open-problem pack, if any, discharges this row. No Harness PR can supply that. |
| Release | PyPI publishing by tag (issue #107) | **complete** — PR #117 MERGED `8e5a100` (`publish.yml`: `v*` tag → build → `twine check` → 3-way version parity → clean-venv wheel smoke → `pypa/gh-action-pypi-publish@release/v1` OIDC, `id-token: write` only, `dry_run: true` by default), runbook `docs/releasing.md`, 11 hermetic tests. Release v0.4.2 prepared with version bump and CHANGELOG entries. |

### OC-HANDOFF — findings-only Harness lane

This Harness-owned OC handoff task is in scope but remains gated. PR #73 is closed as superseded; the safe local implementation in `examples/oc_handoff/worker.py` landed in PR #90 (`6aea14b`) after independent review, focused tests, and green CI run 296. It pins execution to its Harness checkout, requires explicit local signing approval, bounds the Jev request, and limits repository output to HANDOFF/OC_FINDINGS.md. A 2026-09-24 `--local-only` phase preflight exited 1 at score 84.99, as expected with no output or verified receipt. The isolated local self-audit passed 10.00/10 in all four dimensions, with R13, Ruff, and changed-line coverage green; Jev used its offline fallback, so that result is not a live semantic judgment. The actual signed-candidate Jev intake and receipt/output audit remain pending because this checkout contains no candidate or findings file. Never infer or target seat-owned HANDOFF/*_STATE.md files. All implementation, tests, and documentation changes stay in this repository; no external runtime/config changes are included. PR #89 was documentation-only.

The merged worker and its hermetic suite implement the following controls:

- The worker pins its root to the executing Harness checkout; it does not accept a repository root from a manifest. It uses Harness WorktreeIsolation, allows only HANDOFF/OC_FINDINGS.md, rejects manifest-supplied path changes, and audits the exact changed-file set before merge.
- It requires locally authenticated identity and explicit operator approval for signed manifests, and validates signatures, expiry, and replay protection before work starts.
- Use locally fixed database, lock, and outbox roots plus a Harness-owned fixed verification function; never accept paths, commands, or arbitrary executables from a manifest.
- Run edits in the isolated worktree and audit committed plus uncommitted changes against the exact one-file allowlist before merge. Repository writes may update only HANDOFF/OC_FINDINGS.md; worker state may use only separately fixed local state roots.
- Define the Harness worker egress and spend policy in repository-owned configuration and tests. No live provider or external-system calls are part of this repo change.
- Crash/restart recovery reconciles database, lock, and outbox state without duplicate, lost, or unverified handoff writes.
- Hermetic tests cover fixed root/path allowlisting; manifest authentication, approval, expiry, and replay rejection; fixed state roots and verifier; rejection of manifest-controlled executables; exact-file diff auditing; egress/spend policy; and crash/restart/outbox reconciliation.

Worker enforcement, hermetic tests, independent review, and CI are complete in PR #90. The phase bar now checks required-file presence independently of overridable `--evidence` blockers. Its OC-specific hard gate reads the archived signed manifest and local worker receipt/outbox, verifies both the operator attestation and keyed receipt HMAC with `HARNESS_OC_HANDOFF_HMAC_KEY`, checks the authenticated Jev acceptance threshold and non-fallback native pass within the $0.05 / 1,024-token bounds, bounds persisted file/state reads, validates the receipt and manifest digests, and rechecks that the receipt commit changes only `HANDOFF/OC_FINDINGS.md`, has the recorded base as its parent, is merged into `HEAD`, and matches the current output hash. A handoff file by itself cannot pass the phase bar. Keep OC-HANDOFF open / gated until an actual signed, locally approved findings candidate completes that path and `harness jev-phase --phase OC-HANDOFF` passes. The current checkout has no such candidate, receipt, or findings file. `DF-JEV-2`/`DF-JEV-3` now precede the HV-0 close-out in the priority order above.


**P2 done when (all true):** … → PR #36 merged → post-merge `main` CI green → STATUS P2 `complete`. **TRUE on 2026-09-21.**

### P2 evidence snapshot (repair complete — merged)

| Item | Finding |
|---|---|
| PR | [#36](https://github.com/Sovereign-Communication/harness/pull/36) **MERGED** `405bbc1` |
| Repair | hermetic `tests/test_jev_lane_parity.py`; D12 lines executed; battery 118 OK; audit BAR MET; live smoke `jev-1.13.0` non-fallback honest cost |
| Deferred | `JEV-P2-jury` — needs fail-closed contract + dedicated coverage |
| Post-merge CI | `main` audit + test 3.9/3.11/3.13 + package **SUCCESS** |

---

## Tracker

| Phase | IDs | Primary modules | Gate tests | Status |
|---|---|---|---|---|
| 0 Contract | `JEV-P0-*` | `jev.py`, `config.py`, docs | `tests/test_jev.py` + smoke | **complete** — PR #34 |
| 1 One owner | `JEV-P1-*` | policy + lanes | `test_jev_policy` / `lane_parity` / `ledger_spend` | **complete** — PR #35 |
| 2 Pillars | `JEV-P2-*` | consent, sliding_scale, waist triage | `test_jev_triage`, `test_consent_confidence`, `test_min_confidence_gating` + hermetic lane parity + audit | **complete** — PR #36 `405bbc1`; jury deferred; `JEV-P2-dead-code` wired — PR #58 `b21d8f0` |
| 3 Utilization | `JEV-P3-*` | orchestrator, routing, context, panel, calibration | `tests/test_jev_util_*.py` (route, triage-files, context-pack, claims, completion, calibration) | **complete** — PR #43 `7eb18ea` |
| 4 Ops | `JEV-P4-*` | workflows, analytics, docs | live acceptance checklist + dogfood with/without jev | **complete** — PR #46 `a021cfc` |
| 5 Issue-sort | `JEV-P5-*` | `jev_policy` + `harness/jev_packs.py`, waist/orchestrator/CLI/MCP | `tests/test_jev_issue_sort.py` (+ pack/orchestration) | **complete** — PR #42 `c9e1c67`; operator bucket packs, 0 hallucination |
| HUL-A | `HUL-A-*` | `harness/mission_record.py`, CLI `mission` | `tests/test_hul_mission_record.py` | **complete** — PR #41 `64e63a3` |
| HUL-B | `HUL-B-*` | `spend.py` dual envelope | `tests/test_hul_budget_reserve.py` | **complete** — PR #47 `536e75c` |
| HUL-C | `HUL-C-*` | scope packs via `jev_policy.evaluate_scope` (`harness/jev_packs.py`, site=`hul_scope`) | `tests/test_hul_jev_scope_gate.py` | **complete** — PR #48 `469f34f` |
| HUL-D | `HUL-D-*` | `harness/mission_driver.py` + FINDINGS + resume | `tests/test_hul_driver_findings_resume.py` | **complete** — PR #48 `469f34f` |
| Hourglass | `HG-*` | waist/executor/spend/plan consensus/pyramid state | `tests/test_hg_*.py` | **complete** — PR #44 + follow-up PR #68 `4889776` (`DF-HG-1/2/3` closed) |
| Model economics | `EV-*` | `harness/endpoint_pricing.py`, `harness/benchmark_ingest.py`, `harness/discount_probe.py`, `harness/discount_gate.py`, `harness/config.py`, later `routing_table.py`, `jev_policy.py`, `panel.py` | `tests/test_economics_*.py` today; the per-row gates listed above land with their rows | **in progress** — `EV-0` code + hermetic gates landed, live probe receipt pending; `EV-1..5` written in canon, not started |
| SITE-1/2 | `SITE-*` | `site_export.py`, `route_pack.py`, `jev_policy.evaluate_model_route` | `tests/test_site_export.py`, `tests/test_route_pack.py` | **complete** — PR #60 MERGED `a9ae53f`; deny-by-default exporter + 0-hallucination router |
| SITE-3..9 | `SITE-*` | `site_aggregate.py`, `site/` (pages+worker), `harness/server.py` site endpoints, `harness/ui/panes.js` | `tests/test_site_aggregate.py`, `tests/test_site_fold_parity.py`, `tests/test_site_parity_directives.py` | **complete** — PR #60 MERGED `a9ae53f`; 8 gated-run metrics, fold parity (py↔js), CI workflow, UI panes; Jev-directed escalation evidence (PR #58/#59) proven to reach the sanitized bundle + GUI |
| 6 Repo summary | `JEV-P6-*` | `repo_items.py`, `repo_summary.py`, `jev_packs.py`, `jev_policy.evaluate_repo_summary` | `tests/test_repo_items.py`, `tests/test_jev_repo_{pack,judgment,envelope}.py` | **complete** — PR #65 MERGED `1936ed4` |
| Jev bar | `JEV-BAR-*` | `jev_completion.py`, `jev_packs.py` (completion pack), `jev_policy.evaluate_phase_completion`, `packs/phase_completion.pack.json` | `tests/test_jev_bar_sentiment.py`, `tests/test_jev_completion.py` | **complete** — PR #67 MERGED `a9b58ab` |
| Claude lane | `CLAUDE-LANE` | `CLAUDE.md`, `AGENTS.md`, `.claude/`, `harness/mcp.py` (negotiation) | `tests/test_mcp.py` + live skill/MCP receipts | **complete** — PR #66 MERGED `6f00d38` |
| OC findings handoff | OC-HANDOFF | `examples/oc_handoff/worker.py`; root pinned to its executing checkout; output HANDOFF/OC_FINDINGS.md | `tests/test_oc_handoff_worker.py` (signed approval, expiry/replay, fixed output, path containment, bounded Jev request/spend, recovery/outbox); exact-file diff audit | open / gated — PR #90 merged `6aea14b`; focused tests, independent review, and CI run 296 passed; PR #73 closed as superseded; 2026-09-24 local-only preflight correctly blocked at score 84.99; real signed-candidate Jev intake, receipt, and output audit remain pending because no candidate is present |
| Platform unify | `PLAT-*` | `harness/osal.py` (new), `harness/gate_runner.py` (new), `cli.py`, `cli_parser.py`, `filesafety.py`, `ledger.py`, `server.py`, `worktree.py`, `jev_completion.py`, `.gitattributes`, `.github/workflows/ci.yml`, `docs/` | `tests/test_plat_cmd_runnable.py`, `tests/test_cli_gates.py`, `tests/test_osal.py`, `tests/test_osal_boundary.py`, `tests/test_plat_parity_{ledger,jev_report,site_export}.py`, `tests/test_plat_ci_matrix.py`, `tests/test_plat_docs.py` | **complete** — **PR #104 MERGED `6e95a23`**; locally gated on 2026-09-27 (ruff clean, hermetic suite green, parity hashes pinned, boundary enforced) and green in CI on all three OSes (run 36318178076). Evidence: 2026-09-26 scout found cmd-path divergence in live Windows probe (verified: `shell=True` breaks forward-slash paths); allowed-roots case mismatch on macOS; SO_REUSEADDR on Windows server; UTF-8 mode off in 3.9. Delivered: gates are argv data resolved from `sys.executable` with one shell-free `run_gate`; all OS contact lives in `harness/osal.py` (enforced); CI runs the suite + installed-wheel smoke on ubuntu/windows/macos; ledger/report/bundle bytes pinned; platform support documented. |

### PLAT platform unification — cross-OS canonical product (`PLAT-*`)

**Goal:** Prove the product behaves identically across Windows, macOS, and Linux by evidence, not assumption. Define "canonical" as same inputs → same verdict/output bytes. This is foundational for supporting multiple development platforms.

| ID | Work | Primary modules | Gate tests | Status |
|---|---|---|---|---|
| `PLAT-cmd-data` | Extract commands from docs/code strings into data structures (argument list, Python exe resolved to `sys.executable`); single `run_gate` function replaces `shell=True` branch. No command can work in one path and break in another. | `harness/gate_runner.py` (new: `GATES`, `run_gate`, `split_command`), `harness/filesafety.py`, `harness/cli.py`, `harness/cli_parser.py` | `tests/test_plat_cmd_runnable.py`, `tests/test_cli_gates.py` | **complete** — PR #104 MERGED `6e95a23`; `harness gates` is the documented entry point; verify + stage gates share one runner |
| `PLAT-osal-module` | New `harness/osal.py` owns all OS contact: run commands, text I/O with fixed encoding/line-ending (UTF-8, newline=''), atomic file writes, path comparison and containment (realpath + case normalization on Windows), key-file permissions (documented as unimplemented on Windows), UI window launch. Lint rule blocks `subprocess`, `os.name`, `sys.platform`, `webbrowser` elsewhere. | `harness/osal.py` (new), `harness/apply.py`, `harness/mcp.py`, `harness/config.py`, `harness/ui.py`, `harness/server.py`, `harness/worktree.py`, `harness/jev_completion.py`, `harness/ledger.py` | `tests/test_osal.py`, `tests/test_osal_boundary.py` | **complete** — PR #104 MERGED `6e95a23`; boundary enforced by AST check, not by convention |
| `PLAT-ci-matrix` | Add `os: [ubuntu-latest, windows-latest, macos-latest]` to test suite + installed-package test in `ci.yml`. Audit/ruff/Jev bar on Ubuntu only. On every PR: Windows runs; macOS runs if public, or on-schedule if private. | `.github/workflows/ci.yml` (matrix test job + `package-installed` job) | `tests/test_plat_ci_matrix.py`, `tests/test_cli_gates.py` | **complete** — PR #104 MERGED `6e95a23`; the first all-three-OS CI run is green on every job (run 36318178076) |
| `PLAT-parity-tests` | Write expected hashes for output that must be byte-identical across OSes (ledger chain, Jev report, site export, path display). `.gitattributes` pins line endings (eol=lf for fixtures). Parity test rejects any OS that produces different bytes. | `tests/test_plat_parity_ledger.py`, `tests/test_plat_parity_jev_report.py`, `tests/test_plat_parity_site_export.py`, `.gitattributes`, `harness/ledger.py` | Pinned SHA-256 per canonical artifact | **complete** — PR #104 MERGED `6e95a23`; hashes pinned, LF enforced on every evidence write |
| `PLAT-docs` | Update README with platform-support table (surface × OS × evidence level). `security.md`: keyfile protection, allowed-roots case handling. `mcp.md`: registration on both `.venv/Scripts/python.exe` and `.venv/bin/python`. CONTRIBUTING.md: line-ending rules. Confirm `hourglass-vision.md` and untracked docs explain rationale. | `README.md`, `docs/security.md`, `docs/mcp.md`, `CONTRIBUTING.md`, `docs/architecture.md`, `CLAUDE.md` | `tests/test_plat_docs.py` (links resolve; every documented divergence names a live `osal` symbol; no doc hardcodes a gate interpreter) | **complete** — PR #104 MERGED `6e95a23` |

**Acceptance:** CI matrix green on all three OSes; parity tests pass with same expected hashes; documented platform divergences match code (e.g. key-file protection omitted on Windows is stated in docs); platform-aware files list matches lint rule enforcement. **Jev bar:** all gates in PLAT-cmd-data run successfully on every OS.

### Driver integration — verified extraction -> Jev decision -> action (`DRV-*`)

**Goal:** let Harness drive a local machine through a decision it can defend, without Harness owning any of the driving. The ordering is the design and is inherited, not chosen here: **extraction is cross-verified *before* the Jev decision, never after.** A calibrated confidence is calibrated only for the question asked, so a hallucinated extraction produces a confidently wrong answer the model itself cannot detect; cross-checking the *output* instead of the *input* is the plausible mistake and it is the one this architecture exists to prevent. Everything after the decision is a closed vocabulary and a deterministic executor — no model past the execution tier.

Like `MEDIA-1`, this is a **port, not a merge**: driver-core lives in its own repository and Harness carries an envelope. The two projects stay separable in both directions, asserted rather than promised.

| ID | Work | Primary modules | Gate tests | Status |
|---|---|---|---|---|
| `DRV-1` | Thin stdlib adapter to the driver (native `driver_core/` or any wire-compatible service): endpoint resolution, bearer auth, the closed `STOP_REASONS` set, `harness driver ...` CLI face, docs. **A refusal is HTTP 200 `ok:false` and is returned, not raised**; only transport failures and contract violations raise `PerceptionUnavailable`. | `harness/perception_client.py`, `harness/cli.py`, `harness/cli_parser.py`, `docs/driver.md` | `tests/test_perception_client.py`, `tests/test_cli.py` (intercept + parser surface) | see the STATUS row above |
| `DRV-2` | Real backends behind driver-core's declared actions (synthetic input, vision extractor), so a step can act rather than only observe. Native in-repo module `driver_core/`; interoperable with the standalone driver-core project. | `driver_core/osal.py`, `driver_core/executor_registry.py` | `tests/test_driver_core/`, `tests/test_driver_conformance.py` | see the STATUS row above (input is declared and refused) |

**Acceptance for `DRV-1`:** hermetic suite green with no network, screen, model, or driver process in the path; `harness/osal.py` boundary respected; no provider brand strings in the new module; and a **live protocol conformance check against a real driver-core service** (not a mock) showing route names, auth, refusal-as-200, and 4xx/5xx -> `PerceptionUnavailable`.

**Status 2026-09-27 (PR #104 MERGED `6e95a23`):** the five rows above are **complete** — the merge is on `origin/main`, the PR tip's CI run 36318178076 is green on ubuntu/windows/macos (hermetic suite, installed-wheel smoke, and the audit at 10.0/10 on all four dimensions), and the post-merge run 36318617507 keeps `main` green. Parity hashes are pinned to the merged bytes; a mismatch on any OS is a code fix (never a re-pin).

**Known blockers (evidence from 2026-09-26 scout):**
- cmd.exe rejects forward-slash venv paths (`shell=True`); backslash and quoted paths work.
- Python 3.9 has UTF-8 mode off by default; `cp1252` output encoding on Windows can UnicodeEncodeError.
- `allowed_roots` check is case-sensitive; macOS case-insensitive filesystem will refuse lowercase paths.
- `HTTPServer.allow_reuse_address` on Windows allows port hijacking (documented, not a code fix).

### JEV-P3 patterns (implementer notes)

| ID | Work | Rule |
|---|---|---|
| `JEV-P3-route` | Typed apply-route choice via `jev_policy` pack; consumed by router — **no brand names** | packs owned by policy (optional shared pack module when shipped); unkeyed = skip live + `is_fallback` |
| `JEV-P3-triage-files` | Orchestrator file-relevance nouls over candidate list | validate picks against real listing |
| `JEV-P3-context-pack` | Filter state to decision-relevant fields before generative seats | prefer MicroBrief/condensed; expensive seats never get unbounded raw dumps when a pack exists |
| `JEV-P3-claims` | Panel claim-support nouls before judge synthesis | claims lint stays code-owned |
| `JEV-P3-completion` | Artifact/goal nouls before completion judge | missing named artifact → not complete |
| `JEV-P3-calibration` | `ledger_analytics`: jev confidence vs verify outcomes; retune thresholds. **Analysis owner since 2026-10-04:** `harness/jev_calibration.py` (PR #169 MERGED `c031dc5`), tracked on its own `JEV-P3-calibration-analysis` STATUS row | advisory retune, no fake green. That module is pure and dependency-free and has **no production caller** on `main`, so this pattern is exercised by `ledger_analytics` on one side and by the new module's own 23 tests on the other |

### JEV-P5 issue-sort (operator product — after or beside P3)

| ID | Work |
|---|---|
| `JEV-P5-buckets` | Operator pack schema + validation + keyword matcher |
| `JEV-P5-issue-sort` | `JevPolicy.evaluate_issue_sort`; choice criteria ⊆ operator buckets only |
| `JEV-P5-envelope` | Issue/fix combo bound to pack fields; `structural.site=issue_sort` |
| `JEV-P5-orchestration` | Waist/orchestration/HUL attention steering via declared `path_id` only |
| `JEV-P5-cli` | Thin `harness issue-sort` + MCP tool → policy owner only |

### SITE proof bench (implementer notes)

| ID | Work | Rule |
|---|---|---|
| `SITE-1` | Deny-by-default `harness site-export` (consent + chain verify + secret scan → bundle-v1); efficiency bench manifests base-layer-first | exporter is the only ledger→public boundary; refuses unsafe exports |
| `SITE-2` | `harness route` + MCP `route_query`; tier guidance with proof | choice ⊆ declared ladder only; no provider brands in phase code |
| `SITE-3` | `site_aggregate` 8 metrics; gated-runs-only headlines | frontier rarity is a first-class metric (warrant rate + run-depth histogram) |
| `SITE-4/5` | Static site (6 pages) + CF Worker (D1/KV/rate-limit bindings) | fold parity pinned by shared fixture vectors (py↔js) |
| `SITE-6..9` | Consent publish flow + CI deploy; UI panes (legacy preserved); coalesce with Jev escalation events | consumer-tolerant v1/v2 event contract; no parallel STATUS elsewhere |
| `SITE-COALESCED` | Unified with PR #58/#59: the CLI escalation executor's ledgered `escalate` provenance (`directed_by=jev`, confidence, target rung, condensed-context size) flows through the exporter allowlist into public trace cards on both GUI surfaces | ledger is the evidence boundary; condensed context itself never crosses it |

**0-hallucination rule:** operator declares buckets; code owns matching; Jev may only select declared choice keys; unmatched/unkeyed → `is_fallback=true` and `bucket=None` — never invent buckets or actions.

### HUL product phases (Track B)

| ID | Work | Gate test |
|---|---|---|
| `HUL-A` | `missions/<id>/` pack + loader + STATUS generator + receipts + CLI `mission init\|status\|resume\|findings` | `tests/test_hul_mission_record.py` |
| `HUL-B` | Dual budget: `working_remaining = max - spent - terminal_reserve`; attempts never eat reserve | `tests/test_hul_budget_reserve.py` |
| `HUL-C` | Scope packs via `jev_policy` (`site=hul_scope`); unkeyed cannot alone complete | `tests/test_hul_jev_scope_gate.py` |
| `HUL-D` | `mission run` until limits/stall + FINDINGS.md + resume.json | `tests/test_hul_driver_findings_resume.py` |

Reuse `jev_policy` — no second Jev client. Stall default: 5 consecutive attempts with no new artifact/evidence.

### Hourglass remaining (`HG-*`)

| ID | Work |
|---|---|
| `HG-composed-ceiling` | Preflight decompose + waist + Σ node ceilings vs session ceiling **before** spend; envelope carries number |
| `HG-pyramid-resume` | Persist plan/DAG/node results; `plan --resume` skips completed nodes |
| `HG-final-gate` | Default final/stage gate on CLI+MCP+agent; false-ok blocked |
| `HG-hybrid-isolate` | Worktree only for overlap-free concurrent nodes; shared-tree mutex when paths collide |
| `HG-plan-consensus` | Cheap second soundness check before waist when confirm armed |
| `HG-condense-decompose` | Feed MicroBrief/signatures into LLM decompose + waist on all surfaces |
| `HG-decompose-default` | CLI/MCP default decompose_llm follows `resolve_hourglass` (align with agent) |
| `HG-ms-parity` | Remove lane-level ad-hoc model strings; ladders/config only; MS envelope requested vs observed |

### Hourglass step A/B — repo summary (`JEV-P6-*`) — promoted 2026-09-22

Operator mission: push the whole repo through the hourglass stage by stage.
Step A/B (prep + condense): code-owned inventory of every tree element,
Jev-classified against the operator pack at `packs/repo_summary.pack.json`
(axes `stage`/`brief_treatment`/`handling`, score `attention`, nouls
`waist_relevant`/`parallel_safe`), every keyed call governed + settled +
ledgered `site=repo_summary`, aggregated to `docs/repo-summary/`
(envelope JSON + `REPO-MAP.md`). REPO-MAP feeds step B→C (grounded waist
brief → frontier confirm) next.

| ID | Work | Primary modules | Gate tests | Status |
|---|---|---|---|---|
| `JEV-P6-extract` | code-owned inventory: files + AST symbols + imports + headings + gate facts + centrality + tallies; soft-skip enumeration flag (additive, default unchanged) | `harness/repo_items.py`, `harness/repo_scope.py` | `tests/test_repo_items.py` | **complete** — PR #65 MERGED `1936ed4` |
| `JEV-P6-pack` | operator pack seed + validator + typed question pack + declared-keyword fallback | `harness/jev_packs.py`, `packs/repo_summary.pack.json` | `tests/test_jev_repo_pack.py` | **complete** — PR #65 MERGED `1936ed4` |
| `JEV-P6-judgment` | `JevPolicy.evaluate_repo_summary`: preflight → typed call → settle + ONE `jev_eval` per element (`site=repo_summary`); declared ids only, shape-invalid answers never presented as live | `harness/jev_policy.py` | `tests/test_jev_repo_judgment.py` | **complete** — PR #65 MERGED `1936ed4` |
| `JEV-P6-envelope` | envelope aggregated from persisted rows (fallbacks never smoothed, spend == Σ rows) + `REPO-MAP.md` renderer + resume/budget-stop contract | `harness/repo_summary.py` | `tests/test_jev_repo_envelope.py` | **complete** — PR #65 MERGED `1936ed4` |
| `JEV-P6-cli` | `harness repo-summary` face: governor chunking under `HARD_MAX_COST`, explicit `--run-budget` cumulative bound, state/exclude/resume | `harness/cli.py`, `harness/cli_parser.py` | `tests/test_jev_repo_envelope.py` (CLI face + factory) | **complete** — PR #65 MERGED `1936ed4` |
| `JEV-P6-dogfood` | keyed pilot (10 elements, $0.428022) → full run via dedicated TypeSafe key under operator no-ceiling ruling (explicit `--run-budget 100`, per-chunk `$0.10` hard cap retained): **746/746 elements (546 files + 200 centrality symbols), $29.345862 exact (698,711 in / 169,616 out tok), 694 live + 52 fallbacks (7.0%), 601 near-tie flagged**; artifacts committed under `docs/repo-summary/`; `harness ledger verify` green (chain intact, 0 quarantined) | artifacts + receipts | envelope `coverage`/`spend` + ledger chain | **complete** — PR #65 MERGED `1936ed4` |

Rules: one policy owner (no second Jev client); operator declares buckets —
Jev selects declared keys only (0-hallucination); hermetic tests never key;
`HARD_MAX_COST` per governor is never raised — big runs chunk, they do not
loosen the cap.

### Model selection + dogfood policy (all phases)

- Hermetic tests stay hermetic (`test/model` doubles).
- Live tracking/dogfood: **cheap paid** first (`deepseek/deepseek-v4.1-flash` apply, `z-ai/glm-5.3-flash` judge); `HARNESS_USE_FREE=false` for tracking; free-tier is fallback evidence only.
- Hourglass dogfood surface: `harness plan --goal … --decompose-llm --confirm [--execute] --task-max-cost …` + MCP/agent twins; prove cheapest-first + waist model + condensed brief from ledger/envelope.
- Phase PRs must not hardcode provider brands — resolve via router/ladders/`MS-*`/`EV-*`.

### Model economics addendum — ever-evolving cheapest-capable panel (`EV-*`)

**Audit 2026-10-03 (read-only; live GETs, $0.00).** `docs/MODEL_SELECTION_HANDOFF_2026-09-13.md` asked four questions and the answers were *no, no, no, and only on failure*. This addendum tracks the fix. It is a canon section, not a parallel plan; rows below carry the work, the findings live in the dogfood table below as `DF-EV-*`.

**Answers, with the evidence.** (1) **No benchmark data was being pulled at all.** `GET /api/v1/benchmarks` publishes Artificial Analysis `coding_index`/`agentic_index`/`intelligence_index`, Design Arena, and OpenRouter's own tau-bench / GPQA / web-search evals; a repo-wide grep for `capability matrix`, `intelligence index`, `value per dollar`, `cost per capab` returned **zero hits**. (2) **No relative-performance index existed**, and the cost math ran against the wrong price: `CapabilityProfile.from_model` and `SpendGovernor.fetch_pricing` both read `/models` *aggregate* rates, while the per-provider feed publishes spreads of up to ~6x on output for one id. (3) **No matrix of models x cheapest-capable task** — `route_pack` is a declared 4-rung ladder over one operator-chosen list, and `routing_table.TIER_MODELS` is a second hand-maintained list that had drifted onto dropped models. (4) **Rotation is failure-triggered only** (`rate_limited` / unusable output / truncation / judge fallback), and because `order_pool` sorts the paid tier by price ascending the panel head was identical on every run — which is what a static panel *is*.

**Design invariants (asserted by tests, not convention).**

1. **Benchmarks are a prior, never a verdict.** They occupy the same slot declared `/models` metadata occupies today and are corrected by ledger evidence through the existing `NO_DATA_PRIOR`/`_shrink_weight` shrinkage. AA indices are population averages over foreign workloads, and OpenAI itself stopped reporting SWE-bench Verified (Feb 2026) because it no longer measures the frontier. A model with zero observed samples must not win on benchmark priors past a bounded margin.
2. **Code owns arithmetic and bounds; Jev owns judgment.** Cost index, candidate shortlist, and every limit are code-computed. Jev selects among *declared candidate ids only* — the 0-hallucination contract from `evaluate_model_route`/`evaluate_issue_sort`. Unkeyed / transport failure / out-of-list choice returns `is_fallback=True` and the code-owned head. Jev can never invent a model id.
3. **Never mix per-endpoint prices.** Cheapest-in from one provider plus cheapest-out from another is a number no provider will charge. Cost is computed *per endpoint*; only then is a best endpoint chosen.
4. **Auto-movement is bounded, not free.** Jev cannot widen any limit in `EV-4`.
5. **A run resolves its pool once.** A refresh mid-run cannot change seats, which is what keeps a preflight reserve exact.
6. **Offline-first.** Hermetic tests never key and never reach network; auto-movement defaults off so a weekly refresh cannot break CI.

**The finding that gates everything else.** OpenRouter publishes a per-endpoint `discount` beside the listed rates without documenting whether the promotion is already baked in. Both readings are live-defensible and they differ by **exactly 2x** at `discount=0.5`: the discounted-models collection shows `$2/$10` *with* "50% off" (suggesting listed is effective), while `/models` aggregate equals the `openai` endpoint that carries `discount: 0.5` (suggesting it must be applied). Every ranking, reserve, and auto-move decision inverts on the answer, so `EV-0a` settles it **by measurement** and the `harness.economics` gate (`resolve_discount_semantics`) refuses to yield an answer until a conclusive, still-applicable verdict is **committed** as repo evidence (`audits/self/dogfood/EV0A_DISCOUNT_SEMANTICS.json`, inside the corpus D11 hash-pins) — not until one exists on the operator's disk. There is no default: a default here is a silent 2x error in the cost model of the whole system.

| ID | Work | Primary modules | Gate tests | Status |
|---|---|---|---|---|
| `EV-0` | **Evidence ingest.** `GET /api/v1/benchmarks` + `GET /api/v1/models/{slug}/endpoints`, normalized onto one source-independent row shape with the raw payload retained. `endpoint/discount`, `overrides` long-context steps, `input_cache_read`, published uptime and per-provider rates are parsed; a malformed or half-parsed price table raises rather than degrading, because a plausible-but-wrong price is worse than none. **`EV-0a` the discount-truth probe** posts one tiny call through the governed path and compares the provider-reported `usage.cost` against both hypotheses across every published offer, reporting `listed_is_effective` / `discount_is_multiplier` / `ambiguous` / `unresolved` — the last two honestly, because provider price aliasing can make both readings fit. **Endpoint fetching is bounded, not catalog-scoped:** the feed is metered at 30 req/min and 500 req/day and coverage costs 1 GET per model, so a full-catalog fan-out would spend the day's budget on rows nobody routes to; catalog coverage stays on `/models`. `fetch_endpoints_for` is the single owner of that bound and the only shipped caller is the probe (`--probe-model`), which fetches one model through it. **No report surface ships with EV-0:** the shortlist-pricing report (`build_economics_report`/`write_receipt`, `--models`/`--max-fetches`/`--receipt-dir`) and the endpoint-selection policy (`effective_price`, `is_eligible`, `min_uptime`, `cache_eligible`) were cut as unrequested scope (`DF-EV-12`); the CLI exists only because a probe nobody can run is not shipped, and cheapest-endpoint selection plus the cost index are `EV-1`'s row. | `harness/endpoint_pricing.py` (new), `harness/benchmark_ingest.py` (new), `harness/discount_probe.py` (new), `harness/discount_gate.py` (new), `harness/config.py`, `harness/cli.py`, `harness/cli_parser.py` | `tests/test_economics_ingest.py`, `tests/test_economics_discount.py`, `tests/test_economics_cli.py` | **in progress** — code + hermetic gates landed; live probe receipt and the `EV-0a` verdict pending (a row never completes on a report alone) |
| `EV-1` | **The index.** Declared per-lane workload shapes (vote 8k/1k, specialist 8k/2k, apply 16k/4k, judge 24k/3k, frontier 40k/8k) make costs comparable; cheapest-*whole*-endpoint selection lands here (never mixed per field, canon invariant 3) behind the `EV-0` gate `resolve_discount_semantics`; `cost_per_capability = blended_cost / capability_index`, where the index blends benchmark evidence with the existing `capability_score` prior and shrunk ledger observations. Free tier degenerates honestly (cost 0 ⇒ capability-first, uptime and strikes as tiebreaks). Note `overrides` never fire on the declared shapes — they fire in preflight on a genuinely long brief, and the two consumers must not be conflated. **The honest gap:** no public benchmark measures *"parseable JSON within a 4096-token budget with reasoning disabled"*, which is exactly the vote lane; for that band Harness-owned evidence dominates and benchmarks are a tiebreak at best. | `harness/endpoint_pricing.py`, `harness/capability.py`, the `EV-0` gate in `harness/discount_gate.py` | planned `test_economics_index` (lands with `EV-1`) | **not started** |
| `EV-2` | **Dynamic tier table.** `TIER_MODELS`/`TIER_MAX_PRICE`/`TIER_COST_BANDS`/`get_tier_route` **keep their public shape** (`site_export` and `ledger_analytics` depend on them) while their contents become a committed snapshot regenerated from `EV-1`; the current literal stays as the import-time bootstrap fallback so no caller changes and no import-time network. `classify_model_tier` reads the snapshot first and keeps its heuristics only for unlisted ids. Freshness gate mirroring `capabilities --check-shipped`: an aged snapshot, or one naming an id absent from the live catalog, fails CI. Closes `DF-EV-4`. | `harness/routing_table.py`, `packs/tiers.snapshot.json` (new) | `tests/test_routing_table.py` plus a planned tier-snapshot freshness module (lands with `EV-2`) | **not started** |
| `EV-3` | **Jev-driven pool resolver (advisory).** Code shortlists N candidates per band; `packs/economics_route.pack.json` declares the shortlist as its choice criteria so the existing choice-in-criteria check gives 0-hallucination for free; the pack state carries the full history Jev needs to judge — strikes, demotion state, observed success + sample count, uptime, discount and its semantics, days-since-last-rotation, in-rotation streak, capability, blended cost, cost-per-capability. One bounded Jev call per **refresh decision**, never per model call, preflighted at the corrected `$0.042/Mtok` input rate (free output) with one ledgered `jev_eval` and honest `is_fallback` provenance. | `harness/jev_policy.py`, `packs/economics_route.pack.json` (new) | planned economics-route policy module (lands with `EV-3`) | **not started** |
| `EV-4` | **Automatic application + safety envelope + success-path rotation.** This inverts the `DF-LING-2`/`DF-MS-4` operator-ratification precedent, so the bounds are the deliverable: `EV_MIN_VALUE_RATIO` 1.15, `EV_MAX_COST_INCREASE_AUTO` 0.0 (an auto-move may not raise blended cost unless the capability band rises to match), `EV_MAX_MOVES_PER_RUN` 1, `EV_COOLDOWN_DAYS` 14, `EV_MIN_OBSERVED_SAMPLES` 5 (below which a candidate is advisory only), `EV_MAX_HEAD_STREAK` 3, `EV_MIN_UPTIME_1D` 0.90, `EV_AUTO_APPLY` **off** by default. Guards: unrecovered strikes exclude a seat (reusing `UNUSABLE_DEMOTE_STRIKES`); a `jev-freeze`-pinned seat is exempt with an explicit logged refusal; no governor ceiling is ever widened; each move appends `pool_rotation` with before/after ids, index values and the deciding authority; a run snapshots its pool at dispatch. Cross-run diversity rotates the head by a stable offset so observed data accumulates across the pool instead of piling onto the cheapest model, with per-model seat share and run-over-run delta as measured artifacts. `tests/test_model_slates.py` is **re-pinned from exact pool contents to policy** — a weekly refresh would otherwise break CI on arrival. Closes `DF-EV-3`. | `harness/config.py`, `harness/capability.py`, `harness/panel.py`, `tests/test_model_slates.py` | planned pool-auto-rotation module plus the re-pinned `tests/test_model_slates.py` (both land with `EV-4`) | **not started** |
| `EV-5` | **Surfaces + the closed loop.** The `harness economics` probe face grows `report`/`--propose`/`--apply`/`--check-fresh` (`EV-0` ships the probe alone -- see `DF-EV-12`); MCP tool on the `observe` lane with any write gated on `mutation` + `allow_write` like `jev_freeze`; UI/server pane; a Proof Bench matrix page fed by `site-export --economics` so the cheapest-capable-per-task answer is public and falsifiable. Repairs the weekly workflow so the matrix is **committed** rather than uploaded, with a delivery check that fails loudly when absent. Closes `DF-EV-5`, `DF-EV-8`. | `harness/cli.py`, `harness/mcp.py`, `harness/mcp_lanes.py`, `harness/mcp_schemas.py`, `harness/server.py`, `harness/ui/panes.js`, `site/` | `tests/test_economics_cli.py` plus planned parity and workflow modules (land with `EV-5`) | **not started** |

**Sequencing.** One phase PR at a time per `AGENTS.md`. `EV-0`→`EV-2` change no routing behaviour and are useful the day they land; `EV-3` is advisory and cannot move a pool; `EV-4` is the only slice that changes dispatch and lands last with the bounds tests as its bar; `EV-5` closes the loop.

**DoD.** Per row: `harness jev-phase --phase EV-<n>` passes (contract wired in `PHASE_CONTRACTS`), hermetic suite green with no network in any test, ruff + compileall clean, local self-audit BAR MET, a paid-cheap live receipt under `audits/self/dogfood/` (`HARNESS_USE_FREE=false`; never free-tier-only), D12 changed-line coverage refreshed only after real tests execute the new lines, and CI green on the PR tip **and** post-merge `main`. `EV-0` and `EV-4` additionally require their specific receipts (the `EV-0a` discount probe; a real pool move with ledger provenance).

### PR title convention
- FRP: swap-grade, verdicts, evidence, bounds. Fail ≠ approve. Builder ≠ sole grader.
- Coding-agent delegation (Claude lane, `CLAUDE.md`): no static model tiers — delegates inherit the session model unless `--model` names one; Harness lanes use only OpenRouter and Jev. Concurrent live runs use a private `HARNESS_LEDGER`.

### PR title convention
`feat(jev): JEV-P3-…` · `feat(jev): JEV-BAR-…` · `feat(harness): HUL-A-…` · `feat(hourglass): HG-…` · `feat(economics): EV-…` · `fix(<area>): DF-…` · `chore(claude): CLAUDE-LANE …` · `docs(jev): STATUS …`

### Three-lane execution model (operator ruling 2026-09-28)

The completion mission is split across three trackers so the work has owners without becoming a parallel plan. Canon remains this file; the trackers carry execution state only.

| Lane | Tracker | Scope |
|---|---|---|
| 1 — mission integration | PR #116 (`mission/harness-completion`) | #107 PyPI publishing, #108 handoff-scope follow-ups, `DF-JEV-5`/`DF-AUDIT-3` tracking, bounded RSI dogfood iterations, hygiene. |
| 2 — Hourglass chain | PR #119 (`mission/lane2-hourglass`) | `HV-4` → `HV-5` → `HV-6` in order, plus `HV-1` calling-lane wiring and the `HV-2-use` consumer condition. |
| 3 — verification & infra | PR #120 (`mission/lane3-verify-infra`) | PR verification, coverage baseline / `DF-AUDIT-3`, CI + workflow repair, runner capacity. |

**Ruling.** Lanes develop concurrently in disjoint worktrees but **one PR merges at a time**, **first-ready first-merged**: a lane whose PR is green and unblocked merges ahead of a lane that is merely further along. `main` is not branch-protected, so this is policy, not an enforcement mechanism — every lane re-reads canon STATUS after each merge and rebases before its next PR. Lanes never edit each other's owned files; interface changes go back to the owner as a proposal.

**Worked example, 2026-09-28.** Lane 1 held three merge-ready PRs (#121, #117, #122) while Lane 2's #118 was still a draft, so Lane 1 merged all three under the ruling. #122 was based before #121/#117 landed and had to take a `main` merge-in and be re-gated; Lane 3's #120 is agent-owned and stays unmerged until its own items land.

### Next implementation slice

The authoritative priority order and acceptance conditions are in
[Canonical current status — comprehensive sweep 2026-10-05](#canonical-current-status--comprehensive-sweep-2026-10-05)
above. This historical list is retired because it mixed completed rows with
active work and contained stale dispositions. Keep one phase PR active and
start only the first unblocked item from that current table.
## Retired playbooks (history)

- **P2 repair playbook** (Freebuff lane, 2026-09-21): executed and closed — PR #36 `405bbc1` merged with hermetic lane parity, D12 coverage, audit BAR MET. Full text in git history of this file.

---

## Operator notes

- Key resolution: `resolve_jev_key()` → `~/.config/scmorc/jev.env`, `~/.config/harness/jev.env`, then `HARNESS_JEV_KEY` / `TYPESAFE_API_KEY` / `JEV_API_KEY`
- Endpoint (authoritative): `https://api.typesafe.ai/v1/systemone`
- Models: alias `jev-latest` → `jev-1.13.0` (pin version id when calibrating thresholds)
- Verification gates remain authoritative for code correctness; Jev is pre-gate triage and structural refusal, not a substitute for tests






### Follow-up track — Jev log-factor analysis (`JEV-LOG-*`) — promoted 2026-09-21

Operator analysis track promoted from the addendum after all canon product
PRs merged (#39/#47/#48). Code extracts log items → cheap generative seat
proposes an operator pack (buckets + score levels) → operator freezes the
pack → Jev choice+score via `jev_policy` → code aggregates JSON. JSON only;
no narrative from Jev; 0-hallucination operator packs. Details:
`docs/jev-log-analysis-followup.md`. First dogfood: `C:\\temp\\logsSCMessenger.txt`.

| ID | Work | Primary modules | Gate evidence | Status |
|---|---|---|---|---|
| `JEV-LOG-schema` | Operator log-pack schema + validation (P5 pack + `score` block) | `harness/jev_packs.py` (extend, one owner) | `tests/test_jev_log_pack.py` | **complete** — **PR #56 MERGED** `431836d`; `tests/test_jev_log_pack.py` green |
| `JEV-LOG-parse` | Code-owned log item extractor + mechanical tallies | `harness/log_items.py` | hermetic fixture on real SCMessenger log sample | **complete** — **PR #56 MERGED** `431836d`; SCMessenger-sample fixture green |
| `JEV-LOG-factor-pass` | Cheap generative factor/bucket draft → pack proposal adapter; **no brand hardcoding** | small helper + MS resolve | fixture pack draft; operator approve step documented | **complete** — **PR #57 MERGED** `e15a723`; freeze gate + `tests/test_jev_log_envelope.py` green |
| `JEV-LOG-judgment` | `JevPolicy.evaluate_log_item` / batch: choice + score via packs | `harness/jev_policy.py` only | `tests/test_jev_log_judgment.py` — keyed/unkeyed, 0-hallucination, one ledger `jev_eval` per call | **complete** — **PR #56/#57 MERGED** `431836d`/`e15a723`; `tests/test_jev_log_judgment.py` green |
| `JEV-LOG-envelope` | Aggregate JSON artifact + `structural.site=log_factor` | policy + thin CLI/MCP | envelope keys stable; cost honest | **complete** — **PR #57 MERGED** `e15a723`; `tests/test_jev_log_envelope.py` green |
| `JEV-LOG-cli` | Thin `harness log-judgment --pack … --items …` | `harness/cli.py` | calls policy owner only | **complete** — **PR #57 MERGED** `e15a723`; governor parity `dd7fa45`; policy-owner-only face; live dogfood receipt verified in `audits/self/dogfood/JEV_LOG_SELF_DOGFOOD.md` (cost $0.020500, native jev-1.13.0 verified clean) |
| `JEV-LOG-dogfood` | Single-pass run on harness's own output (operator redirection 2026-09-22; old SCMessenger path retired unnecessary) | artifacts + receipts | JSON + cost + fallback rate recorded | **complete** — face capability **PR #57 MERGED** `e15a723`; self-dogfood executed 2026-09-22 on harness itself (receipt: `audits/self/dogfood/JEV_LOG_SELF_DOGFOOD.md`): serve-log leg 0 items/$0 honest, keyed fixture pass 1 live ($0.0205, conf 0.83) + 3 honest keyword fallbacks (TypeSafe score-shape refusal, 0-hallucination contract held) + 1 unmatched; ledger chain verified |

---

### Follow-up addendum (design detail — STATUS rows now live above)

`docs/jev-log-analysis-followup.md` remains the design-detail file for the
now-promoted `JEV-LOG-*` track (STATUS rows above are the tracker). Canon wins;
shared pattern with shipped `JEV-P5` operator packs / `evaluate_issue_sort`.
SCMessenger implements batch callers against **origin/main** only; Harness
owns `jev_policy`/`jev_packs` and the shipped phase PRs #39/#42/#43/#46/#47/#48.


---

## Mission order of operations (operator ruling 2026-09-21) — complete

All three steps are done: canon PRs #39/#47/#48 merged; the `JEV-LOG-*` addendum was promoted and implemented (PRs #56/#57, dogfood #64). The live order is "Next implementation slice" above.
