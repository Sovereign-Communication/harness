Re-merge readiness notes for `harness/local_fit` advisory layer
=================================================================

This is the single source of truth for merging the local-fit advisory layer
into the live Harness repo. It is developed in an isolated clone only.

1. What the layer is
---------------------
An opt-in, advisory-only local neural net layer for Harness. For each
candidate model seat in a dispatch decision, it scores the likelihood of three
seat-level outcomes:

- usable_stop — the seat finishes cleanly with parseable output
- truncated — the seat output is truncated
- unusable — the seat errors, is invalid, or is missing required content

The headline advisory value is `usable_stop - truncated - unusable`, used as a
small tiebreak nudge on top of existing ordering. The layer never replaces the
existing order; it only nudges it when the feature flags are on.

It is a non-LLM, CPU-only, ONNX-based local scorer. No PyTorch, no
transformers, no LLM, no GPU runtime.

2. Files in the clone (new, additive)
---------------------------------------
- harness/local_fit/__init__.py
- harness/local_fit/README.md
- harness/local_fit/RE_MERGE_READINESS.md
- harness/local_fit/schema.py
- harness/local_fit/extract.py
- harness/local_fit/config.py
- harness/local_fit/model_loader.py
- harness/local_fit/train.py
- harness/local_fit/advisory.py
- harness/local_fit/hook.py
- harness/local_fit/dispatch_hook.py
- tests/test_local_fit_extract.py
- tests/test_local_fit_model.py

All of these are new. None of them modify existing Harness modules.

3. What existing Harness files are touched
-------------------------------------------
None. This layer is entirely additive. It does not edit apply.py, panel.py,
trust.py, cli.py, config.py, ledger.py, mcp.py, or any existing test.

4. How the flags work
----------------------
Four environment variables control the layer:

- HARVEST_LOCAL_FIT_ENABLE=1|true|yes — turns the advisory layer on
- HARVEST_LOCAL_FIT_MODEL_DIR=path — directory containing model.onnx and
  model_meta.json
- HARVEST_LOCAL_FIT_USE_ADVISORY_ORDER=1|true|yes — also apply the advisory as
  a small tiebreak on top of an existing existing_order_key (still advisory only)
- HARVEST_LOCAL_FIT_ADVISORY_TIEBREAK_WEIGHT=float, default 0.05 — size of the
  advisory nudge when ordering is enabled

When HARVEST_LOCAL_FIT_ENABLE is off, the layer is completely inert: it attaches
empty advisory dicts, does not load the model, does not score, and does not
reorder. The flags are read dynamically so tests can toggle them at runtime.

5. Training / eval data source
-------------------------------
The layer trains and evaluates only over run JSON files found under
`audits/*/_runs/*/*.json`, via the generic `extract.all_run_files()` scanner.

It does NOT train on ledger/summary files. In this clone,
`audits/self/round2_scores.json` is an audit scores/summary ledger with keys
`scores` and `checks`; it is not a run JSON with panel_results/seat shapes and
is intentionally excluded from extraction. Nobody should train on it by mistake.

In this clone today the only run data is `audits/scmessenger/_runs/v4`
(9 run files, 59 seat rows, all structured_claims panels). The extractor
already supports multiple task shapes (verify_panel, structured_claims, apply,
bench, probe, consent) via `guess_task_type`; only structured_claims exists
here right now.

6. Model artifact story (for the real repo)
--------------------------------------------
Two viable options for the real repo; pick one before merge.

Option A — in-tree model bundle
- Commit model.onnx + model_meta.json as a small bundle somewhere in the repo
  (e.g. harness/local_fit/model/ or a top-level model/ dir).
- Pros: simple, deterministic, versioned with the repo.
- Cons: commits a binary; needs a deliberate rebuild step when the model is
  refreshed.

Option B — build-over-audit-runs
- Add a small build step (script/CI target) that runs
  `extract.all_run_files()` + `train.run_pipeline()` (or `eval_over_seeds()`)
  over the repo's audit runs and writes the artifact as a build output.
- Pros: artifact is reproducible from repo data; no binary in the source tree.
- Cons: requires the train-time dependency onnx in the build environment; needs
  a defined place for the built artifact.

