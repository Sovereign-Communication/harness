# Freebuff transition — Harness JEV + hourglass mission

**Canon:** `docs/jev-roadmap.md` only (trust `origin/main`, not dirty local STATUS).  
**Order of operations:** already encoded on origin/main PR #50 (`7ed728f`).  
**Exclusive implementer note:** this document is for **Freebuff/Freebuff Mission** to pick up remaining work after the operator session lands open PRs.

---

## 1. What is already on `origin/main` (do not redo)

| Track | PR | Merge note |
|---|---|---|
| JEV-P0 contract | #34 | complete |
| JEV-P1 policy + lanes | #35 | complete |
| JEV-P2 pillars | #36 | complete; **JEV-P2-jury deferred** |
| P3 utilization | #43 | complete |
| P5 issue-sort packs | #42 | complete |
| Hourglass composition | #44 | complete |
| HUL-A mission pack | #41 | complete |
| JEV-P4 ops/exit wiring | #46 | complete; residual dogfood A/B + freeze persistence |
| STATUS / order docs | #40, #45, #49, #50 | complete |

**Product APIs to reuse (no forks):** `harness/jev_policy.py`, `harness/jev_packs.py`, `evaluate_issue_sort`, mission pack CLI, hourglass plan lane, `jev-phase` gate (on PR #39 until merge).

---

## 2. Immediate landing checklist (operator → Freebuff handoff)

Open product PRs (drive to green then merge; one at a time; audit BAR MET + tests + CI):

| PR | Branch | Work | Gate |
|---|---|---|---|
| **#39** | `feat/jev-phase-completion-score` | `harness jev-phase` 0–100 dogfood gate | `tests/test_jev_completion.py`; audit BAR MET |
| **#47** | `feat/hul-b-dual-budget` | dual budget `working_remaining` + reserve envelope | `tests/test_hul_budget_reserve.py`; audit BAR MET |
| **#48** | `feat/hul-cd-scope-driver` | HUL-C scope gate + HUL-D mission driver | `tests/test_hul_jev_scope_gate.py` + `tests/test_hul_driver_findings_resume.py`; audit BAR MET |

**Merge order:** #39 → #47 → #48 (or any order once each is independently green). After each merge, flip the matching STATUS row to **complete** with merge SHA on a follow-up docs commit if the PR branch already says complete-on-merge.

**Accountability gate (do not flip STATUS complete on prose alone):**

```bash
$env:PYTHONPATH = "<worktree>"
python -m harness.cli jev-phase --phase JEV-P1 --repo-root . --local-only
# require can_mark_complete=true + score >= 85
python -m harness.cli jev-phase --phase JEV-P2 --repo-root . --local-only
# after HUL merges: same for HUL-A / HUL-B / HUL-C / HUL-D rows when those IDs are in STATUS
```

---

## 3. After canon exit (Jev P4 residual + HUL product green)

### 3.1 Honest P4 residuals (can mark complete with evidence or keep open)

| Residual | How to close |
|---|---|
| Dogfood A/B pass-rate + cost delta | Run same apply/plan with `HARNESS_JEV_DISABLE=1` vs keyed; record pass rate + cost from ledger / envelopes into `docs/jev-dogfood.md` |
| Operator model-pin freeze | Pin `jev_model` to observed id (`jev-1.13.0`) via `freeze_jev_settings` / config after first calibration; document in dogfood doc |
| Jury | Remains **deferred** — do not invent |

### 3.2 Promote JEV-LOG addendum to canonical (step 2)

One **docs PR** (branch e.g. `feat/jev-log-promote` off `origin/main`):

1. Fold IDs from `docs/jev-log-analysis-followup.md` into `docs/jev-roadmap.md` STATUS as **open** rows:
   - `JEV-LOG-schema`, `JEV-LOG-parse`, `JEV-LOG-factor-pass`, `JEV-LOG-judgment`, `JEV-LOG-envelope`, `JEV-LOG-cli`, `JEV-LOG-dogfood`
2. Keep design detail in the addendum file; **canon STATUS is the tracker**.
3. Reference draft: `HANDOFF/JEV_LOG_PROMOTION_DRAFT.md`.
4. CI green → merge.

**Until this promotion, `JEV-LOG-*` are not STATUS rows.**

### 3.3 Implement addendum in full as canonical product (step 3)

| Rule | Detail |
|---|---|
| Worktree | `feat/jev-log-factor-analysis` off `origin/main` |
| Order | One phase PR at a time (schema → parse → factor-pass → judgment → envelope → cli → dogfood) |
| Owners | Extend `jev_packs` + `jev_policy` only; **no second Jev client** |
| 0-hallucination | Operator-declared buckets + score levels only; unkeyed → `is_fallback`; never invent buckets/actions |
| Pipeline | Code parse → cheap generative pack draft → operator freeze → Jev choice+score → code aggregate **JSON only** |
| Dogfood | `C:\temp\logsSCMessenger.txt` (or successor dump); record cost + fallback rate + taxonomy integrity |
| Brands | Resolve models via MS / ladders; no brand hardcoding in phase code |
| Process | FRP: swap-grade, evidence, bounds; builder ≠ sole grader |

---

## 4. Freebuff operating rules (unchanged)

1. **Canon STATUS only** — no parallel plans; origin wins dirty local STATUS.
2. One phase PR at a time; merge only when local gates + CI audit **green**.
3. Fail ≠ approve; never fake complete; blocked = exact command/output.
4. No provider brand hardcoding in phase code.
5. Do not game `audits/self/coverage_baseline.json`.
6. Live tracking: cheap paid rungs (`HARNESS_USE_FREE=false`); not free-only.
7. Do not edit locked merged worktrees; do not rewrite `jev_policy` ownership.
8. **No SCMessenger product code in Harness PRs** — SCMessenger builds callers against origin/main APIs only.

---

## 5. Suggested Freebuff auto-run prompt (after open PRs merge)

```text
Harness mission. Read docs/jev-roadmap.md only (canon on origin/main).
Order of operations is encoded there: (1) finish any incomplete canon STATUS rows
(JEV-P4 residuals, HUL if still open) with gates + jev-phase can_mark_complete;
(2) promote JEV-LOG addendum into STATUS via one docs PR; (3) implement JEV-LOG-*
in full on feat/jev-log-factor-analysis, one phase PR at a time, extending
jev_packs/jev_policy only. 0-hallucination operator packs; JSON only; dogfood
the log dump; no brands; no fake complete; merge only when audit BAR MET + CI green.
Stop only if blocked (STATUS + exact evidence) or all Exit rows true.
```

---

## 6. Worktree hygiene

After merges, prune local worktrees whose tips are on origin. Keep only the active phase worktree + operator main. Remotes retain full history.

---

*Operator handoff 2026-09-21. Canon: `docs/jev-roadmap.md`. Order: PR #50.*
