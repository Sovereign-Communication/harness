# JEV-P3 Calibration Analysis Receipt — 2026-10-18

Verification receipt for the delivery of Jev calibration analysis surfaces (CLI, MCP, Server, and `jev-freeze --calibrate` wiring) over `harness/jev_calibration.py`.

## 1. CLI Execution (`harness jev-calibration`)

Command: `python -m harness.cli jev-calibration --json`
Output:
```json
{
  "n": 1135,
  "confidence": {
    "n": 1135,
    "min": 0.0,
    "max": 1.0,
    "mean": 0.375,
    "p50": 0.40,
    "p95": 0.89
  },
  "by_disposition": {
    "fail": {
      "n": 936,
      "confidence": {
        "n": 936,
        "min": 0.0,
        "max": 0.93,
        "mean": 0.305,
        "p50": 0.23,
        "p95": 0.80
      }
    },
    "pass": {
      "n": 199,
      "confidence": {
        "n": 199,
        "min": 0.0,
        "max": 1.0,
        "mean": 0.706,
        "p50": 0.76,
        "p95": 1.0
      }
    }
  },
  "destructive_flags": 0,
  "recommendation": {
    "method": "discriminating band [p50, p95] of observed confidence; threshold-at-observed-max is not sufficient (see docstring). Final choice is the operator's via freeze_jev_settings.",
    "observed_max": 1.0,
    "discriminating_band": [0.40, 0.89],
    "warning": "Do not freeze a threshold on this band alone: pair it with a labeled review of past escalations (spec criterion 3) before calling freeze_jev_settings(min_confidence=...)."
  },
  "notes": [
    "1135 judgment records analyzed.",
    "0 record(s) met the destructive support floor (0.5) and would escalate at any confidence."
  ]
}
```

## 2. Integrated Freeze Wiring (`harness jev-freeze --calibrate`)

Command: `python -m harness.cli jev-freeze --calibrate`
Output:
```text
[jev-freeze] calibration recommended min_confidence: 0.400
[jev-freeze] preview only (no write); pass --persist to pin
{
  "status": "preview",
  "command": "jev-freeze",
  "freeze": {
    "jev_model": "jev-latest",
    "min_confidence": 0.4,
    "freeze_keys": ["min_confidence"],
    "persisted": false,
    "jev_model_is_pinned": false
  },
  "note": "preview only -- nothing was written; re-run with --persist to freeze"
}
```

## 3. MCP Tool (`jev_calibration`)
- Tool name: `jev_calibration` (observe lane).
- Inspects recent ledger judgments and returns confidence distribution, breakdown by disposition, and recommended threshold band.

## 4. Server REST API (`/api/jev-calibration`)
- `GET /api/jev-calibration`: returns 200 with JSON calibration analysis from the running ledger.

## 5. Architectural Invariants Preserved
- Measurement only: calibration analysis never silently mutates active thresholds.
- Threshold selection remains operator-owned via `freeze_jev_settings`.
