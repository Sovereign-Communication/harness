# EV-1..EV-7 + Issues #195–#197 + REFINE-LOOP Economics & Burndown Receipt

- **Date:** 2026-10-18
- **Phases covered:** `EV-1`, `EV-2`, `EV-3`, `EV-4`, `EV-5`, `EV-6`, `EV-7` (plus `EV-8` probe scaffold and `REFINE-LOOP` #239)
- **GitHub issues closed:** `#195` (`EV-2`: task-aware model class routing), `#196` (`EV-3`: per-lane cost metering and spend breakdown), `#197` (`EV-4`: reversible per-run model trial mode), `#239` (`REFINE-LOOP`: iterative Jev-judged refine loop Phase 0 pilot + P0–P3 prerequisites)
- **PRs:** PR #240 (`a7daf42`, `REFINE-LOOP`) + PR #241 (`EV-1`..`EV-8` unification and canon burndown)

## 1. Phase-by-Phase Evidence

| Phase | Modules | Surfaces (CLI / MCP / HTTP / Site) | Verification Suite |
|---|---|---|---|
| `EV-1` | `harness/endpoint_pricing.py`, `harness/benchmark_ingest.py`, `harness/capability.py`, `harness/routing_table.py`, `packs/tiers.snapshot.json` | `harness refresh-rankings` (`--aa-file`, `--livebench-file`, `--swe-file`, `--arena-file`, `--pricing-file`, `--probe-receipt`, `--write-snapshot`), `harness economics`, `.github/workflows/rankings.yml` | `tests/test_endpoint_pricing.py`, `tests/test_benchmark_ingest.py`, `tests/test_economics_index.py` |
| `EV-2` (`#195`) | `harness/capability.py` (`TASK_MODEL_CLASSES`, `classify_task_class`, `select_task_class_models`), `harness/routing_table.py` (`route_for_task`, `build_task_class_routing`), `harness/config.py` (`model_classes`) | `harness route-task --prompt / --task-class`, `harness economics`, MCP `harness_economics_status` (`task_routing`), HTTP `GET /api/economics` | `tests/test_routing_table.py`, `tests/test_economics_cli.py` |
| `EV-3` (`#196`) | `harness/spend.py` (`record(lane=..., task_class=...)`, `by_lane()`, `by_task_class()`), `harness/session.py`, `harness/ledger_analytics.py` (`summarize_ledger` `by_lane` / `by_task_class`) | `harness spend` (per-lane + per-task-class tables), `harness economics`, `harness/site_export.py` (`lane_economics`), MCP `harness_spend_status` / `harness_economics_status`, HTTP `GET /api/spend` / `GET /api/economics` | `tests/test_economics_cli.py`, `tests/test_mcp.py` |
| `EV-4` (`#197`) | `harness/config.py` (`trial_model`, `trial_Compare`, `trial_overrides`), `harness/chat.py` (`apply_trial_override`, `run_trial_comparison`) | `harness chat --trial-model <id> --trial-compare`, `harness route-task --trial-model <id>` | `tests/test_economics_auto_rotation.py`, `tests/test_economics_cli.py` |
| `EV-5` | `harness/jev_packs.py` (`ECONOMICS_ROUTE_FACTORS`), `harness/jev_policy.py` (`recommend_economic_route`), `packs/economics_route.pack.json` | `harness route-task`, `harness economics`, MCP `harness_economics_status`, HTTP `GET /api/economics` | `tests/test_economics_route_policy.py`, `tests/test_economics_cli.py` |
| `EV-6` | `harness/endpoint_pricing.py` (`fetch_fireworks_Catalog`, `build_fireworks_endpoint_map`, `merge_provider_maps`), `harness/routing_table.py` (`select_cheapest_provider`), `harness/capability.py` (`providers`) | `harness refresh-rankings --fireworks-file`, `harness economics`, MCP `harness_economics_status` (`providers`), HTTP `GET /api/economics` | `tests/test_economics_fireworks_source.py`, `tests/test_economics_cli.py` |
| `EV-7` | `harness/fireworks.py` (`FireworksClient`, `normalize_fireworks_model_id`), `harness/config.py` (`fireworks_api_key`, `fireworks_base_url`, `provider_routing`), `harness/spend.py` (`provider` field + `by_provider()`) | `harness chat --provider auto|openrouter|fireworks` (`HARNESS_PROVIDER`), `harness spend` (`by_provider`), MCP `harness_spend_status` (`by_provider`), HTTP `GET /api/spend` | `tests/test_economics_fireworks_route.py`, `tests/test_economics_cli.py` |
| `EV-8` (scaffold) | `harness/fireworks_probe.py` (`run_fireworks_vs_openrouter_probe`, `evaluate_ev8_gate`, `write_ev8_receipt`) | `harness fireworks-probe` (`--dry-run`, `--models`, `--prompts`, `--runs`, `--out`, `--json`) | `tests/test_economics_fireworks_verify.py`, `tests/test_economics_cli.py` |
| `REFINE-LOOP` (`#239`) | `harness/refine.py`, `harness/jev_packs.py` (`REFINE_CONVERGENCE_FACTORS`), `harness/jev_policy.py` (`score_refine_iteration`), `packs/refine_convergence.pack.json` | `harness refine`, MCP `harness_refine` (`harness/mcp.py`), HTTP `POST /api/refine` (`harness/server.py`) | `tests/test_refine_loop.py` |

## 2. Gate & Phase Completion Summary

- `python -m harness.cli jev-phase --all --local-only`:
  - `EV-0`: `score=99.25 bar_pass=True`
  - `EV-1`: `score=99.25 bar_pass=True`
  - `EV-2`: `score=99.25 bar_pass=True`
  - `EV-3`: `score=99.25 bar_pass=True`
  - `EV-4`: `score=99.25 bar_pass=True`
  - `EV-5`: `score=98.5 bar_pass=True`
  - `EV-6`: `score=99.25 bar_pass=True`
  - `EV-7`: `score=99.25 bar_pass=True`
  - `EV-8`: `score=84.99 bar_pass=False` (honestly gated on live `FIREWORKS_API_KEY` probe execution)
  - `REFINE-LOOP`: `score=98.5 bar_pass=True`
- `audits/self/audit.py`: `A=10.0 R=10.0 SM=10.0 SD=10.0 -> BAR MET`
