# Contributing to Harness

## Setup

```bash
git clone <repo> && cd Harness
python -m pip install -e '.[dev]'
python -m ruff check harness tests
python -W error::ResourceWarning -m unittest discover -s tests   # hermetic, no network
```

Python 3.9+; pure stdlib — the package has zero runtime dependencies.

## Module map (who owns what)

The package is layered; dependencies point one way, downward:

```
cli.py / mcp.py          interfaces (arg parsing, JSON-RPC; boundary
                         normalization; no engine policy)
  mcp_schemas.py         MCP tool contracts as pure data (no imports, no
                         logic; one consumer: mcp.py)
  mcp_lanes.py           MCP lane-scheduling policy (LANES, lane_for):
                         which serial worker runs each tool
  apply.py               apply engine: validates inputs, dispatches models, and
                         orchestrates consent, rotation, rounds, and escalation
  apply_gate.py          candidate write/preview, verification, rewind, and
                         terminal gate policy; receives request/run state but
                         owns no run state
  apply_state.py         immutable ApplyRequest + mutable RunState/AttemptOutcome;
                         request-local data contracts; apply orchestration owns
                         transitions and gate owns only filesystem effects
  results.py             the apply result vocabulary: _round_entry,
                         _terminal_result, _defer_result, _http_error -- one
                         def site per shape the CLI/MCP consume (a new
                         outcome field lands here, not in the engine)
  panel.py               verification engine: rotating panel + judge; owns
                         the whole verify recipe (catalog seed, capability
                         ordering, degrade-to-given-order) -- interfaces only
                         map inputs and present results
    prompts.py           apply prompt contracts + response parsing (READY
                         marker, strict unified diff); pure text, no engine
                         state -- incl. the consent mechanics text
    filesafety.py        atomic write, out-of-tree backups, shell-free gate
                         runner -- every disk/gate mutation policy
    convergence.py       deterministic claim tally + specialist lane
    consent.py           consent probe / continued consensus
    capability.py        declared-vs-observed model capability + routing
                         (ordered_pool is the ONE pool-ordering entry point)
    chat.py              THE model I/O path + assess_output usability policy
    spend.py             SpendGovernor (ceilings, BYOK) + model discovery
    claims.py            self-grounding claim lint
    continuation.py      continuation-state contract + gate identity
    bench.py  ledger.py  task runner; hash-chained evidence store
    (cli `dogfood` composes the lanes: hermetic ground gate -> live panel
    tally gate -> gated self-apply; deliberately not exposed over MCP)
    config.py  tokens.py  output.py  errors.py
```

- **One owner per concern.** Cost policy lives only in `spend.py`; model
  transport and the usable-output verdict only in `chat.py`; gate identity
  and continuation validation only in `continuation.py`; prompt text only in
  `prompts.py`; disk/gate mutation only in `filesafety.py`; the `--quiet`
  gate only in `output.py`. Don't re-implement a policy locally — import it.
- **Routing is per-request.** Pool ordering goes through
  `capability.ordered_pool` — never pre-order a pool in an interface and
  never mutate `Router` state per request; the engine orders the request's
  pool and drops catalog-stale ids at that boundary.
- **Batch is engine capability.** Multi-file batches (CLI repeated `--file`,
  MCP `file` array) run through `ApplyEngine.apply_batch`; interfaces only
  translate arguments.
- **No facades.** There is no `harness/core.py` shim and none may regrow:
  import from the owning module at the use site. Re-export indirection doubles
  every dependency edge and hides the real owner. `tests/test_architecture.py`
  enforces both the import direction and the no-re-export rule (a module-level
  import the module never references is a re-export, mechanically detected).
- **One struct idiom.** Immutable structs are frozen dataclasses
  (`@dataclass(frozen=True)`), with no exceptions -- where a struct's
  construction does real resolution/coercion work, keep the custom
  `__init__` (`init=False`) and set fields via `object.__setattr__`, as
  `PanelLanePolicy` and `CapabilityProfile` do; reworked instances use
  `dataclasses.replace`, never field mutation.
- **Apply results have one shape.** Every round entry and terminal result (ok /
  preview / deferred / verify_failed) is built by `results.py` — interfaces
  consume that shape, they never reassemble it. New result fields go there,
  not at a call site.
- **Engine flags have one definition.** The apply/continue flag cluster is
  declared once (`cli._add_engine_flags`) and output flags once
  (`cli._add_output_flags`); every subcommand that emits a report honors
  `--out` and `--quiet`. A flag that parses but is ignored is a bug.
- **Data flow:** interfaces parse input → engines orchestrate lanes → lanes
  go through `chat()` under `SpendGovernor` preflight/record → every decision
  and cost lands in the `AutonomyLedger` → results flow back as plain dicts
  the interface serializes. Nothing writes to stdout except the final JSON
  (`cli._emit`) or valid MCP frames (`mcp._write`).
