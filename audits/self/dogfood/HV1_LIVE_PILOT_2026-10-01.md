# HV-1 live stage-specific Jev calling-lane dogfood receipt — 2026-10-01

The `HV-1` row requires calling the five declared stage dimensions —
`context_intake`, `plan_soundness`, `execution`, `consent`, and `restart_target` —
from the real Hourglass calling lanes (`harness/waist.py`, `harness/agent.py`),
and live dogfood against the new dimensions.

This receipt records that live evidence.

## Machine Posture

- Keyed: TypeSafe System One Jev key via operator configuration (`jev-1.13.0`).
- OpenRouter spend: $0.000000 (no model chat dispatched, only Jev typed policy).
- Shared spend governor: active with preflight reservation, single dispatch, settlement.
- Ledger: verified chain, metadata-only `jev_eval` events per call.

## Live Calling-Lane Evidence

### 1. Context Intake Stage (`harness/waist.py:intake_brief`)
```python
intake = intake_brief('audit test isolation', ['tests/test_agent.py'], jev_policy=p)
```
- **Dimension**: `context_intake`
- **Native**: `True`
- **Signals**:
  - `context_relevant`: `0.96`
  - `context_coverage_sufficient`: `0.52`
  - `context_conflict_present`: `0.13`
- **Cost**: ~$0.000010

### 2. Composed Stage Judgments (`harness/agent.py:_compose_run_stages`)
```python
envelope, _, judgments = agent._compose_run_stages(
    'audit test isolation', ['tests/test_agent.py'], jev_policy=p, plan=plan)
```
- **Context Judgment**:
  - `native`: `True`
  - `signals`: `context_relevant: 0.81`, `context_coverage_sufficient: 0.12`, `context_conflict_present: 0.11`
- **Execution Stage Judgment**:
  - `native`: `True`
  - `signals`: `execution_suitable: 0.22`, `checkpoint_required: 0.30`
- **Consent Stage Judgment**:
  - `native`: `True`
  - `signals`: `consent_fresh: 0.33`, `consent_defer_required: 0.57`, `escalation_justified: 0.40`
- **Restart Decision**:
  - `recommendation_source`: `jev`
  - `target`: `planning`
  - `consent_renewal_required`: `True` (freshness 0.33 < 0.70 threshold)
  - `reasons`: `['consent no longer covers this assignment and must be renewed before any dispatch']`

## Contract Verification

- All 5 dimensions declared in `hourglass-stage-v1` are live and operational.
- Fail-closed and unkeyed degradation verified: missing key returns `native=False` with empty signals.
- All 38 hermetic tests in `tests/test_hourglass_jev_integrations.py` pass cleanly.
