# Weather-lane live receipt — Pahoa, HI next-week query — 2026-10-12

**Query:** "What's the weather like over the next week in Pahoa, Hi 96778?"
**Call:** `harness.weather.lookup_weather(query)` with the production fetcher
(`_https_json`: in-process urllib, pinned Open-Meteo hosts, no redirects).
**Key:** none (Open-Meteo is keyless). **Spend:** $0.00. **Child processes:** none.

## Result (verbatim `summary`)

Pahoa, Hawaii, United States, 7-day outlook from 2026-10-12: 2026-10-12
light showers (28.1/17.7C, precip 98%); 2026-10-13 showers (27.1/19.6C,
precip 94%); 2026-10-14 light drizzle (27.4/18.4C, precip 40%); 2026-10-15
drizzle (25.8/19.6C, precip 80%); 2026-10-16 drizzle (26.5/19.0C, precip
83%); 2026-10-17 drizzle (26.1/19.7C, precip 100%); 2026-10-18 drizzle
(26.4/19.4C, precip 89%).

## What this proves

- Geocoding resolved "Pahoa" to Pahoa, Hawaii, United States (19.5, -154.95).
- Week phrasing fetched 7 days; the "7-day outlook" label matches the
  actual fact count (7), not a hardcoded span.
- No `None`/`unknown` literals anywhere in the answer text — every field
  the API returned was populated on this run, and missing fields now render
  as `n/a` (hermetic null tests in `tests/test_weather.py`).
- No `harness/_http_worker.py`, no subprocess, no key-limit verification:
  the reported crash class cannot occur on this path.

## Housekeeping

This file is intentionally **untracked-adjacent**: `sd_corpus_integrity`
hash-pins every *tracked* file under `audits/self/dogfood/`, so committing
this receipt must go through `refresh_corpus_manifest.py` as a reviewable
diff, like any other corpus addition.
