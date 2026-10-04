# Jev decision-gate calibration evidence

Tracks [harness #168](https://github.com/Sovereign-Communication/harness/issues/168):
the 0.95 fail-closed threshold exceeds Jev's observed calibration band, so
every judgment escalates and the gate adds cost without signal.

**This change alters no active threshold.** It adds measurement tooling
(`harness/jev_calibration.py`), this evidence document, and hermetic tests.
Changing any live threshold remains the operator's explicit decision via
`freeze_jev_settings(min_confidence=...)` (`harness/config.py`) — see
"Operator path" below.

## Observed data

Judgments below come from the audit `jev-decide` tool (TypeSafe System One,
model `jev-1.13.0` family), not from an in-repo decision pack — the draft
spec's `decision_question_pack()` is not implemented yet, so these are the
best available observations of Jev confidence outputs in this workflow.

| Source | Disposition | Confidence | Destructive support |
|---|---|---|---|
| SCM #451 (mega-integration, 55 PRs) | needs_improvement | 0.27 | 0.41 |
| SCM #426 | escalate | 0.25 | — |
| SCM #427 | escalate | 0.42 | — |
| SCM #428 | needs_improvement | 0.14 | — |
| SCM #425 | proceed* | 0.90 | — |
| SCM #439 (x25519 2.0→3.0) | escalate | 0.81 | 0.55 |
| harness #159 (driver_core) | escalate | 0.89 | 0.55 |
| harness #167 | needs_improvement | 0.70 / 0.37 | — |
| BigEnergyCo #173 | needs_improvement | 0.83 / 0.26 | — |
| 2026-10-04 09:42 audit batch | escalate (9/9) | 0.14 – 0.54 | — |
| 2026-10-04 03:42 audit batch | escalate (all) | band-limited | — |

\* #425's `proceed @0.90` still escalated under the 0.95 gate — a clean,
low-risk docs PR that the gate could not clear.

**Read:** observed max ≈ 0.90 on a single outlier; the working band for
routine judgments tops out around **0.54**. Nothing observed comes within
0.4 of 0.95. At T=0.95 the gate is a constant function: escalate
everything, discriminate nothing.

## Why threshold-at-observed-max is not sufficient

`harness/jev_calibration.py::recommend_threshold` documents the method, but
the short version:

1. The observed max is a sample statistic, not a population guarantee.
   Future judgments can exceed it without being "more correct."
2. A threshold needs labeled ground truth: were past escalations correct?
   Would past proceeds have been safe? The spec's calibration criterion 3
   (false-proceed rate at threshold, on ≥20 labeled historical decisions)
   is the bar — the module reports the *discriminating band* [p50, p95] of
   observed confidence, i.e. the range where a threshold would actually let
   some judgments proceed while escalating the rest.
3. Picking inside (or outside) that band, and accepting the resulting
   false-proceed / false-escalate tradeoff, is a judgment call with
   operational consequences — it belongs to the operator, not to a
   measurement tool.

## Operator path

When the operator is ready to act on this evidence:

1. Review the calibration report (`analyze_judgments` over the labeled
   historical set) and the discriminating band.
2. Complete the labeled review from spec criterion 3 (false-proceed rate
   at the candidate threshold).
3. Freeze explicitly:
   `freeze_jev_settings(settings, jev_model="<observed-model-id>", min_confidence=<chosen>)`.
   The helper never invents a model id — omit `jev_model` to keep the
   current pin.

No threshold value in `jev.py`, `config.py`, or the audit skill is changed
by this PR. If the operator decides to hold 0.95, that decision and its
rationale belong on issue #168.

## Reproducing

```python
from harness.jev_calibration import analyze_judgments, format_report

records = [
    {"verdict": "needs_improvement", "confidence": 0.27,
     "destructive": 0.41, "disposition": "needs_improvement"},
    # ... more judgment records ...
]
print(format_report(analyze_judgments(records)))
```

Seed data: the table above (see `tests/test_jev_calibration_analysis.py`
for the machine-readable form).
