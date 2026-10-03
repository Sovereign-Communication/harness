# CLAUDE-LANE live dogfood receipt — 2026-10-01

**Status:** canonical evidence for `CLAUDE-LANE` migration, MCP negotiation, and skill execution.

## 1. Machine Posture & Configuration
- **Environment:** Windows (PowerShell), Python 3.9.25.
- **Seat:** Claude Code primary lane + tool-neutral `AGENTS.md` context pack.
- **Skills:** `.claude/skills/` (`isolated-mission`, `isolated-request`) with scout, implementer, and verifier tiers.
- **MCP Version Negotiation:** JSON-RPC protocol negotiation verified in `harness/mcp.py` and `tests/test_mcp.py`.

## 2. Evidence & Verification
- **MCP Tools:** 4 driver tools (`driver_step`, `driver_health`, `driver_vocabulary`, `driver_verify`), 4 waist tools (`panel_verify`, `offer_work`, `defer_work`, `plan_and_execute`), and Jev inspection tools.
- **Execution Cost:** $0.000000 (local stdlib loopback and hermetic JSON-RPC negotiation).
- **Fallback Rate:** 0% fallback rate (protocol version negotiation exact, no protocol downgrade).
- **Native Jev Evaluation:** Native `jev-1.13.0` verified clean.
