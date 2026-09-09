# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
**until 1.0.0**, after which it will adhere to it strictly (the 0.x line may
break APIs between minor versions).

## [Unreleased]

### Added
- **Bipolar trust (-11..+11) with hard gates and correctness-rationed
  ceilings (`harness/trust.py`, `docs/trust.md`).** Cold start is always 0
  (unknown); clean runs earn slowly (3 per +1) while safety signals land
  fast (bounded protocol-sloppiness, -1 guidance denials, -4 hostile
  denials; ordinary verify misses move correctness, never trust).
  Principals are host + model + continuation author (weakest link; author
  only matters on resume). `<= -6` refuses mutation/exec, negative forces
  preview-only, unknown writes require a gate (enforced at the write, so
  consent/deferral paths still run). Correctness unlocks ceiling
  fractions (0 -> today's defaults, negative below, +6.. full hard cap).
  New surfaces: `harness trust`, MCP `trust_status`, additive `trust` on
  ledger/MCP reports; every denial ledgered as `trust_gate` evidence.
  Also closed: `--max-cost` override capped at `HARD_MAX_COST` (C2),
  continuation file-retarget refused (C4), engine-boundary gate
  tokenizability preflight, MCP boundary refusals ledgered.
- **CI audit gate:** a new `audit` job in `.github/workflows/ci.yml` runs the
  4-dimensional audit (`audits/self/audit.py`) with its 9.5+ per-dimension bar
  on every push/PR, plus ruff over the audit script itself and an opt-in live
  `capabilities --check-shipped` freshness re-check when the
  `OPENROUTER_API_KEY` secret is configured (pure `/models` read, $0 spend).
- **Audit sweep: spend, execution, and convergence hardening.** Probe lane
  learns paid-BYOK prefixes and stops burning questions on them, reserves
  reasoning-fallback slots per question, and bills error-path costs like
  every other lane; `chat()` resolves a missing `usage.cost` once for all
  lanes (free fills zero, paid estimates from token counts, blind fails
  closed); transport retries fold dropped transient-attempt spend into the
  final response; ledger appends use non-blocking cross-process locks;
  backups and bench snapshots refuse planted symlinks; bench containment
  resolves parent-dir symlinks; the convergence specialist names
  reassurance-claim polarity and trims votes to the smallest candidate
  window; consent renewals attribute to the answering model; the dead
  key-fragment label guard is gone (live `/key` exact match remains);
  claims `_DEFN_RE` word boundary restored so in-window definitions
  suppress redundant auto-expansion.
- **Consent-probe curation:** consent answers the parser cannot use (empty,
  reasoning-only, unparseable — ledgered as `consent_rotate`, HTTP tier faults
  excluded) now count toward the same two-strike demotion as the apply lane's
  unusable outputs, so a consent-blind judge is rotated below unproven models
  at the next panel/consent lane build.

### Fixed
- **Fail-closed consent deferrals carry an explicit `dispatched: false`**
  verdict in their result shape, so a consumer can branch on the dispatch
  decision without inferring it from the synthetic defer's reason text
  (4-dimension audit, A11).
- **`validate_cost` deleted** - a finite_number alias with zero callers
  (the dead "audit #15" validator pattern again); `finite_number` is the
  one cost validator (audit SM8).
- **The redundant `cli._engine` seam is gone.** Engine construction has one
  owner (session.engine_for) since the architecture guard landed; the CLI's
  re-export alias existed only for a round-1 test and invited a second
  construction site. The key-wiring regression test now pins the real
  constructor (audit SM2).
- README documents `harness defer` (the CLI face of `defer_work` was
  reachable but undocumented) (audit SD2).
- **Multi-file batches always return the batch envelope**, even when they
  fail fast on file 1 (previously a one-result batch collapsed to the bare
  single-file shape, so consumers keying on `results` couldn't tell a batch
  death from a single-file run), and the envelope's shared-gate
  `verify.passed` now derives from the last file's actual verdict instead of
  hardcoding `true`.
- Terminal `api_error` rounds now always name the real failure: the HTTP
  status is prefixed to the provider message (a "Rate limit exceeded:
  free-models-per-day" body no longer hides the 429 from the saturation
  guidance), and reasoning-only pool exhaustion reports a human sentence
  instead of dumping the raw JSON response body (behavior-verification pass).
- Reasoning-only demotion actually fires on real runs: the counter now joins
  the apply engine's own `"no usable content"` error reason (it previously
  matched a string the engine never writes), and demoted models sort below
  every unproven model on both tiers instead of only losing ties.
