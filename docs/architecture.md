# Architecture

Harness is a dependency-light Python package with three surfaces:

- `harness.cli`: command-line parsing and presentation;
- `harness.mcp`: JSON-RPC/MCP parsing and presentation;
- library modules: policy, orchestration, transport, and evidence.

## Ownership

- `config.py`: settings and model-pool defaults.
- `validation.py`: shared trust-boundary validation.
- `spend.py`: cost ceilings, pricing, BYOK, and model discovery.
- `chat.py`: model transport payloads and output usability.
- `panel.py`: panel/judge verification.
- `convergence.py`: deterministic structured-claim tally and specialist lane.
- `consent.py`: consent probes and renewal.
- `apply.py`: request preparation, model dispatch, rotation, and round orchestration.
- `apply_gate.py`: candidate writes, verification, previews, escalation gates, and failed-run transaction policy.
- `apply_state.py`: frozen request data and mutable per-run state for the apply engine.
- `batch.py`: multi-file orchestration over the apply engine.
- `prompts.py`: prompt and response contracts.
- `filesafety.py`: atomic writes, backups, and verification execution.
- `continuation.py`: persisted continuation authority and gate identity.
- `ledger.py`: hash-chained evidence.
- `results.py`: apply result vocabulary and exit-code policy.
- `session.py`: dependency composition.
- `mcp.py`: MCP JSON-RPC framing, request lifecycle, tool contracts, boundary validation, and engine dispatch.

Interfaces translate input into shared request policy; they do not reimplement
engine or safety behavior. MCP and library callers pass through the shared
validators in `validation.py` before engine dispatch. Every network call is
governed, every model response is untrusted, and a gated edit is only successful
after the real verification command passes.

## Data flow and state ownership

```
CLI/MCP input -> validation.py -> session composition -> ApplyEngine/PanelEngine
                         |                 |                    |
                         |                 |                    +-> chat.py -> SpendGovernor
                         |                 +-> Router, Ledger, filesystem ports
                         +-> plain request data                         |
                                                          result dict <- results.py
```

`ApplyRequest` is an immutable value object for one apply, including its bound continuation gate. `RunState` is the sole mutable transaction record and is passed explicitly through orchestration and gate operations. `GatePolicy` owns only filesystem/gate effects and result events; `AutonomyLedger` owns persisted evidence; and `Router` owns configured pools but is never mutated per request. `McpServer` owns JSON-RPC framing, tool contracts, shared boundary validation, serialized worker scheduling, cancellation, engine dispatch, and response lifecycle. Identified MCP request IDs remain reserved through response serialization, while notifications never emit responses. Rendering and exit-code policy remain at the CLI/MCP boundary (`cli.py`, `mcp.py`, `output.py`).
