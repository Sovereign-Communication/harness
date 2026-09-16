# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
**until 1.0.0**, after which it will adhere to it strictly (the 0.x line may
break APIs between minor versions).

## [Unreleased]

### Added
- **Batch fail-soft (`apply --keep-going`).** A multi-file batch can
  continue past a failed file instead of aborting: every per-file result --
  failures included -- stays in the batch envelope, the overall status
  names the FIRST failure (a later success can never mask a mixed batch
  into `ok`), and the shared-gate verdict reports not-passed. Fail-fast
  remains the default and is byte-identical to prior behavior; the flag
  lives on the apply parser only (continue/dogfood untouched); no retry
  logic, no analytics. Proven through the real CLI entry point (mixed and
  default runs via `cli.main` with the emitted `--out` JSON), plus loop
  pins on the engine batch surface.
- **Apply change preview in the UI.** Every changed-terminal apply result
  (preview, ok, gated ok, escalated ok) now carries a unified `diff` of the
  touched file plus its `file` path, computed once where the run already
  held both sides in memory (`results._content_diff` -- no filesystem reads,
  no new capability). The web UI's result summary renders it as a
  color-coded, escaped, 400-line-capped changes block -- the scoped-edit
  trust surface, visible exactly where trust gates force preview-only --
  with the raw-JSON toggle remaining for the full envelope. Proven end to
  end: unit pins on every terminal shape, a server-level test that the
  envelope reaches `/api/runs/{id}/result` unstripped, a live probe through
  the real served server, and a render proof executing the actual
  `resultSummary` against the live payload.
- **Rankings surface (server + UI, strictly read-only).** `GET /api/rankings`
  serves the latest rankings report verbatim — the same data the weekly
  workflow files as its artifact (`window`, `top`, `climbers`,
  `ranked_in_catalog`, `proposed_candidates` with probe verdicts) — plus the
  list of reports on disk, newest first. A missing or unreadable report is a
  200 with `available: false` and an actionable note (the empty/stale state
  is normal, never a silent fallback to an older file). A Rankings view in
  the web UI renders it with the established enter-to-refresh contract. The
  hard constraint holds: the endpoint and view never generate, probe, or
  mutate configuration — `harness rankings` stays the one producer, so
  nothing auto-mutates. Covered by endpoint tests through the real server
  surface, including a mechanized read-only check.
- **MCP progress streaming (the one deferred UI-readiness item).** A client
  that includes `params._meta.progressToken` on an identified `tools/call`
  now receives one `notifications/progress` frame per typed run event
  (`panel_call`, `gate_end`, `rotation`, ...) while the tool runs: same
  token, monotonically increasing `progress`, human-readable `message`, no
  `total` (the lanes don't know one). Frames stop at completion (the sink
  is removed with the request -- success, error, or cancel); a request
  without a token gets zero progress frames, exactly the historical
  behavior, and a malformed token is ignored (`_meta` is advisory -- a
  telemetry preference can never fail a run). Implemented as an events-bus
  sink bound to the request, so panel/apply lanes stay telemetry-only and
  the protocol adapter owns only the frame translation. Proven end to end
  through the real stdio frame loop.

### Changed
- **The rankings envelope-to-UI field contract is mechanized.** A contract
  pin derives every member read the real `loadRankings` makes from app.js
  source (not a hand-list) and asserts each resolves on an envelope produced
  by rankings.py's real builder through the real endpoint assembler,
  including both `available: false` fallbacks; where a node runtime exists,
  the real renderer is executed against the real envelope with per-row value
  co-occurrence. Vacuity-proven: planted key renames on either side (UI or
  server envelope) fail the battery instead of silently blanking the view.
