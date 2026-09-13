# 4-Dimensional Audit — Round 2 (2026-09-08)

Executable rubric: [`round2_rubric.md`](round2_rubric.md) defines the four
dimensions; [`audit.py`](audit.py) encodes every check as code (static AST
inspections, hermetic dynamic scenarios, doc-consistency greps) and prints
per-check evidence. Hermetic by default — no network, no keys; the live
`capabilities --check-shipped` freshness re-check runs only with
`HARNESS_AUDIT_LIVE=1`.

Re-run with:

```
python audits/self/audit.py
```

## Result

| Dimension | Baseline | Final |
|---|---|---|
| A — Security | 6.92 | **10.0** |
| R — Reliability | 10.00 | **10.0** |
| SM — Structural hygiene & maintainability | 5.00 | **10.0** |
| SD — Documentation & release integrity | 8.00 | **10.0** |

Verdict: **all four dimensions ≥ 9.5 — bar met** (44/44 checks fully
satisfied). Suite: 339 tests OK hermetically (`-W error::ResourceWarning`);
`ruff check harness tests audits` clean. Machine-readable evidence:
`round2_scores.json` (per-check scores + evidence strings).

## What the audit found and what changed

Product fixes (each pinned by a test or an audit check):

1. **Dead validator deleted** (`validation.validate_cost`) — a
   `finite_number` alias with zero callers, the same dead-"audit #15"
   pattern round 1 removed once already. SM8 now greps for exactly this.
2. **`cli._engine` re-export seam removed** — engine construction has one
   owner (`session.engine_for`) since the architecture guard landed; the
   alias existed only for a round-1 test and invited a second construction
   site. The key-wiring regression test (round-1 defect 1) now pins the
   real constructor.
3. **Consent fail-closed deferrals now carry `dispatched: false`** — the
   synthetic defer (ladder exhausted / unparseable / paid-BYOK) was
   branchable only by string-matching its reason; the shape now states the
   dispatch verdict explicitly (A11).
4. **README documents `harness defer`** — the CLI face of `defer_work` was
   reachable but undocumented (D2).
5. **Test tree mirrors the product tree again** — `apply_gate.py` and
   `apply_state.py` had no direct test module (only indirect engine-level
   coverage). New `tests/test_apply_gate.py` (17 tests) pins the gate
   transaction contracts: bound-gate refusal, atomic+backed-up candidate
   writes, symlink refusal, preview-runs-no-gate, vacuous-pass honesty,
   cancel propagation, rewind-to-original, preview-exhausted honesty, and
   the escalation no-content path.

Audit-harness fixes (the check was wrong, not the product — recorded for
honesty):

- A2: `_chat_reservation_slots` is called as a plain name in panel.py, not
  an attribute; the check matched both forms.
- A5: the "label never echoed" grep matched the word "label" in the
  explanatory comment above the raise; the check now scopes to the raise
  expression itself.
- A12: the CLI-order check now walks each `_cmd_*` function and requires
  `validate_continuation` to precede `_session()` *within the same
  command* (≥2 resume commands), instead of comparing file-global indexes.
- S7: `_defines` no longer counts assignments — a function-local variable
  named `preflight` in consent.py is not an owner of the spend policy.
- D1: operator-precedence bug in the stale-name set expression.
- D2: README documents commands in fenced code blocks without inline
  backticks; the check matches the `harness <cmd>` form anywhere.

## Baseline → final trace

- Baseline run (before any edit): A 6.92, R 10.00, SM 5.00, SD 8.00.
  Three of the four failing SM checks and all four failing A/SD checks
  were audit-check bugs; two were real (dead validator, untested
  gate/state modules) — plus the re-export seam the audit's S2 surfaced
  as a genuine (if benign) violation the moment the alias stopped being
  test-only.
- After product fixes + check fixes: A 8.46 → 10.0, R 9.23 → 10.0
  (R13 dip was a pre-existing unclosed-file leak in test_hardening,
  fixed with a context manager), SM 7.50 → 10.0, SD 9.00 → 10.0.

## Residual notes (non-blocking)

- `HARNESS_AUDIT_LIVE=1` re-checks shipped pool ids against the live
  catalog (costs $0.00); CI wires it as a freshness gate (see below).
  Hermetic runs verify the guard is *wired*, not that today's ids are
  live.
- The symlink-refusal test skips on hosts without symlink privileges
  (Windows defaults); the refusal itself is enforced in `_atomic_write`
  on every platform.
- Scores are check-level means per the rubric; a dimension at 10.0 means
  every check fully satisfied, not the absence of unknown unknowns. The
  audit is evidence-based, not proof of perfection.

## Post-round follow-through (2026-09-08)

- **Live freshness re-check executed** against the real OpenRouter
  catalog: all 10 shipped ids present (catalog size 431), key verified,
  $0.00 spend, exit 0.
- **CI enforcement landed:** a new `audit` job in
  `.github/workflows/ci.yml` runs `ruff check audits` +
  `audits/self/audit.py` (the 9.5+ bar now fails the build) on every
  push/PR, with the live `capabilities --check-shipped` step enabled
  when the `OPENROUTER_API_KEY` secret is configured.
- **Module-scope temp leak closed:** `tests/test_mcp.py`'s module-level
  `TemporaryDirectory` is now released via `atexit`, so the full suite
  is clean under `-W error::ResourceWarning` from any entry point.
