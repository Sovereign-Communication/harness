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
- `apply.py`: edit orchestration and apply lifecycle.
- `batch.py`: multi-file orchestration.
- `prompts.py`: prompt and response contracts.
- `filesafety.py`: atomic writes, backups, and verification execution.
- `continuation.py`: persisted continuation authority and gate identity.
- `ledger.py`: hash-chained evidence.
- `results.py`: apply result vocabulary and exit-code policy.
- `session.py`: dependency composition.

Interfaces translate input into shared request policy; they do not reimplement
engine or safety behavior. Every network call is governed, every model response
is untrusted, and a gated edit is only successful after the real verification
command passes.
