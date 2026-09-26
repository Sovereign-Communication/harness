# HV-0 live vision-assessment pilot receipt — 2026-09-26

The `HV-0` row may only be marked complete when a live, fully-keyed,
non-fallback assessment returns an **assessed** envelope. This receipt is that
evidence. It is the first `HV-0` call to succeed; the 2026-09-25 attempt on
`2cf24b5` returned `status=unassessed` because `DF-JEV-2` rejected a correct
answer.

Machine posture: keyed (TypeSafe seat via the configured policy), spend
governor with one preflight reservation, one no-retry transport attempt, one
settlement, one metadata-only ledger `jev_eval`, private pilot ledger
(`HARNESS_LEDGER` pointed at `tmp/pilot/`), `--max-cost` within
`HARD_MAX_COST` ($0.10). Pack `harness-hourglass-vision-assessment-v1`
**v1.1.0** (the version that declares `score_probability_precision: 2`).

## Pre-dispatch gates — all four passed before any network call

`harness jev-vision-assessment --preflight-only`, `dispatch_attempts: 0`:

| Gate | Measured | Limit | Margin |
|---|---|---|---|
| Complete request (estimate) | 15,896 tokens | 64,000 | **+48,104** |
| `state` + longest question | 14,425 tokens | 32,000 | **+17,575** |
| Serialized payload | 63,581 UTF-8 bytes | — | — |
| Worst-case reserve | **$0.0000667632** | $0.10 hard / $0.05 effective | — |
| Declared price | $0.0042 per Mtok input | operator-verified | — |
| Key resolved | yes (108 chars; never printed) | — | — |

Estimator: `harness.tokens.estimate_prompt_tokens` over the same JSON
serialization used for dispatch. Sources: `docs/hourglass-vision.md` plus the
canonical `### Hourglass vision realization (HV-*)` section of
`docs/jev-roadmap.md`, both SHA-256 pinned in the state and sanitized for
credentials, IPs, and machine home paths before dispatch.

## The live call

| Field | Value |
|---|---|
| `status` | **assessed** |
| `model` / `model_observed` | `jev-1.13.0` / true |
| `fallback_state` | `not_used` (native, no local heuristic) |
| `usage_source` / `cost_source` | `actual` / `actual_input` |
| input / output tokens | 15,301 / 154 |
| estimated input tokens | 15,896 |
| **cost** | **$0.0000642642** |
| `perfect` | false |

`harness ledger verify` on the pilot ledger: 1 entry, 0 quarantined,
`chain_broken_on_load: false`. The settled event carries site/capability, pack
id + version, observed model, result state, fallback state, usage source, cost
source, and token counts — and contains no payload, key, IP, or personal
context.

## What the assessment actually said

Advisory design evidence only. It conveys no `can_mark_complete`, readiness,
or phase status, and the envelope's serialized form contains no completion
field.

| Category | Score | Selected | /10 | Confidence | Improvement bucket |
|---|---|---|---|---|---|
| `modularity` | 2.48 | 3 | 10.0 | 0.48 | — |
| `token_shape` | 2.89 | 3 | 10.0 | 0.89 | — |
| `grounding` | 2.62 | 3 | 10.0 | 0.62 | — |
| `planning` | 2.83 | 3 | 10.0 | 0.83 | — |
| `execution_boundary` | 1.90 | 3 | 10.0 | **0.00** | — |
| `jev_coverage` | 2.27 | 2 | 6.67 | 0.68 | `jev_selection_gap` |
| `sovereignty` | 2.03 | 3 | 10.0 | 0.03 | — |
| `observability` | 1.94 | 2 | 6.67 | 0.42 | `usage_truth_gap` |
| `verification_alignment` | 2.52 | 3 | 10.0 | 0.52 | — |
| `cost_bounds` | 2.82 | 3 | 10.0 | 0.82 | — |

Six of ten categories land on the top level but below the pack's declared
0.80 confidence threshold, so they are flagged for review; the two level-2
categories carry the declared improvement buckets shown. The scores are the
provider's two-decimal values (`2.48`, `2.89`, `1.90`), which is precisely the
shape `DF-JEV-2` used to reject — the fix is exercised by real data, not only
by fixtures.

The two named gaps corroborate independent findings: `jev_selection_gap`
matches the open `HV-1` remainder (context-intake, execution-checkpoint,
consent-freshness, and restart-target judgments are still undelivered), and
`usage_truth_gap` matches what `DF-JEV-3` just fixed and what this program's
later `HV-3` token-allowance owner will formalize. `sovereignty` at 0.03
confidence is the weakest signal in the set and should not be relied on.
