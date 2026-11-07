# Harness dogfood audit — 2026-11-06

GPT-dispatched driving test plus added dogfooding. Task A ran; Task B (the Jev
flush-it-out fallback) was not needed and was not run.

- Seat: FreeBuff Desktop, local, on the operator laptop. Working tree for the
  audit: `C:/Users/SCM/Documents/GitHub/harness-dogfood-wt`, a **detached**
  checkout of `origin/lane/jev-primary-s1` = `1f1443592a23b6b527f49cea65942e0943513c32`.
  Detached on purpose: newest steering forbids pushing that lane, so this seat
  never created, moved or pushed its branch.
- Operator clone `C:/Users/SCM/Documents/GitHub/Harness` was opened, read and
  left exactly as found: `main` at `1621e4977577c583bf1130372aa1ab69292d5f00`,
  8 commits behind `origin/main`, 6 untracked paths, zero tracked modifications.
- Record filed on #453: comment 6702000917.

## What was judged

Three standing claims, tested against primary evidence gathered live:

1. the #295 burndown register's completeness;
2. `main`'s green state;
3. the #452 vision-driver road state.

## Method

The repo's own `harness verify` under `HARNESS_JEV_PRIMARY_MODE=1`, in the
worktree, against the harness repo. Five findings were phrased as defects and
grounded on a 39-line evidence bundle built from live GitHub API and git reads.
The real composition ran: `run_verify -> policy_for -> JevPolicy ->
JevEvaluator -> panel_judge`, with the lane's existing TypeSafe Jev key and
OpenRouter key read through `harness/config.py` and never printed
(`resolve_jev_key()` present, 108 chars; `resolve_api_key()` present, 73 chars;
only presence and length appear anywhere in this record).

```bash
HARNESS_JEV_PRIMARY_MODE=1 python -m harness.cli verify \
  --prompt-file  prompt.txt \
  --claims-file  claims.json \
  --source-file  evidence.txt \
  --panel "deepseek/deepseek-v4.1-flash,inclusionai/ling-3.0-flash" \
  --judge "z-ai/glm-5.3-flash" \
  --max-cost 0.05 --max-tokens 4096 \
  --task-id dogfood-audit-2026-11-06 \
  --out result.json --quiet --no-color
```

Exit 0, empty stderr, judge synthesis `parseable`, claims grounding
`{"ok": true, "issues": []}`.

## Judgment route

`judgment_route: jev_primary`, `judgment_route_reason: null`. Jev answered on
the live key: model `jev-1.14.0`, `is_fallback: false`, `fallback_reason: null`,
site `claims`. The S5 wiring cure in `1f14435` holds outside the hermetic double,
at service level, on a real key.

## Verdicts

### 1. The #295 register's completeness — FAILS

| Measure | Value |
|---|---|
| issue checkbox rows in the tracker body | 24 |
| rows unchecked while the issue is CLOSED | **6** — #276 #278 #287 #282 #288 #293 |
| live open issues | **87** (156 total) |
| open inventory the tracker body states | 74 |
| open issues named in the tracker body | 29 |
| open issues named in the canon on `main` | 47 |
| open issues named in **neither** | **30** |
| of those, created on or before 2026-10-28 | **14** — #380 #381, #413 #414 #416 #419, #423 #424 #425 #427 #428, #432 #433 #434 |