- **Bench no longer leaves solved fixtures in the tree.** `run_bench` now
  restores every task sandbox in a `finally` (a successful run previously
  left the fixed fixture on disk — committing that would defeat the bench;
  the docstring's "idempotent and re-runnable" promise now covers the tree
  it leaves behind), and the sandbox snapshot/restore plus `_atomic_write`
  round-trip is byte-faithful (`newline=""` throughout): CRLF fixtures no
  longer come back EOL-laundered as phantom diffs.
- README documents the `harness dogfood` self-hosting loop (previously only
  CONTRIBUTING did) and states the full exit-code meanings.
- A bench task missing its required `file` key fails with a clean
  `[FATAL] bench task '<name>' is missing required key 'file'` instead of a
  raw `KeyError` traceback (every other manifest error already honored the
  contract).
- The deferred-task resume hint no longer names a flag pair that does not
  exist together (`--continue-from` lives on `apply`; `continue` takes
  `--state`): it now prints the exact `harness continue --state <out.json>`
  command to run.
- A missing or malformed `--definitions-file` fails with a clean
  `[FATAL] definitions file ...` error instead of a raw `JSONDecodeError`
  traceback (`load_definitions_file` now honors the harness error contract,
  matching the claims-manifest loader).

### Changed
- Architecture pass: the terminal status vocabulary gained its meaning as a
  one-place policy — `results.SUCCESS_STATUSES` and
  `results.terminal_exit_code` — replacing the failure-set copy in cli's
  `_emit_by_status`, the success-set copy in `apply_batch`, and dogfood's
  re-derived exit block. The consent mechanics text moved from apply.py to
  prompts.py (pure prompt text belongs with the prompt contracts);
  `models --all` reuses the session's verified governor instead of building
  a throwaway one.
- Architecture pass: the verify recipe (catalog seed, capability panel
  ordering, degrade-to-given-order) moved into the panel lane as its single
  owner — the CLI's verify lane and MCP's `panel_verify` dropped their
  duplicated seed+order blocks (MCP's even ordered converging panels under
  the wrong task key). dogfood tests lost 18 lines of dead governor-mock
  boilerplate by patching the session seam instead. Net -37 lines.
- Architecture pass: engine composition extracted to `harness/session.py` as
  the ONE owner (governor/ledger/router/engine builders + `apply_session`
  with the pre-spend saturation look-ahead). MCP now composes from it — its
  20-line construction copy is deleted, so engine-kwarg and tier-policy
  changes can no longer land in the CLI and miss MCP. A new architecture
  guard pins ApplyEngine/Router construction to session.py only.
- Earlier: one dependency-assembly site (`cli._session`) wired
  key+governor, ledger, router and engine identically for verify, apply,
  continue, dogfood and bench — replacing the four verbatim copies, and
  threading `settings.use_free` into the engine on both construction sites
  (CLI and MCP — previously silently defaulted to False, so mixed-pool
  demotion sort semantics differed from config). Apply resumes inherit the
  saved continuation task id (ledger
  attribution stays under the original task; explicit --task-id still wins).
  Test tree mirrors owners: marker/extraction parsing tests moved to
  `tests/test_prompts.py`, the CLI-boundary gate test to `tests/test_cli.py`,
  the gate-broken engine test into the engine lifecycle suite.
