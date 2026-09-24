# Jev Full-Functionality Roadmap

**Status:** active tracking doc  
**Implementer lane:** Claude Code (`CLAUDE.md`, `/isolated-mission`) — migrated 2026-09-22 from the Freebuff / Buffy lane, which landed the baseline (PR-Jev-Live `b6fa945`, waist pre-plan `a591739`, cost foundation `b3960e7`)  
**Audit date:** 2026-09-22 (Claude dogfood audit: 9 components, 31 confirmed findings — see `DF-*` below)  
**Live probe:** TypeSafe `POST https://api.typesafe.ai/v1/systemone` succeeded with key at `~/.config/harness/jev.env`

This file is the **single source of truth** for Harness mission work (Jev / System One, HUL, hourglass, site, and follow-ups). Every future PR must carry an ID from the tracker below and reference an acceptance test named here. Do not open parallel ad-hoc plans — add or update rows here.

**Mission prompts:** [jev-mission-prompt.md](jev-mission-prompt.md) (`/isolated-mission --bar <ID>`). Trust `origin/main`; a stale local STATUS is not evidence.

**Worktrees:** one per phase PR, `Harness-<slug>` off `origin/main` (convention in `AGENTS.md`); `git worktree list` is authoritative.

Related design context (not the tracker): [system-one-integration.md](system-one-integration.md), [hourglass-frontier-eval.md](hourglass-frontier-eval.md) (D8), skill at `.agents/skills/typesafe-ai/SKILL.md`, live API at <https://docs.typesafe.ai/api.md>.

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
1. **Cost fabricated.** TypeSafe `usage` is `{input_tokens, output_tokens}` only. Price: **$42 / Mtok input, output free**. Client invents `usage.cost`.
2. **`verify_diff_mechanics` questions ill-posed.** State is `{diff, instruction, file_path}` but questions ask about “context support.” Live probe: `supported=0.21`, `syntax_clean=0.92`, verdict fail. Local fallback passes any valid-looking diff — keyed path is stricter and wrong; unkeyed path is a no-op.
3. **Confidence semantics conflated.** TypeSafe: Noul = yes-probability (no confidence); Choice/Score confidence = distribution shape. Code mixes Noul probability, Score level, and distribution confidence.
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
| `JEV-P0-cost` | `cost = input_tokens * 42 / 1e6`; never invent `usage.cost`; expose tokens on `JevEvaluationResult` |
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

## Canonical STATUS (update here only — truth as of 2026-09-22 post PR #65 + Claude dogfood audit)

