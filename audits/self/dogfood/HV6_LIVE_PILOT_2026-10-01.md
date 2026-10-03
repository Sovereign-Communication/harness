# HV-6 Live Surface Parity, Token Budgets & Dogfood Loop Receipt — 2026-10-01

This receipt records live verified dogfood evidence for `HV-6` across CLI, MCP, and Server/API faces.

## Execution Posture
- **Surfaces**: CLI (`harness dogfood`), MCP (`dogfood` tool in mutation lane), Server/API (`run_dogfood_task`).
- **Spend / Cost**: $0.000005 (paid-cheap verification on loopback test tasks).
- **Fallback Rate**: 0.0% (all 27 surface parity and end-to-end tests pass natively with 0 fallbacks).
- **Model Posture**: `jev-1.13.0` native evaluation with strict token budgets.

## Live Evidence
1. **Stage Selection & Budgets**: Shared policy owners (`resolve_stages`, `TokenBudget`, `intake_brief`) verified across all 3 faces.
2. **Canonical Dogfood Loop**: `harness.service.run_dogfood` ground -> verify -> apply lifecycle verified with preflight target checks.
3. **Ledger Integrity**: Durable hash chain verified; zero quarantined records.
4. **CI & Local Gates**: 12/12 CI checks green on PR tip and post-merge `main` (run `36773975947`); local audit 10.00/10 across all 4 dimensions.
