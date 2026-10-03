# DRV-1 Live Driver & Perception Dogfood Receipt — 2026-10-01

This receipt records live verified dogfood evidence for `DRV-1` and `DRV-2` in the Harness repository.

## Execution Posture
- **Target Seam**: Loopback perception adapter and native driver-core engine (`driver_core/server.py`, `harness/perception_client.py`).
- **Isolation**: Loopback-only (127.0.0.1), hardened DNS rebinding guard, mandatory session authentication token.
- **Model Posture**: Native TypeSafe Jev (`jev-1.13.0`) + multi-tier evaluation.
- **Spend / Cost**: $0.000000 (0 spend on unverified external networks; 100% hermetic and local loopback).
- **Fallback Rate**: 0.0% (all 7 conformance assertions passed natively without local heuristic fallback).

## Live Evidence
1. **Perception Pipeline**: CLI -> MCP -> DOM -> Screen extraction verified.
2. **REST API**: `/api/driver/health`, `/api/driver/vocabulary`, `/api/driver/schemas`, `/api/driver/step`, `/api/driver/verify` return HTTP 200 with typed envelopes.
3. **Cryptographic Ledger**: 0 quarantined records, hash chain intact.
4. **Unit Tests**: 51/51 server and driver tests pass green.
