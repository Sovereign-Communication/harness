# Jev Full-Functionality Roadmap

**Status:** active tracking doc  
**Picked up from:** Freebuff / Buffy lane (PR-Jev-Live `b6fa945`, waist pre-plan `a591739`, cost foundation `b3960e7`)  
**Audit date:** 2026-09-21 (WIP freebuff audit)  
**Live probe:** TypeSafe `POST https://api.typesafe.ai/v1/systemone` succeeded with key at `~/.config/harness/jev.env`

This file is the **single source of truth** for finishing Jev / System One utilization in Harness. Every future jev-related PR must carry a `JEV-Pn-…` ID from the tracker below and reference an acceptance test named here. Do not open parallel ad-hoc jev plans.

**Mission paste:** [jev-mission-prompt.md](jev-mission-prompt.md). Prefer that file for the **next Freebuff pass** (P2 repair). Do **not** run from a dirty local `main` STATUS that still says P1 incomplete — that is stale.

**Worktrees (do not invent new P1 work):**

| Path | Branch | Use |
|---|---|---|
| `Harness-jev-p1` | `feat/jev-p1-policy-and-lanes` | **Merged PR #35 — leave alone** (may be locked) |
| `Harness-jev-p2` | `feat/jev-p2-system-one-pillars` | **Current WIP** — PR #36 open; repair here only |

Related design context (not the tracker): [system-one-integration.md](system-one-integration.md), [hourglass-frontier-eval.md](hourglass-frontier-eval.md) (D8), skill at `.agents/skills/typesafe-ai/SKILL.md`, live API at <https://docs.typesafe.ai/api.md>.

---

## North star

> **Jev is Harness’s tier-0 System One decision layer.** Every material plan, consent, write, and verify-adjacent judgment can be gated by typed Jev questions, with calibrated confidence used as a *second axis* (not free-text voting). Local AST/JSON/diff fallback remains the unkeyed degraded path. Costs are token-priced, preflighted, billed, and ledgered. CLI, MCP, agent, and plan lanes share **one policy owner**. The verification gate remains the authority for code correctness — Jev triages, escalates, and refuses *before* expensive generative work.

---

## What freebuff landed (baseline)

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

## Canonical STATUS (update here only — truth as of 2026-09-21 post P3 + HUL-A merge)

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
| `HUL-D` until-limits driver | **complete** | **PR #48 MERGED** `469f34f`; `mission run` until limits/stall + FINDINGS.md + interrupt-safe resume; D12 coverage honestly refreshed (`a3afc16`) |
| `HG-*` hourglass composition | **complete** | **PR #44 MERGED** `f22accb` |
| `SITE-*` proof bench site | **complete** | **PR #60 MERGED** `a9ae53f`; `harness site-export` → bundle-v1 → aggregate/Worker; tiers page + router; sanitized opt-in only |
| `MS-*` cheapest-capable + context | **open** | ad-hoc model strings → config/ladders; expensive seats get condensed state |
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
| Hourglass | `HG-*` | waist/executor/spend/plan consensus/pyramid state | `tests/test_hg_*.py` | **complete** — PR #44 |
| SITE-1/2 | `SITE-*` | `site_export.py`, `route_pack.py`, `jev_policy.evaluate_model_route` | `tests/test_site_export.py`, `tests/test_route_pack.py` | **complete** — PR #60 MERGED `a9ae53f`; deny-by-default exporter + 0-hallucination router |
| SITE-3..9 | `SITE-*` | `site_aggregate.py`, `site/` (pages+worker), `harness/server.py` site endpoints, `harness/ui/panes.js` | `tests/test_site_aggregate.py`, `tests/test_site_fold_parity.py`, `tests/test_site_parity_directives.py` | **complete** — PR #60 MERGED `a9ae53f`; 8 gated-run metrics, fold parity (py↔js), CI workflow, UI panes; Jev-directed escalation evidence (PR #58/#59) proven to reach the sanitized bundle + GUI |

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
| `JEV-P6-extract` | code-owned inventory: files + AST symbols + imports + headings + gate facts + centrality + tallies; soft-skip enumeration flag (additive, default unchanged) | `harness/repo_items.py`, `harness/repo_scope.py` | `tests/test_repo_items.py` | **complete — this PR** |
| `JEV-P6-pack` | operator pack seed + validator + typed question pack + declared-keyword fallback | `harness/jev_packs.py`, `packs/repo_summary.pack.json` | `tests/test_jev_repo_pack.py` | **complete — this PR** |
| `JEV-P6-judgment` | `JevPolicy.evaluate_repo_summary`: preflight → typed call → settle + ONE `jev_eval` per element (`site=repo_summary`); declared ids only, shape-invalid answers never presented as live | `harness/jev_policy.py` | `tests/test_jev_repo_judgment.py` | **complete — this PR** |
| `JEV-P6-envelope` | envelope aggregated from persisted rows (fallbacks never smoothed, spend == Σ rows) + `REPO-MAP.md` renderer + resume/budget-stop contract | `harness/repo_summary.py` | `tests/test_jev_repo_envelope.py` | **complete — this PR** |
| `JEV-P6-cli` | `harness repo-summary` face: governor chunking under `HARD_MAX_COST`, explicit `--run-budget` cumulative bound, state/exclude/resume | `harness/cli.py`, `harness/cli_parser.py` | `tests/test_jev_repo_envelope.py` (CLI face + factory) | **complete — this PR** |
| `JEV-P6-dogfood` | keyed pilot (10 elements, $0.428022) → full run via dedicated TypeSafe key under operator no-ceiling ruling (explicit `--run-budget 100`, per-chunk `$0.10` hard cap retained): **746/746 elements (546 files + 200 centrality symbols), $29.345862 exact (698,711 in / 169,616 out tok), 694 live + 52 fallbacks (7.0%), 601 near-tie flagged**; artifacts committed under `docs/repo-summary/`; `harness ledger verify` green (chain intact, 0 quarantined) | artifacts + receipts | envelope `coverage`/`spend` + ledger chain | **complete — this PR** |

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