- **State:** per-request state belongs to the request (`apply_edit` locals;
  gate binding is stored only on `ApplyRequest`); session state belongs to the
  engine objects the interface constructs; evidence belongs to the ledger;
  configuration belongs to `Settings` (built once in `load_settings`).

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
   One deliberate exception: `harness dogfood` (the self-hosting loop:
   ground -> live panel verify -> gated self-apply) is CLI-only by design —
   it chains verify into a *self-edit*, and that authority stays with the
   operator at the shell, not with protocol clients. Its phases remain
   individually reachable over MCP (`panel_verify`, `apply_edit`).

## Lint & test before pushing

```bash
ruff check harness tests audits
python -W error::ResourceWarning -m unittest discover -s tests
```

CI runs both on Python 3.9 / 3.11 / 3.13. A failing or skipped check blocks
merge.
The battery also runs locally on every CI interpreter: uv-managed CPython
3.9 / 3.11 / 3.13 (via `uv python install X.Y`; find the interpreter with
`uv python find X.Y`) plus the local default -- so a release battery
statement covers the CI matrix by direct execution.

## Where things live

| Path | Owns |
|---|---|
| `harness/config.py` | settings, key resolution, lane curation |
| `harness/spend.py` | SpendGovernor: spend ceilings, BYOK, model discovery |
| `harness/router.py` | cheap-first routing ladder: model pools, rotation, gated escalation |
| `harness/capability.py` | model capability profiles + observed evidence |
| `harness/saturation.py` | one free-tier saturation policy: per-attempt evidence -> the plain-language verdict terminal surfaces print when the tier fail-closes |
| `harness/rankings.py` | rankings-driven pool-candidate refresh: OpenRouter daily-traffic evidence -> candidate report (evidence-driven, not folklore-driven) |
| `harness/apply.py` | apply engine lifecycle: construction, the public `apply_edit` entry, and batch dispatch; per-round machinery mixed in from apply_policy |
| `harness/apply_policy.py` | the apply engine's per-round machinery (billing, edit loop, consent, rotation, deferrals, escalation) -- mixed into `ApplyEngine` verbatim |
| `harness/batch.py` | multi-file batch orchestration: one governed session per file, shared task budget, fail-fast -- owns the LOOP, the engine owns the per-file apply |
| `harness/dag.py` | task decomposition & dependency DAG: TaskDAG, DAGNode, topological batching |
| `harness/executor.py` | concurrent multi-threaded batch dispatch & file mutex manager |
| `harness/condenser.py` | context distillation & micro-brief pipeline: AST signature extraction, error pruning |
| `harness/sliding_scale.py` | dynamic sliding-scale tier classification (Tiers 0, 1, 2) & frontier model routing ladder |
| `harness/agent.py` | autonomous agent orchestration: natural language prompt intent classification, file/gate discovery, sliding-scale DAG execution, self-healing retry |
| `harness/apply_gate.py` | one candidate-to-gate transaction: write, verify, preview, rewind, and terminal gate results |
| `harness/apply_state.py` | apply request data and mutable per-run state |
| `harness/results.py` | the apply result vocabulary (round entries, terminal/deferred results, HTTP error rendering) -- one def site per result shape, plus the status-meaning policy: `SUCCESS_STATUSES` and `terminal_exit_code` (interfaces never re-derive what a status means) |
| `harness/escalation.py` | auto-escalation driver: judge-directed rung stepping for apply -- a rung only counts if the real gate passes |
| `harness/session.py` | composition owner: governor_for/ledger_for/router_for/engine_for + `apply_session` (pre-spend saturation look-ahead included) -- how ANY interface gets its dependencies; engine kwargs and tier policy change here exactly once |
| `harness/service.py` | the ONE verify/claims run assembly (prompt, claims flags, resolved inputs, cancelled envelope, cost/meta) -- what `harness verify`, `harness serve`, and MCP all consume |
| `harness/validation.py` | shared validation for untrusted CLI/MCP/batch/library inputs -- every safety-sensitive limit passes through here before any model call or file mutation |
| `harness/cli.py` | handlers + dispatch + CLI bootstrap (exit codes, color/events policy); presentation re-exports (`_emit`, `_emit_by_status`, `_print_capabilities_table`) kept as patch points; session aliases (`_governor`/`_engine`/...) kept as test seams |
| `harness/cli_report.py` | the ONE result-rendering owner (`_emit`, `_emit_by_status`, `_print_capabilities_table`): --out files, machine JSON vs TTY pretty mode, exit-code surfacing -- moved verbatim from cli.py |
| `harness/cli_parser.py` | the argparse surface as pure construction (`build_parser`, flag builders) — handlers live in cli.py, flags in exactly one owner |
| `harness/consent.py` | the consent probe (sovereignty) |
| `harness/ledger.py` | autonomy ledger storage/integrity lifecycle: append, hash chain, rotation, repair, verify |
| `harness/ledger_analytics.py` | read-only ledger analytics (participation_report, defer_stats calibration) -- mixed into `AutonomyLedger` verbatim |
| `harness/trust.py` | bipolar trust (-11..+11) per host/model/author: levels AND gates -- thresholds unlock actions, safety signals drop trust fast |
| `harness/events.py` | typed progress event stream: the ONE owner of live run telemetry (structured JSON to sinks; advisory, never control flow) |
| `harness/mcp.py` | MCP framing, boundary normalization, engine dispatch, cooperative cancellation + per-tool deadlines (the frame loop owns the cancellation lifecycle), and response lifecycle; composes dependencies from session.py |
| `harness/mcp_schemas.py` | MCP tool contracts as pure data (no imports, no logic; one consumer: mcp.py) |
| `harness/mcp_lanes.py` | MCP lane-scheduling policy (mutation/spendy/observe): `LANES`, `lane_for` -- which serial worker runs each tool |
| `harness/server.py` | `harness serve`: localhost web UI + JSON API -- the third face; dispatch calls the same engine entry points, no new policy |
| `harness/render.py` | the ONE human-facing pretty-printer: TTY tables of result envelopes on stderr; read-only, machine JSON stays the stdout contract |
| `harness/bench.py` | hermetic known-answer benchmarks |
| `harness/chat.py` | the one chat-completion path and the one assessment of what a model actually produced |
| `harness/web.py` | deliberate web access for the chat lane: allowlist-only fetch (https, redirects refused) + one operator-configured search endpoint; opt-in per run, honest failures, never an SSRF surface |
| `harness/claims.py` | structured-claims grounding: source_refs lint + claims curation from the ledger's own evidence |
| `harness/continuation.py` | the resumable-task state contract and its verification identity |
| `harness/convergence.py` | deterministic tally over panel votes + the rotating specialist lane |
| `harness/errors.py` | shared exception types (`HarnessError`) |
| `harness/filesafety.py` | file-safety primitives: every mutation of a real file goes through these |
| `harness/output.py` | the ONE stderr owner for progress chatter and --quiet |
| `harness/panel.py` | panel + judge verification: rotating independent takes, one synthesis |
| `harness/prompts.py` | apply prompt contracts + response parsing (pure functions, no I/O) |
| `harness/tokens.py` | token estimation shared by every cost preflight |
| `harness/ui.py` | the pywebview desktop shell around the harness web UI |
| `tests/` | one test module per product owner (test_spend, test_panel, test_convergence, test_specialist, test_chat, test_ledger, test_prompts, ...); shared fakes and the `_gov` helper live in `tests/_fake.py` |

