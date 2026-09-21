# Efficiency manifest — Proof Bench proof rows (SITE-1)

Known-answer tasks spanning the ladder's cost tiers. The verify gate is the
ground truth: "passed" means provably correct, never self-reported. Every run
lands in the autonomy ledger (`bench/<name>` task ids), so `harness
site-export` turns these runs into the site's base-layer evidence.

Base-layer-first by design: 9 cheap-tier tasks produce the day-one baseline
every frontier run is compared against; 3 frontier-tier tasks exist so the
escalation walk has somewhere real to go.

| Task | Tier | What it proves |
|---|---|---|
| typo-string | T0 (scout) | misspelled string fix at sub-cent cost |
| docstring | T0 | behavior-preserving documentation edit |
| name-repair | T0 | NameError-causing identifier typo |
| import-order | T0 | ordering/style fix, behavior preserved |
| arg-default | T1 (distiller) | classic mutable-default trap |
| dict-group | T1 | implement structured grouping logic |
| lru-cache | T1 | caching fix verified via `cache_info()` |
| multi-round-tokens | T1 | implement-and-pass with up to 4 verify rounds |
| unpack-fixed | T1 | argument-unpacking repair |
| race-guard | T2 (frontier) | thread-safe counter under real concurrency |
| invariant-ledger | T2 | double-entry invariant enforcement |
| retry-backoff | T2 | exponential backoff retry protocol |

Tier labels are intent (which models *should* pass cheaply); the ledger
records which tier actually ran, and the site shows both. Additions welcome
via one directory per task (`task.json` + starter file + `check.py`), where
the check is a strict known-answer gate — no self-reporting.

Run: `harness bench bench/manifests/efficiency`
