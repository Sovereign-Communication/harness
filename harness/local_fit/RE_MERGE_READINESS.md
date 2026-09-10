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

The headline advisory signal is `p_unusable` per candidate against a
threshold (default 0.6, inclusive): flagged models sort after same-tier
peers, never across the demotion boundary. The layer never replaces the
existing order; it only demotes within a tier when the feature flags are on
and the scores genuinely separate (the degenerate guard stands down
otherwise).

It is a non-LLM, CPU-only, ONNX-based local scorer. No PyTorch, no
transformers, no LLM, no GPU runtime.

2. Files in the clone (new, additive, minus the deleted prototype seam)
--------------------------------------------------------------------------
- harness/local_fit/__init__.py
- harness/local_fit/README.md
- harness/local_fit/RE_MERGE_READINESS.md
- harness/local_fit/schema.py
- harness/local_fit/extract.py
- harness/local_fit/config.py
- harness/local_fit/model_loader.py
- harness/local_fit/train.py
- harness/local_fit/features.py
- harness/local_fit/infer.py
- harness/local_fit/dispatch.py
- tests/test_local_fit_extract.py
- tests/test_local_fit_model.py
- tests/test_local_fit_features.py
- tests/test_local_fit_infer.py
- tests/test_local_fit_wiring.py
- tests/test_local_fit_package.py
- tests/test_local_fit_guard.py
- tests/test_local_fit_probe.py

The `hook.py` / `dispatch_hook.py` / `advisory.py` prototype seam was
deleted during review: it was unreachable from the live path, and its
weight-nudge ordering could cross demotion tiers, contradicting the
dispatch invariants. `dispatch.maybe_order_pool` is the single live
integration; `train.py` is intentionally NOT bound in `__init__` (numpy
at import time would break stdlib-only installs).

3. What existing Harness files are touched
-------------------------------------------
`harness/capability.py` only (`order_pool` consults
`local_fit.dispatch.maybe_order_pool` behind the flags; the hook is never
imported unless enabled), plus `pyproject.toml` (the `local-fit-train`
optional extra) and CHANGELOG/docs. Everything else is additive.

4. How the flags work
----------------------
Four environment variables control the layer:

- HARNESS_LOCAL_FIT_ENABLE=1|true|yes — turns the advisory layer on
- HARNESS_LOCAL_FIT_MODEL_DIR=path — directory containing
  `model_weights.json` and `model_meta.json` (stdlib path; legacy
  `model.onnx`-only dirs fall back to onnxruntime when importable)
- HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER=1|true|yes — advance OBSERVE to
  INFLUENCE: models with p_unusable >= threshold sort after same-tier
  peers (still advisory only; the demotion boundary is never crossed)
- HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD=float, default 0.6, inclusive

When HARNESS_LOCAL_FIT_ENABLE is off, the layer is completely inert: the
hook module is never imported, nothing loads, nothing scores, and the
baseline order is returned untouched. The flags are read dynamically so
tests can toggle them at runtime.

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
- With flags off: `dispatch.maybe_order_pool` returns the baseline order
  untouched without importing anything beyond stdlib; the hook module is
  never imported. No existing Harness behavior changes.
- With HARNESS_LOCAL_FIT_ENABLE on but MODEL_DIR missing/invalid: inert
  (scorer fails to load, stage degrades to OFF semantics).
- With both ENABLE and MODEL_DIR on (OBSERVE): scores are computed and
  returned for logging; order is still exactly baseline.
- With USE_ADVISORY_ORDER on as well (INFLUENCE): flagged models demote
  within their demotion tier only; unflagged relative order, the demotion
  boundary, and paid-tier price ordering are preserved exactly.

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

10. Current verification status (post review fixes)
----------------------------------------------------
- Review pass fixed: eval observed-map leakage (splits now enrich eval rows
  from the TRAIN map only), specialist-row corruption (model/raw/cost read
  from the conv dict, not the verdict), panel_failure role skew (trains as
  panel, matching dispatch), structured_required flag parity, generator
  inputs, seeded shuffling, ONNX width derivation, prototype-seam deletion,
  CI-blocking numpy import, and the dead test assertion.
- Tests cover: label rules (incl. exact severity priority), extraction
  basics, all-audits extraction, feature schema, train/dispatch parity pin,
  training + export smoke, ONNX runtime load + score, run-level split
  determinism + no-leakage, hold-out eval artifact + metadata + top1/top2 +
  stats-and-observed-from-train-only, all-audits train/eval, multi-seed
  eval range, degenerate-guard unit + fail-closed behavior, mock-free
  honest-behavior pins, mock-scorer reorder-mechanism pin, stdlib-only
  runtime import isolation, package binding + flag contract, and
  no-network-import guardrails.
