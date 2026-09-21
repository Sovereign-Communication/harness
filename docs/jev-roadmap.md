# Harness Mission Canon — Jev + Until-Limits

**This file is the ONLY canonical operational plan** for the Harness mission tracks (Jev into Harness + mission-until-limits).  
**Canonical ID tracker + STATUS + DoD + schedule live here.** Do not open parallel plans.

| Role | Path |
|---|---|
| **CANON (this file)** | `docs/jev-roadmap.md` |
| Short Freebuff Mission paste | `docs/jev-mission-prompt.md` (must point only here) |
| Architecture (rationale only) | `docs/system-one-integration.md` |
| Hourglass D8 context | `docs/hourglass-frontier-eval.md` |
| TypeSafe skill | `.agents/skills/typesafe-ai/SKILL.md` |
| Live API | <https://docs.typesafe.ai/api.md> |

**Pointer docs (non-canonical):** `docs/jev-mission-plan.md`, `docs/harness-mission-until-limits.md` — stubs only; if content disagrees with this file, **this file wins**.

**Mission runtime:** Freebuff Mission / auto-run (outer loop).  
**Do not stop after Phase 0.** Continue every incomplete row in STATUS until Jev P4 **and** HUL product phases are done, or an open-problem HUL pack reaches an honest terminal with FINDINGS.md.

**Picked up from:** Freebuff / Buffy (`#PR-Jev-Live` `b6fa945`, waist pre-plan `a591739`, cost foundation `b3960e7`).  
**P0 live probe:** 2026-09-20 — `POST https://api.typesafe.ai/v1/systemone` OK; key at `~/.config/harness/jev.env`.

---

## North stars

**Jev (software track)**  
> Tier-0 System One decision layer. Verification gates remain authority for code correctness. Code owns counting/math/paths/gates; Jev owns bounded semantic judgment; generative models own open-ended synthesis. ONE policy owner; costs token-priced, preflighted, billed, ledgered; unkeyed path is explicit local fallback.

**HUL (any request track)**  
> Mission Until Limits: iterate bounded attempts until success (with verifier if any) **or** working budget/API/token limits **or** stall — never “feeling done.” Jev typed judgments score artifacts against the mission `success_definition`. Terminal reserve funds FINDINGS.md only. Full resume pack after every attempt.

---

## Canonical STATUS (update here only)

| Track | Phase / item | Status | PR / evidence |
|---|---|---|---|
| Preflight | 0.0 sync main↔origin P0 (`d042d70`) | **complete** | `main` == `origin/main` at `d042d70` (0/0); P0 client on main |
| Jev | P0 contract truth | **complete** | PR #34 / `d042d70`; live smoke OK |
| Jev | P1 one owner + all lanes | **in progress** | WIP only on `feat/jev-p1-policy-and-lanes` / `Harness-jev-p1` (uncommitted); **no PR**; `jev_policy.py` present; apply/session/agent/gate touched; `tests.test_jev` 16 OK; **`test_apply_node_jev_structural_evaluation_retry` FAIL** (`failed`≠`ok`); missing `test_jev_policy.py` / `test_jev_lane_parity.py` / `test_jev_ledger_spend.py`; waist/CLI/MCP envelope + preflight incomplete |
| Jev | P2 System One pillars | **open** | after P1 DoD + FRP process rules |
| Jev | P3 utilization | **open** | after P2 DoD |
| Jev | P4 ops / exit | **open** | after P3 DoD |
| HUL | A mission pack / persistence | **open** | |
| HUL | B dual-budget (working vs reserve) | **open** | after A |
| HUL | C Jev scope gate | **open** | after A + Jev P1 policy |
| HUL | D run-until-limits driver + findings | **open** | after A–C |
| HUL | E operator template + Freebuff bridge | **docs done** | this file + mission prompt |
| FRP | Process rules (swap-grade, verdicts, evidence, bounds) | **docs done** | no brand mandates in missions |
| MS | Model selection component (per-task classify→ladder) | **design in canon** | `MS-*` + existing `sliding_scale`/`router`; Jev optional input |
| Exit | Jev P4 checklist all true on origin/main | **open** | |
| Exit | HUL A–D shipped **or** open-problem packs using HUL contract via Freebuff | **open** | |

