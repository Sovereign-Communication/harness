# DRV-1 & DRV-2 Live Protocol & Paid-Cheap Dogfood Receipt — 2026-10-18

Verification receipt for the live execution of the `harness driver` adapter (`DRV-1`)
and `driver_core` extraction/synthetic input backends (`DRV-2`) against a running service.

## 1. Environment & Service Execution
- Service: Native `driver_core.server.serve` on local loopback `http://127.0.0.1:56669`
- Bearer Auth: Validated per-process token (RFC-compliant bearer header)
- Service Health: `status=up`, `keyed=True`, `jev_model=None`
- Action Vocabulary: `driver-core-actions@1.0.0` (14 closed-set actions)
- Declared Schemas: `['driver-core-screen', 'driver-core-cli', 'driver-core-dom']`
- Write & Native Input: `allow_write=True`, `native_input=True` (Windows OSAL `focus` capability registered)

## 2. Live Perception & Extraction Agreement
- Target: `harness-driver-live` (class: `cli`)
- Extraction Quorum: 2 slots answered with 100% agreement across all fields (`exit_code`, `stdout`, `stderr`)
- Agreement Ratio: `1.0` (unanimous consensus)
- Structured Capture Fingerprint: `ae423d1ce011f071`

## 3. Live Native Jev Semantic Decision Tier
- Jev Model: `jev-1.13.0`
- Execution Status: `native=True` (0% fallback rate; non-fallback live Jev execution)
- Calibrated Confidence: `0.9`
- Input Tokens: `844`
- Output Tokens: `172`
- Measured Decision Cost: `$0.00003545` (cheap paid rate at $0.042/Mtok)
- Guards Evaluated:
  - `state_is_stable`: `0.96`
  - `a_blocking_choice_is_required`: `0.05`
- Action Distribution:
```json
{
  "call_read_tool": 0.0,
  "click": 0.0,
  "delete_file": 0.0,
  "focus": 0.0,
  "no_action": 0.91,
  "observe": 0.06,
  "press_key": 0.0,
  "read_dom": 0.0,
  "read_value": 0.0,
  "run_probe": 0.03,
  "scroll": 0.0,
  "submit_irreversible": 0.0,
  "type_text": 0.0,
  "write_file": 0.0
}
```
- Recommended Action: `no_action`
- Step Outcome: `ok=False`, `stopped_at=decision`, `reason=no_action_recommended`
- Envelope Detail: `the correct response is to change nothing`

## 4. Protocol Invariants & Refusal Enforcement
1. Refusal-as-200 Contract: The driver returned HTTP 200 with `ok: false` and closed-set reason `no_action_recommended` without raising transport exceptions.
2. Route Isolation: Unknown route returned HTTP 404.
3. Bearer Auth Security: Invalid token returned HTTP 401.
4. Transport Failures: Sealed behind `PerceptionUnavailable` without conflating decision refusals.
5. OSAL Isolation: `driver_core` maintains absolute zero imports of `harness` or third-party frameworks.

## 5. Audit Chain Verification
- Total Audit Records: `0`
- Audit Chain Status: `VERIFIED`
- Budget Reserve & Remaining: `$5.000000` remaining
