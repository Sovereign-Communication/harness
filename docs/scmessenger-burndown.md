# SCMessenger → Harness handoff burndown (Sep 13, 2026)

> **STATUS: BURNED DOWN Sep 13, 2026.** P0, both P1s, and P3 are fixed and
> pinned hermetically in `tests/test_burndown.py` (24 tests; full suite
> 616 green, ruff clean). P2 was a **mis-diagnosis** of my own probe: the
> ledger's `abort` rows *do* carry `reason` (all 276 say
> `verify rounds exhausted`) — no code change was needed; my probe had
> looked at the wrong field (`event_note`). Corrections to the live
> evidence are noted inline below.

Source audit of what SCMessenger's live Harness usage reveals, ranked for
burn-down. Evidence comes from three places:

1. **The harness ledger** (`%USERPROFILE%\.config\harness\ledger.jsonl`,
   3,009 entries, Sep 3–13; 35 rows today — SCMessenger is actively running
   Harness right now, caller `bod-governance` most recently).
2. **Handoff run artifacts** under `audits/scmessenger/` (round7 panel runs,
   `seat-gates/`, `rule8-281/`, `readiness-20260911/`).
3. **Code reading** of the lanes those artifacts exercised.

Headline numbers from the ledger:

| Metric | Value |
|---|---|
| JSON-expected calls that failed to parse (`json_ok=false`) | **387** |
| Cost burned on those failures | **$0.0260** (free tier mostly; $0.0252 of it today, paid gpt-5 judge) |
| Judge failures (`event_note=judge`) | 58, incl. 3 **today** (gpt-5, `reasoning_only`) |
| Panel failures (`event_note=panel`) | 211 (mostly free-tier rotation noise) |
| 224 panel runs by `agreement` | 106 high / 76 low / **28 unknown (lost judges)** / 7 medium / 7 none |
| round7 structured runs deferred | 4 of 6 (`responder_disagreement`) — correct fail-closed behavior |
| Ledger chain / spend governance | healthy: no unparseable ledger lines, ceiling respected |

---

## P0 — Judge seat has no rotation; one bad judge body loses the whole verdict

**Severity: high (live today).** The panel and convergence-specialist lanes
rotate down a fallback pool on imperfect output, but the **judge seat is a
single attempt**. Three distinct loss modes are proven in the handoff:

| Evidence | Mode | Outcome |
|---|---|---|
| `_runs/seat-gates/verify_20260911T175623Z.json` | judge synthesis **truncated at 57 chars** (fenced `{"verdict": "APPROVE", "agreement": "high",` then nothing — unbalanced braces, no closing fence) | `agreement: unknown, defer: true`; 3 good panel votes discarded |
| `round7/01_is_poison_circuit_listener.json` | judge `http_502` | same: raw outputs + defer |
| ledger seq 2987/2995/3003 (today, `openai/gpt-5`) | `reasoning_only` → `json_ok=false` | 3 governance judge calls lost, $0.0083 each |

In the Sep-11 case the panel itself had **converged** — the deterministic
information was in hand; only the judge body was bad.

**FIXED (Sep 13, 2026).** `harness/panel.py`:

1. The judge seat now rotates like `run_convergence_specialist`: one
   bounded same-seat retry on transient HTTP (5xx/408/429), then fallback
   to un-voted/un-failed free panel-pool members. Preflight reserves one
   judge-sized call per free fallback candidate, so the ceiling stays a
   guarantee; the result names the model that actually spoke
   (`judge_model`).
2. Truncation detection (`_looks_truncated`: unbalanced braces/fence) names
   the Sep-11 failure mode honestly (`judge_synthesis_status: "truncated"`)
   and triggers rotation instead of a silent defer.
3. Exhausted seats still defer with raw panel outputs — no verdict is ever
   fabricated (pinned by `test_exhausted_seat_still_defers_with_raw_outputs`).

## P1 — Reasoning-model recognition misses `gpt-5` (live failure mode)