### P1 audit snapshot (2026-09-20, read-only)

| `JEV-P1-*` | Status | Notes |
|---|---|---|
| policy | partial | `harness/jev_policy.py` untracked WIP; cost ceiling helper exists; **no preflight** of worst-case tokens |
| apply | partial | `session.engine_for` → engine `jev_policy`; `GatePolicy.apply_candidate` pre-write check + `jev_failed` / `jev_refusal` |
| waist | missing | no policy wiring in `waist.py` |
| agent | partial | uses `policy_for` but still owns plan/apply structural loops; double-eval risk vs gate |
| ledger | partial | `jev_eval` / `jev_refusal` coded; untested |
| spend | partial | `record_actual` on live cost only; no preflight reserve |
| envelope | partial | structural on some apply/agent paths; CLI/MCP not evidenced |
| gate tests | missing | three required modules absent |
| commit/PR | none | branch tip == `origin/main`; work uncommitted |
| regression | mixed | P0 jev tests green; hourglass jev retry test **red** |

**P1 done when:** WIP committed on a branch off `origin/main` → required tests green (including fixed structural-retry contract) → waist/CLI/MCP parity + preflight + envelope → PR merged → post-merge gates green → STATUS P1 `complete`.

---

## Baseline (what already shipped)

| Commit | Shipped |
|---|---|
| `b3960e7` | Cost foundation + first jev structural client |
| `a591739` | Waist amend-first + Jev pre-planning |
| `b6fa945` | `#PR-Jev-Live`: live client, primitives, skill |
| `d042d70` | PR #34 JEV-P0 contract truth (origin/main) |

**P0 complete:** token cost (`input_tokens * 42 / 1e6`), official noul/choice/score parse, `settings.min_confidence`, question packs, code-owned diff mechanics, `jev_model`, smoke `tests/test_jev_smoke.py`.  
**Still open:** agent-only wiring; no shared policy/ledger/spend/envelope; consent/abstention unwired; HUL product code not shipped.

---

## Track A — Jev into Harness (`JEV-Pn-*`)

### Phase 0.0 — Sync (blocking) — **complete**

- Conflicts (origin wins these): `README.md`, `docs/jev-roadmap.md`, `harness/agent.py`, `harness/config.py`, `harness/jev.py`, `skills-lock.json`, `tests/test_jev.py`
- Rule: never reintroduce pre-P0 `jev.py`; `jev_cost` + `JEV_INPUT_PRICE_PER_MILLION = 42.0` must be present after sync
- Gate: `python -m unittest tests.test_jev tests.test_jev_smoke tests.test_agent tests.test_waist`
- Done when: `origin/main` is ancestor of `main` **or** trees match after resolve
- **Evidence:** `main` @ `d042d70` matches `origin/main` (0/0)

### Phase 0 — Contract truth — `JEV-P0-*` (**complete**)

| ID | Work |
|---|---|
| `JEV-P0-cost` | Token-priced cost; expose input/output tokens |
| `JEV-P0-parse` | Official answer shapes; no default-pass |
| `JEV-P0-threshold` | `settings.min_confidence`; Noul ≠ confidence |
| `JEV-P0-questions` | Per-site packs; reject non-primitives |
| `JEV-P0-diff-pack` | Code-owned mechanical facts first |
| `JEV-P0-config-docs` | Endpoint `/v1/systemone`; `jev_model`; docs |
| `JEV-P0-tests` / `JEV-P0-smoke` | Hermetic + operator live smoke |

### Phase 1 — One owner, all lanes — `JEV-P1-*`

| ID | Work |
|---|---|
| `JEV-P1-policy` | ONE owner (`harness/jev_policy.py`): when/packs/thresholds/fallback/cost ceiling |
| `JEV-P1-apply` | Shared apply path (CLI, MCP, agent, batch) |
| `JEV-P1-waist` | Pre-planning via `waist.compose_plan` / confirmation |
| `JEV-P1-agent` | Agent calls owner — no private jev policy |
| `JEV-P1-ledger` | Hash-chained `jev_eval` |
| `JEV-P1-spend` | Preflight + `record_actual`; unkeyed $0 fallback |
| `JEV-P1-envelope` | `structural: {verdict, confidence, supported, cost, input_tokens, is_fallback, model, site}` |

