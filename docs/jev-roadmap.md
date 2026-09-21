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

## Canonical STATUS (update here only — truth as of 2026-09-21 hourglass-composition pass)

| Track / phase | Status | PR / evidence |
|---|---|---|
| Preflight sync `main`↔`origin` P0 | **complete** | `origin/main` includes P0 `d042d70` |
| `JEV-P0-*` contract truth | **complete** | PR #34 / `d042d70`; live smoke OK |
| `JEV-P1-*` one owner + lanes | **complete** | **PR #35 MERGED** → `origin/main` `9d5ff14`; required tests present; structural retry green in CI |
| `JEV-P2-*` System One pillars | **in progress / repair** | WIP `Harness-jev-p2` / `feat/jev-p2-system-one-pillars`; **PR #36 OPEN**; lane-parity **hermetic**; D12 target lines **executed** by named tests; local battery green; audit **BAR MET** locally; live Jev smoke OK; lean jury **`JEV-P2-jury` deferred** — STATUS stays **in progress until merge + CI green** |
| `JEV-P3-*` utilization | **open** | after P2 merge + STATUS complete |
| `JEV-P4-*` ops / exit | **open** | after P3 DoD |
| `HG-composed-ceiling` | **implemented on branch** | `waist.composed_worst_case` + refuse-before-spend; `tests/test_hg_composed_ceiling.py::test_composed_pyramid_ceiling_refuses_before_any_spend` (spend==0) |
| `HG-final-gate` | **implemented on branch** | `PlanExecutor.resolve_final_gate` default ON when a verify cmd is discovered/declared; CLI/MCP/agent share it; `--no-final-gate`; `tests/test_hg_final_gate.py::test_default_final_gate_blocks_false_ok` |
| `HG-decompose-default` | **implemented on branch** | `config.resolve_hourglass` includes `decompose` (True when confirm/parallel on); CLI/MCP/agent share mapping; `tests/test_hg_hourglass_defaults.py` |
| `HG-condense-decompose` | **implemented on branch** | `compose_plan` passes distilled signatures into `decompose_via_llm(repo_context=…)`; `tests/test_hg_condense_decompose.py` |
| `HG-plan-consensus` | **implemented on branch** | optional `plan_consensus` before waist; unsound forces waist amend path; ledger `plan_consensus`; `tests/test_hg_plan_consensus.py` |
| `HG-pyramid-resume` | **implemented on branch** | `harness/pyramid_state.py` persist/load; `plan --resume` skips completed ok nodes; `tests/test_hg_pyramid_resume.py` |
| `HG-hybrid-isolate` | **implemented on branch** | `executor.partition_by_target_overlap`: overlap-free → worktree; overlapping → serial shared-tree; `tests/test_hg_hybrid_isolate.py` |
| `HG-waist-unreachable` | **implemented on branch** | confirm-armed + ALL waist rungs fail on execute → REFUSE (fail closed); `tests/test_hg_waist_unreachable_refuses.py` |
| `HG-ms-parity` | **implemented on branch** | `chat.chat_ladder` last-resort from settings/config; `sliding_scale.resolve_frontier_model` paid default = `config.DEFAULT_FRONTIER_PAID`; `tests/test_hg_ms_parity.py` |
| Hourglass composition PR | **in progress** | branch `feat/hourglass-composition` worktree `Harness-hg-remain`; full discover **1224 green** (46 skipped); audit A=10 R=10; **D12 changed-line coverage still <95%** + residual ruff/coverage hygiene — STATUS stays **in progress** until CI audit BAR MET + merge |

**Hourglass composition done when (all true):** named `tests/test_hg_*.py` green → keep-green battery (`test_waist`/`test_executor`/`test_planning_surface`/`test_spend`/`test_worktree`/`test_mcp`/`test_agent`/`test_cli`) green → `python audits/self/audit.py` **BAR MET** → PR merged → post-merge `main` CI green → HG STATUS rows `complete`. Do **not** mark complete while any of those are red.

**P2 done when (all true):** repair finished on PR #36 branch → named P2 tests **and** full listed battery green **locally and on CI** → `python audits/self/audit.py` **BAR MET** (D12 ≥95%) → STATUS on that branch honest (jury deferred if not shipped) → PR #36 merged → post-merge `main` CI green → STATUS P2 `complete`.

