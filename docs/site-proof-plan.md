# Proof Bench site — planning record (SESSION-SITE)

> Planning record only. Canon STATUS lives in `docs/jev-roadmap.md` (rows `SITE-*`).
> This file preserves the approved plan + audit for the site work; it is **not** a
> second STATUS.

## Mission (operator, 2026-09-21)

Show the **real efficiency of models across the board** — every run sanitized and
published as proof of capability/$ — built from the ground up on the harness
evidence chain, letting smaller models demonstrate capability when planned by a
cheap frontier-class model with hourglass bottom (context → condensed → iterate
until ready for frontier planning directive escalation). The escalation cascade
defers any non-99%+ confident work to the next most capable/$ rung until either
no open questions remain or the highest capability rung plans/iterates (possibly
requesting more context, which is condensed first to keep the neck constrained).
Frontier should be **rare** — base-layer data dominates, and the fewer frontier
runs become the comparison story. Tier guidance must be shown **with proof**
("unless you're formulating a new mathematical proof for quantum encryption, leave
it to cheaper models"). An interactive router lets a user type a request and get
the most appropriate model rung (site demo + full product via `harness route`).

## Approved implementation (what shipped)

| Slice | Scope | Key files |
|---|---|---|
| SITE-1 | Fail-closed exporter `harness site-export` (consent → chain verify → sanitize allowlist → secret scan → bundle-v1, bundle_id = SHA-256 identity core incl. consent); efficiency bench manifests (12 T0/T1 + 3 T2) with proven starter-fails/fix-passes gates; cross-session contract doc | `harness/site_export.py`, `bench/manifests/efficiency/*`, `docs/site-proof-contract.md` |
| SITE-2 | Zero-hallucination router: `route_pack.py` pack/matching owner + `evaluate_model_route` in policy owner; `harness route` CLI + MCP `route_query`; choice always ⊆ declared ladder | `harness/route_pack.py`, `harness/jev_policy.py` (append-only), CLI/MCP wiring |
| SITE-3 | 8 metrics over bundle-v1: run-depth distribution, frontier warrant rate, hourglass savings (modeled, basis stated), cost per gated task, escalation escape rate, etc.; gated-runs-only headlines; `fold_rollup` parity fn | `harness/site_aggregate.py` |
| SITE-4 | Static site, 6 pages (index/tiers/router/traces/methodology/publish), vanilla ES modules, no build step; local mode against `harness serve` (`/api/snapshot`, `/api/route`, `/api/site/demo-snapshot`) | `site/public/**`, `harness/server.py` |
| SITE-5 | Cloudflare Worker (Workers Static Assets): `/api/submit` (rate-limited, schema-checked, secret-scanned), `/api/aggregate` (KV rollup), `/api/route`; D1 store + KV rollup; JS fold mirrors Python `fold_rollup` | `site/worker/index.js`, `wrangler.toml`, `migrations/` |
| SITE-6 | Publish flow: exporter consent affirm, site publish page consent checklist, CI workflow (build + tests + fold vectors + guarded manual deploy) | `.github/workflows/site.yml`, `site/public/publish/index.html` |
| SITE-7 | Router page wired: local `harness serve` `/api/route` first, hosted Worker fallback | `site/public/router/index.html`, `harness/route_pack.py` |
| SITE-8 | Local UI remake, additive: tab strip Chat (legacy, untouched) / Proof / Insights / Legacy API pane | `harness/ui/panes.js`, `harness/ui/panes.css`, `harness/ui/index.html`, `harness/server.py` STATIC_FILES |
| SITE-9 | Coalescing pass (consumer-tolerant v1/v2 escalation events), STATUS rows, planning record | this doc + roadmap rows |
| COALESCED | Merged origin/main at PR #58/#59 (Jev-directed escalation). Parity proven end-to-end: CLI escalation executor → ledgered `escalate` provenance (`directed_by=jev`, confidence, target rung, condensed-context size) → exporter allowlist → public trace cards on the site traces page AND the local UI Proof pane. `tests/test_site_parity_directives.py` pins the chain | `tests/test_site_parity_directives.py`, `harness/site_aggregate.py` (`run_trace`/`session_traces`) |

## Cross-session contract (summary)

Full text: `docs/site-proof-contract.md`. Essentials:
- My slice consumes ledger events; it never edits escalation drivers (`jev_policy`
  `evaluate_route`/`evaluate_issue_sort` owners untouched except append-only
  `evaluate_model_route`; escalation wiring belongs to the other session).
- Event tolerance: exporter accepts both v1 (`escalation.needed`/`dispatch_start`)
  and v2 (`defer_*`/`jev_eval` shapes) — unknown events ignored, known fields
  optional-with-defaults. Coalescing therefore needs no merge-order coordination.
- Worker validates `bundle_id` = SHA-256 over the identity core (incl. consent
  record) so consent cannot be swapped post-hoc.

## Proof rules the site enforces

1. Headlines come from **gated runs only** (verify-gate passed/failed with
   evidence), never raw model self-reports.
2. Frontier rarity is first-class: run-depth histogram + warrant rate + flagged
   unwarranted climbs — the site says the quiet part out loud.
3. Modeled numbers (always-frontier counterfactual, savings) are labeled with
   their basis; nothing is presented as measured when it is modeled.
4. Sanitization is an allowlist, not a blocklist; export is fail-closed on
   consent absence, chain break, or credential-shaped content.
5. Fold parity: Python rollup (authoritative CI rebuild) and worker JS fold (edge)
   are pinned to the same fixture vectors — one drift fails CI.

## Deployment (operator runbook)

1. `wrangler d1 create proof-bench` → paste database_id into `site/worker/wrangler.toml`.
2. `wrangler kv namespace create ROLLUP` → paste id.
3. `wrangler d1 execute proof-bench --remote --file site/worker/migrations/0001_init.sql`.
4. Set repo secret `CF_API_TOKEN` (Workers deploy + D1 write). Deploys are manual
   (`workflow_dispatch` with deploy=true) — never automatic.
5. First publish: operator runs `harness site-export --ledger <their ledger>
   --consent <their consent> --yes --out site/seed/bundle.json` then submits via
   the publish page (or POSTs to `/api/submit`).
