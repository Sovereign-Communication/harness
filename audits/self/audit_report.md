# Self-dogfood audit — Round 1 (2026-09-06)

The harness used on itself: hermetic `lint-claims` grounding, live multi-model
`verify` on claims about its own code, then live `apply` edits gated by its own
test suite. Fixtures: `prompts/` (claims + verbatim source windows),
runs in `_runs/`.

## Self-audit (verify lane)

Fixture `prompts/claims.json` restates the stale-pool defect against a verbatim
window of `harness/config.py`. Two live runs ($0.00 spend):

- Run 1: 1/3 panel participation (free-tier saturation) → gate **deferred
  honestly** rather than passing a thin panel.
- Run 2: full 3/3 panel. claim_1 (stale panel pool id) real 3/3; claim_2
  (stale apply pool id) real 3/3; claim_3 ("no working refresh path")
  **not_real 3/3** — the tally caught the claim's overstatement: the literal
  `harness spend --models` is rejected, but `harness models` and
  `capabilities --refresh` both work. The config comment was wrong, not the
  capability.

## Self-apply (apply lane)

Target: replace the delisted `z-ai/glm-5.2:free` (3 occurrences) with
`google/gemma-4-26b-a4b-it:free`; gate: the full test suite; backend: diff.
After several interrupted attempts (tier-wide 401s, consent saturation, honest
model deferrals, merge-failure retries), the run completed:

- status `ok`, gate passed, **$0.00** spent, exactly the 3-line swap landed.
- The stale-pool guard tests (`tests/test_capability.py::
  StalePoolSelfHealingTest`) went red-on-stale-pools → green-after-edit.

## Defects the dogfood surfaced (all fixed, all pinned)

1. **`cli._engine` dropped `api_key`** (architecture-pass regression): every
   engine-routed call went out without an Authorization header. The earlier
   "bench 401 rotation" was misattributed to free-tier flakiness — it was this
   bug. Pinned by `tests/test_cli.py::EngineKeyWiringTest`.
2. **Ungated partial writes**: a capability-deferred run wrote the model's
   partial output straight to the target file — no gate had seen it — and a
   later run in the continuation chain silently built on the corrupted tree.
   Deferred partials now travel in the continuation state only; the tree
   changes only through a passed gate. Also: a failed run now rewinds the
   target to its pre-run content.
3. **Merge feedback collided with the broken-gate detector**: two identical
   merge errors read as "the gate failed identically" and aborted the run
   although no gate had run. Only real `verify_failed` outputs participate in
   that detector now.
4. **Malformed diffs escaped as FATAL** (pre-existing): the strict-merge
   contract promised retry-with-feedback but never caught its own exception.
   Now the round loop records merge failures as feedback rounds.
5. **Round feedback contained literal `\n` escapes** (edit mangling): cleaned.
6. **Consent gate blinded itself**: the probe showed only a 60-line excerpt
   (2,400 chars) of the target, so models honestly refused to consent to edits
   they could not fully see. Both probe sites now use the full file via
   `consent.consent_preview` (the one owner of that policy).
7. **Config ceilings unenforced / dead validator**: `HARD_MAX_COST` was
   documented but nothing enforced it; the "audit #15" validator had no
   callers and contradicted the live `_num` bounds. Ceilings are enforced in
   `load_settings` (live: `[FATAL] max_cost=5.0 is out of range [0, 0.1]`),
   dead code deleted, config load moved inside the CLI's fail-clean path.
   (The model-authored half of this contract arrived via the corrupted
   partial write; the tests asserted the right contract and were kept.)
8. **Ledger chain fork**: concurrent harness processes (bench + verify)
   appended from the same `prev_hash` — the append lock serializes *writes*,
   not *chain state* — producing duplicate seqs and a broken chain
   (`first_bad_seq: 812`). `append()` now rebases under the lock;
   `harness ledger repair` truncates to the longest valid prefix (live:
   kept 814, dropped 5). Both pinned in `tests/test_ledger.py`.

## Structural notes

- One-owner rule held: preview policy in `consent.py`, prompt/parse in
  `prompts.py`, routing in `capability.py`, disk policy in `filesafety.py`,
  chain policy in `ledger.py`.
- The gate never once passed an edit that broke the suite — including the
  run whose model hallucinated a 32-line deletion; `verify_failed` refused it.
  The one corruption incident came from the *ungated* partial-write path
  (defect 2), which is exactly why that path was closed.
