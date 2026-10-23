# Time-lane live receipt — Pahoa, HI clock query — 2026-10-12

**Query:** "what time is it in Pahoa, Hi 96778?"
**Call:** `harness.time_lookup.lookup_time(query)` with the production
fetcher (in-process urllib through `weather.geocode` + `weather.current_time`;
pinned Open-Meteo hosts, no redirects).
**Key:** none. **Spend:** $0.00. **Model calls:** zero. **Child processes:** none.

## Result (verbatim `summary`)

It is 2026-10-12T11:45 in Pahoa, Hawaii, United States (Pacific/Honolulu,
UTC-10:00).

## What this proves

- The next lookup-style type after weather (clock questions, previously a
  conversation-lane memory answer behind model rounds + Jev review) now
  resolves deterministically: geocode to Pahoa, Hawaii, United States,
  server-computed local clock, IANA zone, numeric UTC offset.
- No tz-database dependency: the offset and wall time come from the
  server (`timezone=auto`), so the lane is identical on every platform
  with pure stdlib parsing.
- Failure contract is honest: unknown place / missing clock raise
  `HarnessError`, which the agent renders as a deferral with a next step
  (hermetic pins in `tests/test_time_lookup.py`); nothing is invented.
- Lane tests prove both entries (`run_hourglass_request`,
  `_handle_conversation`) return without invoking any model, and that
  non-clock prompts never enter the lane.

## Housekeeping

Untracked like the weather receipt: committing must go through
`refresh_corpus_manifest.py` as a reviewable diff per `sd_corpus_integrity`.