**DoD:** parity tests; ledger + governor; no orphan call sites; system-one M4 `[x]` when done; regression green; merged + post-merge green.

**P1 extra DoD (shadow audit 2026-09-20):** commit WIP on `feat/jev-p1-policy-and-lanes`; fix `test_apply_node_jev_structural_evaluation_retry` vs pre-write `jev_failed` (one gate owner); add `tests/test_jev_policy.py`, `tests/test_jev_lane_parity.py`, `tests/test_jev_ledger_spend.py`; wire waist; CLI/MCP `structural` envelope; jev preflight vs governor; no double-bill/double-gate; apply `FRP-swap-grade` (builder ≠ sole grader).

### P1 implementer playbook (read before coding)

Target worktree: `Harness-jev-p1` (or a fresh branch off `origin/main`). Do **not** re-implement P0 (`harness/jev.py` contract, `jev_cost`, packs). Extend the existing seams.

#### What already exists in P1 WIP (keep, do not fork)

| Seam | Location | Use it |
|---|---|---|
| `JevPolicy` / `policy_for` | `harness/jev_policy.py` | ONLY lane-facing owner |
| `evaluate_diff` / `evaluate_candidate` / `evaluate_plan` | same | one call + account |
| `_preflight` + `JEV_MAX_INPUT_TOKENS` / `jev_cost_ceiling` | same | refuse over-budget live calls |
| `_account` → `record_actual` + ledger `jev_eval` | same | spend + evidence |
| `structural` envelope keys | same | `verdict, confidence, supported, cost, input_tokens, is_fallback, model, site` |
| `aggregate_structural(results, site)` | same | batch/plan envelopes; `None` if nothing evaluated |
| `policy.attach(envelope, structural)` | same | attach without changing status |
| Engine inject | `session.engine_for(..., jev_policy=policy_for(...))` | shared apply path |
| Gate pre-write | `apply_gate.GatePolicy.apply_candidate` → `evaluate_candidate`; fail → `jev_failed` + `jev_refusal` | real engines |
| Envelope on apply terminals | `apply_policy._attach_structural` | keep structural on results |
| Agent call sites | `agent.py` `policy_for` plan + `evaluate_diff` post-apply | must become **caller-only** |

#### One gate story (fixes the red test — pick this contract)

**Rule:** There is **one** structural gate decision per candidate.

| Engine | Who decides | Agent behavior |
|---|---|---|
| Real engine with `jev_policy` set | **`GatePolicy` pre-write** (`jev_failed` is terminal for that write) | **Do not** re-run Jev on the same candidate; **do not** invent a second bill |
| Engine **without** `jev_policy` (disarmed / pure mock) | **Agent post-write check** may heal-retry **once** | Emit `structural_eval`; on fail, one `STRUCTURAL EVALUATION FAILED` rewrite |

**Why `test_apply_node_jev_structural_evaluation_retry` fails:** mocked `apply_session` engine returns `ok` + broken diff; agent post-checks with **real** `policy_for` → mechanical fail → healing path, but orchestrator/envelope can still surface `failed` if preflight/raise, double-jev, or final status is taken from a non-success node result. Implementer must:

1. When `getattr(engine, "jev_policy", None)` is set → **skip** agent post-Jev (gate already ran or will run inside engine).  
2. When engine has **no** `jev_policy` (MagicMock) → agent post-check **is** the gate; after one successful heal apply (`ok` + valid diff), node result **must** stay `ok` unless a **later** non-structural failure occurs.  
3. Never let `_preflight` raise into a node without mapping to an honest `structural` + fail/refuse **status** (no raw exceptions as “failed mystery”).  
4. If both paths can run in tests, inject a **fake `JevPolicy`** on the mock (`engine.jev_policy = fake_fail_then_pass`) and assert **2** `apply_edit` calls **or** change the test to assert `jev_failed` when using a real fail policy — **one** documented contract, not both.

**Hermetic test intent (keep green):**

