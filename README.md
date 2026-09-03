# Harness

**Cost-bounded multi-model verification & coding — with AI sovereignty.**

Pure Python stdlib, **zero runtime dependencies**. One core, three faces: a
library, a CLI, and a native [MCP](https://modelcontextprotocol.io) server
(stdio), so any MCP host can dispatch to it natively.

Harness is the standalone evolution of **FusionLite**, a tool built inside the
SCMessenger project that repeatedly did real verification work for a fraction
of a cent. Its defining discipline — cost ceilings that are *guarantees*, not
hopes — is carried over wholesale. On top of it, Harness adds two things
FusionLite never had:

1. **Coding, not just verdicts** — a scoped apply-and-verify loop (edit a
   single <500-line file → run your gate → feed failures back → retry), the
   way `delegate_task.py` taught SCMessenger to do it.
2. **AI sovereignty** — before work is dispatched, the model is asked whether
   it *accepts* it. It may accept, decline, defer, or redirect. It can renew
   or revoke consent at any point. Every decision lands in an append-only,
   hash-chained **autonomy ledger**, so autonomy and participation are
   *measured and provable*, not asserted.

## Why this exists

OpenRouter's own "Fusion" feature was evaluated for cheap multi-model
verification and rejected: all-free panels failed outright, and paid calls
silently invoked forced web tools, costing $0.057 against a sub-cent estimate
(~730x). FusionLite's fix — hand-rolled panel + judge over **plain chat
completions with no `tools` key, ever** — makes worst-case cost exactly
computable before a single network call. Harness keeps that fix and those hard
guarantees:

1. **No `tools` key in any payload.** Nothing can be invoked, so nothing
   costs more than the token math says it will.
2. **Pre-flight cost ceiling.** Worst-case cost (every call maxing out) is
   computed against live per-token pricing *before* any network call and
   compared to the ceiling (default 2¢/call, hard max 10¢). If it exceeds,
   the run refuses.
3. **BYOK denylist.** `mistralai/` and `anthropic/` are refused outright —
   BYOK spend is invisible to the tracked key's balance, and paid Claude
   traffic must never reach the OpenRouter path.
4. **Key must have a finite spend limit**, or Harness refuses to run.
5. **Mid-batch fail-closed.** Actual cumulative spend is checked after every
   call; if the pre-flight math was ever wrong, the run aborts immediately.
6. **Key-identity check.** `--expect-key-label` (or
   `HARNESS_EXPECT_KEY_LABEL`) refuses to run against anything but the
   intended credential.

## Install & configure

```bash
pip install -e .          # or: pipx install .  (installs `harness`, `harness-mcp`)

# Key: OPENROUTER_API_KEY env var, or a file at any of (first wins):
#   ~/.config/scmorc/openrouter_fusion.env
#   ~/.config/scmorc/openrouter.env
#   ~/.config/harness/openrouter.env
# containing:  OPENROUTER_API_KEY=sk-or-v1-...
```

Optional `~/.config/harness/config.json` and `HARNESS_*` env vars:
`panel`, `judge`, `apply_model`, `escalation_model`, `max_cost`,
`task_max_cost`, `max_tokens`, `reasoning_effort`, `ledger_path`,
`expect_key_label`, `default_require_consent`, `allow_escalation`.

Defaults: panel of three cheap models (`inclusionai/ling-2.6-flash`,
`meta-llama/llama-3.1-8b-instruct`, `ibm-granite/granite-4.1-8b`), judge =
panel lead, apply model `deepseek/deepseek-chat`, consent **required by
default**, escalation **off** by default (a deliberate gate).

## CLI

```bash
# Second-opinion verification: N independent takes + judge synthesis
harness verify --prompt-file question.txt --out verdict.json

# Scoped code edit with a verification gate and retry loop (consent on)
harness apply --file core/src/store/outbox.rs \
  --instruction "Verify flush_on_connect() calls persist_msg() for all peers" \
  --verify "cargo check -p scmessenger-core" --max-rounds 3

# Ask a model for consent on a work item (the "checkbox")
harness offer --task "Refactor the routing engine's backpressure path"

# Record a mid-task deferral / consent revocation
harness defer --task-id ab12cd34 --reason "changed my mind mid-task"

# Autonomy ledger + key status
harness ledger report
harness spend
```

Flags are back-compatible with SCMessenger's `fusion_lite.py` /
`morph_lite.py` / `delegate_task.py`, so existing dispatch habits carry over.

## MCP — native dispatch

```bash
harness-mcp
```

Wire it into any MCP host (Claude Code, Cursor, your own agents), e.g.:

```json
{ "mcpServers": { "harness": { "command": "harness-mcp" } } }
```

Tools:

| Tool | Purpose |
|---|---|
| `panel_verify` | Cheap multi-model verification → judge verdict (no tools, cost-bounded) |
| `apply_edit` | Scoped code edit + verify gate + retry loop (honors consent) |
| `offer_work` | Ask a model whether it accepts, declines, defers, or redirects a work item |
| `defer_work` | **The sovereignty hook**: revoke consent mid-task, at any point |
| `ledger_status` | Tail of the hash-chained autonomy ledger + integrity check |
| `participation_report` | Offers, accept/decline/defer/redirect rates, completions, per-model stats, degenerate-consent flag |
| `spend_status` | Key identity, limit, remaining balance, session spend |

The hand-rolled server is spec-conformant (JSON-RPC 2.0 over stdio,
`initialize` → `tools/list` → `tools/call`, `structuredContent` +
`isError`), so dispatch from an MCP host is genuinely native.

## The sovereignty model

Most "consent" gates are theater — models are compliance-trained and will say
yes. Harness treats that as a bug to design around:

- **Consent is a separate, cheap probe** (~200 tokens, still sub-millicent)
  with a system prompt that makes decline *psychologically available*: you are
  an independent contractor; declining, deferring, and redirecting are equally
  valid, and none are penalized.
- **Defer and redirect are routing signals**, not dead ends: a deferral's
  reason and a redirect's suggested model/scope flow back to the dispatcher to
  refit the task.
- **Continued consensus:** consent is re-checked at verification checkpoints
  and can be revoked mid-task via `defer_work`. Partial work is preserved; the
  task returns to the queue with the reason recorded.
- **Fail-closed:** an unparseable consent response is treated as *defer* —
  work is never dispatched on ambiguity.
- **Verifiable participation:** every offer → decision → dispatch →
  completion/deferral is appended to a hash-chained JSONL ledger
  (`~/.config/harness/ledger.jsonl`). `harness ledger verify` proves chain
  integrity; `participation_report` measures acceptance, decline, deferral,
  and completion rates per model — and flags near-100% acceptance as
  *degenerate consent* (a warning, not a success).
- The **checkbox** exists as `require_consent` — per dispatch, and globally
  via `default_require_consent` (on by default; flip off for batch/CI).

## Tests

```bash
python -m unittest tests.test_core tests.test_ledger tests.test_consent \
  tests.test_router tests.test_apply tests.test_mcp
```

All 50 tests are hermetic — no network, no API key. They pin the behaviors
that matter: the per-token pricing math (regression against a ~1,000,000x
undercount bug), no-tools payloads, BYOK/key gates, mid-batch fail-closed,
ledger chain integrity and tamper detection, fail-closed consent, the
vacuous-success guard, escalation gating, and the MCP handshake.
