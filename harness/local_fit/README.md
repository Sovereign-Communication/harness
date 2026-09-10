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
- `model_loader.py` — thin loader so the advisory path can import the scorer
  without pulling in training code.
- `config.py` — feature-flag entrypoint (`HARNESS_LOCAL_FIT_ENABLE` and
  `HARNESS_LOCAL_FIT_MODEL_DIR`). Read dynamically so tests can toggle it.
- `advisory.py` — advisory entrypoint: `maybe_score_candidates` and
  `score_one`.
- `hook.py` — flag-gated advisory hook prototype: `score_candidates`,
  `apply_advisory_tiebreak`, `explain`.
- `dispatch_hook.py` — illustrative wrapper showing where a real dispatch path
  would consult the advisory. Inert unless flags are on.

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
- `HARNESS_LOCAL_FIT_MODEL_DIR` — directory containing `model.onnx` and
  `model_meta.json`.
- `HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER` — set to `1`, `true`, or `yes` to
  also apply the advisory as a small tiebreak on top of an existing
  `existing_order_key`. Still advisory-only.
- `HARNESS_LOCAL_FIT_ADVISORY_TIEBREAK_WEIGHT` — float, default 0.05. Size of
  the advisory nudge when ordering is enabled.

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
- The split is deterministic given a seed.
- Statistics (means/stdevs) are computed from train rows only and applied to
  both train and eval rows, so eval cannot leak through normalization.
- The exported `model_meta.json` includes an `eval` block with the frozen
  train/eval split, row counts, per-class metrics, confusion matrix, and
  top-1/top-2 accuracy.

### Multi-seed eval range on current clone data

5 seeds, train_ratio 0.75, over the 9 v4 runs (59 union rows):

```
eval top1 range:   0.833 - 0.941   (mean 0.883)
eval top2 range:   1.000 - 1.000
train top1 range:  0.800 - 0.905
train top2 range:  1.000 - 1.000
```

Per-class (across seeds):
- `unusable`: P=1.000 across seeds; R ranges ~0.57-0.83 (small eval support).
- `usable_stop`: R=1.000 across seeds; P ranges ~0.79-0.92.
- `truncated`: no truncated seats in this dataset, so 0/0 per seed.

The model reliably separates usable_stop and unusable seats; truncated is
currently untested by this data.

## Integration point (illustrative, flag-gated)

`dispatch_hook.maybe_score_and_order(candidates)` is an illustrative wrapper
that shows where a real dispatch path would consult the advisory:

1. When flags are off, it is inert: attaches empty advisory dicts and returns
   candidates unchanged.
2. When enabled, it attaches advisory scores to each candidate.
3. When `HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER` is also on, it re-sorts by a
   small nudge on top of each candidate's `existing_order_key`. The existing
   order is never discarded.

There is also `maybe_score_only(candidates)` for the safest integration: attach
advisory scores without touching any ordering.

This module does not touch any existing Harness routing math. It is a prototype
for re-merge discussion.

## Development notes

- Exported model uses ONNX opset 26 for compatibility with onnxruntime 1.29.
- Inference uses `onnxruntime` `CPUExecutionProvider` only.
- This code is developed in an isolated clone and is intended to be re-merged
  only after separate verification.
- Dependencies for training/export/eval: `numpy`, `onnx`, `onnxruntime`.
- Runtime inference dependency: `onnxruntime` only.
- `onnx` is needed only to build/export the model artifact, not for runtime
  inference.