### P2 evidence snapshot (repair pass — update when gates change)

| Item | Finding |
|---|---|
| PR | [#36](https://github.com/Sovereign-Communication/harness/pull/36) `feat(jev): JEV-P2 confidence gating and triage` — **OPEN**, base `main` |
| Implemented | consent confidence + min-confidence abstain; `evaluate_triage` / waist triage envelope; tests `test_jev_triage`, `test_consent_confidence`, `test_min_confidence_gating` |
| Deferred | `JEV-P2-jury` lean typed pre-gate — **deferred**; “not in PR #36; needs fail-closed contract + dedicated coverage.” |
| Lane parity repair | `tests/test_jev_lane_parity.py` hermetic: isolated settings (`jev_api_key=None`, hourglass off), scripted `run_verify` (Windows-safe; no host-shell `true`), consent off, posts sized for every apply chat call; production fail-closed readiness **not** weakened |
| D12 coverage repair | Named tests **execute** previously untested lines: `apply_state.py:41`; `consent.py:225,226,231`; `jev_policy.py:227,231,233,235,239,241,242`; `waist.py:1018,1019,1020` (exception/reservation-release path included). Baseline refreshed only **after** real tests via `audits/self/refresh_coverage_baseline.py` |
| Local battery | Full repair battery green (118 tests, 1 live smoke skip); regression `test_agent` + `test_sliding_scale` + `test_consent` green (102 tests) |
| Live dogfood | `tests.test_jev_smoke.LiveJevSmokeTests` OK — `jev-1.13.0`, non-fallback, honest token cost |
| Gate to merge | Local battery + `python audits/self/audit.py` **BAR MET** + CI audit success on PR #36 tip; do **not** mark complete while any of those are red |

---

## Tracker

| Phase | IDs | Primary modules | Gate tests | Status |
|---|---|---|---|---|
| 0 Contract | `JEV-P0-*` | `jev.py`, `config.py`, docs | `tests/test_jev.py` + `tests/test_jev_smoke.py` | **complete** — PR #34 |
| 1 One owner | `JEV-P1-*` | policy + apply/waist/CLI/MCP/agent | `tests/test_jev_policy.py`, `tests/test_jev_lane_parity.py`, `tests/test_jev_ledger_spend.py` | **complete** — PR #35 merged; do not re-open |
| 2 Pillars | `JEV-P2-*` | consent, sliding_scale, panel | `tests/test_jev_triage.py`, `tests/test_consent_confidence.py`, `tests/test_min_confidence_gating.py` + hermetic `tests/test_jev_lane_parity.py` + audit | **in progress / repair** — PR #36 open; local gates green after hermetic + D12 repair; **not complete until merge + CI audit green**; jury deferred |
| 3 Utilization | `JEV-P3-*` | orchestrator, routing, context | per-pattern hermetic tests | planned — **after P2 merge** |
| 4 Ops | `JEV-P4-*` | workflows, analytics, docs | live acceptance checklist | planned |

### PR title convention
`feat(jev): JEV-P0-cost — token-priced TypeSafe usage on JevEvaluationResult`

### Next implementation slice (priority order — no guessing)
1. **P2 repair only** on `Harness-jev-p2` / PR #36 — follow **P2 repair playbook** below. Do **not** re-implement P1. Do **not** start P3. Do **not** mark complete while audit or local gates are red.
   - Lane parity is now **hermetic** (scripted gate + isolated settings); keep that contract.
   - D12 lines listed in the evidence snapshot are **executed** by real tests; do not game the baseline to hide them.
   - STATUS stays `**in progress / repair**` until PR #36 is **merged** with local+CI green; `JEV-P2-jury` stays **deferred**.
2. After P2 merge + STATUS complete → **P3** utilization patterns (plus promoted rows: HUL product, JEV-P5 issue-sort, hourglass remaining).
3. **P4** dogfood + freeze thresholds.

**Do not mark P2 complete while:** PR #36 is open, local lane-parity is red, audit is red, or CI on the tip is red. Canon + `AGENTS.md` on `origin/main` are the tracking truth if this row disagrees.


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