**Severity: high (happening now).** `looks_reasoning()` in `harness/chat.py`
hints on names, and `openai/gpt-5` matches none — so `auto` effort sends no
reasoning param, the model burns its completion budget on hidden thinking,
and the judge/structured lanes receive `reasoning_only` output (the
`REASONING_FALLBACK_PREFIX` note). Today's three `bod-governance` judge
failures are exactly this. Commit `119a9c2` added nemotron +
`openrouter/free` to the hints; `gpt-5` (and `o1`, `gpt-5-mini`) are still
missing.

**FIXED (Sep 13, 2026).** `_REASONING_HINTS` in `harness/chat.py` now
includes `gpt-5` and `o1` (matches `gpt-5-mini` too); pinned in
`tests/test_burndown.py` alongside the shipped-pool non-reasoning ids.
The reasoning-only judge mode is also a rotation trigger now (P0).

Note the counterweight already in `config.py`: reasoning-heavy models are
demoted *out* of the judge seat by the free pools; this item is about
correctly *recognizing* reasoning models wherever an operator (like
bod-governance) pins one.

## P1 — BOM tolerance still missing in the claims loaders

**Severity: medium (blocks SCMessenger's `--claims-file` flows).**
`rule8-281/claims.json` is UTF-8-BOM'd (PowerShell `>` redirect on Windows
— the exact defect class from commit `119a9c2`), and it fails plain
`json.load` with "Unexpected UTF-8 BOM". `119a9c2` fixed
`local_fit/extract.py` only; **`harness/claims.py` lines 146 and 260 still
read `encoding="utf-8"`**, and the CLI's prompt/diff file readers (used by
`--prompt-file`, `--source-file`) have the same exposure for handoff
artifacts produced by PowerShell.

**FIXED (Sep 13, 2026).** `harness/claims.py` (both loaders) and
`cli._read_text` (feeding `--prompt-file`, `--source-file`,
`--continue-from`/`--state` JSON) decode `utf-8-sig`. BOM'd fixture tests
in `tests/test_burndown.py::BomToleranceTests`.

## P2 — ~~Transient HTTP errors on the judge~~ / ~~abort event_note~~ (both closed)

The 5xx judge retry was folded into the P0 rotation (bounded same-seat
retry before fallback). The `abort event_note` item was a **mis-diagnosis**:
all 276 abort rows already carry `reason="verify rounds exhausted"`; my
original probe read `event_note` (null on abort rows) instead of `reason`.
No code change needed — the ledger was always honest here.

## P3 — Defer-rate observability for operators — FIXED (Sep 13, 2026)

`harness ledger defer-stats [window]` (owner: `AutonomyLedger.defer_stats`)
aggregates panel defer rate (`agreement=unknown` complete rows = lost
judges), mid-task defer categories, and consent outcomes; surfaced on the
CLI, the web UI Ledger view, and `GET /api/ledger/defer-stats`. On today's
ledger it would have shown the 28 unknown-agreement runs at a glance.

## What the handoff proves is *working* (no action)

- **Fail-closed everywhere it matters**: truncated/`http_502`/reasoning-only
  judge bodies never produced a fabricated verdict — always defer.
- **Deterministic tally + judge conflicts surfaced** (`tally_conflicts`).
- **Free-pool reasoning demotions** (north-mini-code off the judge seat)
  held: zero `reasoning_only` judge failures on the free default pools.
- **Spend governance**: every failed-parse dollar is individually metered;
  the ceiling never blurred. The $0.0252 today is the *cost of the bug*, not
  a governance failure.
- **Ledger integrity**: 3,009/3,009 lines parse, hash chain intact, zero BOM
  contamination.

## Burn order (executed)

1. ~~P1 reasoning hints (`gpt-5`)~~ — done.
2. ~~P1 claims BOM~~ — done.
3. ~~P0 judge rotation + truncation detection~~ — done.
4. ~~P2~~ — closed (one folded into P0, one was a mis-diagnosis).
5. ~~P3 defer stats~~ — done.

**Remaining follow-ups not in this burndown:** an optional last-resort
emitting the deterministic panel tally when every judge candidate fails on
a *converged* structured panel (defer still wins today, honestly), and
live verification that the next `bod-governance` gpt-5 judge call parses.
