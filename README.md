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

# Scoped code edit with verify gate, retry, per-round consent, and deferral
harness apply --file core/src/store/outbox.rs \
  --instruction "Verify flush_on_connect() persists all peers" \
  --verify "cargo check -p scmessenger-core" --max-rounds 3

# Ask a model for consent on a work item (the "checkbox")
harness offer --task "Refactor the routing engine's backpressure path"

# Resume a deferred/incomplete task (capability or consent deferral, or failed verify)
harness apply --out state.json ...          # run 1
harness continue --state state.json --out state2.json   # run 2 (takes over partial work)

# Autonomy ledger, live free models, key status
harness ledger report
harness models
harness spend
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
`ledger_status`, `participation_report`, `spend_status`. The hand-rolled
server is spec-conformant (JSON-RPC 2.0 over stdio, `initialize` →
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

- **5/5 unanimous == 100%.** A claim *converges* only when every panelist that
  answered agrees on `real`. If all claims converge, `consensus.agreement` is
  lifted to `high` and `consensus.confidence` is set to the actual
  convergence rate (1.0 = 100%), overriding the judge's self-reported number.
- The specialist **defaults to the same model as the judge**
  (`--convergence-model` or `HARNESS_CONVERGENCE_MODEL` to override), so
  adding it costs one extra free call and no extra key/config.
- Output includes `convergence.tally` (per-claim votes, unanimity, mean
  confidence) and `convergence.specialist` (the specialist's rendered verdict).

This is what made the SCMessenger audits trustworthy: a judge self-reporting
`0.9–0.95` confidence is weaker than **5/5 panel agreement on specific
claims** — the former is opinion, the latter is a count.

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
  tests.test_byok tests.test_bench
```

89 hermetic tests — no network, no key. They pin: per-token pricing (regression
on a ~1,000,000x undercount bug), no-tools payloads, hard/learned BYOK handling,
key gates, mid-batch fail-closed, reasoning modes (incl. the
retry-without-reasoning path), panel rotation, structured consensus parsing,
ledger chain integrity and tamper detection, fail-closed consent, capability
deferral, the forced self-check (defer→rotate, all-defer accept, confident→proceed),
confidence calibration (readiness vs verify join, unmatched-verdict handling),
continuation resume, rotation on error, the vacuous-success guard, escalation
gating, the MCP handshake, the bench manifest/task runner, and the convergence
specialist (deterministic per-claim tally, 5/5 unanimous == 100%, split
non-convergence, default-to-judge-model).

## License

MIT — see `LICENSE`.