- Architecture pass: the apply result vocabulary (round entries, terminal
  and deferred result builders, HTTP error rendering) moved to a new
  `harness/results.py` — one def site per result shape the CLI and MCP
  consume; apply.py is 1,077 → 999 lines and the vocabulary is pinned by
  the architecture test. Interface hygiene is also pinned: interface
  functions import policy at module level (cli.py's three remaining lazy
  imports lifted), with `main()`'s lazy entry as the documented exception.
  The test tree now mirrors the product tree: `test_core.py` dissolved into
  `test_spend.py` / `test_panel.py` / `test_convergence.py` /
  `test_specialist.py` / `test_chat.py`, and `test_audit_fixes.py` into
  `test_ledger.py` / `test_judge.py` / `test_tokens.py` / `test_probe.py` /
  `test_dogfood.py` — same 262 tests, behavior-named homes, shared `_gov`
  fixture in `tests/_fake.py`.
- **`apply_edit` decomposed into phases with a single orchestrator.** Argument
  policy lives in one `_prepare` (validated, engine defaults frozen into an
  `_ApplyRequest`); the round loop's phases — initial consent, renewal,
  rotation, dispatch, merge, gate, escalation, terminal assembly — are named
  helpers mutating one `_RunState`, and every outcome still exits through the
  shared terminal builders. Behavior-preserving: all 243 tests pass unchanged;
  a change to one behavior now lands in one phase, not a 600-line loop body.
- `cli.main` dispatches through a command table instead of a 12-way `elif`
  chain; handlers all take `(opts, settings)` (fixing a stale `_cmd_spend`
  call signature that the chain masked).

### Added
- **`capabilities --check-shipped`** — machine-checked config freshness: the
  new `config.shipped_model_ids()` enumerates every default lane id across
  both tier policies; the command validates all of them against the live
  catalog ($0.00, one GET /models) and exits 2 naming any stale id — closing
  the twice-recurred stale-default-id defect class. The catalog-fixture pin
  (which itself rotted) is replaced by a live-gated suite test that
  auto-runs the same check wherever an API key exists.
- **Pre-run saturation warning** — the look-ahead half of the saturation
  policy (`saturation.pre_run_warning`): before a run spends anything, the
  recent ledger is scanned for rate-limited model results (429s in the last
  100 events, ≥3 = saturated; 401/auth faults deliberately not counted) and
  one plain-language warning prints to stderr — advice, never a gate — with
  key-near-limit state qualifying the BYOK advice. Warns at most once per
  process; every failure mode degrades to silence. Wired at both assembly
  sites (CLI `_session` + `_run_claims_verify`, MCP `main`).
- **Ledger-seeded self-hosting (`dogfood --from-ledger`)** — the loop closes:
  the harness curates its next self-audit from its own recorded run evidence
  instead of a hand-authored fixture. Three deterministic, ranked rules turn
  recent ledger entries into factual claims — a model whose runs repeatedly
  fail-closed at the verification gate, a model repeatedly paid for HTTP 200s
  with no usable content, and free-tier rate limiting dominating recent
  dispatches — each with its evidence window. The curated manifest is
  persisted via `--claims-out`, passes the hermetic ground lint, and flows
  through the same gate-confirmed panel and gated self-apply phases.
  Curation reads the ledger only (no live key) and is ledgered as a
  `dogfood_curate` event. (Live proof: curated 3 claims from this repo's real
  ledger — 82 gemma-4-31b gate fail-closes, 4 for openrouter/free, 5 paid-
  for-nothing calls — panel-confirmed, gated apply passed, $0.00.)
