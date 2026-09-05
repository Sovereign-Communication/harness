# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
**until 1.0.0**, after which it will adhere to it strictly (the 0.x line may
break APIs between minor versions).

## [0.2.0] — 2026-09-05

### Changed
- **Package renamed to `sovereign-harness`.** The PyPI name `harness` belongs
  to an unrelated project ("Language-neutral meta-framework for server-less
  style services"), so this distribution now publishes as `sovereign-harness`.
  The import name is unchanged (`import harness`), and the CLI entry points
  remain `harness` and `harness-mcp`.
- **Version is single-sourced** from `pyproject.toml`:
  `harness.__version__` and the MCP `serverInfo` now derive from it
  (installed-package metadata first, source-checkout fallback second), so the
  three can no longer drift.
- Added `[project.urls]` metadata (Homepage, Issues) to the package.

### Fixed
- Removed a malformed `# noqa` directive (`harness/core.py`) flagged by ruff.
- Documentation drift: README and CONTRIBUTING no longer hardcode a test
  count that immediately went stale.

## [0.1.0] — 2026-09-02

Initial public development release. Development continued on the 0.1.0 line
through 2026-09-05; the highlights below span that whole window.

### Added
- **Cost-bounded multi-model verification core** (`harness.core`): rotating
  panel + structured judge over plain chat completions — no `tools` key in
  any payload, pre-flight worst-case cost ceilings against live per-token
  pricing (default 2¢/call, hard max 10¢), mid-batch fail-closed spend
  checks, and BYOK route handling with learned org-prefix rotation.
- **Sovereignty model** (`harness.consent`): an independent, cheap consent
  probe with decline/defer/redirect all valid; parsed defers honored without
  re-asking; fail-closed on unparseable consent; re-checked before every
  verify round; revocable mid-task via `defer_work`.
- **Autonomy ledger** (`harness.ledger`): append-only, hash-chained JSONL
  with chain verification, tamper quarantine, cross-process advisory lock,
  and a participation report with per-model confidence calibration
  (`HARNESS_READY` verdict joined to verify outcome).
- **Forced self-check**: the apply prompt requires an opening
  `HARNESS_READY: confident|defer` verdict; defer rotates to the next model
  before any code is written.
- **Capability-deferral protocol**: models emit `HARNESS_DEFER:` with
  remaining scope instead of guessing; partial work is preserved as a
  continuation any subsequent model can take over (`harness continue`).
- **Scoped apply-and-verify loop** (`harness.apply`): single-file (<500
  line) edits with a real verification gate, retry rounds, per-round consent,
  unified-diff and multi-file apply paths, and atomic writes inside a path
  containment sandbox.
- **MorphLite backend** (`harness apply --backend morph`): Morph V3 Fast
  contract inside the same spend-governed engine; `--verify-only` previews
  are read-only.
- **Convergence specialist** (`harness verify --converge`): deterministic
  per-claim tally over panel votes (5/5 unanimous == 100% at the merge gate),
  separate responder vs gate convergence signals, defect-proposition polarity
  convention, reassurance-claim exclusion, and a rotating, token-cap-aware
  specialist lane with disclosed budgets.
- **Self-grounding claims (P0)** (`harness.claims`): source-ref lint (R1
  ungrounded assertion, R2 out-of-window ref, R3 contradicted-by-source)
  run before any network call; verbatim transitive auto-expansion of
  called-not-defined identifiers.
- **Model capability layer** (`harness.capability`): declared hypothesis from
  live `/models` metadata corrected by observed ledger/probe evidence;
  composite reliability with neutral-prior shrink; capability-aware routing
  used identically by CLI, core, continuation, and MCP; registry with 24h
  TTL and schema-version stamp.
- **`harness bench`** (`harness.bench`): manifest-driven free-tier task
  runner (9 shipped known-answer tasks) with snapshot/restore idempotency,
  real verify-gate ground truth, and results fed into the autonomy ledger.
- **Native MCP server** (`harness.mcp`): hand-rolled spec-conformant JSON-RPC
  2.0 stdio server (`panel_verify`, `apply_edit`, `offer_work`, `defer_work`,
  `ledger_status`, `participation_report`, `spend_status`) with
  `allow_verify`/cancel gates and structured error codes.
- **CLI**: `verify` (with `--converge`, `--claims-file`, `--max-cost`),
  `apply`, `continue`, `offer`, `bench`, `lint-claims`, `ledger report/verify`,
  `models`, `spend`, `capabilities` (with `--bench` probe). Exit codes:
  0 ok, 1 fatal, 2 verify failed, 3 deferred.
- **CI**: hermetic tests + ruff on Python 3.9 / 3.11 / 3.13.
- **Audit record**: SCMessenger security-audit artifacts (rounds 1–4,
  convergence reports, CTO review) demonstrating the engine reproducing
  round-3 verdicts claim-by-claim on the free tier at $0.00.

### Hardening (late-0.1.0)
- Sandbox path containment (absolute escapes, `..` traversal, symlink swaps
  refused; original file mode preserved), parallel panel fan-out, MCP write
  gate, `notifications/cancelled` aborts.
- Config range validation with unknown-key warnings; capabilities registry
  schema-version stamp; per-model cost reporting and `--quiet`; token-
  estimator property tests; protocol-marker stripping from file content.
- Consent lane preflighted against the ceiling; `--max-cost` wired
  everywhere; consentless-apply crash fix; defer markers honored anywhere in
  a model response; honest spend accounting for failed rotations.

[0.2.0]: https://github.com/Sovereign-Communication/harness/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Sovereign-Communication/harness/commits/v0.1.0