Before merge, decide: (a) in-tree bundle vs build-over-audit-runs, (b) where
the artifact lives in the real repo, (c) whether the artifact is committed or
produced, (d) how/when it is refreshed, (e) the onnx dependency story.

7. Exact off-by-default behavior
---------------------------------
- Default state of the layer: OFF.
- With flags off:
  - `maybe_score_candidates`, `score_one`, `hook.score_candidates`,
    `dispatch_hook.maybe_score_and_order`, `maybe_score_only`, `decision_snapshot`
    all attach empty advisory dicts and do not load the model.
  - `apply_advisory_tiebreak` returns candidates in their existing order.
  - No existing Harness behavior changes.
- With HARVEST_LOCAL_FIT_ENABLE on but MODEL_DIR missing/invalid: inert
  (scorer fails to load, layer falls back to empty advisories).
- With both ENABLE and MODEL_DIR on:
  - advisory scores are attached to candidates
  - ordering is nudged only if HARVEST_LOCAL_FIT_USE_ADVISORY_ORDER is also on
  - the existing order is never discarded

8. Guarantees preserved
------------------------
The layer does not change any of the following:
- ceilings
- consent
- deferral
- continuation
- trust
- verify gate behavior
- existing capability/reliability ordering math (unless a separate advisory
  ordering flag is also on, and even then only as a small tiebreak nudge)

The layer is advisory-only. It can suggest, not decide.

9. What must be true before turning the flag on in the real repo
------------------------------------------------------------------
1. The model artifact (model.onnx + model_meta.json) exists in the real repo
   in the agreed location, or the build-over-audit-runs path is wired and
   produces it.
2. The onnx dependency (train-time only, if build-over-audit-runs) is
   acceptable in the build environment.
3. A multi-seed hold-out eval has been run on the real repo's audit data and the
   numbers are acceptable to the person turning it on.
4. The flag defaults are confirmed: ENABLE off by default; USE_ADVISORY_ORDER
   off by default.
5. The integration point (if any) is behind the flag and has a test proving no
   behavior change when off.
6. The person turning it on accepts that this is advisory-only and does not
   change any existing guarantee.

10. Current verification status
--------------------------------
- Clone is at commit 571073644f116d3521dae774b5df718ef78d6e88, same as the
  live Harness source.
- 45 local_fit tests pass, all green.
- Tests cover: label rules, extraction basics, all-audits extraction + union,
  feature schema, feature vector shape, training + export smoke, ONNX runtime
  load + score, advisory hook reads scores, flag-off inert, advisory key
  ordering, run-level split determinism + no-leakage, hold-out eval artifact +
  metadata + top1/top2 + stats-from-train-only, all-audits train/eval,
  multi-seed eval range, flag-gated hook inert/score/ordering/explanation, and
  no-network-import guardrails.
- No network calls in the local_fit pipeline (asserted by tests).
- Multi-seed eval over the full clone dataset (9 v4 runs, 59 union rows):
  - eval top1 range 0.833 - 0.941, mean 0.883
  - eval top2 range 1.000 - 1.000
  - usable_stop R=1.000 across seeds; unusable P=1.000 across seeds
  - truncated 0/0 (none in this data)

11. Open scoping questions before merge
----------------------------------------
1. In-tree model bundle vs build-over-audit-runs? Where exactly does the
   artifact live in the real repo?
2. Do we want the advisory to influence live candidate ordering in the real
   harness, or only emit advisory scores for reporting first?
3. Do we want to expand training beyond the v4 structured_claims panels to more
   run shapes (apply, bench, probe, consent) before re-merging, or is v4
   sufficient for the first model?
4. Do we want the model artifact versioned/updated as a deliberate step, or
   regenerated on demand?
5. Is the onnx dependency acceptable in the real harness environment (train-time
   only if build-over-audit-runs; not needed for runtime inference)?
6. Which integration point, if any, should be wired in the real repo first:
   maybe_score_only (safest, no ordering), or
   maybe_score_and_order with USE_ADVISORY_ORDER on?
7. Who turns the flag on in the real repo, and what is the rollout story?

12. Decision: model-artifact story for the real repo
-----------------------------------------------------
Default recommendation: **in-tree model bundle, off-by-default, shipped as a
documented but disabled feature.**