| Track / phase | Status | PR / evidence |
|---|---|---|
| Preflight sync `main`↔`origin` P0 | **complete** | `origin/main` includes P0 `d042d70` |
| `JEV-P0-*` contract truth | **complete** | **PR #34 MERGED** `d042d70`; live smoke OK |
| `JEV-P1-*` one owner + lanes | **complete** | **PR #35 MERGED** → `origin/main` `9d5ff14` |
| `JEV-P2-*` System One pillars | **complete** | **PR #36 MERGED** → `origin/main` `405bbc1`; hermetic lane-parity; D12 executed; local audit BAR MET; CI green on PR tip **and** post-merge `main`; live Jev smoke OK; **`JEV-P2-jury` deferred** (follow-up, not blocking P2 complete) |
| `JEV-P3-*` utilization | **complete** | **PR #43 MERGED** `7eb18ea`; route/triage/context/claims/completion/calibration |
| `JEV-P4-*` ops / exit | **complete** | **PR #46 MERGED** `a021cfc`; jury deferred; residual: dogfood A/B pass-rate + freeze persistence |
| JEV-COMPLETION | **complete** | **PR #39 MERGED** `5e15f8d`; `harness jev-phase` gate on main |
| `JEV-P5-*` issue-sort buckets | **complete** | **PR #42 MERGED** `c9e1c67`; operator bucket packs, 0 hallucination |
| `HUL-A` mission pack | **complete** | **PR #41 MERGED** `64e63a3`; mission pack + CLI + gate tests; CI green |
| `HUL-B` dual budget | **complete** | **PR #47 MERGED** `536e75c`; dual envelope + tests |
| `HUL-C` Jev scope gate | **complete** | **PR #48 MERGED** `469f34f`; scope packs via `jev_policy.evaluate_scope` (site=`hul_scope`); unkeyed cannot alone complete |
| `HUL-D` until-limits driver | **complete** | **PR #48 MERGED** `469f34f`; `mission run` until limits/stall + FINDINGS.md + interrupt-safe resume; D12 coverage honestly refreshed (`a3afc16`); dogfood follow-ups `DF-HUL-1..3` **closed** (PR #78 MERGED `dd431c3`) |
| `HG-*` hourglass composition | **complete** | **PR #44 MERGED** `f22accb`; follow-up **PR #68 MERGED** `4889776` (`DF-HG-1` composed ceiling, `DF-HG-2` cold-start pyramid resume, `DF-HG-3` preview decompose fallback; local audit BAR MET 10/10/10/10, CI green) |
| `SITE-*` proof bench site | **complete** | **PR #60 MERGED** `a9ae53f`; `harness site-export` → bundle-v1 → aggregate/Worker; tiers page + router; sanitized opt-in only; dogfood follow-up tracked as `DF-SITE-1` |
| `JEV-P6-*` repo summary A/B | **complete** | **PR #65 MERGED** `1936ed4`; 746/746 elements, $29.345862 exact, ledger chain verified (receipts in the JEV-P6 section); stage C = `JEV-P6-waist-brief` |
| `CLAUDE-LANE` Claude Code migration | **complete** | **PR #66 MERGED** `6f00d38`; `CLAUDE.md` + tool-neutral `AGENTS.md`, `.claude/` skills (`/isolated-mission`, `/isolated-request`) + scout/implementer/verifier tiers + settings, docs migrated (`docs/claude-context.md`, `docs/jev-mission-prompt.md`), MCP version negotiation fix |
| `JEV-BAR-*` sentiment-bucket bar | **complete** | **PR #67 MERGED** `a9b58ab`; per-axis sentiment + declared improvement buckets drive bar pass; bar calls routed through `JevPolicy`; `packs/phase_completion.pack.json` |
| `DF-*` dogfood follow-ups | **open** | 2026-09-22 audit (9 components, 32 findings, 31 confirmed by independent re-verification, $0.29 live spend) — rows in "Dogfood audit follow-ups" |
| `MS-*` cheapest-capable routing + failover | **complete** | **PR #69 MERGED** `e47001a`; cheapest capable routing (no Ling defaults, Scout defaults to Gemma 4 31b, planning markers to Distiller Tier 1, router Jev route evaluation), auto paid failover (cheap paid pools appended, sorted cheapest first), hermetic Jev fake transport, `DF-MS-1` + `DF-MS-3` closed (`DF-MS-2` documentation-only, see `DF-MS-2b`); local audit BAR MET 10/10/10/10, CI green. Scope note (Claude audit 2026-09-22): the routing/failover slice is complete; the "context" half moved to `MS-context` |
| `MS-context` condensed state for expensive seats | **open** | split from `MS-*` on 2026-09-22 — PR #69 did not touch context condensation; owner `condenser`/`brief` + MS ladders |
| `JEV-AUDIT-GATE` Jev 4-dimension self-audit gate | **complete** | **PR #79 MERGED** `d3994e9`; `audits/self/audit.py` gate + `JevPolicy.evaluate_audit_dimensions` + `tests/test_audit_dimensions.py`; hermetic keyed-path test (D12) added, partial-run "not evaluated" scoring fixed; local audit BAR MET 10/10/10/10, CI green |
| Dogfood / paid smoke | **ongoing** | every phase: hermetic gates + operator live smoke when client/lane changes; paid cheap rungs (`HARNESS_USE_FREE=false`) |
| Exit | Jev P4 checklist all true on `origin/main` | **open** |
| Exit | HUL A–D shipped **or** open-problem packs on HUL contract | **open** |


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
| SITE-1/2 | `SITE-*` | `site_export.py`, `route_pack.py`, `jev_policy.evaluate_model_route` | `tests/test_site_export.py`, `tests/test_route_pack.py` | **complete** — PR #60 MERGED `a9ae53f`; deny-by-default exporter + 0-hallucination router |
| SITE-3..9 | `SITE-*` | `site_aggregate.py`, `site/` (pages+worker), `harness/server.py` site endpoints, `harness/ui/panes.js` | `tests/test_site_aggregate.py`, `tests/test_site_fold_parity.py`, `tests/test_site_parity_directives.py` | **complete** — PR #60 MERGED `a9ae53f`; 8 gated-run metrics, fold parity (py↔js), CI workflow, UI panes; Jev-directed escalation evidence (PR #58/#59) proven to reach the sanitized bundle + GUI |
| 6 Repo summary | `JEV-P6-*` | `repo_items.py`, `repo_summary.py`, `jev_packs.py`, `jev_policy.evaluate_repo_summary` | `tests/test_repo_items.py`, `tests/test_jev_repo_{pack,judgment,envelope}.py` | **complete** — PR #65 MERGED `1936ed4` |
| Jev bar | `JEV-BAR-*` | `jev_completion.py`, `jev_packs.py` (completion pack), `jev_policy.evaluate_phase_completion`, `packs/phase_completion.pack.json` | `tests/test_jev_bar_sentiment.py`, `tests/test_jev_completion.py` | **complete** — PR #67 MERGED `a9b58ab` |
| Claude lane | `CLAUDE-LANE` | `CLAUDE.md`, `AGENTS.md`, `.claude/`, `harness/mcp.py` (negotiation) | `tests/test_mcp.py` + live skill/MCP receipts | **complete** — PR #66 MERGED `6f00d38` |

### JEV-P3 patterns (implementer notes)

| ID | Work | Rule |
|---|---|---|
| `JEV-P3-route` | Typed apply-route choice via `jev_policy` pack; consumed by router — **no brand names** | packs owned by policy (optional shared pack module when shipped); unkeyed = skip live + `is_fallback` |
| `JEV-P3-triage-files` | Orchestrator file-relevance nouls over candidate list | validate picks against real listing |
| `JEV-P3-context-pack` | Filter state to decision-relevant fields before generative seats | prefer MicroBrief/condensed; expensive seats never get unbounded raw dumps when a pack exists |
| `JEV-P3-claims` | Panel claim-support nouls before judge synthesis | claims lint stays code-owned |
| `JEV-P3-completion` | Artifact/goal nouls before completion judge | missing named artifact → not complete |
| `JEV-P3-calibration` | `ledger_analytics`: jev confidence vs verify outcomes | advisory retune; no fake green |

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
- Phase PRs must not hardcode provider brands — resolve via router/ladders/`MS-*`.
- FRP: swap-grade, verdicts, evidence, bounds. Fail ≠ approve. Builder ≠ sole grader.
- Coding-agent tiers (Claude lane, `CLAUDE.md`): Opus orchestrates/plans/reviews; Haiku scouts; Sonnet implements and verifies. Concurrent live runs use a private `HARNESS_LEDGER`.

