# Local model-fit advisory layer for Harness

This directory contains an **opt-in, advisory-only** local model-fit layer.

It does **not** replace Harness' existing capability/reliability routing and it
does **not** change ceilings, consent, deferral, continuation, trust, or verify
gate behavior. It only provides an advisory score that may influence candidate
ordering when enabled.

## What it does

For each candidate model seat in a dispatch decision, the layer scores the
likelihood of three seat-level outcomes:

- `usable_stop` — the seat finishes cleanly with parseable output
- `truncated` — the seat output is truncated
- `unusable` — the seat errors, is invalid, or is missing required content

The headline advisory value is `usable_stop - truncated - unusable`, used as a
small tiebreak nudge on top of existing ordering. The layer never replaces the
existing order; it only nudges it when the feature flag is on.

## Components

- `schema.py` — label definitions, feature schema, categorical vocabularies,
  and the `SeatRow` contract.
- `extract.py` — read-only extractor that emits one seat row per model attempt
  from existing run JSON under `audits/`. Includes `all_run_files()` for
  scanning every `audits/*/_runs/*/*.json` in the repo. Defensive against
  schema drift.
- `train.py` — builds a feature vector from canonical feature order, trains a
  small 2-layer feedforward net, exports it to ONNX opset 26 + metadata JSON,
  and provides the `LocalScorer` inference wrapper. Also includes hold-out
  eval and multi-seed evaluation.
- `features.py` — the single canonical feature-vector builder (pure stdlib),
  shared by training and inference so the two paths cannot drift.
- `infer.py` — pure-stdlib runtime scorer: loads `model_weights.json` +
  `model_meta.json` and runs the same forward pass as the exported ONNX graph
  with no third-party imports.
- `model_loader.py` — thin loader so the advisory path can import the scorer
  without pulling in training code. Prefers the stdlib artifact
  (`model_weights.json`); falls back to the ONNX scorer only when
  onnxruntime is importable.
- `config.py` — feature-flag entrypoint (`HARNESS_LOCAL_FIT_ENABLE` and
  `HARNESS_LOCAL_FIT_MODEL_DIR`) plus scorer loading. Read dynamically so
  tests can toggle it.
- `dispatch.py` — **the live integration**: `maybe_order_pool` is what
  `capability.order_pool` calls. Three-stage gating (OFF / OBSERVE /
  INFLUENCE) with tier-preserving demotion; see below.

## Feature set

Features are pre-dispatch only. No outcome data leaks into the input vector.

- task features: task_type, seat_role, structured_output_required,
  max_tokens_requested, reasoning_effort, prompt_chars,
  source_window_attached, claims_count, convergence_expected, is_iterative
- model features: model_id_hash, free_tier, declared context/structured/reasoning
  stats, observed usability/truncation/unusable rates, observed response-length
  stats, observed sample count
- interaction features: prompt_tokens_est_over_context,
  max_tokens_over_mean_resp, structured_need_vs_declared_json,
  iterative_vs_truncation_rate

Categorical features are one-hot encoded against fixed vocabularies.
Numeric features are z-scored using stats persisted in the model metadata.

## Train/serve parity

Training and dispatch-side scoring share exactly one feature-dict builder
(`features.build_dispatch_features`) and one vector builder
(`features.build_feature_vector`). A pinning test
(`tests/test_local_fit_features.py`) fails if the two call sites drift.

### Known skew (read before enabling INFLUENCE mode)

- **declared_* / free_tier**: the training extractor historically zero-filled
  these (run JSON does not carry profile data; `build_observed_map` hardcodes
  them). Dispatch fills them from the real `CapabilityProfile`. For any model
  whose declared context/JSON/reasoning matters, dispatch-time inputs sit
  outside the trained distribution. **Retrain over audit runs enriched with
  profile data before relying on INFLUENCE ordering.**
- **truncation_rate**: the ledger records no truncation events today, so the
  dispatch-side feature is always 0.0; OBSERVE-mode scores still reflect the
  usable/unusable signal, which the ledger does capture.
- **prompt_chars / max_tokens**: pool ordering happens before the prompt is
  built; dispatch uses the extractor's unknown-run defaults (0 chars, 2048
  max tokens, reasoning "auto"). Task type, seat role, declared profile, and
  observed ledger rates — the strongest signals — are all genuine at dispatch
  time.

## Label rules

Labels are mutually exclusive and severity-ordered:

`unusable > truncated > usable_stop`

- `unusable`: finish_reason == "error", or status in {error, byok,
  invalid_output}, or content missing when required.
- `truncated`: finish_reason == "length", or an explicit truncated flag on the
  seat.
- `usable_stop`: finish_reason == "stop", content present, and parseable when
  structured output is required.

## Environment flags

- `HARNESS_LOCAL_FIT_ENABLE` — set to `1`, `true`, or `yes` to enable the
  advisory layer.
- `HARNESS_LOCAL_FIT_MODEL_DIR` — directory containing `model_weights.json`
  and `model_meta.json` (stdlib runtime path; a legacy `model.onnx` alone
  falls back to onnxruntime when importable).
- `HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER` — set to `1`, `true`, or `yes` to
  advance from OBSERVE to INFLUENCE: likely-unusable models (per the threshold
  below) sort after their same-tier peers in live pool ordering. Still
  advisory-only; the strike-demotion boundary is never crossed.
- `HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD` — float, default 0.6, inclusive.
  Models with p_unusable >= this are flagged in INFLUENCE mode.

When disabled, the layer is inert and does not affect any existing behavior.

## Data source

The layer trains/evaluates only over run JSON files found under
`audits/*/_runs/*/*.json`. It does **not** train on ledger/summary files.