```text
mock engine, jev_policy=None → agent post-gate:
  apply#1 ok+broken_diff → structural fail → apply#2 heal
  assert res["status"]=="ok", apply_edit.call_count==2,
  heal instruction contains "STRUCTURAL EVALUATION FAILED"
```

#### File-level P1 checklist

| Order | File(s) | Implementer actions |
|---|---|---|
| 1 | `harness/jev_policy.py` | Keep as ONE owner. Export stable envelope shape + `policy_for`. Ensure `evaluate_plan` used by waist; document `site` values: `apply`, `agent-plan`, `agent-apply`, `waist`. |
| 2 | `harness/jev.py` | Only if `verify_diff_mechanics(..., preflight=...)` seam is missing — wire preflight callback before live POST; do not change P0 packs/cost math. |
| 3 | `harness/spend.py` | Add `preflight_jev(max_input_tokens, label=...)` if absent (worst-case `jev_cost` vs remaining). Keep `record_actual`. |
| 4 | `harness/session.py` | `engine_for` already injects `jev_policy`. Ensure CLI/MCP/batch engines all go through `engine_for` / `apply_session` (no second constructor). |
| 5 | `harness/apply.py` / `apply_gate.py` / `apply_policy.py` / `apply_state.py` | Gate pre-write + `_attach_structural` on every terminal. On `jev_failed`: no write, ledger `jev_refusal`, envelope carries `structural`. |
| 6 | `harness/waist.py` | `compose_plan` / `confirm_plan`: accept optional `jev_policy`; call `evaluate_plan(site="waist")`; put `aggregate_structural` on plan envelope; **do not** private-prompt jev. |
| 7 | `harness/agent.py` | Plan: `policy.evaluate_plan(site="agent-plan")`. Apply: **one gate story** above. Drop raw `JevEvaluator` / duplicate packs. |
| 8 | `harness/cli.py` / `harness/mcp.py` | After plan/apply, surface `structural` (from policy/aggregate) on the JSON/tool envelope. |
| 9 | `harness/executor.py` | If nodes run apply engine that already has `jev_policy`, no extra jev; if not, pass policy through `PlanExecutor` seams only. |
| 10 | Docs | Update **this** STATUS when PR green; system-one M4 `[x]` only when DoD true. |

#### Required new tests (names + minimum asserts)

| Module | Must prove |
|---|---|
| `tests/test_jev_policy.py` | keyed/unkeyed; preflight refuse when ceiling exceeded; `record_actual` on live; ledger `jev_eval` fields; envelope keys; `aggregate_structural` empty→None, mixed model `"mixed"`; invalid questions → fail not crash |
| `tests/test_jev_lane_parity.py` | CLI + MCP + agent envelopes contain `structural` after a jev-touched run (hermetic doubles); same policy object behavior; unkeyed `is_fallback=true` |
| `tests/test_jev_ledger_spend.py` | governor spent increases by `result.cost` on live; unkeyed cost 0; no double `record_actual` when agent skips post-gate |
| Existing | `tests.test_jev` still green; `test_apply_node_jev_structural_evaluation_retry` green under **one gate story**; `test_jev_preplanning_injects_algorithmic_guideline` still green |

#### Do not

- Fork a second `jev_policy` or private packs in agent/waist  
- Hardcode provider brands in phase code/PRs — resolve rungs via § Model selection / `MS-*`  
- Count empty/malformed Jev output as approve  
- Bill jev twice for one candidate  
- Change TypeSafe endpoint/cost math from P0  
- Stop after code without updating **canon STATUS**

### Phase 2 — System One pillars — `JEV-P2-*`

| ID | Work |
|---|---|
| `JEV-P2-consent-confidence` | Parse consent `confidence`; ledger it (M2) |
| `JEV-P2-min-confidence` | Abstain/escalate **before** write/gate spend (M3) |
| `JEV-P2-triage` | Complexity + `requires_iteration` + scope → router/waist |
| `JEV-P2-jury` | Optional lean typed pre-gate (flag) |
| `JEV-P2-dead-code` | Wire `decide_probe_verify_escalate` **or delete** |

**FRP apply in P2:** structured verdicts; abstention = evidence/threshold (not a brand rule). **`JEV-P2-triage`:** Jev complexity may **feed** `sliding_scale` tier when keyed; ladder resolution stays in router — no brand names in this phase’s PR.