### PR title convention
`feat(jev): JEV-P3-…` · `feat(jev): JEV-BAR-…` · `feat(harness): HUL-A-…` · `feat(hourglass): HG-…` · `fix(<area>): DF-…` · `chore(claude): CLAUDE-LANE …` · `docs(jev): STATUS …`

### Next implementation slice (priority order — no guessing)

Run each item with `/isolated-mission --bar <ID>` (see [jev-mission-prompt.md](jev-mission-prompt.md)); PRs are grouped by owning module so each lands once.

1. ~~**`CLAUDE-LANE`**~~ — **complete** (PR #66 merged `6f00d38`).
2. ~~**`JEV-BAR-*`**~~ — **complete** (PR #67 merged `a9b58ab`; audit BAR MET 10/10/10/10).
3. ~~**HG repair PR**~~ — **complete** (PR #68 merged `4889776`; `DF-HG-1/2/3` closed, local audit BAR MET 10/10/10/10, CI green).
4. ~~**Budget-honesty PR (`MS-*`)**~~ — **complete** (PR #69 merged `e47001a`; `DF-MS-1..3` closed, cheapest-capable routing, auto paid failover, hermetic Jev fake, local audit BAR MET 10/10/10/10, CI green).
5. ~~**Lane-correctness PR**~~ — **complete** (PR #70 merged `412f7e1`; `main` green again).
6. ~~**`/bod` rulings**~~ — convened 2026-09-22; no resolution reached 5/5 (see `HANDOFF/BOD_STATE.md`); open operator decisions: `DF-LING-2`, DF-HG-3 default, `DF-BOD-1`.
7. ~~**HG / verify repair**~~ — **complete** (PR #72 merged `42bac79`; DF-CLI-2, DF-BOD-1, DF-HG-3b fail-closed preview, DF-HG-4; CI green).
8. ~~**CI + docs hygiene**~~ — **complete** (PR #74 merged `718a87d`; DF-CI-1..3, DF-DOCS-1..5, DF-AUDIT-1; CI green).
9. ~~**`DF-LING-1`**~~ — **complete** (PR #76 merged `c5c4180`; `DF-LING-2` remains an operator decision).
10. ~~**`JEV-AUDIT-GATE`**~~ — **complete** (PR #79 merged `d3994e9`; hermetic keyed-path test + partial-run scoring fix; local audit BAR MET 10/10/10/10, CI green).
11. ~~**HUL-D follow-ups**~~ — **complete** (PR #78 merged `dd431c3`; `DF-HUL-1..3` closed).
12. **`HV-0` JEV integration foundation + vision-assessment pilot** — first Hourglass vision slice after the two preceding open implementation items land. Implement the typed assessment, apply the operator-verified TypeSafe account rate to the shared pricing/preflight owner, measure the complete sanitized payload, and run one live no-retry call only if it fits both context and authorized spend limits. A pilot that fails either limit remains unassessed; see the contract below.
13. **`HV-1`, `HV-2`, `HV-3`** — after `HV-0`, the three disjoint-owner lanes may work concurrently in separate worktrees, but open and merge only one phase PR at a time under `AGENTS.md`. Each later lane rebases onto the accepted contract and merged predecessors before opening its PR. See [hourglass-freebuff-prompts.md](hourglass-freebuff-prompts.md).
14. **`HV-4` → `HV-5` → `HV-6`** — sequential composition/planning, execution + sovereignty handoffs, then surface parity and measured acceptance.
15. **Remaining existing roadmap work** — ~~`DF-MS-2b`~~ **complete** (PR #82 merged `6d5a2f8`); UI parity: ~~`DF-UI-1`~~, ~~`DF-UI-3`~~ **complete** (PR #83 merged `bf2024f`), `DF-UI-2` remains open; `DF-LEDGER-1`, `DF-AUDIT-2` **complete** (PR #81 merged `361aa6c`); `JEV-P4` dogfood A/B + freeze persistence; `JEV-P6` stage C (`waist-brief`, `noul-audit`, freshness); operator rulings such as `DF-LING-2`; then Exit rows. These remain open, with HV-0 prioritized after the audit gate and HUL-D follow-ups by this canon proposal. Reconcile duplicates/closed items as each PR lands; do not infer they are complete from the vision plan.

### Hourglass vision realization (`HV-*`)

Purpose: realize [hourglass-vision.md](hourglass-vision.md) as a modular, token-budget-shaped workflow: broad context intake, constrained planning waist, then expanded-token execution, with JEV available as a composed set of typed decision integrations. This section is the operational plan for the vision; the linked document is the product definition. Existing HG, MS-context, JEV-P6, consent, and spend work is foundation/evidence, not presumed completion of these contracts.

**Shared completion rule:** every slice has named hermetic tests; actual and estimated token usage are labeled honestly; token limits and dollar ceilings are tested independently; malformed, missing, unkeyed, or unavailable JEV results cannot become live approval; the ledger/envelope records stage, integration/site, model, fallback, budget, and outcome; CLI/MCP/agent surfaces use the shared owner. No slice is complete from prose or an aggregate score alone.

**Priority and parallelization rule:** finish `JEV-AUDIT-GATE` and the HUL-D follow-ups before `HV-0`. HV-0 builds the reusable JEV assessment contract and pilots it when the bounded call is feasible; its hermetic implementation may still be reviewed if the live call is blocked, but the pilot cannot be called complete. Review declared categories and address substantiated gaps. After HV-0's contract is merged, `HV-1`, `HV-2`, and `HV-3` may be developed concurrently in disjoint worktrees. `AGENTS.md` still permits only one active phase PR: open, gate, and merge those PRs serially, rebasing each remaining lane onto current `origin/main` before its PR. Integrators must not edit another lane's owned files; propose interface changes back to the owner. `HV-4` depends on those merged contracts; `HV-5` depends on `HV-4`; `HV-6` closes parity and measured acceptance.

**Contract freeze for parallel kickoff (answers can be refined before implementation):**

- Stage identity is one of context, planning, execution, verification; partial runs declare selected stages and supplied artifacts.
- Each call carries `max_input_tokens` and `max_output_tokens`; optional run/stage aggregate token ceilings compose by reservations. Unknown provider usage remains estimated/unknown and is never reported as measured actual.
- Token ceilings never substitute for the existing composed monetary ceiling. Both are preflighted before a call; concurrent reservations cannot overcommit either.
- A brief is versioned and carries goal, grounded claims/evidence references, included/excluded scope, coverage, freshness/source identity, uncertainty, and estimated token count.
- A JEV integration has a stable capability/site name, typed input/output schema, operator-declared criteria/buckets where categorical, authority/fallback/confidence rules, budget/preflight, ledger evidence, and hermetic contract tests. No arbitrary plugin code or model-created category can bypass owners.
- Consent is tied to the assignment, relevant context/brief identity, selected model/pool, and limits; defer stops that assignment and creates a resumable handoff.
- Completion alignment receives the original request and retained relevant source context directly, without a new generative condensation pass. JEV may select only a declared restart target (context, planning, or execution); code validates the target, preserves completed work/evidence, reapplies limits, and renews consent when the assignment changed.

**HV-0 design-assessment contract:** ten operator-declared Score questions share one structured state and one network request. The categories are modularity, hourglass token shape, grounding, planning, execution boundary, JEV coverage, sovereignty, observability, verification/alignment, and cost/resource bounds. JEV returns typed scores and distributions, not prose rationales. Code associates each category with source references and exactly one declared improvement bucket for each below-top level. Select the highest-probability level, breaking ties toward the lower ordinal; flag review below a pack-declared assessment confidence threshold. “Perfect” requires all ten top levels, all confidences at or above that threshold, and no improvement or review bucket. It conveys no `can_mark_complete`, readiness, or phase status.

Validate exactly the declared ten answer IDs and legends, with no missing or extra IDs; finite scores within declared ordinal bounds; finite 0–1 probabilities whose keys match the declared levels and sum to one; finite 0–1 confidence; and an observed model identity. Reject the entire assessment if any field is partial or malformed. Unkeyed, transport-failed, invalid, or budget-refused results are **unassessed** with every category score `None`; structural fallback is not a vision score. Return only typed result fields and code-owned evidence references, never fabricated model explanations.

Before the live pilot, assemble and sanitize the complete context. Record its section outline, UTF-8 serialized byte count, and estimated input tokens using `harness.tokens.estimate_prompt_tokens` on the same JSON serialization used for dispatch. [TypeSafe's Jev 1.13 model page](https://docs.typesafe.ai/models) lists 64k **tokens** for the complete request and 32k tokens for `state` plus the longest question; require positive estimated margins under both. No silent truncation, category reduction, or split call. The operator verified this account's applicable rate in the TypeSafe billing console as **$0.0042 per million input tokens**; no further price evidence is required for this assignment. The current `harness.jev.jev_cost` constant is $42 per million and must be corrected or made account-configurable in the shared pricing/preflight owner, with named tests, before dispatch. At the verified rate, even 64k input tokens reserve only $0.0002688, below the normal $0.10 `HARD_MAX_COST`; the account's included credit does not alter that per-call policy. Compute the reserve from the corrected shared price and full request estimate. Do not raise the ceiling implicitly.

The logical preflight is a local governor reservation, with no network request. The HV-0 policy method owns one reservation, one no-retry transport attempt, validation, settlement, and one metadata-only event for a dispatched call. A pre-dispatch refusal records the refusal, not a settled `jev_eval`. The settled event identifies site/capability, pack ID/version, observed model, result state, fallback state, measured/estimated/unavailable usage source, token counts where observed, and cost; it contains no payload, key, IP address, or personal context. A failed or malformed response still settles any reported usage and remains unassessed. No caller manually reserves and then invokes a method that reserves again.

| ID | Goal and definition of done | Primary ownership / tests | Dependencies and parallel boundary |
|---|---|---|---|
| `HV-0` JEV integration foundation + vision-assessment pilot | Deliver the narrow versioned assessment pack, one `JevPolicy` method, an all-or-nothing typed/unassessed envelope, shared strict validation and accounting helpers, and one thin supported entry point if needed. Meet the detailed HV-0 contract above. Hermetic tests cover keyed success; unkeyed, retryable HTTP/transport, malformed, missing/extra, and budget-refused paths; exact category and legend validation; one reservation/settlement/event; and absence of phase-completion fields. Run one live assessment only after source, payload-limit, price, key, and budget gates pass; otherwise report the exact blocker and leave all categories unassessed. JEV's score is advisory and never marks the phase complete. | `harness/jev_policy.py`, `harness/jev_packs.py`, design assessment pack and `tests/test_jev_vision_assessment.py`; one CLI/artifact entry point only if a current surface can own it cleanly. | After JEV-AUDIT-GATE and HUL-D follow-ups. Owns the shared JEV assessment contract; no arbitrary executable plugin loader. |
| `HV-1` stage-specific JEV integration coverage | Apply the HV-0 contract to the distinct Hourglass judgment points. Provide selectable typed integrations for context relevance/coverage/conflicts, plan sufficiency/soundness/evidence requests, execution suitability/checkpoints, consent freshness/defer/escalation signals, final completeness/alignment against the original request plus relevant uncondensed source context, and a declared restart-target choice among context, planning, and execution. For every dimension, name code-owned facts versus JEV semantic judgment, pack/version, abstention/fallback, and evidence; avoid redundant calls when no decision is needed. JEV recommends a target; code enforces allowed transitions, provenance, preserved completed work, current limits, and any renewed consent. | `harness/jev_policy.py`, `harness/jev_packs.py`, JEV integration matrix and `tests/test_hourglass_jev_integrations.py`. Do not edit `condenser.py`, spend/config budget owner, waist composition, CLI/MCP surfaces; publish APIs. | One of three parallel lanes after HV-0. Extends the new integration protocol; no second Jev client/policy. |
| `HV-2` evidence-bearing context brief | Extend the existing condenser/brief path to produce a reusable, bounded artifact with source identity/freshness, scope coverage, grounded references, uncertainties/conflicts, and token estimate. Preserve useful evidence under pruning; make truncation/omission visible. Provide independent create/validate/render behavior so callers can supply a brief without running intake. | `harness/condenser.py` and a focused brief module only if needed; `tests/test_hourglass_brief.py`. Do not edit JEV policy/packs, config/spend, waist, or surfaces; publish schema/helpers. | One of three parallel lanes after HV-0. Existing `MicroBrief` is starting point, not assumed schema-complete. |
| `HV-3` token allowance and accounting owner | Implement one policy owner for per-call input/output maxima and composable stage/run reservations, reconcile reported usage, label estimates and unavailable usage, and expose remaining/used allowances. Enforce token budgets independently from `SpendGovernor` dollar reservations, including concurrent calls and failures. | Prefer `harness/spend.py` or a dedicated narrowly owned token-budget module after kickoff; `config.py` settings; `tests/test_hourglass_token_budget.py`. Do not edit Jev pack/policy, condenser/brief, waist, or CLI/MCP surfaces; publish API. | One of three parallel lanes after HV-0. Must compose with—not weaken—existing cost preflight. |
| `HV-4` stage composition and planning waist | Compose optional context/planning/execution/verification stages from shared contracts. Planning consumes successively curated briefs with decreasing explicit token allowances, stops on a sufficient answer, or emits a validated bounded plan / bounded evidence request / honest defer. It cannot raise its own limits. Supplied brief and plan paths bypass omitted stages. Preserve Hourglass defaults only where existing compatibility requires them. | `harness/waist.py`, `harness/config.py` stage resolution, DAG/plan contract and `tests/test_hourglass_stage_composition.py`, `tests/test_hourglass_planning_budget.py`. | After HV-1..3 merged. Owns composition and waist; do not reimplement JEV validation, brief schema, token reservations, or spend. |
| `HV-5` expanded-token execution and sovereign handoffs | Dispatch validated work packages to the least-cost capable workers with execution token allowances wider than the planning waist, bounded by composed token and monetary budgets. Consent applies to exact package/context/limits; declines and deferrals stop dispatch; mid-task defer persists completed evidence and resumable handoff. At completion, the final JEV alignment check sees the original request plus relevant retained source context without generative condensation; it checks requirements and evidence and may request a declared restart target. Code validates the target, preserves completed work and provenance, reapplies limits, and renews consent for changed assignments. Material plan changes return through a bounded planning amendment. Independent verification remains completion authority. | `harness/agent.py`, `harness/apply*.py`, `harness/consent.py`, executor/continuation integration; `tests/test_hourglass_execution_budget.py`, `tests/test_hourglass_consent_handoff.py`. Do not edit shared owners from HV-1..4; call their APIs. | After HV-4. Keep each package isolated or locked by existing safety policy; no silent plan rewrite. |
| `HV-6` surface parity, observability, and acceptance | Expose stage selection and budgets through CLI, MCP, and agent/API surfaces using shared policy; report selected/skipped stages, requested/actual/estimated tokens, spend, JEV judgments/fallbacks, consent, handoffs, and verification. Demonstrate partial-stage use and full flow with hermetic fakes; run a small operator-authorized paid-cheap dogfood comparison for cost, token profile, fallbacks, defer/resume, and verified outcomes. Document limitations and defaults. | Thin adapters in `cli_parser.py`/`cli.py`, `mcp_schemas.py`/`mcp.py`, envelopes/ledger analytics as identified; `tests/test_hourglass_surface_parity.py`, `tests/test_hourglass_end_to_end.py`; update vision only for approved product changes. | After HV-4 and HV-5. One integration owner coordinates surface changes; keep CLI and MCP adapters thin. |

**Acceptance for the full vision:** tests prove each stage can run alone and any supported subset composes without activating omitted stages; test traces prove input/output token allowances narrow into planning and expand at execution; both token and dollar ceilings reject over-budget calls before network and remain sound under concurrency; briefs retain provenance/uncertainty; JEV integrations are selectable, typed, declared, ledgered, and fail closed; every dispatch has valid consent or is stopped; defer/resume does not repeat completed work or misreport completion; final completion requires independent verification. The dogfood report compares these measures against a baseline and calls out provider usage fields that are only estimates.

**Out of scope unless separately ruled:** arbitrary third-party executable plugins; automatic unlimited browsing or repo reads; a frontier call on every green stage; letting JEV override code-owned gates, model availability, consent mechanics, or budgets; guaranteeing model capability from price alone; claiming provider-reported token categories that the provider does not expose.

Each `HV-*` slice must be added to the completion pack only with its actual independent gates and must receive a named `jev-phase` contract before it can be marked complete. If the phase bar cannot score product/design quality separately from implementation completion, track that as a JEV integration gap rather than misusing the completion bar.

**Do not mark any row complete without:** named tests green + audit BAR MET on PR tip + Jev bar pass (`jev-phase`) + honest STATUS + merge CI green + live dogfood where the lane is user-facing.

### Dogfood audit follow-ups (`DF-*`) — 2026-09-22

Claude-lane dogfood: 9 components audited by Sonnet agents on private ledgers, every finding re-run by an independent Haiku verifier (31/32 confirmed; 1 refuted), $0.29 live spend. Owner = the existing track the fix extends (no new owners).

| ID | Sev | Finding (confirmed) | Owner | Fix | Gate |
|---|---|---|---|---|---|
| `DF-HG-1` | high | `plan` composed-ceiling preflight ignores `--task-max-cost`; silently pinned to config `max_cost` | `HG-composed-ceiling` | **fixed** (PR #68 `4889776`): plan governor honours the flag; envelope shows number used | `tests/test_hg_composed_ceiling.py` CLI-face case |
| `DF-HG-2` | high | `plan --resume PATH` cannot bootstrap a fresh pyramid state from a cold start | `HG-pyramid-resume` | **fixed** (PR #68 `4889776`): missing state path bootstraps fresh run; `--persist-state` supported | `tests/test_hg_pyramid_resume.py` cold-start case |
| `DF-HG-3` | med | default decompose seat intermittently returns unparseable JSON → plan-only preview FATAL (execute path already falls back) | `HG-condense-decompose` | **fixed** (PR #68 `4889776`): one strict retry, then loud heuristic fallback in preview too. Audit 2026-09-22: this reverses the prior fail-closed preview guarantee (its pinning test was inverted) — Board 2026-09-22 **rejected** accepting it → `DF-HG-3b`: **fixed** (PR #72 `42bac79`): fail-closed default restored, degrade opt-in | `tests/test_hg_condense_decompose.py` |
| `DF-SITE-1` | med | `/site/` pages unreachable in a browser when `HARNESS_UI_AUTH_TOKEN` is set (header-only guard on static assets) | `SITE-6..9` | **fixed** (PR #70 `412f7e1`): static `/site/` assets no longer need `X-Harness-Auth`; JSON API still guarded | `tests/test_site_server.py` |
| `DF-CLI-1` | med | `harness ledger verify` exits 0 on a broken chain, contradicting the documented exit codes | `JEV-P1-ledger` | **fixed** (PR #70 `412f7e1`): `ledger verify` exits 2 on a broken chain | `tests/test_cli.py` |
| `DF-APPLY-1` | med | `harness continue --instruction X` silently replays the stale continuation instruction | `JEV-P1-apply` (continuation) | **fixed** (PR #70 `412f7e1`): `--instruction` overrides the saved `remaining_scope` without mutating the continuation | `tests/test_apply_instruction_override.py`, `tests/test_cli.py` |
| `DF-MS-1` | med | `apply --task-max-cost` excludes Jev structural cost; keyed applies exceed the stated ceiling | `MS-*` / `JEV-P1-spend` | **fixed** (PR #69 `e47001a`): fold Jev worst-case into apply preflight (as verify does) | `tests/test_jev_ledger_spend.py` |
| `DF-MS-2` | med | `verify --max-cost` preflight needs ~$0.036 headroom for ~$0.0004 actual (3-panel + judge) | `MS-*` | **documented only** (PR #69 `e47001a`): `--max-cost` help states the minimum headroom; the over-reservation itself is unchanged → `DF-MS-2b` | `tests/test_cli.py` |
| `DF-MS-3` | low | mid-task consent renewal silently uses the frontier judge instead of the declared cheap judge | `MS-*` | **fixed** (PR #69 `e47001a`): renewal uses `settings.judge` unless escalation allowed | `tests/test_apply.py` |
| `DF-HUL-1` | med | driver emits a stale "HUL-B dual-budget enforcement not present" note although `record_spend` enforces it | `HUL-D` | **fixed** (PR #78 `dd431c3`): stale note removed/replaced with honest budget-enforcement status | `tests/test_hul_driver_findings_resume.py` |
| `DF-HUL-2` | med | `mission run` CLI has no paid attempt seat (library-only injection) — lane cannot be live-dogfooded via CLI | `HUL-D` | **fixed** (PR #78 `dd431c3`): CLI attempt seat clarified/resolved via MS ladder | driver CLI test |
| `DF-HUL-3` | med | `mission resume` is a read-only status call, not a resume | `HUL-D` | **fixed** (PR #78 `dd431c3`): `mission resume --run` invokes the driver when resumable | `tests/test_hul_driver_findings_resume.py` |
| `DF-CI-1` | med | `rankings.yml`: `continue-on-error: true` step + tautological job-level `if` (`or true`) | CI | **fixed** (PR #74 `718a87d`): explicit soft-fail condition | workflow review |
| `DF-CI-2` | med | `site.yml` deploy exits 0 silently without `CF_API_TOKEN` (green is not deployed) | `SITE-6..9` CI | **fixed** (PR #74 `718a87d`): `::warning::` + job summary when skipping | workflow review |
| `DF-CI-3` | med | package job never exercises `harness serve` / `harness-mcp` from the wheel | CI | **fixed** (PR #74 `718a87d`): in-process smoke exercises the installed wheel's MCP handshake (network-free, key-verify monkeypatched) | CI package job |
| `DF-DOCS-1` | med | README MCP tools line omits `log_judgment`; CLI examples omit issue-sort / repo-summary / brief / mission | docs | **fixed** (PR #74 `718a87d`): added from real `--help` | audit D1/D2 |
| `DF-DOCS-2` | low | `harness-mcp` ignores argv (no `--help`/`--version`) | MCP | **fixed** (PR #74 `718a87d`): minimal argparse | `tests/test_mcp.py` |
| `DF-DOCS-3` | low | log-judgment fixture-format sensitivity undocumented in `--help` | `JEV-LOG-parse` | **fixed** (PR #74 `718a87d`): help text / stderr note on zero matched lines | log CLI test |
| `DF-DOCS-4` | low | audit D5 fails on stale installed metadata after a version bump; undocumented | docs | **fixed** (PR #74 `718a87d`): CONTRIBUTING note (CLAUDE.md already has it) | audit D10 |
| `DF-JEV-1` | med | `jev-phase` live path bypassed `JevPolicy`; P3/P4/MS contracts trivially satisfiable; live score scale bug (JEV-P2 live 2.34/100 vs local 100) | `JEV-BAR-*` | the JEV-BAR rework | `tests/test_jev_bar_sentiment.py` |
| `DF-MCP-1` | high | MCP server rejected Claude Code 2.1's `initialize` (`2025-11-25`) instead of negotiating, so no connection | `CLAUDE-LANE` | **fixed in this PR** (spec negotiation) | `tests/test_mcp.py` (+2 tests) |
| `DF-CANON-1` | med | canon drift: stale AGENTS.md mission/worktrees, JEV-P6 rows "this PR", obsolete P2 playbook + Freebuff prompts | `CLAUDE-LANE` | **fixed in this PR** | Jev bar `JEV-P6` |

| `DF-MS-2b` | med | verify preflight still reserves every configured retry (~90x the actual spend) — PR #69 took the documentation branch only | `MS-context` / `panel` | **fixed** (PR #82 `6d5a2f8`): `panel._judge_fallback_reserve` now sizes the judge-fallback reserve to `panel_pool[max_panelists:]` -- the dispatch tail that can actually go undispatched -- instead of the whole pool, since every panelist within `max_panelists` is guaranteed to be attempted as a vote and can never also be an untried judge-fallback candidate; `--max-cost` help text updated off the stale `$0.036` figure | `tests/test_panel.py`, `tests/test_reserve_call_parity.py` |
| `DF-LING-1` | high | `[judge] inclusionai/ling-*: unparseable` root cause (picked-up Antigravity research, reproduced): `chat._extract_json` gives up after the first `{` fails to parse (think/prose-wrapped JSON), and `_REASONING_HINTS` lacks `ling` so Ling never gets reasoning handling | `chat` (one owner) | **fixed** (PR #76 `c5c4180`): loop over successive `{` candidates; added the `ling` reasoning hint | `tests/test_chat.py`, `tests/test_extra.py` |
| `DF-LING-2` | med | PR #69 removed Ling from `FREE_APPLY_POOL` on an unevidenced capability claim, without the operator sign-off its own plan required | `MS-*` | re-evaluate after `DF-LING-1` with live evidence; Board 2026-09-22 did not reach unanimity (3/4) — **operator decision** | pool tests + dogfood receipt |
| `DF-HG-4` | low | `waist.compose_plan` docstring promises `decomposition='heuristic:llm_failed'` but code emits `'heuristic'`; `plan --task-max-cost` is called an alias of `--max-cost` but has its own precedence | `HG-condense-decompose` | **fixed** (PR #72 `42bac79`): docstring + help text aligned | `tests/test_hg_condense_decompose.py` |
| `DF-DOCS-5` | low | README free-tier text does not disclose that a configured paid key now gives automatic (ledgered, ceiling-bounded) paid fallback across apply/panel/specialist pools | docs / `MS-*` | **fixed** (PR #74 `718a87d`): disclosed in README + `--help` | audit D1/D2 |
| `DF-AUDIT-1` | low | `audits/self/audit.py` R14 self-check makes one small live paid call when a key is present (observed $0.0009) — local audit runs are not hermetic by default | audits | **fixed** (PR #74 `718a87d`): live probe made opt-in (flag/env) | audit R14 |
| `DF-UI-1` | high | server supports `verify` / `continue` / `bench` run kinds but the GUI only wires `chat` | UI (`harness/ui/panes.js`, `server.py`) | **fixed** (PR #83 `bf2024f`): `verify`/`continue` form panes added (`harness/ui/panes.js`, `panes.css`) | `tests/test_mcp.py` |
| `DF-UI-2` | med | 13 of 24 CLI subcommands have no HTTP API or MCP tool (priority: `plan` HTTP/GUI, `mission`, `cost`, `dogfood`, `jev-phase`) | UI / MCP | add thin faces over the existing owners | server + MCP tests |
| `DF-UI-3` | low | MCP exposes 12 tools vs 24 CLI subcommands (`continue`, `mission` first) | MCP | **fixed** (PR #83 `bf2024f`): `mission_status` + `continue_work` MCP tools added (`harness/mcp.py`, `mcp_lanes.py`, `mcp_schemas.py`) | `tests/test_mcp.py` |
| `DF-CLI-2` | med | `harness verify --panel` is declared in the parser but never forwarded (silently ignored) | `panel` / CLI | **fixed** (PR #72 `42bac79`): forwarded to `service.run_verify` | `tests/test_cli.py` |
| `DF-BOD-1` | med | a 5-model panel with `max_panelists=5` seats only 4 (`google/gemini-3.8-flash` silently dropped, `required_panelists=4`) — the Board's 5/5 rule is unreachable | `panel` | **fixed** (PR #72 `42bac79`): seats N of N eligible or reports the drop in the envelope | panel tests |
| `DF-GOV-1` | high | three STATUS-only commits were pushed directly to `main` (427bcde, 858f90e, 0a7a6fb); two left `main` CI red (0a7a6fb flipped MS without updating its regression canary) | process | every change to `main`, STATUS included, lands via a PR with green CI (AGENTS.md rule 7); **fixed** by PR #70 `412f7e1` (canary updated; `main` CI green again) | CI on `main` |
| `DF-LEDGER-1` | high | `AutonomyLedger._rotated_paths` treated ANY `ledger.jsonl.*` file as a chain segment (matched only the shared prefix), so a human backup (`ledger.jsonl.bak-20260918-dogfood`) was loaded as evidence, its lines quarantined wholesale, and the chain reported broken | `harness/ledger.py` | **fixed** (PR #81 `361aa6c`): segment discovery matches only the exact rotator suffix format (`%Y%m%dT%H%M%S.%f`); non-matching files are skipped with a one-line stderr note | `tests/test_ledger.py::LedgerSegmentDiscoveryTests` |
| `DF-AUDIT-2` | med | `harness/worktree.py` `WorktreeIsolation.create` named branches/paths deterministically from the node id alone (`harness/task_N`); concurrent runs against the same repo (or a crashed run's leftover) collide on that fixed name and block later runs (observed blocking R13) | `harness/worktree.py` | **fixed** (PR #81 `361aa6c`): branch/path name carries a per-call unique suffix (pid + short uuid); cleanup already runs in `executor.py`'s `finally` | `tests/test_worktree.py::WorktreeIsolationTests::test_same_node_id_gets_unique_branch_and_path`, `test_leftover_branch_never_blocks_a_later_create` |

Refuted by the verifier (not a row): concurrent `mission run` on one pack (process had already exited). Historical note: baseline commits `b3960e7`/`a591739`/`b6fa945` are not ancestors of `origin/main`; their content landed through the P0 branch (`feat/jev-p0` → PR #34 `d042d70`).

### Antigravity session audit (2026-09-22)

Antigravity session a4439ee2 ("Fix CI And Continue Work") ran while the Claude session was paused and stalled on `429 RESOURCE_EXHAUSTED`. Claude audit: 9 Sonnet auditors + 9 Haiku skeptics (54/56 findings confirmed). Verdict: PRs #66–#69 were CI-gated and sound (#67 matches the JEV-BAR spec; #68/#69 coverage refreshes legitimate), with caveats `DF-HG-3`, `DF-MS-2b`, `DF-LING-2`, `MS-context`; three direct STATUS pushes to `main` broke CI (`DF-GOV-1`). Its uncommitted lane-correctness work was valid and lands here; its out-of-scope audit-gate work is preserved as `JEV-AUDIT-GATE`; its two stalled research tasks were picked up (`DF-LING-1`, `DF-UI-1..3`). Incidents: one Claude research probe hit a live POST route (`$0.000162`) and one audit run triggered `DF-AUDIT-1` (`$0.0009`).

### Claude lane receipts (2026-09-22)

| Check | Result |
|---|---|
| Baseline `main` `1936ed4` | 1781 tests OK (39 skipped), ruff clean, audit BAR MET 10/10/10/10 (after refreshing a stale local editable install — D5 read 0.4.0 metadata) |
| Harness MCP in Claude Code | before fix: `Failed to connect — unsupported MCP protocol version '2025-11-25'`; after fix: `Connected`; headless Haiku session listed 12 tools, `ledger_status` / `spend_status` / `trust_status` ok, `apply_edit` refused (writes disabled) — $0.12 |
| `/isolated-request` | slash command in a fresh headless session → helper → nested Haiku session returned the correct answer ($0.05 + $0.04) |
| `/isolated-mission` | verification mission `--bar JEV-P2` under `dontAsk` with skill grants only: init → Haiku scout → gates (33 tests OK) → bar pass → independent Sonnet verifier pass → terminal `complete`; full HUL pack written — $0.73 |
| Skill hardening found by the tests | helper must be ONE literal command (no shell variables/compounds); scratch lives under `tmp/claude/` (`.claude/` is protected); phase agents in the foreground; untrusted workspaces ignore project allow rules |

---

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
| `JEV-LOG-cli` | Thin `harness log-judgment --pack … --items …` | `harness/cli.py` | calls policy owner only | **complete** — **PR #57 MERGED** `e15a723`; governor parity `dd7fa45`; policy-owner-only face |
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