### PR title convention
`feat(jev): JEV-P3-…` · `feat(jev): JEV-P5-…` · `feat(harness): HUL-A-…` · `feat(hourglass): HG-…` · `docs(jev): STATUS …`

### Next implementation slice (priority order — no guessing)
1. **Docs STATUS promote** (this branch): P2 complete + open rows for P3/P4/P5/HUL/HG/MS.
2. **P3 utilization** on `Harness-jev-p3` — one pattern per commit acceptable; full DoD before PR merge.
3. **HUL-A** on `Harness-hul-a` (Track B; can develop in parallel; merge after/with green CI when operator schedules PRs).
4. **HG composition** on `Harness-hg-remain` (product integrity for full hourglass).
5. **JEV-P5 issue-sort** after P3 pack pattern exists (or immediately after docs if operator prioritizes steering).
6. **P4** dogfood + freeze + exit checklist once P3 (+ preferred P5/HG core) exist.
7. **Paid dogfood battery** continuous; record costs/models in PR evidence.

**Do not mark P3/HUL/HG complete without:** named tests green + audit BAR MET on PR tip + honest STATUS + merge CI green + live dogfood where the lane is user-facing.



---

## P2 repair playbook (Freebuff / implementer — read before coding)

**Only work tree:** `C:\Users\SCM\Documents\GitHub\Harness-jev-p2`  
**Only branch:** `feat/jev-p2-system-one-pillars` (PR #36)  
**Forbidden trees:** `Harness-jev-p1` (locked/merged), freeform new plans, redo P0/P1.

### Goal
Drive PR #36 from “code exists, claims green” to **reproducible green**: local gates + CI audit BAR MET + honest STATUS. Then merge. Nothing else this pass.

### Ordered checklist (do in order)

| # | Action | Done when |
|---|---|---|
| 1 | **Make lane-parity hermetic** — `tests/test_jev_lane_parity.py` | Apply/batch tests pass on a machine with **real** harness settings **and** on clean CI |
| 2 | **Cover D12 changed lines** listed above | `python audits/self/audit.py` prints **BAR MET**; D12 ≥95% |
| 3 | **Honest STATUS on the PR branch** | Tracker/STATUS rows: P2 `in progress` until merge; `JEV-P2-jury` **deferred**; M2/M3 `[x]` only with named tests |
| 4 | **Run full battery locally, paste output** | Command below all green |
| 5 | **Push same branch to PR #36** | All CI checks green including **audit** |
| 6 | **Merge allowed only after 1–5** | Post-merge `main` CI green → STATUS P2 **complete** → then P3 |

### 1) Hermetic lane parity (exact intent)

Current fail mode (operator re-run):

```text
[apply] test/model did not emit HARNESS_READY; treating as confident
AssertionError: no canned chat response left
```

Cause class: `load_settings()` on a real machine enables paths that issue **extra** chat calls; fixtures only queue one `comp(CHANGED)`.

Implementer must pick **one** contract and test it:

- In test `setUp`/`_engine`, force a **disarmed hermetic envelope**: `jev_api_key=None`, consent off, readiness treated as confident **without** another model call, no hourglass/escalation extras that post chat, and FakeTransport posts sized for **every** call apply will make (or a transport that ignores surplus).
- Do **not** weaken production apply readiness to make one test pass; fix the **test doubles / settings isolation**.
- Keep production fail-closed behavior intact.

Minimum assertions stay the same: result contains `structural`, `structural["is_fallback"] is True` for unkeyed policy; batch child carries the same block.

### 2) D12 coverage (exact untested lines)

Add hermetic tests that **execute** (not mock away):

- `harness/apply_state.py:41` (min_confidence on request/state)
- `harness/consent.py:225,226,231` (low-confidence accept → defer path)
- `harness/jev_policy.py:227,231,233,235,239,241,242` (plan/triage refusal + reservation release paths)
- `harness/waist.py:1018,1019,1020` (triage `requires_iteration` wiring)

Rules:

- Prefer real unit tests over expanding mocks that never hit the lines.
- **Do not** game `audits/self/coverage_baseline.json` by committing a baseline that hides untested new lines. Regenerate only via the official refresh script **after** real tests exist, as a reviewable diff.
- Paste `python audits/self/audit.py` output in the PR.

### 3) STATUS honesty (exact wording targets)

On the **PR branch** docs (not operator-local dirty main):

- Tracker P2 status: `**in progress / repair** — PR #36; evidence pending audit + hermetic gates` until merge.
- After merge: `**complete** — PR #36; audit BAR MET; local+CI gates green; JEV-P2-jury deferred to follow-up`.
- `JEV-P2-jury`: **deferred** — “not in PR #36; needs fail-closed contract + dedicated coverage.”
- `docs/system-one-integration.md` M4: P1 **complete** via PR #35 / `9d5ff14` (remove “not merged yet”). M2/M3 only `[x]` when named tests are green on the merge tip.

### 4) Local gates (must paste raw output)

```powershell
$env:PYTHONPATH = "C:\Users\SCM\Documents\GitHub\Harness-jev-p2"
python -m unittest tests.test_jev tests.test_jev_smoke tests.test_jev_policy tests.test_jev_lane_parity tests.test_jev_ledger_spend tests.test_jev_triage tests.test_consent_confidence tests.test_min_confidence_gating tests.test_agent.TestHourglassLane tests.test_waist -v
python audits/self/audit.py
```

CI on PR #36 must show **audit = success** (not only unittest jobs).

### 5) Forbidden this pass

- Re-implement or reopen P1 / PR #35
- Start P3 / HUL product code
- Mark STATUS complete while audit red or lane-parity red locally
- Merge red CI
- Edit `Harness-jev-p1`
- Provider brand hardcoding in phase code
- Fake “done” without commit + green tests + audit paste

### Blocked? 
Write STATUS `blocked` + the **exact** failing command/output. No “will do”.

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

## Mission order of operations (operator ruling 2026-09-21)

**No conflict — sequence only.**

| Step | Work | Rule |
|---|---|---|
| **1** | **Drive canon to completion — COMPLETE 2026-09-21** | **#39** completion gate, **#47** HUL-B dual budget, **#48** HUL-C/D scope+driver all **MERGED** (`5e15f8d` / `536e75c` / `469f34f`; #48 audit green after BOM-tolerant D12 + honest coverage refresh `a3afc16`). STATUS rows flipped with merge evidence. `JEV-P2-dead-code` wired with Jev-directed escalation — PR #58 `b21d8f0` (2026-09-21): `JevPolicy.evaluate_escalation_decision` + `jev_escalation_directive` + `EscalationDriver` consume calibrated nouls on the production apply path; lifecycle seam test in battery (`tests/test_jev_escalation_lifecycle.py`). Exit checklist: Jev P4 residual + FRP dogfood remain open. |
| **2 (now)** | **Promote addendum to canonical — DONE in this docs PR** | `JEV-LOG-*` rows added to STATUS as **open** (see promoted track section above). Addendum file remains design detail. |
| **3** | **Implement addendum in full** | Fresh worktree `feat/jev-log-factor-analysis` off `origin/main` (after this docs PR merges); one phase PR at a time; extend `jev_packs`/`jev_policy` only (no second Jev client); 0-hallucination operator packs + score block; code owns parse/aggregate; JSON only; dogfood `C:\\temp\\logsSCMessenger.txt`. |

Promotion landed 2026-09-21 (rows above); `JEV-LOG-*` are now canon STATUS rows — implement on `feat/jev-log-factor-analysis` off `origin/main`, one phase PR at a time.
