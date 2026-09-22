# JEV-LOG self-dogfood receipt — 2026-09-22

**Operator redirection (2026-09-22):** the canon row's original target
(`C:\temp\logsSCMessenger.txt`) is retired as unnecessary. The dogfood runs on
**harness itself**: harness's own runtime output, and harness's own in-repo
fixture log through the shipped `harness log-judgment` face. Machine posture:
keyed (TypeSafe seat via the configured policy), spend-governed, ceiling
`--max-cost` within `HARD_MAX_COST` ($0.10). The old file was never read.

## Leg A — harness's own runtime log (honest 0/0)

- Input: `jev-log-self-serve.log` — genuine `harness serve` output (banner +
  five request lines, all endpoints 200), captured on loopback, $0 extraction.
- Result (`jev-log-self-serve.analysis.json`): `total_items: 0`, `$0` spend.
  Harness is pure Python and emits no Rust `tracing` headers, so the honest
  outcome is **zero items** — the extractor never invents records from an
  unmatched format (the 0-hallucination parse contract, exercised live).

## Leg B — keyed single pass on the in-repo fixture

- Inputs: `jev-log-fixture.log` (the canonical `LOG_FIXTURE` from
  `tests/test_jev_log_pack.py`) + frozen operator pack
  `jev-log-self-pack.json` (`scmessenger-ops-log-v1`, generated from the
  in-repo `sample_log_pack()` source of truth, validate_log_pack clean).
- Canonical run (`jev-log-fixture.analysis.json`, `--max-cost 0.10`):
  4 items → **1 live judgment** (item-000001: bucket `ble`, score level
  "actionable — likely defect or policy issue", value 0.84, confidence 0.83;
  ledger: verdict pass, 489 in / 53 out tokens, **$0.0205**),
  **3 honest fallbacks**, **1 unmatched** (custody-sweep WARN, no declared
  bucket fits — reported, not smoothed).
- Bounded run (`jev-log-fixture.bounded005.analysis.json`, `--max-cost 0.05`):
  identical coverage — the cost bound changes nothing here, which is itself
  the evidence that the governor bound is honored without changing verdicts.
- Why 3 fallbacks (verbatim reasons from the policy, live transport):
  TypeSafe returned malformed score questions on 3 of 4 items —
  `invalid TypeSafe response: sentiment.probabilities must sum to 1` — the
  keyed path **refused the malformed answers fail-closed** and degraded to the
  code-owned keyword matcher; buckets stayed pack-owned (`ble` ×3), one item's
  transport choice (`transport`, conf 0.41) was correctly NOT promoted because
  the response was already refused. No invented buckets, no invented score
  levels. **Operator model-policy note:** the TypeSafe score-question shape is
  unstable on this seat — worth a probe snapshot entry alongside the 429 note.
- Ledger: one hash-chained `jev_eval` per call (`caller=cli`,
  `site=log_factor`); `harness ledger verify`: chain intact, 0 quarantined.
  Total live spend across all probes this session: ≈ $0.10.

## Verdict

`JEV-LOG-dogfood` evidence complete per the row's own contract: single-pass
JSON artifacts + cost + fallback rate recorded, receipts committed and pinned
by `audits/self/corpus_manifest.json` (refreshed via the official script).
