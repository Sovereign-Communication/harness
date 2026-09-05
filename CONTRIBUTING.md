# Contributing to Harness

## Setup

```bash
git clone <repo> && cd Harness
pip install -e .
pip install ruff        # lint (CI enforces it)
python -m unittest discover -s tests   # hermetic tests, no network needed
```

Python 3.9+; pure stdlib — the package has zero runtime dependencies.

## Ground rules

1. **Hermetic tests only.** Every test must run with no network, no API key,
   and no filesystem writes outside a temp dir. Live OpenRouter behavior is
   verified manually (`harness capabilities --bench`), never in CI.
2. **The spend ceiling is sacred.** Any new network call must go through
   `SpendGovernor.preflight` before the request and `record_actual` after it.
   A PR that adds a path where cost can accrue unaccounted will be rejected.
3. **Fail closed.** Malformed model output, missing panelists, torn ledger
   lines, and out-of-range config are errors — never silently coerced into
   success.
4. **Sovereignty is non-negotiable.** A parsed `HARNESS_DEFER` from a model
   is honored, never shopped around. Deferral is a valid outcome, not a
   failure to retry away.
5. **Both surfaces stay in parity.** A feature added to the CLI must be
   reachable from the MCP server with the same defaults (and vice versa).

## Lint & test before pushing

```bash
ruff check harness tests
python -m unittest discover -s tests
```

CI runs both on Python 3.9 / 3.11 / 3.13. A failing or skipped check blocks
merge.

## Where things live

| Path | Owns |
|---|---|
| `harness/config.py` | settings, key resolution, lane curation |
| `harness/core.py` | panel/judge/specialist engine, SpendGovernor |
| `harness/capability.py` | model capability profiles + observed evidence |
| `harness/apply.py` | edit application, verification gate, continuations |
| `harness/consent.py` | the consent probe (sovereignty) |
| `harness/ledger.py` | hash-chained JSONL autonomy ledger |
| `harness/mcp.py` | MCP stdio server (dispatch surface) |
| `harness/bench.py` | hermetic known-answer benchmarks |