**P2 implementer notes:**

| ID | Touch | How |
|---|---|---|
| `JEV-P2-consent-confidence` | `consent.py` + ledger events | Parse optional `confidence` from consent JSON if present; store on `consent_*` events; **do not** invent confidence when missing |
| `JEV-P2-min-confidence` | `consent.py` / `apply_policy.py` + `sliding_scale.should_abstain` | If confidence present and `< settings.min_confidence` → treat as defer/escalate **before** write spend; tests must show no file write |
| `JEV-P2-triage` | `sliding_scale` / waist directive | Optional Jev pack via `jev_policy.evaluate` when keyed; else existing keyword heuristic; never block free tier |
| `JEV-P2-jury` | `panel.py` / `convergence.py` | Flag-gated lean nouls only; default OFF; generative judge remains |
| `JEV-P2-dead-code` | `sliding_scale.decide_probe_verify_escalate` | Wire to `structural_valid` + confidence from jev/apply **or delete** and remove tests — no orphan API |

### Phase 3 — Utilization — `JEV-P3-*`

| ID | Pattern |
|---|---|
| `JEV-P3-route` | Apply-route Choice |
| `JEV-P3-triage-files` | Orchestrator file relevance nouls |
| `JEV-P3-context-pack` | Filter state before generative lanes |
| `JEV-P3-claims` | Panel claim support before judge |
| `JEV-P3-completion` | Artifact nouls before completion judge |
| `JEV-P3-calibration` | Ledger analytics vs verify outcomes |

**Policy:** code owns exact mechanics; Jev = semantic judgment; generative = synthesis. Routing picks **cheapest capable** via `MS-*` / ladders — brands are not named in phase docs.

**P3 implementer notes:** one pattern per commit; each pack lives in `jev_policy` (or a single `jev_packs.py` imported by policy); call sites pass filtered state only; unkeyed = skip live + `is_fallback`; calibration reads ledger `jev_eval` vs verify outcomes — no new planning docs. `JEV-P3-route` is a typed **route choice** consumed by the router, not a brand pin.

### Phase 4 — Jev exit checklist — `JEV-P4-*`

Implementer: dogfood + pin `jev_model` + docs **after** P1–P3 DoD. Every checkbox needs **evidence** (PR/test/dogfood link), not claims.

- [ ] Keyed → live Jev on default plan/write paths (fallback only on transport failure)
- [ ] Unkeyed → explicit `is_fallback` + honest local checks
- [ ] Envelope cost = token math; preflight blocks over-ceiling jev
- [ ] Ledger + analytics show jev events
- [ ] Consent confidence gating live (M2–M3)
- [ ] Docs: README, architecture, system-one, hourglass D8
- [ ] Dogfood with/without jev recorded
- [ ] Model pin + threshold freeze
- [ ] No orphan `jev_for` / `JevEvaluator` outside policy owner
- [ ] Post-merge gates green on `origin/main`

---

## Track B — Mission Until Limits (`HUL-*`)

Harness capability for **any** operator request (including open research). Freebuff Mission can run this **today** via a mission pack + short prompt; product code is `HUL-A`…`HUL-D`.

### North-star behaviors

1. Iterate until **limits** (cost/tokens/API/errors/wall clock) or **honest success** or **stall** — not “feels done.”  
2. **Jev scope gate:** complete only if typed judgments meet `success_definition` (+ verifier if specified). False-done blocked.  
3. **Dual budget:** `working_remaining = max - spent - terminal_reserve`; attempts never eat reserve.  
4. **Terminal reserve → FINDINGS.md** always (even on failure/unproven).  
5. **Resume pack** after every attempt; interrupt-safe; no redo.

### Mission record (on disk)

```
missions/<id>/
  mission.yaml     # request, scope, success_definition, limits, terminal_reserve, verifier
  STATUS.md
  FINDINGS.md      # terminal only
  receipts.jsonl   # append-only attempts
  jev_evals.jsonl
  budget.json      # spent, working_remaining, reserve
  resume.json      # next_action, dead_ends
  artifacts/
  INDEX.md
```