Rationale: for a first merge of an advisory-only local model, the safest and
most reviewable shape is a committed model bundle behind flags, with the runtime
dependency kept to onnxruntime only. That avoids forcing every Harness install to
have the onnx train-time dependency just to exist, and it avoids depending on a
build step that scans audit runs as part of normal install/use. The model can
still be regenerated from audit runs whenever we want a refresh; the bundle is
just the distributable artifact.

Chosen approach
~~~~~~~~~~~~~~~~
- Artifact: a committed bundle of model.onnx + model_meta.json.
- Location in the real repo: harness/local_fit/model/ (next to the code that
  consumes it).
- Runtime dependency for the live harness: onnxruntime only (CPUExecutionProvider).
- Train-time dependency to regenerate the artifact: numpy + onnx + onnxruntime.
  Not required for the live harness at runtime.
- Default state when merged: OFF. Flags default to disabled. The layer is
  present in the repo but inert unless a maintainer explicitly turns it on.

Tradeoffs
~~~~~~~~~
In-tree bundle (chosen default)
- Pros: deterministic, versioned with the repo, no build-step dependency for
  users, easy to review as part of a merge, runtime stays lean (onnxruntime
  only).
- Cons: commits a small binary; regeneration is a deliberate out-of-band step
  (run train.run_pipeline / eval_over_seeds against the repo's audit runs and
  replace the bundle); the bundle can drift from the repo's audit data until
  deliberately refreshed.

Build-over-audit-runs (alternative, not chosen as default)
- Pros: artifact is always reproducible from the repo's own audit runs; no
  binary drift.
- Cons: forces the onnx train-time dependency into the build/CI environment;
  requires a defined build output location and a policy for when the artifact is
  rebuilt; adds a data-dependent build step to a repo that otherwise does not
  need one.

Shipped flag defaults (what would ship in the real repo)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- HARVEST_LOCAL_FIT_ENABLE: off by default (unset / "0").
- HARVEST_LOCAL_FIT_MODEL_DIR: unset by default; when ENABLE is on, the layer
  falls back inert if MODEL_DIR is missing or the artifact cannot be loaded.
- HARVEST_LOCAL_FIT_USE_ADVISORY_ORDER: off by default (unset / "0").
  Even when ENABLE is on, ordering is only nudged if this is also on.
- HARVEST_LOCAL_FIT_ADVISORY_TIEBREAK_WEIGHT: default 0.05 if set; does not
  apply unless USE_ADVISORY_ORDER is on.

This means a merge ships the layer, the hook, and the dispatch_hook prototype,
but nothing behaves differently for end users until a maintainer explicitly opts
in by setting the flags and pointing at a model bundle.

Minimal safe diff a maintainer would apply to merge off-by-default
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. Add the new files to the real repo:
   - harness/local_fit/ (the whole directory as added in this clone)
   - tests/test_local_fit_extract.py
   - tests/test_local_fit_model.py
2. Add harness/local_fit/model/ to .gitignore only if and when the artifact is
   produced by a build step; if the bundle is committed, do NOT ignore it.
3. Add a short note to the top-level README that an optional local-fit advisory
   layer exists and is off by default.
4. Do NOT wire the hook into any existing dispatch/panel/trust/apply code as
   part of this merge. The dispatch_hook prototype is illustrative only and is
   not called by any existing Harness module.
5. Confirm the test suite is green in the real repo's environment.
6. Turn the flags on only as a separate, deliberate step after the layer is in
   the tree and the model bundle is in place.

Refresh policy (once the bundle exists in the real repo)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- The bundle is refreshed by regenerating it from the repo's audit runs using
  the same train.run_pipeline or eval_over_seeds machinery in this layer, then
  replacing harness/local_fit/model/model.onnx + model_meta.json.
- A refresh should be accompanied by a new multi-seed eval report so the person
  opting back in can see whether the numbers still hold.
- The bundle version is tracked in model_meta.json model_version.

Decision made: for the first merge, ship an in-tree committed model bundle,
off-by-default, runtime dependency onnxruntime only, train-time dependency
numpy+onnx+onnxruntime only needed to regenerate. Do not wire into live
dispatch as part of the merge.
