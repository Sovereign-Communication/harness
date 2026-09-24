# UI-readiness audit (Phase 1)

What the data layer already gave a UI, what Phase 1 added, and what remains.
Companion to the UI plan: every Phase 2 surface (web, desktop) consumes only
what this document certifies. Hermetic tests pin each contract listed here
(`tests/test_events.py`).

## Producers: the structured contracts a UI consumes

| Contract | Owner | Shape notes |
|---|---|---|
| Result envelopes (every command) | `cli._emit` -> stdout / `--out` | JSON, one shape per terminal status; `meta` block added in Phase 1 |
| Run metadata | `cli._run_meta` | settings snapshot + ceiling; never the key label; Mock-safe |
| Typed progress events | `harness/events.py` | JSONL `{ts, seq, type, ...}`; advisory; zero-cost when no sink |
| Pretty rendering | `harness/render.py` | TTY-pretty / piped-JSON; stderr-only; `NO_COLOR`/`--no-color`; total over unknown shapes |
| Ledger chain status | `ledger.chain_status` | now also on `ledger tail` envelopes |
| Session spend view | `spend.key_status` + `session` | key data + governor spent/ceiling/remaining |

## Event vocabulary (emitted by lane)

| Event | Lane(s) | Fields |
|---|---|---|
| `preflight` | panel | `worst_case`, `ceiling`, `calls[]` |
| `attempt_start` | apply | `model`, `round`, `backend` |
| `panel_call` / `panel_vote` | panel | `model`, `cost`, `finish_reason`, `truncated` |
| `judge_call` / `judge_result` / `judge_trim` | panel | `status`, `cost`, `dropped` |
| `rotation` | panel, apply, consent, specialist, chat | `reason` (`http_error`, `rate_limited`, `paid_byok`, `reasoning_only`, `readiness_defer`, `consent_unusable`, `unparseable_json`, `reasoning_param_rejected`, `malformed_claims`, `unusable_output`) |
| `pool_filtered` | panel | `lane`, `reason` (`learned_byok`\|`demotion_strike`), `models[]` -- the dispatch pool shrank and says so |
| `readiness` | apply | `decision` (`confident`\|`defer`\|`missing`), `round` |
| `consent_result` | consent | `decision`, `dispatched`, `fail_closed` |
| `gate_start` / `gate_end` | apply gate | `command`, `passed`, `rc`, `output_tail` |
| `escalation_rung` | escalation + gate | `rung`, `model` |
| `spend_check` | spend + panel | key view (`limit`, `remaining`, no label) and run view (`spent`, `ceiling`) |
| `bench_task` | bench | `phase` (start/end), `status` |
| `rankings_probe` | rankings | `model`, `phase` (start/end), `ok`, `cost` |
| `terminal` | apply gate | `status`, `cost`, `rounds`, `passed`, `gate_ran` |

Sink policy: `--events FILE` (JSONL, lazy-open, per-line flush). In-process
`add_sink` is what the Phase 2 server registers. A broken sink is dropped with
one warning, never raised into a lane.

## Run kind coverage (DF-UI-1)

The server (`harness/server.py` `RUNNERS`) supports four dispatchable run
kinds over `POST /api/runs` -- `apply`, `verify`, `continue`, `bench` --
plus the dedicated `chat` entry point at `POST /api/chat`. The GUI
(`harness/ui/panes.js`) now wires:

| Kind | Surface | Notes |
|---|---|---|
| `chat` | Chat tab (default) | full stepper/event UI, unchanged |
| `verify` | Verify pane | minimal form (prompt, judge, panel, max_cost) -> `POST /api/runs {"kind":"verify", ...}` -> polls `/api/runs/{id}/result` |
| `continue` | Continue pane | minimal form (state file path, instruction, verify, max_rounds) -> `POST /api/runs {"kind":"continue", ...}` -> polls `/api/runs/{id}/result` |
| `apply` | *API-only* | no dedicated pane yet; `harness apply` (CLI) or a direct `POST /api/runs {"kind":"apply", ...}` |
| `bench` | *API-only* | `manifest` is a filesystem path with no UI file-picker concept yet; use `harness bench <manifest>` (CLI) or `POST /api/runs {"kind":"bench","args":{"manifest": "..."}}` directly. Results are the same structured bench envelope the CLI prints. |