- No network calls in the local_fit pipeline (asserted by tests).
- Multi-seed eval over the full clone dataset (9 v4 runs, 59 union rows),
  leakage-free, 5 seeds:
  - eval top1 range 0.647 - 0.867, mean 0.761 (majority baseline ~0.64)
  - eval top2 range 1.000 - 1.000
  - unusable P 0.50-1.00, R 0.46-1.00; usable_stop P 0.65-1.00, R 0.73-1.00
  - truncated 0/0 (none in this data)
- Leave-one-run-out routing study (9 folds, train-map calibration,
  dispatch-shaped features): net vs trivial worst-train-rate baseline --
  Spearman +0.730 vs +0.750 overall (+0.639 vs +0.670 excluding the one
  dominant failing model), precision@1 8/8 both, precision@2 7/7 both.
  Honest reading: on this corpus the net recapitulates ledger observed
  rates and adds no measurable routing lift; the degenerate guard
  correctly refuses to act on flat scores. INFLUENCE is safe to ship
  (fail-closed) but its value on larger, more varied corpora is unproven.
  The mock-scorer mechanism test proves the reorder path itself works
  when signal exists.

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
- HARNESS_LOCAL_FIT_ENABLE: off by default (unset / "0").
- HARNESS_LOCAL_FIT_MODEL_DIR: unset by default; when ENABLE is on, the layer
  falls back inert if MODEL_DIR is missing or the artifact cannot be loaded.
- HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER: off by default (unset / "0").
  Even when ENABLE is on, ordering changes only if this is also on, and
  then only as within-tier demotion of flagged models.
- HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD: default 0.6, inclusive.

This means a merge ships the layer, but nothing behaves differently for end
users until a maintainer explicitly opts in by setting the flags and
pointing at a model bundle. No model bundle ships with the layer: with no
MODEL_DIR the enabled path degrades to OFF semantics, so there is nothing
to enable accidentally.

Minimal safe diff a maintainer would apply to merge off-by-default
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. Add the new files to the real repo:
    - harness/local_fit/ (schema, extract, config, model_loader, train,
      features, infer, dispatch, README; no prototype seam)
    - tests/test_local_fit_*.py
2. Add the `local-fit-train` optional extra for regenerating artifacts.
3. Add a short note to the top-level README that an optional local-fit advisory
   layer exists and is off by default.
4. The live wiring (`capability.order_pool` -> `dispatch.maybe_order_pool`)
   ships in the same merge, guarded to bit-identical behavior unless all
   three INFLUENCE conditions hold (ENABLE + MODEL_DIR + USE_ADVISORY_ORDER).
5. Confirm the test suite is green in the real repo's environment.
6. Turn the flags on only as a separate, deliberate step after the layer is in
   the tree and a calibrated model bundle is in place (see the measured
   value bounds in section 10: retrain over profile-enriched data first).

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

13. Wiring addendum (supersedes parts of sections 6, 9, 12 above)
------------------------------------------------------------------
Status update after the wiring decision was approved. Where this addendum
disagrees with sections 6/9/12, this addendum wins.

What changed vs the original decision:

1. The layer IS now wired into live dispatch: capability.order_pool calls
   local_fit.dispatch.maybe_order_pool. The call is lazy and guarded: with
   HARNESS_LOCAL_FIT_ENABLE unset, the hook is never imported and never
   called, and the baseline order is returned untouched (pinned by tests).
2. The runtime dependency story changed for the better: inference is pure
   stdlib (infer.py reads model_weights.json). onnxruntime is NOT required at
   runtime; it remains a train-time-only dependency (optional extra
   local-fit-train) for regenerating artifacts. The enabled ordering path is
   tested to never import numpy/onnx/onnxruntime.
3. Flags were renamed HARVEST_LOCAL_FIT_* -> HARNESS_LOCAL_FIT_* (no
   consumers existed; legacy names are tested inert).
4. The graduated gating replaced the old single tiebreak nudge:
   OFF -> OBSERVE (score-and-log, order untouched) -> INFLUENCE
   (HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER=1: p_unusable >=
   HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD, default 0.6 inclusive, sorts the
   model after its peers WITHIN the same strike-demotion tier). The old
   HARNESS_LOCAL_FIT_ADVISORY_TIEBREAK_WEIGHT flag and the prototype
   candidate-list hook are deleted (the weight-nudge could cross tiers,
   contradicting these invariants).
5. Known skew is documented in README (Training/serve parity section): the
   training extractor zero-fills declared_*/free_tier while dispatch reads
   real profiles, and the ledger has no truncation events. Retrain over
   profile-enriched data before relying on INFLUENCE ordering.

Wiring contract (what reviewers should verify):

- capability.order_pool's only behavioral change when the flag is off: none
  (the hook is not called; suite proven green).
- With the flag on but no valid artifact: OBSERVE degrades to OFF semantics
  (empty scores, baseline order).
- With INFLUENCE on: only flagged models move; they move after same-tier
  peers only; the demotion boundary, unflagged relative order, and paid-tier
  price ordering are all preserved exactly (property tests pin each).
- Every failure mode (crash, bad artifact, scorer exception) degrades to the
  baseline order. order_pool still never raises.