`mission.yaml` minimum: `id`, `request`, `scope.in_scope` / `out_of_scope`, `success_definition`, `limits.max_cost_usd`, `terminal_reserve.cost_usd` (+ optional tokens), `persistence.root`, `verifier.kind`.

### Jev scope pack (HUL-C; needs Jev P0 + P1 policy)

| ID | Type | Role |
|---|---|---|
| `scope_coverage` | score | Evidence vs `scope.in_scope` |
| `success_definition_met` | noul | Artifacts meet success_definition as written |
| `claims_supported` | noul | Claims follow from evidence in state |
| `needs_human` | noul | Stop for human vs more attempts |
| `complexity_class` | choice | research_open / engineering / blocked_external |

Aggregation: verifier fail → not complete; `success_definition_met` low → not complete; low scope score → not complete; high `needs_human` → terminal `human_review` + findings. Unkeyed fallback may **not** alone mark HUL complete. Every pass → ledger `jev_eval` site=`hul_scope`.

### Product phases

| ID | Work | Gate tests |
|---|---|---|
| `HUL-A-mission-record` | Schema + loader | `tests/test_hul_mission_record.py` |
| `HUL-A-pack` | Pack layout + STATUS generator | same |
| `HUL-A-receipts` | Append-only receipts + budget.json | same |
| `HUL-A-cli` | `harness mission init\|status\|resume\|findings` | same |
| `HUL-B-envelope` | Governor dual envelope | `tests/test_hul_budget_reserve.py` |
| `HUL-B-preflight` | Attempts use working_remaining only | same |
| `HUL-B-terminal-auth` | Findings spend only in terminal | same |
| `HUL-C-pack` | Scope question packs in jev_policy | `tests/test_hul_jev_scope_gate.py` |
| `HUL-C-determination` | complete/continue/human/terminal rules | same |
| `HUL-C-ledger` | `jev_eval` + mission jev_evals.jsonl | same |
| `HUL-C-false-done` | Missing scope → complete blocked | same |
| `HUL-D-loop` | Iterate + stall rules | `tests/test_hul_driver_findings_resume.py` |
| `HUL-D-limits` | Cost/token/error → terminal reasons | same |
| `HUL-D-findings` | FINDINGS template + reserve consumption | same |
| `HUL-D-mcp` | Optional MCP mission tools | same |

**Stall default:** 5 consecutive attempts with no new artifact/evidence → terminal `stalled` + findings.  
**DoD HUL code:** fixture mission reaches limit/stall with FINDINGS.md; resume continues; working vs reserve accounting correct; false-done blocked.

**HUL implementer notes:**

| ID | Deliverable | Acceptance hook |
|---|---|---|
| `HUL-A` | `missions/<id>/` loader + STATUS generator + receipts append-only | Round-trip test: init → simulate attempts → STATUS regenerates; interrupt loses no receipts |
| `HUL-B` | Governor dual envelope | Attempts refused when worst-case would eat reserve; terminal findings may spend up to reserve |
| `HUL-C` | Jev scope pack via `jev_policy` (after P1) | Missing scope / low `success_definition_met` → complete **false**; unkeyed cannot alone complete |
| `HUL-D` | `harness mission run` loop + FINDINGS.md template | Fixture ends at limit/stall with findings + resume.json |

Reuse `jev_policy` — do not invent a second Jev client for HUL.

### Open-problem contract (operator; works via Freebuff now)

`success_definition` example:

> Preferred: machine-checked proof. Acceptable: rigorous lemma(s), counterexample to a natural strengthening, or clear obstruction. Not success: restatement, empty sketches, “I tried” without artifacts. Serious pursuit = receipts show multiple distinct approaches with written outcomes before stall.

---

## FRP — plan/review process (this repo’s rules)

Process craft adapted from public claudex artifacts. **Execution phases do not choose models.** Provider brands stay out of mission prompts and phase DoD — that is the **Model selection** component below.

### Process rules (cost-neutral; apply to every phase)

