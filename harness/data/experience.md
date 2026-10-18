# Lessons library — refine loop (issue #239)

Refinement lessons from the Propose-Probe-Compose loop. Each entry follows the schema below.
Seed entries are hand-written from known past harness failures (tagged `seed`).

## Schema

```
## <task_id> | <timestamp ISO> | <family>
- base: <main HEAD sha at cycle start>
- trigger: <failure | refinement-pass> | jev_outcome: <0.0-1.0>
- candidates: invariant -> <diff ref or path>, domain -> <diff ref or path>
- jev: invariant {completion, efficiency, safety, quality, composite}, domain {...}
- winner: <invariant | domain | base> | reason: <1 sentence>
- regression: <pass | fail | deferred> | held_out: [<task_ids>]
- lesson: <1-3 sentences, actionable>
- tags: [<family>, <component>, <failure-mode>]
```

## Entries

## seed-001 | 2026-10-18T20:00:00Z | pr-review
- base: n/a (seed)
- trigger: n/a | jev_outcome: n/a
- candidates: n/a
- jev: n/a
- winner: n/a | reason: n/a
- regression: n/a | held_out: []
- lesson: Hermetic suite failures across all 5 platforms on a docs-only PR were fixed by rebasing onto current main, not by changing the PR content. When CI fails uniformly on a small change, check branch staleness before investigating the diff.
- tags: [pr-review, scripts, wrong-diagnosis]

## seed-002 | 2026-10-18T20:00:00Z | code-fix
- base: n/a (seed)
- trigger: n/a | jev_outcome: n/a
- candidates: n/a
- jev: n/a
- winner: n/a | reason: n/a
- regression: n/a | held_out: []
- lesson: The gh-api CLI fails with ModuleNotFoundError unless PYTHONPATH includes the custom-connector-setup bin directory. Tool failures that look like broken credentials are often environment issues — test the actual check directly before diagnosing.
- tags: [code-fix, scripts, environment]

## seed-003 | 2026-10-18T20:00:00Z | pr-review
- base: n/a (seed)
- trigger: n/a | jev_outcome: n/a
- candidates: n/a
- jev: n/a
- winner: n/a | reason: n/a
- regression: n/a | held_out: []
- lesson: The combined commit-status API always returns pending for these repos because CI runs via GitHub Actions. Always read check-runs, never the status API, for real CI state.
- tags: [pr-review, tool-guidance, wrong-tool]

## seed-004 | 2026-10-18T20:00:00Z | branch-hygiene
- base: n/a (seed)
- trigger: n/a | jev_outcome: n/a
- candidates: n/a
- jev: n/a
- winner: n/a | reason: n/a
- regression: n/a | held_out: []
- lesson: Merged PR head branches frequently survive the merge. A post-merge branch-deletion check on every merged PR keeps the branch census clean without touching WIP.
- tags: [branch-hygiene, scripts, process]

## seed-005 | 2026-10-18T20:00:00Z | plan-assessment
- base: n/a (seed)
- trigger: n/a | jev_outcome: n/a
- candidates: n/a
- jev: n/a
- winner: n/a | reason: n/a
- regression: n/a | held_out: []
- lesson: Jev assessments of self-modifying-system plans plateau at "revise" no matter how much spec is added — the uncertainty is structural, not a spec gap. For such plans, a constrained pilot with human oversight is the verification mechanism, not more document iteration.
- tags: [plan-assessment, guardrails, calibration]