The verify/continue panes reuse the existing pane patterns (a form row
list + `POST` + `pollRunResult` waiting on `/api/runs/{id}/result`) and add
no new server-side validation or runner -- `validate_dispatch` and
`RUNNERS["verify"/"continue"]` are the same code path the CLI and chat
lane already exercise.

## DF-UI-2: read-mostly HTTP/MCP faces

Thin faces over existing owners -- no second implementation, same auth
guard (`X-Harness-Auth` / loopback) as every other `/api` route.

| Face | Owner | Notes |
|---|---|---|
| `GET /api/jev-phase[?phase=ID][&repo_root=.][&min_score=85]` | `harness.jev_completion` (`dogfood_phase`/`score_all_phases`) | Always local-only: `settings=None`, `use_live_jev=False` -- same guarantee `harness jev-phase --local-only` and the MCP `jev_phase` tool give (never a live Jev judge). No `phase` scores the whole board. |
| `GET /api/cost[?last=][&by_tier=][&by_model=][&savings=]` | `AutonomyLedger.cost_report` | Same call `harness cost` makes; flags mirror the CLI's `--last`/`--by-tier`/`--by-model`/`--savings`. |
| `GET /api/missions[?root=missions]` | `harness.mission_record` | Read-only summaries of every pack under `root` (`load_mission_pack` + `pack_summary`, no writes); missing/empty root is `{"missions": []}`, not an error. |
| `GET /api/missions/<id>[?root=missions]` | `harness.mission_record` | Same regenerate-and-summarize semantics as `harness mission status` / the MCP `mission_status` tool (refreshes `STATUS.md`/`INDEX.md`, never mutates budget/receipts/resume). |
| MCP `jev_phase` | `harness.jev_completion` | Same local-only semantics as the HTTP face above; `{repo_root, phase, all, min_score}`. |

**`plan` stays CLI/MCP-only.** `plan_and_execute` (MCP) and the DAG planner
have no server `RUNNERS` entry to reuse: the HTTP dispatch lane's four run
kinds (`apply`/`verify`/`continue`/`bench`) are each one call into an
existing engine method, but planning is a multi-node executor with its own
confirmation/waist flow that no existing server runner shape fits. Wiring
it over HTTP would mean writing a second `plan` implementation, which is
exactly the one-owner rule this document exists to keep. Use `harness plan`
(CLI) or the MCP `plan_and_execute` tool until a server runner kind that
actually fits shows up.

## Deliberate non-goals in Phase 1

- MCP `tools/call` streaming: the MCP lane keeps its ledger-based reporting;
  wiring `_events` into MCP responses is deferred until a host asks for it.
- Ledger entry *content* schema changes: the ledger is hash-chained; UI reads
  it as-is (the tail envelope carries `chain` for integrity display).
- Readiness `defer`-then-deferral: the deferral path emits `rotation` with
  `reason=readiness_defer`; a distinct "all models deferred" event can be
  added when a UI needs it.

## Phase 2 requirements satisfied

1. Live run timeline: `attempt_start` -> `readiness` -> `gate_start/end` ->
   `terminal` plus `rotation`/`spend_check` interleaves = full per-round view.
2. Consent flow in the browser: `consent_result` carries the sovereign
   decision; dispatch still requires the CLI/engine consent gate (the UI
   adds human confirmation on top, never instead).
3. Spend dashboard: `spend_check` (key + session) and `preflight` (worst-case
   vs ceiling) without parsing stderr.
4. Ledger/trust views: tail + `chain` + participation/trust envelopes.
5. Bench/capabilities matrices: bench envelope (results/statuses/calibration)
   and capabilities rows are already structured.
