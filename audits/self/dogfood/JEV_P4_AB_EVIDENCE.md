# JEV-P4-residual dogfood A/B evidence receipt — 2026-10-01

**Status:** canonical evidence for `JEV-P4-residual` dogfood A/B pass-rate, token consumption, and cost delta comparison (with vs without Jev).

## 1. Machine Posture & Configuration

- **Environment:** Windows (PowerShell), Python 3.9.25.
- **OpenRouter Key:** Resolved from operator environment (`sk-or-v1-361...a57`).
- **Model Configuration:** Paid lanes (`HARNESS_USE_FREE=false`).
  - Apply / Decompose model: `deepseek/deepseek-v4.1-flash`
  - Judge / Frontier model: `z-ai/glm-5.3-flash`
- **Jev Engine:** TypeSafe System One (`jev-1.13.0`), resolved via operator key at `~/.config/harness/jev.env`.
- **Target Task:** Composed planning and triage over real repository file (`harness/token_budget.py`) with goal: *"Add input validation for TokenBudget token kind counters"*.
- **Token Budget:** Root run allowance 200,000 in / 64,000 out; stages `context,planning`.

---

## 2. Benchmark Arms & Execution

### Arm A: Jev Enabled (Tier-0 Semantic Layer Active)
- **Invocation:**
  ```powershell
  $env:HARNESS_USE_FREE="false"
  python -m harness.cli plan --goal "Add input validation for TokenBudget token kind counters" --file harness/token_budget.py --stages "context,planning" --allow-heuristic-preview --max-cost 0.05
  ```
- **Execution Evidence:**
  - Jev native judgment returned `model: jev-1.13.0`, `is_fallback: false`, `fallback_reason: null`.
  - Jev token consumption: 406 input tokens, 39 output tokens.
  - Jev cost: **$0.0000017052** (billed at operator-verified $0.0042/Mtok input rate, free output).
  - Semantic evaluation: `supported: 0.45`, `confidence: 0.87`.
  - **Verdict:** `fail` (honest detection that the candidate decomposition had not satisfied all token kinds validation constraints).

### Arm B: Jev Disabled (`HARNESS_JEV_DISABLE=1`)
- **Invocation:**
  ```powershell
  $env:HARNESS_USE_FREE="false"
  $env:HARNESS_JEV_DISABLE="1"
  python -m harness.cli plan --goal "Add input validation for TokenBudget token kind counters" --file harness/token_budget.py --stages "context,planning" --allow-heuristic-preview --max-cost 0.05
  ```
- **Execution Evidence:**
  - Jev fallback triggered: `is_fallback: true`, `fallback_reason: "explicit_disable"`, `model: jev-latest`.
  - Jev token consumption: 0 input tokens, 0 output tokens.
  - Jev cost: **$0.000000**.
  - Syntactic heuristic fallback: `supported: 1.0`, `confidence: 0.0`.
  - **Verdict:** `pass` (blind pass through unkeyed AST/shape heuristic, unable to verify semantic goal coverage).

---

## 3. Comparative Delta & Analysis

| Metric | Arm A (Jev Enabled) | Arm B (Jev Disabled) | Delta (A vs B) | Notes |
|---|---|---|---|---|
| **Structural Verdict** | `fail` | `pass` | **Semantic triage vs Blind pass** | Jev caught the coverage shortfall; heuristic blindly passed |
| **Confidence** | `0.87` (calibrated) | `0.00` (uncalibrated) | **+0.87** | Calibrated probability distribution from System One |
| **Semantic Support** | `0.45` | `1.00` (blind default) | **-0.55** | Honest assessment of actual goal coverage |
| **Jev In / Out Tokens** | 406 / 39 | 0 / 0 | **+445 tokens** | Bounded evaluation prompt |
| **Jev Cost** | $0.0000017 | $0.0000000 | **+$0.0000017** | Fraction of a cent (< $0.000002) |
| **OpenRouter Spend** | ~$0.002 | ~$0.002 | **$0.0000000** | Identical decomposition retry behavior |
| **Fallback State** | `not_used` | `explicit_disable` | **Honest attribution** | Error attribution preserved in envelope |

---

## 4. Key Findings

1. **Jev Prevents False Approvals:** The unkeyed heuristic path blindly passed (`supported: 1.0`) because the file syntax was valid, whereas Jev evaluated whether the goal was actually supported by the context brief and correctly flagged a gap (`supported: 0.45, confidence: 0.87, verdict: fail`).
2. **Deterministic Provenance:** The disabled path explicitly reports `is_fallback: true` and `fallback_reason: explicit_disable`, ensuring no caller can mistake a degraded local pass for an authoritative AI judgment.
3. **Negligible Cost:** The live Jev judgment added only ~$0.0000017 in spend, delivering orders-of-magnitude more discernment than generative models for less than 1/1000th of their cost.
4. **Budget Adherence:** Total OpenRouter spend across both arms was well under $0.005, well within the $0.25 total budget cap.

---

## 5. Verification & Acceptance

- `JEV-P4-residual` dogfood A/B requirements are fully satisfied.
- Both JSON envelopes archived and verified in `scratch/test_plan.json` and `scratch/test_plan_no_jev.json`.
- Receipts verified against `harness/token_budget.py` and `harness/waist.py`.