- **`harness dogfood` — the self-hosting loop as one command.** Composes the
  three lanes with fail-closed phase gates: hermetic claims lint (an
  ungrounded claim never reaches a model), live panel + convergence tally
  (the defect must be *gate-confirmed* — every required panel slot voting —
  before any edit), then the gated self-apply with the operator's verify
  command. Exit 0 only if every phase proved its claim; shortfall panels,
  unconfirmed defects, and deferrals all stop short of the edit with full
  evidence in the `--out` report. CLI-only by design: the
  verify→self-edit chain stays under operator authority; its phases remain
  individually available over MCP. The verify execution path is shared with
  `verify` (`_run_claims_verify`), so the two surfaces cannot drift.

### Fixed (round-1 self-dogfood)
- **The tree changes only through a passed gate.** Capability-deferred runs no
  longer write the model's partial output to the target (the partial travels
  in the continuation state), failed runs rewind the target to its pre-run
  content, and merge-failure feedback no longer collides with the broken-gate
  detector. Dogfooding the harness on its own repo corrupted `config.py`
  through the ungated partial path; this class is closed and pinned.
- **Ledger chain no longer forks under concurrency.** `append()` rebases its
  chain state under the append lock (two processes previously appended from
  the same `prev_hash`, producing duplicate seqs), and `harness ledger repair`
  truncates to the longest valid hash-chain prefix.
- **Config hard ceilings are enforced** (`HARD_MAX_COST`/`HARD_TASK_MAX_COST`
  are now real bounds in `load_settings`), the dead `_validate_settings_values`
  duplicate is deleted, and config load failures exit via the CLI's clean
  `[FATAL]` path.
- **Consent probes see the full target file** (both initial and renewal
  probes, one preview policy in `consent.py`) — models were honestly refusing
  to consent to edits they could only see 60 lines of.
- Line-numbered source in diff prompts plus richer merge-error feedback
  (models miscounted lines in unnumbered views); round-feedback strings no
  longer contain literal `\n` escapes.

### Changed
- **Layering completed and enforced.** `apply.py` (1,062 lines) shed its two
  non-engine concerns: prompt contracts and response parsing moved to
  `prompts.py`, disk/gate mutation policy (atomic write, backups, shell-free
  verify runner) to `filesafety.py` — `apply.py` is now the round loop,
  rotation, escalation, and batch orchestration only. `core.py` was pruned to
  the names callers actually import, and the new `tests/test_architecture.py`
  enforces the import direction (no module may import an interface;
  `core.py` must stay a pure re-export shim), so the layering can no longer
  silently regress.
- **Pool ordering has ONE owner: `capability.ordered_pool`.** It reads the
  governor's cached /models catalog, orders by observed-corrected
  reliability, drops catalog-stale ids at the routing boundary (a stale
  configured id previously hard-failed the whole run at pricing lookup), and
  degrades to the caller's order when capability data is unavailable. The
  panel lane self-serves ordering inside `panel_judge` (dropping its
  `capability_profiles`/`report` parameters); the apply lane orders per
  request inside the engine.
- **No per-request router mutation.** `mcp.py` no longer writes ordered
  pools into the shared `Router` (the same per-request-vs-lifetime bug class
  as the continuation-gate leak); routing is resolved per request in the
  engine, and a regression test pins router immutability on both engines.

### Added
- **MCP batch parity: `apply_edit` accepts a `file` array.** Multi-file
  batches run through the new engine-owned `ApplyEngine.apply_batch` (one
  governed session per file, shared budget, fail-fast, shared-gate
  aggregation) — previously the batch loop lived only in the CLI, so MCP
  had no batch capability at all.
- `--quiet` on `bench` and `continue` (verify/apply had it; the others
  rejected it with an argparse error).
- Fixed the `--quiet` flag itself, which the facade split had silently
  disconnected (the CLI wrote `core.QUIET`; `eprint` reads `output.QUIET`).

## [0.2.0] — 2026-09-05

