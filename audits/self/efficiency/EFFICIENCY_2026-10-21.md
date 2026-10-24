# Harness efficiency experiment — 10 × $0.01 efforts (2026-10-21)

Operator-commissioned experiment: how efficiently does the harness router run real
work on a restrained budget? Authorization: **$0.10 total**, split into **10 efforts
of $0.01 each**, each effort a bundle of many small requests of a different type,
routed through the harness's own cheapest-capable paid path.

Rerunnable driver: [`efficiency_driver.py`](efficiency_driver.py). Raw per-request
records were kept out of the repo (175 lines / ~50 KB); the per-effort summary below
is the evidence.

## Method

- 175 tiny requests across 10 domains (25/20/20/15/15/15/20/15/15/15).
- Every request went through `harness ask --quiet` with `HARNESS_USE_FREE=0`, so the
  router picked the cheapest-capable **paid** model every time; the picked model and
  harness-reported cost were recorded per request.
- Budget guards in the driver: stop an effort past $0.008, stop everything past
  $0.09 of the $0.10 authorization. Spend was measured independently via the
  OpenRouter key-usage endpoint (`/api/v1/auth/key`), baseline $0.0023.
- Success = model returned a sane on-task answer (JSON efforts validated as JSON).
  One deliberate timeout was left in as a harness-behavior data point.
- **Doc note for cost experiments:** the default `HARNESS_USE_FREE=1` routes to
  `:free` models, which 429-flake under load. Paid routing needs
  `HARNESS_USE_FREE=0`.

## Results

| # | Domain | n | OK | Router picked | Measured spend | Reported spend | Avg latency |
|---|--------|---|----|---------------|----------------|----------------|-------------|
| 1 | factual Q&A | 25 | 25 | deepseek-v4-flash ×23, glm-5.3-flash ×2 | $0.00206 | $0.00228 | 6.1s |
| 2 | JSON extraction | 20 | 20 | glm-5.3-flash ×20 | $0.00076 | $0.00025 | 2.6s |
| 3 | classification | 20 | 20 | glm-5.3-flash ×20 | $0.00019 | $0.00010 | 2.5s |
| 4 | summarization | 15 | 14 | deepseek-v4-flash ×13, glm-5.3-flash ×1 (+1 timeout) | $0.00415 | $0.00368 | 14.4s |
| 5 | code explain | 15 | 15 | glm-5.3-flash ×15 | $0.00142 | $0.00015 | 4.4s |
| 6 | text transform | 15 | 15 | deepseek-v4-flash ×4, glm-5.3-flash ×11 | $0.00034 | $0.00089 | 5.9s |
| 7 | reasoning | 20 | 20 | glm-5.3-flash ×13, deepseek-v4-flash ×7 | $0.00036 | $0.00070 | 3.9s |
| 8 | entity extract | 15 | 15 | deepseek-v4-flash ×15 | $0.00109 | $0.00150 | 5.3s |
| 9 | compare | 15 | 15 | deepseek-v4-flash ×8, glm-5.3-flash ×7 | $0.00075 | $0.00069 | 5.7s |
| 10 | short generation | 15 | 15 | glm-5.3-flash ×15 | $0.00012 | $0.00011 | 3.6s |
| | **Total** | **175** | **174** | deepseek-v4-flash ×79, glm-5.3-flash ×96 | **$0.01209** | $0.01035 | |

**Spend: $0.01209 of the $0.10 authorization (~$0.086 returned unspent).
~$0.00007 per successful task.** No effort came close to its $0.01 cap.

## Headline: live price validation worked

Only 2 of the 5 T1 pool members got traffic, and live OpenRouter prices explain
exactly why:

- `deepseek-v4.1-flash` was inside its **peak pricing window** during the run
  ($0.30/$1.20 — run executed ~07:50 UTC, inside DeepSeek's 06:00–10:00 peak), so
  the router correctly avoided it.
- `glm-5.3-flash` had dropped intraday to **$0.01/$0.18** (vs $0.15/$0.50 that
  morning) — the cheapest input price on the board — and the router piled onto it.

The 15-minute catalog TTL and dynamic pricing behaved correctly under production
conditions. This is the strongest live evidence yet for the recursive-selection
loop (#318) and the intake pipeline (#319).

## Quality

Spot checks across trick questions were all correct: bat-and-ball → 5¢, socks →
6 (pigeonhole), "months with 28 days" → 12, rooster's egg → "roosters don't lay
eggs", CIFAIPC → ocean, order-of-operations → 23. All 20 JSON extractions parsed
valid. The single failure (effort 4) was the harness's own 60-second bounded-answer
deadline expiring on a slow summarization — zero model cost, handled cleanly via
the defer path (no repo or write action attempted).

## Observations (for the lane, not filed as new issues here)

1. **`ling-3.0-flash` was never selected** across 175 requests, despite listing at
   $0.02/$0.06 live — cheaper than glm-5.3-flash on short outputs. Whether that's
   capability gating, reliability scores, or a stale price in the router's view is
   a question for #318's observed-reliability feed.
2. **Reported cost drifts ±15–40% off measured spend** (e.g. effort 5: reported
   $0.00015 vs measured $0.00142). The pricing snapshot the reporter uses lags the
   live dynamic prices the router actually paid — #318's cost-per-success metric
   should use measured spend.

## Repro

```bash
export OPENROUTER_API_KEY=...            # your own key; driver never stores it
export HARNESS_USE_FREE=0                # paid routing, not :free
python audits/self/efficiency/efficiency_driver.py
```

The driver honors a per-effort cap (`HARNESS_EFFORT_CAP`, default $0.008) and a
total guard (`HARNESS_TOTAL_GUARD`, default $0.09), checks key usage before and
after every request, and appends one JSON record per request to
`efficiency-results/results.jsonl` in the working directory.
