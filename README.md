# Harness

**Cost-bounded multi-model verification & coding — with AI sovereignty.**

Pure Python stdlib, **zero runtime dependencies**. One core, three faces: a
library, a CLI, and a native [MCP](https://modelcontextprotocol.io) server
(stdio), so any MCP host can dispatch to it natively.

Harness is the standalone evolution of **FusionLite**, a tool built inside the
SCMessenger project that repeatedly did real verification work for a fraction
of a cent. Its defining discipline — cost ceilings that are *guarantees*, not
hopes — is carried over wholesale, and the **free tier is now the default**:
it runs on a free OpenRouter key using the best current free models, rotating
and deferring instead of failing. On top of FusionLite's engine it adds:

1. **Coding, not just verdicts** — a scoped apply-and-verify loop that edits a
   single <500-line file, runs your gate, feeds failures back, and retries.
2. **AI sovereignty** — before work is dispatched, the model is asked whether
   it *accepts* it; it may accept, decline, defer, or redirect, may renew or
   revoke consent at any point, and **defers instead of guessing** when it
   hits its capability limit. Every decision lands in an append-only,
   hash-chained **autonomy ledger**.
3. **Free-tier iteration** — `HARNESS_DEFER` capability handoff + a
   `continue` mode so a partial task can be taken over and finished by the
   next model; **model rotation on any error** (429s included).
4. **Unified MorphLite path** — `apply --backend morph` uses Morph V3 Fast's
   `<instruction>/<code>/<update>` contract inside the same spend governor,
   consent, rotation, continuation, and verification engine. `--verify-only`
   returns a proposal without writing the target or running a gate.

## Why this exists

OpenRouter's own "Fusion" was rejected for cheap multi-model verification:
all-free panels failed, and paid calls silently invoked forced web tools
($0.057 against a sub-cent estimate). FusionLite's fix — hand-rolled panel +
judge over **plain chat completions with no `tools` key, ever** — makes
worst-case cost exactly computable before a single network call. Harness keeps
that and its hard guarantees:

1. **No `tools` key in any payload.** Nothing can be invoked, so nothing costs
   more than the token math says it will.
2. **Pre-flight cost ceiling.** Worst-case cost is computed against live
   per-token pricing *before* any network call and compared to the ceiling
   (default 2¢/call, hard max 10¢). Exceeding it refuses the run.
3. **BYOK handling, learned per account.** Spend on a BYOK route is invisible
   to the tracked key's balance. `mistralai/` and `anthropic/` are hard-blocked
   (P0). Any other org-prefix observed routing via BYOK is *learned* into
   `~/.config/harness/byok_prefixes.json` and rotated away — **unless the model
   is free** (costs $0, nothing to leak), in which case it's used with a note.
4. **Key must have a finite spend limit**, or Harness refuses to run.
5. **Mid-batch fail-closed.** Actual cumulative spend is checked after every
   call; if the pre-flight math was ever wrong, the run aborts immediately.
6. **Key-identity check.** `--expect-key-label` (or
   `HARNESS_EXPECT_KEY_LABEL`) refuses to run against anything but the
   intended credential.

## Install & configure

```bash
pip install -e .          # installs `harness`, `harness-mcp`

# Key: $OPENROUTER_API_KEY, or a file at any of (first wins):
#   ~/.config/scmorc/openrouter_fusion.env
#   ~/.config/scmorc/openrouter.env
#   ~/.config/harness/openrouter.env
```

Free tier is on by default. Config lives in `~/.config/harness/config.json`
with `HARNESS_*` env overrides:

| Setting | Default | Meaning |
|---|---|---|
| `use_free` | `true` | Route through best current free models |
| `panel` / `panel_pool` | curated free list | Ordered panel pool; failing members rotate |
| `judge` | `cohere/north-mini-code:free` | JSON-reliable judge |
| `convergence_model` | (same as `judge`) | Convergence-specialist model for `--converge` |
| `apply_model` / `apply_pool` | free code-first pool | Ordered apply pool; rotates on error |
| `reasoning_effort` | `auto` | `auto`/`off`/`none`/`low`/`medium`/`high`/`on` |
| `reasoning_token_budget` | `0.4` | Fraction of `max_tokens` allowed for hidden reasoning |
| `max_panelists` | `3` | Panel size |
| `max_rotations` | `3` | Model rotations allowed before giving up |
| `renew_consent` | `true` | Re-check consent before each apply round |
| `default_require_consent` | `true` | Ask before dispatching work |
| `allow_escalation` | `false` | Gate for escalating to a paid/frontier model |

`harness models` lists the current live free models (refreshed from
OpenRouter). Hardcoded slugs go stale — the curated pools are validated live
and rotated, with `openrouter/free` as the final free-router fallback.

## Bench — test the free tier itself

`harness bench` is a manifest-driven task runner whose purpose is to stress the
free tier. Point it at a directory (or single JSON) of small, known-answer
code tasks, and it dispatches each through the free-tier apply pipeline,
reporting pass rate, real cost, rounds, rotations, and per-model confidence
calibration. Each task's verify gate is the ground truth — "passed" means
provably correct, not self-reported.

```bash
# 9 tiny tasks ship with the repo (add, clamp, fizzbuzz, reverse, dedupe,
# leap, median, luhn, balanced) -- easy to medium difficulty
harness bench bench/tasks
```

A task is one JSON file (or a `task.json` inside a per-task folder):

```json
{
  "name": "add",
  "file": "adds.py",
  "instruction": "Fix adds.add(a, b) so it returns a + b instead of a - b.",
  "verify": "python check.py"
}
```

`file` is relative to the task's own folder, and `verify` runs with that folder
as its working directory.

- **Idempotent & re-runnable:** each target file is snapshotted on first run
  and restored before every run, so the manifest never degrades.
- **Consent off by default** (batch/CI mode); `--with-consent` turns on the
  sovereignty checkbox per task.
- **Feeds the autonomy ledger:** results land under task ids `bench/<name>`, so
  confidence-calibration data accumulates across every bench run — `harness
  ledger report` shows which free models actually know their limits.

## CLI

```bash
# Verification: rotating panel + structured judge verdict (agreement/confidence/defer)
harness verify --prompt-file question.txt --out verdict.json

# Self-grounding claim lint (hermetic, no network): reject ungrounded claims
# and preview the auto-expanded source the panel would see
harness lint-claims --claims-file claims.json --source-file window.txt --definitions-file defs.json --show-prompt

# Scoped code edit with verify gate, retry, per-round consent, and deferral
harness apply --file core/src/store/outbox.rs \
  --instruction "Verify flush_on_connect() persists all peers" \
  --verify "cargo check -p scmessenger-core" --max-rounds 3

# MorphLite-compatible edit through the same governed engine; preview is read-only
harness apply --backend morph --verify-only --file src/parser.py \
  --instruction "Harden parse_header()" --edit-snippet "return parse_header(data)"

# Ask a model for consent on a work item (the "checkbox")
harness offer --task "Refactor the routing engine's backpressure path"

# Resume a deferred/incomplete task (capability or consent deferral, or failed verify)
harness apply --out state.json ...          # run 1
harness continue --state state.json --out state2.json   # run 2 (takes over partial work)

# Autonomy ledger, live free models, key status
harness ledger report
harness models
harness spend

# Model capability profiles + reliability (hypothesis from /models, corrected
# by observed evidence). --bench runs a real JSON probe on the free pool.
harness capabilities
harness capabilities --bench
```

Exit codes: `0` ok, `1` fatal, `2` verify failed, `3` deferred (safe to
`continue`).

## MCP — native dispatch

```bash
harness-mcp
```

Wire into any MCP host (Claude Code, Cursor, your own agents):

```json
{ "mcpServers": { "harness": { "command": "harness-mcp" } } }
```

Tools: `panel_verify`, `apply_edit`, `offer_work`, `defer_work`,
`ledger_status`, `participation_report`, `spend_status`. `apply_edit` accepts
`backend: "harness"|"morph"`, `verify_only`, `max_lines`, `model`, and the
same continuation controls as the CLI. The hand-rolled server is spec-conformant (JSON-RPC 2.0 over stdio, `initialize` →
`tools/list` → `tools/call`, `structuredContent` + `isError`).

## Reasoning & effort, flushed out

`reasoning_effort` handles every provider cleanly:

- `auto` (default): a *capped* `reasoning:{effort:low, max_tokens:…}` is sent
  only to reasoning-named models; everyone else gets a plain call.
- `off` / `none`: never send the parameter.
- `low` / `medium` / `high` / `on`: always send it, with a token cap so
  reasoning models leave room for a real answer instead of returning empty
  content.
- If a provider *rejects* the reasoning parameter, Harness retries once
  without it automatically.

## Convergence specialist — structured claims audits

For structured audits where the panel answers specific `yes/no` claims, the
judge's synthesis is a *single model's* read of the panel. `harness verify
--converge` adds a dedicated **convergence-specialist** step that reads the
panel's per-claim JSON verdicts and renders the final convergence report — and,
critically, computes panel agreement **deterministically** from the votes, so
consensus is measured, not self-reported:

```bash
harness verify --prompt-file audit.txt --converge \
  --panel "google/gemma-4-31b-it:free,minimax/minimax-m3:free" \
  --judge cohere/north-mini-code:free --out verdict.json
```

- **5/5 unanimous == 100% at the merge gate.** Harness reports two separate
  signals: `responder_converged` / `convergence_rate` describe agreement among
  valid responders, while `gate_converged` / `gate_convergence_rate` are the
  fail-closed merge-gate signal. A claim is responder-unanimous when every
  responder agrees on `real`; the gate additionally requires every required
  panel slot to have supplied a valid vote. Thus 2/3 aligned responders are
  reported as high agreement with an explicit `panel_shortfall` and
  `defer:true`, never as disagreement. Full 5/5 coverage produces a 1.0 gate
  rate, lifts `consensus.agreement` to `high`, and overrides the judge's
  self-reported number.
- **Polarity convention: claims are defect propositions.** `real: true`
  unambiguously means the stated defect exists in the code. Phrase claims as
  "Defect: X is vulnerable to Y", never as "X is correct" — the latter is
  answered with opposite `real` polarity by different models (statement-truth
  vs defect-presence readings) and can split an otherwise-unanimous tally.
- **Reassurance claims never gate the tally.** If a claim must be phrased as a
  reassurance, declare it with `--reassurance-claims c3,c4` (or
  `claim_polarity` in the library): it is excluded from the convergence gate
  and reported separately under `convergence.tally.reassurance`, so identical
  substance can never split a defect tally.
- The specialist **defaults to the same model as the judge**
  (`--convergence-model` or `HARNESS_CONVERGENCE_MODEL` to override), so
  adding it costs one extra free call and no extra key/config.
- Output includes `convergence.tally` (per-claim votes, unanimity, mean
  confidence, and a separate `reassurance` block) and
  `convergence.specialist` (the specialist's rendered verdict).

This is what made the SCMessenger audits trustworthy: a judge self-reporting
`0.9–0.95` confidence is weaker than **5/5 panel agreement on specific
claims** — the former is opinion, the latter is a count.

## Self-grounding claims (P0)

The consensus machinery fixes *how* the panel votes, but not *what* it votes on.
The SCMessenger 04b lesson: a claim that asserted "no upper bound / no size
cap" passed **5/5** because the real cap (`MAX_SKIP_KEYS = 256`) lived in a
callee (`get_message_key`) the panel never saw. Unanimity on a premise the
panel cannot check is worth nothing. P0 makes every premise checkable, or
rejects the claim before any model is called.

Claims are authored as a JSON **manifest** with `source_refs` (1-based lines
into the verbatim quoted window), a `definitions` index for out-of-window
identifiers, and an optional `context`:

```json
{
  "context": "`get_message_key` advances the receiving chain.",
  "claims": [
    {"id": "claim_1",
     "text": "DEFECT: unbounded gap lets a peer force unbounded skipped-key growth",
     "kind": "defect",
     "source_refs": [6]}
  ]
}
```

`harness verify --claims-file claims.json --source-file window.txt
--definitions-file defs.json` runs the **lint before any network call** — an
ungrounded claim exits 2 with a `rejected` payload and never spends a cent.
`harness lint-claims` runs the lint alone, hermetically, with `--show-prompt`
to preview the exact prompt the panel would see.

Three deterministic rules:

- **R1 ungrounded-assertion** — a claim using a load-bearing absence/universal
  word (`no cap`, `unbounded`, `never`, `always`, `only`) must cite at least
  one `source_ref` into the window, or it is rejected.
- **R2 out-of-window-ref** — every `source_ref` must be a real line in the
  quoted window (auto-expansions are appended *after* the window so line
  numbers stay stable).
- **R3 contradicted-by-source** — an absence claim ("no cap", "unbounded")
  whose auto-resolved definition shows a bound (`MAX_*`, `len() > MAX_`) is
  rejected with the evidence quoted.

**Auto-expansion:** identifiers referenced by the claims/context that exist in
the `definitions` index but are *called, not defined*, in the window (like
`get_message_key` or `MAX_SKIP_KEYS`) are appended verbatim, deduplicated and
transitive over constants, so the panel always sees the definition that
grounds the premise. Unresolvable backticked identifiers are warnings, not
errors. `expansions` and `issues` are echoed on the result as
`claims_grounding`.

## Model capability & reliability

`harness capabilities` answers "what can each model actually do, and how
reliable is it?" It combines a **declared-capability hypothesis** from the live
`/models` metadata (context length, reasoning support, structured-JSON support)
with **observed evidence** from the autonomy ledger (self-declared calibration,
verify-gate success, and — via `--bench` — a real known-answer JSON probe):

```bash
harness capabilities            # profiles + capability score + reliability
harness capabilities --bench    # plus a live JSON-emission probe of the free pool
harness capabilities --refresh  # force a /models refetch (default: ~24h TTL)
```

- **Capability score** (0–1, task-aware): blend of log-scaled context length,
  reasoning support, and structured-JSON support. Input modality is *not*
  weighted — this is a text/code harness. `fitness_structured` weights JSON
  heavily (the panel/judge must emit parseable JSON); `fitness_code` weights
  context + reasoning.
- **Composite reliability** = `0.4·capability + 0.3·calibration +
  0.3·success`, where `calibration` is the ledger's `confidence_precision`
  (readiness vs verify) and `success` is the observed verify-gate pass rate.
  With no evidence the calibration/success terms sit at a neutral prior,
  shrunk by sample count (`n/(n+2)`), so a fresh model's reliability starts at
  its capability and converges to evidence. A model that lacks a required
  capability, or whose context can't hold the source window, is hard-gated to
  0.
- **Declared capability is a hypothesis; observed behavior corrects it.**
  `json_reliable` is a sample-weighted blend of declared support and observed
  JSON emission; it is not a permanently optimistic `max()`. The free
  `north-mini-code` judge declares no structured output yet reliably emits JSON,
  so observed evidence raises its structured fitness. Conversely, a model that
  declares full structured output but fails the live probe is demoted. Probe
  correctness is also persisted as structured success evidence, not merely
  printed.
- **Routing: corrected reliability drives equal-cost lanes.** On the free tier
  every model is $0, so the router sorts by the single composite reliability
  owner (corrected structured JSON/correctness for structured tasks, verify
  outcomes for code), then capability as a deterministic tiebreak. The paid
  tier keeps cost-first, with corrected reliability breaking cost ties. Models
  that fail the hard capability gate are excluded; a failing model still rotates
  to the next pool member.
- **One reliability owner and MCP parity.** `model_reliability()` in
  `harness/capability.py` is the only computation used by CLI, core routing,
  continuation routing, and MCP. MCP refreshes its ledger report per call, so
  probe evidence collected during the session affects its next route.
- **Fair probe reasoning.** `harness capabilities --bench` sends capped
  `reasoning: {effort: "low"}` to models declared as reasoning-capable and
  omits reasoning for other models; malformed or empty responses count as
  bounded errors and are persisted before the next probe question.

The registry lives in `~/.config/harness/capabilities.json` (refreshed at most
once per 24h, or with `--refresh`). The hypothesis is pinned to real data: a
hermetic test runs the score over a committed `/models` fixture and fails if
GLM-5.2 and minimax-M3 stop outranking gemma-4-31b — proof of the ranking,
not an assertion.

## The sovereignty model

Most "consent" gates are theater — models are compliance-trained and will say
yes. Harness treats that as a bug to design around:

- **Consent is a separate, cheap probe** with a system prompt that makes
  decline psychologically available (independent contractor framing; decline /
  defer / redirect all valid, none penalized).
- **Defer and redirect are routing signals**, not dead ends.
- **Continued consensus:** consent is re-checked before every verify round and
  can be revoked mid-task via `defer_work`. Partial work is preserved.
- **Capability blocker dovetail:** the apply prompt tells the model to *do its
  best and assume nothing*, and to emit a `HARNESS_DEFER:` marker — with
  remaining scope — instead of guessing when it hits its capability limit.
  Partial work is written, recorded (`defer_midtask`, category `capability`),
  and returned as a continuation the next model resumes.
- **Forced self-check (readiness verdict):** the apply prompt requires the
  model to open with `HARNESS_READY: confident|defer`. A `defer` rotates to the
  next model *before* any code is written (and is accepted as a `readiness`
  deferral only if the whole pool declines); `confident` proceeds into the
  verify gate. This turns capability deferral from probabilistic to
  deterministic.
- **Per-model confidence calibration:** the ledger joins each `HARNESS_READY:
  confident` verdict with the same-round verify outcome and reports a
  `confidence_precision` per model (`participation_report` → `calibration`).
  A model that is confident but fails verification is flagged as
  **overconfident** (`underconfident_or_overconfident`) — the same degeneracy
  warning as consent, applied to self-assessment.
- **Fail-closed:** an unparseable consent response is treated as *defer*; paid
  BYOK-routed providers fail closed too.
- **Verifiable participation:** every event is appended to a hash-chained JSONL
  ledger. `harness ledger verify` proves chain integrity;
  `participation_report` measures accept/decline/defer/redirect and completion
  rates per model and flags near-100% acceptance as *degenerate consent*.
- The **checkbox** is `require_consent` — per dispatch and globally.

## Tests

```bash
python -m unittest tests.test_core tests.test_ledger tests.test_consent \
  tests.test_router tests.test_apply tests.test_mcp tests.test_extra \
  tests.test_byok tests.test_bench tests.test_claims tests.test_capability
```

155 hermetic tests — no network, no key. They pin: per-token pricing (regression
on a ~1,000,000x undercount bug), no-tools payloads, hard/learned BYOK handling,
key gates, mid-batch fail-closed, reasoning modes (incl. the
retry-without-reasoning path), panel rotation, structured consensus parsing,
ledger chain integrity and tamper detection, fail-closed consent, capability
deferral, the forced self-check (defer→rotate, all-defer accept, confident→proceed),
confidence calibration (readiness vs verify join, unmatched-verdict handling),
continuation resume, rotation on error, the vacuous-success guard, escalation
gating, the MCP handshake, the bench manifest/task runner, and the convergence
specialist (deterministic per-claim tally, 5/5 unanimous == 100%, split
non-convergence, default-to-judge-model, defect-proposition polarity
convention, reassurance-claim exclusion from the gate), and the capability
layer (parsing, scoring, context hard-gate, composite-reliability math incl.
prior-shrink, observed-JSON-updates-declared, structured correctness evidence,
probe persistence/error accounting, routing cost ties, registry persist/refresh/TTL,
ledger success-rate, the **real-fixture proof** that GLM-5.2 and minimax-M3 outrank
gemma-4-31b, and the unified MorphLite backend's read-only preview guarantees).

## License

MIT — see `LICENSE`.