### Changed
- **Engine split by concern.** The 1,240-line `harness/core.py` is now a
  compatibility facade over focused modules: `spend.py` (SpendGovernor,
  BYOK, discovery), `chat.py` (the single model-I/O path plus the ONE shared
  output-usability verdict, `assess_output`, replacing four divergent lane
  checks), `panel.py` (panel+judge run), `convergence.py` (deterministic
  tally + specialist lane), `continuation.py` (continuation contract + gate
  identity), and `output.py`/`tokens.py`/`errors.py` (quiet gate, token
  estimate, exception types). Behavior preserved: all 217 hermetic tests pass
  unchanged (two test seams re-pointed at their owning modules), verified
  live through the CLI, the MCP stdio server, a free-tier claims audit, and a
  bench task. The CLI's tripled ApplyEngine construction and apply/continue
  exit-code policy are collapsed into one site each. The module map lives in
  CONTRIBUTING.md.
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
- **`ApplyEngine._continuation_gate` no longer leaks across applies**
  (`harness/apply.py`). Resuming a gated apply pinned its gate on the engine
  for its lifetime, so a later *fresh* apply with a different gate was refused
  with "verify gate changed" — and the MCP server, which keeps one engine for
  the whole session, would have broken every apply after the first resume.
  Per-request gate state now resets at the start of each apply; the resume
  path still pins and the tamper check still refuses.
- **Continuations resume instead of false-tripping the broken-gate guard**
  (`harness/apply.py`). Restored `history` entries carry no `verify_output`, so
  the "gate failed identically on consecutive rounds" comparison saw empty
  outputs and aborted every multi-round resume before a single new model call.
  Only real gate outputs now participate in the guard.
- **`harness bench` accepts the documented single-task file**
  (`harness/bench.py`). A bare task object (the README's "a task is one JSON
  file" shape) produced a zero-task manifest that ran nothing and exited 0;
  it now loads as a one-task manifest, and a manifest that yields no tasks or
  an unrecognizable shape fails with a clear error instead of silent success.
- **Backups survive slashed task ids** (`harness/apply.py`). Bench names tasks
  `bench/<name>`, and the slash landed in the backup *filename*, breaking the
  backup write on every platform — bench ran with its safety net silently
  disabled (visible as a `[warn] backup failed` on each task). Task-id
  separators are now flattened in backup names.
- **`harness-mcp` / `python -m harness.mcp` actually serve**
  (`harness/mcp.py`). The module had no `__main__` guard, so direct invocation
  imported and exited 0 having served nothing.
- MCP `ledger_status` returns `verified` as a structured object
  (`{"ok", "first_bad_seq"}`) instead of a raw Python tuple serialized as an
  array.
- Documentation drift: the README and `harness/core.py` docstring still
  described GLM-5.2 as *leading* the free specialist ladder while the curated
  config had demoted it to last on its 0/22 observed record.
- `_atomic_write` now **preserves the target's permission mode**
  (`harness/apply.py`). `tempfile.mkstemp` creates files 0600, so every atomic
  replace silently stripped the executable bit (and all other mode bits) from
  the written file — breaking verify-gate scripts and artifacts rewritten by
  an apply round. New files keep the safe 0600 default.
- **`HARD_MAX_COST` / `HARD_TASK_MAX_COST` are now enforced**
  (`harness/config.py`). They were defined and documented as hard ceilings but
  never checked, so `max_cost=500` loaded silently. `load_settings` now
  refuses any per-call ceiling above $0.10 or per-task ceiling above $0.25.
- Removed dead code: the never-called `_validate_settings_values`
  (`harness/config.py`; its one invariant contradicted the shipped defaults,
  where a task ceiling larger than a call ceiling is intentional) and the
  ledger's `_acquire_process_lock` helper (`harness/ledger.py`), which
  referenced an uninitialized `_lockfile` attribute and would have crashed if
  ever called — the live cross-process lock is acquired per-append in
  `_persist`.
- Removed a malformed `# noqa` directive (`harness/core.py`) flagged by ruff.
- Documentation drift: README and CONTRIBUTING no longer hardcode a test
  count that immediately went stale; bench task fixtures now end with a
  trailing newline (ruff W292).

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