In this clone today the only such data is `audits/scmessenger/_runs/v4`
(9 run files, 59 seat rows, all `structured_claims`).

`audits/self/round2_scores.json` is an **audit scores/summary ledger**, not a
run JSON with `panel_results`/seat shapes. It is intentionally excluded from
extraction and must not be used as a seat-extraction source.

## All-audits scan

`extract.all_run_files(root="audits")` returns every run JSON under
`audits/*/_runs/*/*.json`. It is the single top-level entry point for
"train over everything that exists in the clone today". It is generic and not
hardcoded to v4.

The extractor itself already supports multiple run shapes
(verify_panel, structured_claims, apply, bench, probe, consent) via
`guess_task_type`; only structured_claims exists in this clone right now.

## Hold-out evaluation

The pipeline includes a run-level hold-out evaluation so the exported artifact
carries frozen evaluation evidence, not just a smoke test.

- `split_files(files, train_ratio, seed)` — deterministic split of run files
  into train and eval groups. Seats from the same run never appear in both.
- `run_eval(train_files, eval_files, out_dir, ...)` — trains on train runs,
  evaluates on held-out runs, exports the ONNX artifact + metadata (with eval
  frozen inside) + a human-readable `eval_summary.txt`.
- `eval_metrics(probs, y, label_index, class_names)` — per-class
  precision/recall/f1, overall accuracy, and top-1 / top-2 best-guess
  accuracy, plus a confusion matrix.
- `eval_over_seeds(all_files, out_dir, train_ratio, seeds, ...)` — runs
  hold-out eval over multiple seeds and returns a compact range report with
  per-seed metrics and union row counts by task_type/label.

Key properties:
- Split is by *run file*, not by row, so there is no within-run leakage.
- The split is deterministic given a seed (including the shuffle stream).
- Statistics (means/stdevs) are computed from train rows only and applied to
  both train and eval rows, and eval rows are enriched with the TRAIN
  observed map only -- so eval cannot leak through normalization or through
  label-derived observed rates. (An earlier revision built each split's
  observed rates from its own labels and measured ~0.88; that number was
  the leak, not the model.)
- The exported `model_meta.json` includes an `eval` block with the frozen
  train/eval split, row counts, per-class metrics, confusion matrix, and
  top-1/top-2 accuracy.

### Multi-seed eval range on current clone data

5 seeds, train_ratio 0.75, over the 9 v4 runs (59 union rows), leakage-free:

```
eval top1 range:   0.647 - 0.867   (mean 0.761)
eval top2 range:   1.000 - 1.000
train top1 range:  0.800 - 0.905
train top2 range:  1.000 - 1.000
```

Per-class (across seeds):
- `unusable`: P ranges 0.50-1.00, R ranges 0.46-1.00 (small eval support).
- `usable_stop`: P ranges 0.65-1.00, R ranges 0.73-1.00.
- `truncated`: no truncated seats in this dataset, so 0/0 per seed.

The model beats the majority baseline (~0.64) modestly on extract
features; truncated is untested by this data. Dispatch-time ranking
quality is a separate, harder question -- see "Known skew" and the
degenerate-artifact guard: when scores cannot separate candidates, the
layer stands down instead of reordering on noise.

## Live integration: capability.order_pool

The advisory layer is wired into `order_pool` in `harness/capability.py` (the
single ordering choke point used by both the apply lane and the panel lane via
`ordered_pool`). The call is guarded and lazy:

1. With `HARNESS_LOCAL_FIT_ENABLE` unset (default), the hook is not imported
   and not called; the baseline order is returned untouched.
2. With ENABLE + `HARNESS_LOCAL_FIT_MODEL_DIR` set (**OBSERVE**), candidates
   are scored via the stdlib scorer and the scores are returned for logging;
   the order still equals the baseline exactly.
3. With `HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER=1` also set (**INFLUENCE**),
   models whose `p_unusable` >= `HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD`
   (default 0.6, inclusive) sort after their peers **within the same demotion
   tier**. The hook cannot cross the strike-demotion boundary, cannot reorder
   unflagged models among themselves, and cannot promote a flagged model.
   Paid-tier price ordering is respected the same way: a flagged model moves
   only behind same-tier peers. One stderr line is printed when a reorder
   actually happens.

Any error anywhere in the hook (missing artifact, corrupt weights, scoring
exception) degrades to the baseline order. Routing never breaks.

### Operationally recommended rollout

1. Ship with flags off (the default). Confirm the full hermetic suite is green.
2. Train an artifact from your own audit runs (see Training above) and set
   ENABLE + MODEL_DIR to run in OBSERVE for a few days; compare logged scores
   against actual seat outcomes.
3. Retrain over profile-enriched data (see Known skew) before enabling
   USE_ADVISORY_ORDER, and start with a high threshold (e.g. 0.9) and lower it
   as evidence accumulates.

## Development notes

- Exported model uses ONNX opset 26 for compatibility with onnxruntime 1.29.
- Runtime inference is **pure stdlib**: the scorer reads `model_weights.json`
  and needs no third-party packages, preserving the repo's zero-dependency
  identity even when the layer is enabled. A test pins this by asserting
  numpy/onnx/onnxruntime never appear in `sys.modules` while scoring.
- This code is developed in an isolated clone and is intended to be re-merged
  only after separate verification.
- Dependencies for training/export/eval only: `numpy`, `onnx`, `onnxruntime`
  (installable via the `local-fit-train` optional extra). Runtime needs none.
- ONNX remains available as a fallback artifact format for environments that
  already have onnxruntime.
