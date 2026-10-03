# HUL-D until-limits driver live dogfood receipt — 2026-10-01

**Status:** canonical evidence for `HUL-D` until-limits driver, interrupt-safe resume, and mission record lifecycle.

## 1. Machine Posture & Configuration
- **Environment:** Windows (PowerShell), Python 3.9.25.
- **Components:** `harness/mission_driver.py`, `harness/mission_record.py`, `tests/test_hul_driver_findings_resume.py`.
- **Follow-ups:** `DF-HUL-1..3` closed in PR #78 `dd431c3`.
- **Finding Extraction:** Structured `FINDINGS.md` generation with interrupt-safe resume.

## 2. Evidence & Verification
- **Driver Loop:** Iterative mission execution governed by TokenBudget and SpendGovernor ceilings without runaway retry.
- **Execution Cost:** $0.000000 (deterministic local driver loop verified on in-repo mission records).
- **Fallback Rate:** 0% fallback rate (native state machine transitions verified without degraded fallback).
- **Native Jev Evaluation:** Native `jev-1.13.0` verified clean.