The canonical pointer is dead. The tracker body sends readers to
`docs/jev-roadmap.md`, section "Canonical reconciliation and verified
web-research plan — 2026-10-28". On `origin/main` that file is 1510 lines with
**0 matches** for the phrase and **0 occurrences** of the date; the same holds
on `origin/lane/jev-primary-s1` and `origin/lane/laptop-verify`. The phrase
exists only in commit `5570245` ("docs: reconcile Jev web research and audit
plan"), which is **PR #431 — state CLOSED, `mergedAt: null`**: closed without
being merged. The register points at a section that no branch ships, for a count
that no longer holds. Its own Definition of Done ("open-issue count returns to
zero except operator holds") is likewise unmet.

### 2. `main`'s green state — TRUE on the owner-declared rail, NOT derivable from the repo's stated rule

| Rail | Reading on `828474615e2a23c4d8bfffd4d7d0e0c777354039` |
|---|---|
| CircleCI commit statuses | **14 distinct contexts, every context's latest state `success`** |
| CircleCI pipelines | numbers 83–96 |
| GitHub Actions check-runs | **15 entries: 14 `failure`/`skipped`, zero `success`** (all app `github-actions`, billing-locked) |
| combined commit status | `success` |

The 14 green CircleCI contexts are: `audit`, `test`, `package`, `Checkout
hygiene`, `ui-playwright-smoke`, six `hermetic suite` legs (ubuntu-22.04 py3.9,
ubuntu-latest py3.11/py3.13/py3.14, windows-latest py3.11, macos-latest
py3.11), and three `installed package` legs. Under the owner's 2026-11-05 ruling
that verification rides CircleCI, main is green and the standing claim holds.

The defect is in the rule, not the rail: the repo states "CI from check-runs,
never the combined status", and a check-runs-only reading of the same commit
returns RED. CircleCI publishes commit *statuses*; the only check-runs present
are the billing-locked Actions ones. The repo cannot currently read its own
green from the rule it states.

### 3. The #452 road state — step 1 landed but unevidenced on the issue; steps 2–5 not met

| Step | Requirement | Actual |
|---|---|---|
| 1 LANDING | pipeline number + merged SHA + main's first green pipeline, recorded on the issue | PR #442 merged 2026-11-06T20:05:32Z, head `463a29a`; `main` `8284746` is that merge commit and carries the green rail — but **no evidence block on the issue**, and the body still reads "Step 1 is the only step in motion: lane head `205d6338`" |
| 2 PERCEPTION BRIDGE | one evidenced screen-capture step by a non-desktop driver **against the FreeBuff desktop** | Linux describe/capture probe on an Xvfb/openbox **sandbox** (comment 2026-11-06T19:13:36Z) — not met |
| 3 S3 CALIBRATION | per-field bars at or above 95/100 on SR-3 session-start states | specimens only, self-described as "inputs, not a calibration pack: no per-field bar is claimed here" (21:35:15Z) — not met |
| 4 TM LADDER | rungs in order | no evidence filed |
| 5 THE TEST | one supervised shadow relaunch | no evidence filed |

The landing genuinely happened; the road's own state block is stale by one full
step and step 1's required evidence was never posted.

## Per-claim verdicts from the run

| id | finding as tested | Jev flag | panel (n=1) | final | decided by |
|---|---|---|---|---|---|
| c1 | #295 register incomplete and stale | supported 0.78 | real | **REAL** | 6 stale rows, 30 uncovered, dead pointer |
| c2 | main not green on CircleCI | supported 0.94 | not_real | **NOT REAL** | flat 14/14 success |
| c3 | #452 step 1 incomplete-with-evidence | not supported 0.06 | real | **REAL** | body stale at `205d6338`, no evidence block |
| c4 | #452 step 2 acceptance unmet | supported 0.95 | real | **REAL** | sandbox probe, not FreeBuff desktop |
| c5 | check-run rule contradicts the CircleCI rail | supported 0.53 | real | **REAL** | 14 statuses success vs 14 check-run failures |

The Jev flags and the LLM tier disagree on c2 and c3, and on both the primary
evidence sides with the LLM tier. The claim-support path is documented as
advisory typed flags and stayed advisory, so this is not a contract violation —
but on this evidence-shaped claim set the primary route was less accurate than
the cheap residual panel. That is recorded as observed, not smoothed.

## Meter record

**Jev line — judgment route, site `claims`**

- model `jev-1.14.0`, `is_fallback: false`, `fallback_reason: null`
- **cost `$0.000101178`**
- input 2409 tokens / output 99 tokens
- aggregate `verdict: fail`, `confidence: 0.0`, `supported: 0.06`

**LLM lines — residual**

- judge `z-ai/glm-5.3-flash`: **`$0.000443911`**
- panelist `inclusionai/ling-3.0-flash`: **`$0.000116694`**
- `deepseek/deepseek-v4.1-flash`: `$0.00`, removed pre-dispatch
  (`learned-BYOK org filter (paid route)`)

**Totals**

- `actual_cost` **`$0.000661782`** against a `$0.05` ceiling
- preflight reserve `$0.022783516`
- Jev share of spend on this run: **13.3%**

## Dogfood observations on the mode

1. **Single-seat panel.** `required_panelists: 1`, `voted_by: 1 of 1`. One
   requested panelist was dropped before dispatch, so independence was not
   measured; "agreement: high" is one sample. This run does not evidence the
   panel-residual path at depth.
2. **Convergence step produced nothing and still exited 0.** The `convergence`
   sub-object records `status: error`, `verdict_status: inconclusive`,
   `degrade_reason: specialist_unavailable` (glm returned reasoning-only, then
   two free gemma providers errored), `judge_fallback: true`, cost
   `$0.000349`. The top-level `verdict` still renders as a clean deterministic
   tally. An inconclusive convergence is visible only to a reader who opens the
   sub-object; the envelope should surface it at top level.
3. **`use_free: true` with paid models.** `meta.use_free` is true while every
   dispatched model is paid. Inert here, but a meter mislabel a reader can trip
   on.
4. **The route name is not a quality claim.** `judgment_route: jev_primary` was
   true and correct; the flags it carries were still wrong twice against primary
   evidence. The mode's honesty property is that it names the route. Nothing in
   it certifies the flags.

## Findings routed

- **#295** needs a register refresh, not a note: 6 boxes, 74 → 87 count drift,
  30 uncovered open issues (14 pre-dating the reconciliation date), and the dead
  canonical pointer to unmerged PR #431.
- **#452** needs its state block corrected (step 1 landed on `8284746`) and an
  evidence block added; steps 2–5 remain open.
- The check-run-vs-statuses contradiction is a gate-rule defect in its own
  right and is filed here for routing rather than fixed in this audit.

## Lucas Gate v2 receipt

Runner `harness/lucas_gate.py::run_typed_track`; `validation:
deterministic_scorecard_contract`; `judgment_owner: calling agent`;
`product_jev_run: false`; `model_called: false`.

- **status `pass`**
- **`artifact_sha256` `0afe3a010ed53f7291306805cd0527996ea09f6e1de21e6428c3ab98cbc2c25e`**
  over the #453 record body verbatim (UTF-8), receipt appendix excluded.
- `overall_average` **9.24** across 10 applicable typed categories; no failing
  categories, no failing bias checks, no rounding exceptions used,
  `investigated_exception: false`.
- Private operator profile unavailable to this seat, recorded per
  `docs/lucas-gate-v2.md`; the gate ran against the mission, the issue
  specification and the source evidence. No private-profile review is claimed.

Scorecard (categories 1,2,3,4,5,6,8,9,10,11; bias B1,B3,B4,B5,B8,B9,B10,B11):
category scores 9.1, 9.2, 9.0, 9.3, 9.6, 9.4, 9.5, 9.2, 9.1, 9.0; bias scores
10, 9, 9, 9, 9, 8, 8, 9. Reproduce with `harness/lucas_gate.py::run_typed_track`
over the same artifact text.

## Reproduction appendix

### `evidence.txt` (source window; line numbers are the claims' `source_refs`)

```text
EVIDENCE BUNDLE - harness repo state, captured 2026-11-06 by the dogfood audit seat (live GitHub API + git)
1 origin/main = 828474615e2a23c4d8bfffd4d7d0e0c777354039 "lane/burndown - standing draft PR (LANE PROTOCOL) (#442)"
2 PR #442 (head lane/burndown@463a29a988731e0983f0302c435951631ceeca87) MERGED into main 2026-11-06T20:05:32Z
3 CircleCI commit statuses on 8284746: 14 distinct contexts, every context latest state = success
4 contexts: audit, test, package, Checkout hygiene, ui-playwright-smoke, hermetic suite (py3.9 ubuntu-22.04),
5 hermetic suite (ubuntu-latest py3.11/py3.13/py3.14), hermetic suite (windows-latest py3.11), hermetic suite
6 (macos-latest py3.11), installed package (ubuntu/macos/windows). CircleCI pipeline numbers 83..96.
7 GitHub Actions check-runs on the SAME commit 8284746: 15 total, conclusions failure or skipped, none success
8 (hermetic suite x6 failure, UI Playwright smoke failure, Checkout hygiene failure, Change scope failure,
9 handoff scope failure, package failure, audit skipped, installed package skipped, pyright skipped,
10 Driving hygiene skipped). The Actions rail is billing-locked per the owner decision of 2026-11-05.
11 Combined commit-status state on 8284746 = success (14 CircleCI contexts).
12 A check-runs-only reading of the same commit therefore reports main RED while the CircleCI rail reports GREEN.
13 #295 tracker body: 24 issue checkbox rows across Waves 0-5.
14 Of those 24 rows, 6 are still unchecked while the issue is CLOSED on GitHub: #276 #278 #287 #282 #288 #293.
15 Live open issue count in the repo: 87. The #295 body states a 74-open-issue inventory as of 2026-10-28.
16 Open issues named in the #295 body: 29. Open issues named in docs/jev-roadmap.md on main: 47.
17 Open issues named in NEITHER the #295 body nor the canon: 30.
18 Of those 30 uncovered open issues, 14 predate 2026-10-29 and so are not merely newer than the register:
19 #380 #381 (2026-10-25), #413 #414 #416 #419 (2026-10-27), #423 #424 #425 #427 #428 (2026-10-28),
20 #432 #433 #434 (2026-10-28).
21 #295 body pointer: 'The current 74-open-issue inventory ... maintained in docs/jev-roadmap.md, section
22 "Canonical reconciliation and verified web-research plan - 2026-10-28"'.
23 docs/jev-roadmap.md on origin/main contains no section named "Canonical reconciliation": 0 matches for the
24 phrase and 0 occurrences of the date 2026-10-28 anywhere in the 1510-line file.
25 Same check on origin/lane/jev-primary-s1 and origin/lane/laptop-verify: 0 matches on both.
26 The phrase exists only in commit 5570245 'docs: reconcile Jev web research and audit plan', which is PR #431.
27 PR #431 state = CLOSED, mergedAt = null: closed WITHOUT being merged. The canonical pointer is dead on main.
28 #452 body STATE AT FILING: 'Step 1 is the only step in motion: lane head 205d6338 ... Steps 2-5 are unblocked
29 in sequence by it.' The body was not updated after the landing it depends on actually merged.
30 #452 step 1 requires evidence: pipeline number + merged SHA + main's first green pipeline. Merge SHA and
31 CircleCI pipelines exist (lines 1-6); the issue carries no evidence block for them.
32 #452 step 2 acceptance: 'one evidenced screen-capture step by a non-desktop driver against the FreeBuff
33 desktop'. Evidence filed instead: Linux describe + capture fallback proven on an Xvfb/openbox sandbox
34 (comment 2026-11-06T19:13:36Z), i.e. NOT against the FreeBuff desktop.
35 #452 step 3 acceptance: per-field bars >=95/100 on SR-3 session-start states. Evidence filed instead:
36 specimens only, explicitly 'inputs, not a calibration pack: no per-field bar is claimed here' (21:35:15Z).
37 #452 steps 4 (TM ladder) and 5 (supervised shadow relaunch) have no evidence filed on the issue.
38 harness-oc MCP: UNREACHABLE this session (SSH to ec2-user@13.217.204.112, connection closed), 2nd confirmation.
```

### `claims.json`

```json
{
  "context": "Audit of three standing claims about the Sovereign-Communication/harness repo, judged against the numbered evidence bundle. Each entry is phrased as a DEFECT: real=true means the defect is present and the standing claim therefore fails as written; real=false means the standing claim survives.",
  "claims": [
    {"id": "c1", "text": "DEFECT: the #295 burndown register is incomplete and stale - 6 of its 24 checkbox rows are unchecked while the issue is closed, 30 of 87 open issues are named in neither the register body nor the canon, and its canonical pointer names a 'Canonical reconciliation' section that does not exist anywhere in docs/jev-roadmap.md on main", "kind": "defect", "source_refs": [13,14,15,16,17,18,21,23,24,25,26,27]},
    {"id": "c2", "text": "DEFECT: main at 8284746 is not green - at least one of the 14 distinct CircleCI contexts on that exact commit reports a non-success state", "kind": "defect", "source_refs": [1,3,11]},
    {"id": "c3", "text": "DEFECT: #452 step 1 (LANDING) is not complete-with-evidence as that issue requires - the issue body still states step 1 is the only step in motion at lane head 205d6338 and carries no pipeline-number, merged-SHA or first-green-pipeline evidence block", "kind": "defect", "source_refs": [2,28,29,30,31]},
    {"id": "c4", "text": "DEFECT: #452 step 2 perception-bridge acceptance is not met - the filed evidence is a Linux describe/capture probe on an Xvfb sandbox, not one evidenced screen-capture step by a non-desktop driver against the FreeBuff desktop", "kind": "defect", "source_refs": [32,33,34]},
    {"id": "c5", "text": "DEFECT: the repo's stated gate rule (CI truth read from check-runs, never the combined status) and the owner-declared CircleCI rail disagree about main's state on the same commit, so 'main is green' is not derivable from the rule the repo states", "kind": "defect", "source_refs": [7,8,9,10,11,12]}
  ]
}
```

### `result.json` — fields that decide the record

```json
{
  "judgment_route": "jev_primary",
  "judgment_route_reason": null,
  "judge_model": "z-ai/glm-5.3-flash",
  "judge_synthesis_status": "parseable",
  "status": "ok",
  "actual_cost": 0.000661782429,
  "max_cost_ceiling": 0.05,
  "estimated_worst_case_cost": 0.022783516,
  "cost_by_model": {
    "z-ai/glm-5.3-flash": 0.00044391015899999996,
    "inclusionai/ling-3.0-flash": 0.00011669427,
    "jev:claims": 0.00010117800000000001
  },
  "claim_support": {
    "verdict": "fail", "confidence": 0.0, "supported": 0.06,
    "cost": 0.00010117800000000001,
    "input_tokens": 2409, "output_tokens": 99,
    "is_fallback": false, "fallback_reason": null,
    "model": "jev-1.14.0", "site": "claims", "discarded": false,
    "claim_flags": [
      {"id": "c1", "supported": true,  "noul": 0.78, "fallback": false},
      {"id": "c2", "supported": true,  "noul": 0.94, "fallback": false},
      {"id": "c3", "supported": false, "noul": 0.06, "fallback": false},
      {"id": "c4", "supported": true,  "noul": 0.95, "fallback": false},
      {"id": "c5", "supported": true,  "noul": 0.53, "fallback": false}
    ]
  },
  "convergence": {"status": "error", "verdict_status": "inconclusive",
                  "degrade_reason": "specialist_unavailable", "judge_fallback": true},
  "panel_failures": [{"model": "deepseek/deepseek-v4.1-flash", "status": "pool_filtered",
                      "reason": "removed pre-dispatch: learned-BYOK org filter (paid route)"}],
  "required_panelists": 1,
  "meta": {"use_free": true, "panel": ["deepseek/deepseek-v4.1-flash", "inclusionai/ling-3.0-flash"],
           "judge": "z-ai/glm-5.3-flash", "max_cost_ceiling": 0.05}
}
```

## Limitations

- One keyed run, one panelist, no convergence specialist. Multi-panelist
  agreement is not measured here.
- `jev_primary_mode` is not on `main`; it exists only on `lane/jev-primary-s1`.
  Nothing here claims otherwise.
- Five claims were tested, not the whole register. The counts are a point-in-time
  read of live GitHub, taken 2026-11-06.
- `harness-oc` MCP was UNREACHABLE this session (SSH to
  `ec2-user@13.217.204.112` closed), a second independent confirmation of the
  probe filed on #463. Nothing in Task A depended on it.
- The private operator profile for Lucas Gate was unavailable; the gate ran
  against mission, issue specification and source evidence, as
  `docs/lucas-gate-v2.md` directs.

## Branch note

This report is committed on `lane/dogfood-audit`, a new branch created from the
detached worktree tip (`1f14435`). It is documentation only: no product code is
changed by this audit, and nothing was pushed to `lane/jev-primary-s1`,
`lane/laptop-verify`, or `main`.