| ID | Rule | Applied in |
|---|---|---|
| `FRP-swap-grade` | **Whoever built it never grades it** — builder ≠ complete judge; gate + Jev + independent inspect own “done” | P1 envelope, HUL-C, attestation |
| `FRP-plan-hash` | Plan/DoD approval bound to **content hash**; material amend → re-check | waist, mission.yaml |
| `FRP-verdicts` | `APPROVED \| REVISE \| BLOCKED`; empty/malformed/**fail** ≠ approve | waist, Jev scope, HUL |
| `FRP-personas` | Plan review as **narrow MRs** (architecture / security / cost / ops) | micro-requests, P2–P3, HUL |
| `FRP-evidence` | Tool/test output is proof; missing evidence = blocked; findings on failure | gates, FINDINGS.md |
| `FRP-bounds` | Round caps + spend ceilings; exhausted → honest terminal, not fake green | waist, HUL stall |
| `FRP-artifacts` | Durable plan + append-only log (canon STATUS + receipts) | canon, mission packs |
| `FRP-authz` | Plan/review ≠ license to implement; phase DoD gates dispatch | mission loop |

**Anti-goals:** expensive multi-model burn by default; process that treats brand X as mandatory; fake green without evidence.

### Model selection — separate component (upfront per task)

Not part of mission STATUS rows for P1–P4/HUL code work. Every **task** (node, apply, plan seat, panel, mission attempt) resolves a rung **before dispatch**:

| Step | Owner (existing or planned) | Output |
|---|---|---|
| 1. Classify task | `sliding_scale.classify_task_tier` + file/DAG facts | tier 0/1/2 + score |
| 2. Optional Jev signal | `jev_policy` complexity pack when keyed (P2-triage) | `requires_iteration`, complexity — **feeds tier**, does not name brands |
| 3. Resolve ladder | `tier_model_ladder` / `router` / pools / escalation | ordered **capable** rungs, cheapest-first |
| 4. Dispatch | existing apply/plan/panel lanes | first usable rung; **rotation / substitution to another capable rung is normal** |
| 5. Evidence | ledger + envelope | **requested vs observed** model on every result |

**Policy:** prefer the cheapest rung that is capable for the resolved tier; substitution on failure/rate-limit is expected; never invent brand requirements in phase docs. Operators may pin ladders in config; missions do not.

**Related IDs (product, not mission script):**

| ID | Work |
|---|---|
| `MS-classify` | Keep one owner for tier/score (sliding_scale); wire Jev complexity as optional input when keyed |
| `MS-ladder` | Ensure every site (apply, waist, panel, consent, judge) resolves via router/ladders — no ad-hoc model strings in lane code |
| `MS-envelope` | Always record requested vs observed model |
| `MS-calibration` | Use verify outcomes to reorder pools (capability.py / rankings — advisory) |

`JEV-P2-triage` and `JEV-P3-route` implement the **Jev-assisted classify** and **route-choice** pieces; they still must not hardcode brands in the mission plan.

---

## Mission loop protocol (Freebuff + future `harness mission run`)

Each relaunch:

1. Read **this file** only for phase/STATUS/DoD.  
2. Pick first **incomplete** STATUS row (Track A sync/Pn or Track B HUL / open pack).  
3. If already true on `origin/main` (or pack terminal), mark STATUS and advance.  
4. Execute **one phase** (or one bounded HUL attempt set) per § schedules.  
5. Gates green → docs STATUS updated → PR → merge → post-merge verify.  
6. **Immediately** continue; do not stop after a green PR.  
7. Hard stop: blocked (STATUS + reason) **or** all Exit rows complete.

### Per-phase git schedule (Track A software)

| Phase | Branch | PR title | After merge |
|---|---|---|---|
| 0.0 | `chore/jev-sync-main-with-origin` | `chore(jev): sync main with origin/main P0` | STATUS 0.0 complete |
| P1 | `feat/jev-p1-policy-and-lanes` | `feat(jev): JEV-P1-…` | start P2 |
| P2 | `feat/jev-p2-system-one-pillars` | `feat(jev): JEV-P2-…` | start P3 |
| P3 | `feat/jev-p3-utilization` (or cluster PRs) | `feat(jev): JEV-P3-…` | start P4 |
| P4 | `feat/jev-p4-ops-and-exit` | `feat(jev): JEV-P4-…` | Jev exit |
| HUL-A | `feat/hul-a-mission-pack` | `feat(harness): HUL-A mission pack` | |
| HUL-B | `feat/hul-b-dual-budget` | `feat(spend): HUL-B dual budget` | after A |
| HUL-C | `feat/hul-c-jev-scope` | `feat(jev): HUL-C scope gate` | after A + Jev P1 |
| HUL-D | `feat/hul-d-mission-run` | `feat(mission): HUL-D until-limits driver` | after A–C |
| HUL-E | `docs/hul-e` | `docs(mission): HUL-E prompts` | can ship anytime |

**Rules:** worktree off `origin/main`; one phase per PR; iterate same PR until DoD; never force-push main; never merge red; post-merge re-run regression; update **this** STATUS only.

**Model selection:** out of band — canon § Model selection / `MS-*`. Phase PRs must not hardcode provider brands; resolve rungs via router/ladders per task. Process (swap-grade, verdicts, evidence, bounds) still applies.

**Regression (software phases):**  
`tests.test_jev`, `tests.test_jev_smoke`, `tests.test_agent`, `tests.test_waist`, `tests.test_sliding_scale` + phase-specific modules.

**Live smoke** (client/contract changes):  
`$env:HARNESS_JEV_LIVE_SMOKE="1"` then `python -m unittest tests.test_jev_smoke.LiveJevSmokeTests -v`  
Baseline: model `jev-1.13.0`; cost = tokens × 42 / 1e6.

**PR title convention:**  
`feat(jev): JEV-P1-policy — …` · `feat(harness): HUL-C-…` · `chore(jev): sync …`

**PR body must include:** Track, IDs, DoD evidence, tests run, STATUS update confirmation, out-of-scope.

**Implementer checklist (every PR):**

1. Read canon phase IDs + that phase’s **implementer notes / playbook** (P1: “P1 implementer playbook”).  
2. Prefer existing one-owner modules; do not fork jev_policy.  
3. Run gate tests **before** push; paste summary in PR.  
4. Update canon STATUS only when DoD holds on the PR tip.  
5. Process FRP only (swap-grade, verdicts, evidence). **No provider brands** in phase PRs — § Model selection / `MS-*`.  
6. If blocked: STATUS `blocked` + reason — never fake complete.

---

## Return format after each Mission pass

```
Canon: docs/jev-roadmap.md
Completed: <0.0 | JEV-P1 | HUL-A | open-problem <id> terminal …>
STATUS updated: Y/N
PR: <url> <open|merged>
Tests: <summary>
Next: <phase id | continue HUL attempt | blocked — reason | mission complete>
```

---

## Operator notes

- **One canon:** `docs/jev-roadmap.md`. Short paste: `docs/jev-mission-prompt.md`.  
- **Keys:** `resolve_jev_key()` → `~/.config/scmorc/jev.env`, `~/.config/harness/jev.env`, or `HARNESS_JEV_KEY` / `TYPESAFE_API_KEY` / `JEV_API_KEY`  
- **Endpoint:** `https://api.typesafe.ai/v1/systemone`  
- **Model:** alias `jev-latest` → `jev-1.13.0` (pin when calibrating)  
- **Price:** $42 / Mtok input; output free  
- Gates remain authoritative for code; Jev does not replace tests  
- HUL does not guarantee proofs; it guarantees **structured attempts + findings + resume** until limits  
- **Model selection:** separate component (§ Model selection / `MS-*`); do not hardcode brands in mission or phase DoD  
- **P1 next actions:** finish WIP on `Harness-jev-p1` per **P1 implementer playbook** → PR → merge → STATUS complete → P2

---

## Mission complete definition

**Jev track done** ⇔ all Phase 4 boxes true on `origin/main` + STATUS Jev rows complete.  
**HUL product done** ⇔ HUL-A…D shipped with tests + HUL-E docs.  
**HUL open-problem use** ⇔ pack exists; attempts/receipts written; terminal with FINDINGS.md under limit rules (success **or** limit/stall/human).  
**Full operator mission** ⇔ Jev P4 done **and** (HUL product done **or** open-problem packs on this contract) **and** FRP process rules followed (swap-grade/evidence/bounds — not brand mandates).