## Audit corpus integrity

The self-audit (audits/self/audit.py) reads a pinned evidence corpus --
the dogfood artifacts and audit reports under audits/self/. D11 verifies
every pinned file's SHA-256 against audits/self/corpus_manifest.json
before the evidence is trusted: a silent edit fails the audit
deterministically. To change corpus content, run the scripted refresh
and commit the corpus change and manifest as one reviewable diff:

  python audits/self/refresh_corpus_manifest.py

Never hand-edit corpus_manifest.json. round2_scores.json is
deliberately unpinned (rewritten by every audit run -- restore it from
git after every local run); _runs/ is untracked scratch.

## Coverage baseline

The battery's reach is measured, not just counted. D12 compares harness
lines changed since the coverage baseline's commit against
audits/self/coverage_baseline.json (the line numbers the full battery
executed under stdlib trace): changed executable lines that the traced
suite never ran fail the audit below a 95% bar. Regenerate the baseline
with the traced battery run and commit it with the code change it
reflects:

  python audits/self/refresh_coverage_baseline.py

A traced run costs roughly a minute; do it when landing substantive
harness changes, not per commit. Missing data is a visible SKIP, never
a silent pass.

## Release driver

audits/self/release.py mechanizes docs/releasing.md's mechanical steps in
order: state gate (clean tree, HEAD == origin/main, one non-polled CI
look -- a queued run exits 3 with "still queued, re-run me"), the release
edits (CHANGELOG flatten with a fresh "Nothing yet." [Unreleased], version
bump in both single-source sites, editable reinstall with a metadata
check), the release battery (ruff; compileall + the full unittest battery
under -W error::ResourceWarning with the R13 leak-signature scan, per
interpreter -- local default plus the uv-managed CI set; then the
self-audit with a hard BAR MET gate and the round2_scores.json restore
verified), and the publish mechanics (build, twine, outside-repo venv
smoke with the direct site-packages/harness/ui probe).

THE SPLIT: the script automates mechanics and gates each step on the
previous one; a human still decides WHEN to release, reviews and merges
the PR, and runs the tag/publish steps -- the script prints them, it does
not decide them. The driver calls the repo's own checks (including the
self-audit BAR MET gate); it never bypasses or reimplements an audit
check. Rehearse without mutating anything:

  python audits/self/release.py --dry-run

The S6 pin lives in tests/test_release_driver.py (interpreter coverage,
audit-invocation shape, and leak signatures identical to R13's owner).