- **Skip hygiene is mechanized.** The suite's 10 skips (on Windows) are all
  environment gates, not convenience: 7 symlink-privilege gates
  (WinError 1314 without Developer Mode -- security-relevant symlink-escape
  paths that CI's Linux legs run for real) and 3 POSIX-mode-bit gates; the
  optional-dep gates (local_fit's numpy/onnx training deps, the live-key
  catalog freshness check) follow the same classified pattern and are
  already exercised wherever the environment provides them. A new
  architecture-guard class (`SkipHygieneTests`) keeps it that way: every
  skip reason must state its category (platform / privilege / optional dep
  / live credential), and blunt unconditional `@unittest.skip` disables are
  banned -- proven to fire on both violation shapes.
- **`ci.yml` gains a `workflow_dispatch` trigger.** CI never fired for the
  branch's final heads (zero check-runs for `f187cb3`/`141d21a` across two
  pushes -- confirmed environmental, PR #8 merge-basis comment); a manual
  dispatch is the cheapest re-emit path once Actions minutes are restored.
  No job, matrix, or gate content changed.
- **One per-file options owner for the batch CLI face.** `cli._cmd_apply`'s
  ~18 hand-threaded kwargs into `apply_batch` (re-packed into a dict by
  `run_batch`) collapsed into the immutable `BatchOptions` bundle: one
  definition constructed in one place, consumed by the loop, and every
  caller (CLI apply/continue/dogfood, MCP tools/call, both server task
  runners) now passes it -- `run_batch` is single-mode. Run-level knobs
  (task_id, keep_going, apply_pool, cancel_check, resume routing) stay
  run_batch parameters -- they describe the batch, not a file's session.
  `_cmd_continue` now builds the same bundle through the same helper.
  `--keep-going`'s apply-parser-only scope is documented at the definition
  site as the deliberate divergence it is (the multi-file batch is the only
  multi-file surface). Behavior byte-identical: full battery green; the
  bundle's field defaults are pinned equal to run_batch's legacy signature
  (vacuity-proven) and the run-level pool/cancel-check merge is pinned with
  non-None values.

### Fixed
- **Resume through a BatchOptions bundle delivers the saved state.** The
  dual-mode options path silently dropped the run-level `continuation` to
  None in the per-file payload (the merge covered only apply_pool and
  cancel_check), so a CLI resume ran as a fresh apply; no test drove a
  successful resume end to end. Single-mode run_batch now validates and
  delivers the run-level parameter (or the bundle-carried one), pinned by
  a regression test and proven end to end through the real CLI entry
  point on both resume faces (apply --continue-from, continue).
- **Miscounted-diff hunk headers no longer waste the apply lane.** Dogfood
  evidence recorded 12/12 near-miss refusals of diffs whose body lines were
  correct but whose `@@` header miscounted ("truncated: expected -6/+24,
  got -6/26"), each burning a full failed apply round; the worst variant
  merged while silently dropping the body's tail. Hunk bodies now parse to
  their natural end (next `@@`, junk line, or EOF) and the body decides; a
  recovered hunk must still describe a change, a short old-side at EOF is
  still refused as truncation-ambiguous, junk after a miscounted body is
  still refused, and the exact-source match still gates every line that
  reaches disk -- the validation gate is unchanged.

## [0.3.0] — 2026-09-15

### Added
- **Explicit reasoning disable (the "off means OFF" fix).** `off`/`none`
  now send `reasoning:{"effort":"none"}` instead of omitting the key --
  omitting means the provider default (reasoning ON) for reasoning-native
  models, the failure that killed the Sep-13 BoD runs. The disable payload
  carries no token cap; a mandatory-reasoning route (glm-5.3-flash,
  gpt-5-mini) draws the documented HTTP 400 and the existing param-rejection
  retry runs the provider default with the rejected attempt's billable cost
  merged. glm-5.3 joins the reasoning hints. `off` is now the vote-lane
  default.

- **Task-shaped lane budgets (ONE owner).** `config.effective_lane_policy`
  is the single owner of per-lane output budgets and reasoning modes: votes
  >= 4096 with reasoning disabled; judge synthesis and escalation rungs
  >= 8192 with `auto`; the convergence specialist stays at the vote floor
  (its window-aware trim must stay usable on small-context specialists);
  apply keeps 4096. Explicit caller configuration always wins; defaults
  flow through preflight reservations, actual-cost accounting, run metadata,
  and ledger evidence.

- **Verified 2026-09-13 model slates (paid tier).** `DEFAULT_PANEL_PAID`
  moves to the probe-verified vote pool (deepseek-v4-flash, v4.1-flash,
  ling-3.0-flash, gpt-4o-mini, gpt-5-mini, gpt-5.6-luna, gemini-3.8-flash);
  the paid judge is `z-ai/glm-5.3-flash` (value deep-thinker);
  `deepseek-v4.1-flash` leads the apply pool; the escalation ladder is the
  verified deep-think tier (glm-5.3-flash, deepseek-v4-pro, gpt-4.1,
  gpt-5.6-sol). gemini-2.5-pro, gpt-5 (non-mini), and the V3-era deepseek
  ids are dropped per operator rulings. `DEFAULT_MAX_COST` rises to $0.05
  (actual verified 5-vote cost: $0.0038; `HARD_MAX_COST` unchanged).

- **Rankings-driven candidate refresh.** `harness rankings [--probe]` (new
  `harness/rankings.py`) pulls the OpenRouter daily rankings, aggregates
  top/climbing models, intersects with the live catalog, and proposes
  pool candidates; `--probe` gates each candidate through a billable
  one-vote probe (reasoning disabled). A scheduled weekly workflow
  (`.github/workflows/rankings.yml`) files the report as an artifact;
  nothing mutates configuration automatically.

- **Canonical service layer (`harness/service.py`).** The web server's
  verify runner and the CLI's verify path consume ONE assembly: shared
  BOM-tolerant prompt/claims-window reading, the cancelled-run spend
  envelope, and cost/meta attachment. Server tests now pin the service
  seam.

- **Non-authoritative deterministic tally artifact.** When a structured
  judge is exhausted (unparseable/failed), the consensus now carries a
  `tally_artifact` rendering the deterministic per-claim vote summary,
  explicitly marked `authoritative: false` -- reviewable evidence, never a
  fabricated verdict.

- **Pool-policy visibility.** Learned-BYOK filtering and strike/demotion
  gating announce what they remove (stderr notes + `pool_filtered` events);
  silent pool shrinkage is gone. The UI live feed renders `pool_filtered`
  and `rankings_probe` events, and the Trust view's Refresh button works.

- **Full web-UI parity for the verify lane.** The Dispatch → Verify tab now
  covers the structured-claims workflow (claims manifest + source file +
  optional definitions), with pre-network lint rejection surfaced as a
  `rejected` run; `/api/trust` exposes the CLI `trust` snapshot on a new
  Trust view; Result cards show a human verdict summary (verdict, votes,
  judge, cost, synthesis) with the raw JSON envelope behind a toggle; every
  view refreshes when entered; Settings shows server status from
  `/api/status`.

- **Judge-seat fallback rotation.** A failed judge (HTTP 5xx/408/429,
  reasoning-only, truncated, or unparseable body) no longer discards a
  converged panel's evidence: one bounded same-seat retry on transient
  errors, then rotation to un-voted free panel-pool members. Every fallback
  call is preflight-reserved, so the worst-case cost guarantee holds; paid
  judges keep single-attempt semantics; the seat still never fabricates a
  verdict (an exhausted seat defers with raw panel outputs).

- **Truncation honesty on the judge seat.** A judge body cut off mid-JSON
  (unbalanced braces/fence) is reported as `truncated`, not lumped in with
  complete-but-malformed `unparseable` output (the Sep-11 seat-gate loss).

- **`harness ledger defer-stats [window]`.** Operator aggregate of WHY runs
  deferred: panel defer rate (lost judges), mid-task categories, consent
  outcomes. Also on the web UI Ledger view and
  `GET /api/ledger/defer-stats`.

- **gpt-5 / o1 recognized as reasoning models.** `looks_reasoning` hints
  extended so `auto` effort caps hidden thinking for OpenAI reasoning models
  (three live `bod-governance` judge calls failed reasoning-only on Sep 13).

- **Multi-rung apply escalation ladder (opt-in).** When `allow_escalation` is
  set and `escalation_pool` is configured, a failed cheap apply walks the
  ladder (cheapest → most capable). Each rung produces COMPLETE file content
  and is finished through the real verification gate.

- **Minority-dissent demotion.** Structured panels record models that vote in
  the minority on defect claims (`minority_models` + ledger
  `panel_minority_dissent`). After two strikes, `order_pool` sorts them below
  unproven peers (same policy as unusable/consent-unusable). A lone dissenter
  that is *correct* is not banned — it is demoted after *repeated* lone
  dissent that invents conflicts.

- **MCP shared-secret auth (optional).** `HARNESS_MCP_AUTH_TOKEN` /
  `mcp_auth_token`: when set, `tools/call` requires matching
  `params._meta.harness_token`. Unset keeps the documented stdio trust model.

- **MCP `panel_verify.task_max_cost`.** Optional per-call ceiling (0–0.25);
  refuses before network spend if the session budget cannot absorb it.

- **Polish pass (sandpaper):** README test-coverage list rewritten to match
  the current suite; `Router.next_model` and `spend.resolve_models`
  deleted with their tests (zero production callers, per the no-dead-code
  rubric); lint extended to `B007/B017/B904/UP015/UP031` with chained
  (`from e`) or suppressed (`from None`) raises throughout; output
  hygiene enforced (config warnings and ledger quarantine notices go
  through `eprint`, `[ledger]` stays audible under `--quiet`, the
  capabilities table moved off stdout so piped JSON parses); security and
  releasing docs updated to the MCP lanes, trust gates, anchors, and the
  real validation commands.

- **Dogfood-driven close-out of the four deferred audit items** (panel
  evidence under `audits/self/dogfood/item{1,2,3b,4}.*`):
  - *Ledger rotation anchors.* Every rotation opens the fresh active file
    with a chained `segment` event naming the moved file and its tip, so
    segment boundaries stay cryptographically linked. New `chain_status()`
    reports validity plus retention shape (`segmented`, per-file bounds,
    `first_retained_seq`, `pruned`); a pruned prefix reports as an explicit
    cut, never as genesis. `repair()` now truncates only the cut file,
    leaves healthy segments byte-identical, and is a no-op on healthy
    ledgers; `ledger verify` / MCP `ledger_status` surface the chain
    status. The dogfood panel confirmed the gap 2/2 before the fix.
  - *MCP lanes + deadlines.* Three serial workers (`mutation`,
    `spendy`, `observe`) replace the single worker, so a long apply/panel
    no longer head-of-line-blocks status queries (frames correlate by id,
    never position). Every request carries a cooperative deadline
    (`HARNESS_MCP_TOOL_TIMEOUT`, 60..7200s, default 1800) tripped through
    the same cancel path as `notifications/cancelled`. Confirmed 3/3.
  - *Per-caller tagging.* Ledger events carry the session caller id
    (`cli`, `mcp`, `mcp:<name>/<version>` from initialize clientInfo);
    reports break out `per_caller` history and host trust scores named
    peers separately, with the untagged global as fallback. New `harness
    trust --caller` and MCP-side attribution included.
  - *Tally-first judging.* The context-budget trim shapes only a copy for
    the judge prompt (disclosed in-prompt); the deterministic tally counts
    full votes and the result keeps them, so a resource-cap trim reports
    as `trimmed_for_judge` -- never as transport `panel_shortfall`.

- **Dogfood found a live crash first:** integer settings arrived as floats
  (`finite_number` returns float), so `range()/max_workers` crashed the
  first live panel fan-out -- a path the hermetic suite never took.
  Settings coerce back to int, panel hardens its target, regression
  pinned. The run spent $0.00 before failing.

- **Free-tier diff-merge finding:** 12 model attempts across two items
  wrote near-miss unified diffs (correct content, wrong hunk counts)
  that the strict merger refused 12/12. Model-written diffs are not a
  viable lane at this tier today; panel verification + hermetic gates
  carried these items instead. Evidence in the item reports.

- **Whole-file exercise (bench schema validation):** panel-confirmed,
  then 9 model whole-file attempts failed file fidelity (fences left in,
  dropped functions, syntax breaks) before one honest capability
  deferral; the 5-line fix landed directly with the red test as gate.
  Two self-hosting findings came free: a deferred self-edit that breaks
  `harness/bench.py` used to kill the CLI at its own import line (bench
  imports guarded at module level now -- `continue` survives), and
  whole-file writes normalized CRLF checkouts to LF (fixed:
  `_atomic_write` now aims at the target's detected style, with an
  explicit byte-exact mode that snapshot restore uses).

- **`run_bench` validates its schema:** a task missing `instruction`
  aborts as `HarnessError` like every other manifest schema error (was a
  raw `KeyError`), and an unnamed task defaults to `"task"` mirroring
  the loader.

- **Local model-fit advisory layer wired into pool ordering
  (`harness/local_fit/`, opt-in, off by default).** A small locally-trained
  neural net (no LLM, no runtime dependencies) scores each candidate model
  seat for unusable/truncated/usable-stop risk from pre-dispatch features
  only (task, lane, declared profile, ledger calibration), and — only when
  explicitly enabled — demotes scorer-flagged likely-unusable models after
  their peers *within* the existing demotion tier of
  `capability.order_pool`. Three-stage flag gating: OFF (default; the hook
  is never imported or called), OBSERVE (`HARNESS_LOCAL_FIT_ENABLE` +
  `HARNESS_LOCAL_FIT_MODEL_DIR`: score-and-log, order untouched), INFLUENCE
  (`HARNESS_LOCAL_FIT_USE_ADVISORY_ORDER=1`, threshold
  `HARNESS_LOCAL_FIT_UNUSABLE_THRESHOLD` default 0.6, inclusive). The hook
  is fail-closed end to end: any error degrades to the baseline order, and
  it can never cross the strike-demotion boundary or reorder unflagged
  models among themselves. Training/eval over `audits/*/_runs/*/*.json` via
  the read-only extractor; train-time deps are the optional
  `local-fit-train` extra; runtime inference is pure stdlib
  (`model_weights.json` + `infer.py`, parity-pinned against the ONNX
  export). Train/serve feature parity is enforced by a pinning test; the
  enabled ordering path is tested to never import numpy/onnx/onnxruntime.

- **Local-fit merge review (same bar as everything else):** the prototype
  `hook`/`dispatch_hook`/`advisory` seam is deleted (unreachable from the
  live path, with contradictory tier-crossing ordering); eval observed
  maps are train-only (the old per-split maps leaked eval answers into
  eval features); specialist rows read model/raw/cost from the conv dict;
  shuffling is seeded; ONNX export derives width from the net; train-test
  numpy imports are guarded for clean CI. Honestly measured on 9 v4 runs:
  leakage-free eval top1 0.647–0.867 (mean 0.761 vs ~0.64 majority), and a
  leave-one-run-out routing study shows the net recapitulating ledger
  observed rates with no measurable lift over the trivial baseline
  (Spearman +0.730 vs +0.750; precision@1 8/8 both) -- INFLUENCE is safe
  to ship fail-closed, value on larger corpora unproven; see
  `harness/local_fit/RE_MERGE_READINESS.md`.

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

- **MCP batch parity: `apply_edit` accepts a `file` array.** Multi-file
  batches run through the new engine-owned `ApplyEngine.apply_batch` (one
  governed session per file, shared budget, fail-fast, shared-gate
  aggregation) — previously the batch loop lived only in the CLI, so MCP
  had no batch capability at all.

- `--quiet` on `bench` and `continue` (verify/apply had it; the others
  rejected it with an argparse error).

- Fixed the `--quiet` flag itself, which the facade split had silently
  disconnected (the CLI wrote `core.QUIET`; `eprint` reads `output.QUIET`).
### Changed
- **No verify assembly left handler-side.** `mcp.py`'s `panel_verify` no
  longer lane-defaults locally (panel pool, judge, convergence model,
  specialists): validated-None arguments flow through to
  `service.run_verify`, whose caller→router→settings resolution is the one
  owner. `cli._read_text` is now a delegation stub to
  `service.read_text_file`, the ONE BOM-tolerant reader; the name stays for
  its other call sites and patch seam. Error wording unchanged.

- **One resolved-inputs owner for verify.** `service.run_verify`'s eight
  inline `arg or router.X or settings.X` fallback chains collapsed into the
  immutable `ResolvedVerifyInputs` struct (the `PanelLanePolicy` move),
  built once and read by `pre_run_warning` and the `panel_judge` call;
  fallback order preserved exactly, public signature unchanged, behavior
  proven byte-identical by a nine-scenario resolved-inputs oracle.

- **MCP verify lane unified onto the service layer.** `panel_verify` no
  longer imports `panel_judge` or self-assembles convergence/router kwargs:
  it delegates to `service.run_verify` (the same owner the CLI and web
  server consume) with session-injected governor/ledger/transport/router,
  keeping only its protocol concerns -- boundary validation, tool schemas,
  JSON-RPC frames, and its historical result shape (no meta/cost
  attachment, no service-side task id). The cancelled-run envelope moved to
  the service; MCP still answers its established `-32800` protocol error.

- **MCP tool schemas are pure data.** The ~140-line schema dict literal
  moved from `mcp.py` into a new `mcp_schemas.py` data-only module (no
  imports, no logic), leaving the protocol adapter as framing + lanes +
  dispatch (785 → 664 lines). Byte-identity proven by replaying the full
  `tools/list` response through the real stdio frame loop before and after
  the extraction.

- **MCP lane scheduling is pure policy.** `LANES`, the lane membership
  constants, and `lane_for` moved verbatim from `mcp.py` into a new
  `mcp_lanes.py`, leaving the protocol adapter as framing + dispatch +
  cancellation lifecycle (664 → 646 lines). Per-lane dispatch proven
  byte-identical by an instrumented frame-loop probe (one request per
  lane, submit-to-pool correlation) before and after the extraction.

- **One immutable-struct idiom.** `PanelLanePolicy` and
  `ResolvedVerifyInputs` converted from `__slots__` + read-only-property
  boilerplate to frozen dataclasses (`apply_state.py`'s idiom), deleting
  ~33 net lines of boilerplate; the keyword-only constructors are kept
  (`init=False` + `object.__setattr__`) so resolution logic, attribute
  surface, and every call site are unchanged. Behavior proven identical by
  the nine-scenario resolved-inputs oracle and the lane-policy
  attribute-surface replay -- and immutability is now genuinely enforced:
  attribute assignment raises `FrozenInstanceError` (the previous idiom was
  mutable-by-convention; nothing relied on that laxity).

- **The struct-idiom rule is universal.** `CapabilityProfile` converted to
  a frozen dataclass (the last bare-`__slots__` class): the TTL refresh
  goes through `dataclasses.replace`, and the custom init gained a
  `_fetched_at` keyword (replace re-calls `__init__` with every field) so
  every existing caller stays valid. Zero `__slots__` remain in
  `harness/`, enforced by a new architecture-guard check.

- **CLI claims assembly consolidated.** `harness verify --claims-file` now
  prepares its prompt through `service.prepare_verify` (one owner for
  BOM-tolerant reading, manifest parsing, grounding, convergence
  activation, and reassurance derivation); the CLI keeps lint printing and
  error wording verbatim.

- **Paid-lane default ceiling.** `DEFAULT_MAX_COST` $0.02 -> $0.05 to fit
  the wider verified vote pool's worst-case reserve (~$0.04). The hard
  ceiling (`HARD_MAX_COST`, $0.10) is unchanged.

- **UI close-out.** `#btn-trust-refresh` handler wired; trailing-EOF fix
  in `harness/ui/app.js`.

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
### Fixed
- **UI assets missing from the wheel.** `harness/ui/*` (index.html, app.js,
  app.css) now ship via `[tool.setuptools.package-data]`; previously an
  installed `harness serve` / `harness desktop` 500'd every static page.

- **Abort-spend honesty.** A cancelled verify no longer drops the spend of
  in-flight panel calls that billed before the cooperative cancel landed:
  the envelope reports the true `actual_cost` and per-model breakdown, and
  the UI maps the lane-level `ToolCancelled` to an honest `cancelled` run.

- **Event-sink leak in the UI server tests.** `ServerHarness` now uninstalls
  its events sink on teardown; leaked sinks could silently starve later
  servers once the bus's `MAX_SINKS` cap was reached.

- **Cancel works in the UI verify lane.** `run_verify_task` accepted the
  run's cancel closure but never forwarded it to `panel_judge`, so Cancel
  was a silent no-op in the web UI's main lane (apply and continue already
  forwarded it). A cancelled verify now aborts at the next check (in-flight
  calls are discarded, no judge call, no verdict), and the run reads
  `cancelled: "cancelled by user"` instead of a misleading `error`.

- **Result on a failed/cancelled run.** The UI Result button rendered
  `null`; it now shows the run's error message.

- **BOM tolerance on inbound files.** `--claims-file`, definitions files,
  and CLI text/JSON readers decode `utf-8-sig`, so PowerShell-redirected
  handoff artifacts (BOM'd `claims.json` from the rule8-281 run) load
  instead of failing `json.load` with "Unexpected UTF-8 BOM".

- **Verdict honesty.** Panel tallies print `N R / M NR` vote counts, not
  `(3/3)` participation that looked like unanimity. Shortfall lines say
  `SHORTFALL` explicitly.

- **Specialist vs tally.** When the specialist's claim map disagrees with the
  deterministic majority, the conflict is recorded on
  `convergence.specialist.tally_conflicts` and the tally is named authoritative.

- **MCP verify default tokens.** `max_tokens` default raised 300 → 2048 to
  match the CLI verify lane.

- **Atomic write staging.** Temp files are staged under `realpath(parent)`.

- **Bench snapshot.** `.orig` created with `O_CREAT|O_EXCL`.

- **Verify gate tokenize.** Engine always shell-tokenizes `verify_cmd`;
  PATH existence remains opt-in for hermetic stubs.

- **Library apply filesystem jail.** `ApplyEngine(allowed_roots=...)` (wired
  from `settings.mcp_allowed_roots` / `HARNESS_MCP_ALLOWED_ROOTS`) refuses
  targets outside configured roots via realpath. Empty roots keeps the
  historical unrestricted CLI behavior.

- **Backup TOCTOU.** Backups use `O_CREAT|O_EXCL` with a unique dest name
  and never prune the just-created file.

- **Ledger load integrity.** Load recomputes each entry hash + prev_hash
  linkage, requires integer `seq`, flags `chain_broken`, and `verify()`
  fails closed when load quarantined damage (never a silent pass).
  `repair()` heals quarantined torn tails (unsegmented rewrite; segmented
  keeps older segments byte-identical).

- **local_fit extract skips non-run JSON** (list `summary.json` etc.).

- **PR #4 review (cubic/codex) follow-ups.** Escalation `needed` must be JSON
  boolean `true`; invalid `target_rung` coerces to 0; specialist
  `escalation`/`plan` surface on the consensus payload; apply ladder enforces
  `--task-max-cost`, handles `diff` backend + `HARNESS_DEFER`, seeds judge
  condensed context into RunState; legacy `escalation_model` keeps precedence
  over the default ladder; `judge_top` used only when escalation is allowed;
  dead `RunState` plan fields removed; train-time skips require
  numpy+onnx+onnxruntime; `_defined_in` ignores comment lines; paid-pool
  test no longer tautological; local_fit extract test accepts extra run dirs.

- **`--out` creates parent directories.** Relative paths like
  `results/foo.json` no longer fail after a paid/free panel run with
  `cannot write --out` when the parent folder is missing.

- **Claims lint sees Python definitions.** `_DEFN_RE` now matches
  `def`/`class` as well as Rust `fn`/`const`/… (and `pub(crate)`).

- **Train-time tests skip without the `local-fit-train` extra.**
  Clean runners without numpy/onnx no longer error; classes that import
  `train` are skipped with an explicit reason.

- **Local-fit advisory scores were inert on real data (saturation).** The
  trained net's real-data logits were tiny, so exported probabilities
  saturated (p_unusable ~0.15 for every candidate, spread ~0.03) and the
  INFLUENCE path reordered nothing at any threshold. Three-part mechanism
  fix (data expansion is intentionally out of scope):
  export-time temperature calibration (`train.export_temperature`, persisted
  as `temperature` in `model_weights.json`/`model_meta.json`, applied as
  logits/T by the stdlib scorer so the ONNX parity pin still holds); input
  z-score clipping (`Z_CLIP=8`) in the runtime scorer so unseen-at-train-time
  dispatch features (e.g. declared context length) cannot blow logits into a
  pinned softmax; and a fail-closed degenerate-artifact guard in
  `dispatch.maybe_order_pool` that detects unseparable score maps
  (`score_spread_too_small`) and collapsed top-class probability
  (`top_class_saturated`), keeps the baseline order, reports the reason on
  the result and stderr (`capability.order_pool` logs the stand-down).
  A pool that legitimately scores all-healthy (real spread, unpinned top-1)
  is explicitly NOT degenerate: no reorder is then the correct outcome.
  New mock-free end-to-end test trains on synthetic audit data containing a
  genuinely failing model and proves INFLUENCE demotes it within its
  strike-demotion tier while OFF/OBSERVE stay order-identical to baseline.

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
