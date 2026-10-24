# UI-readiness audit

What the data layer gives the UI, which panes drive which owner, and what the
browser deliberately does not do. Every row is a thin face over an existing
engine entry point — no pane owns policy. Hermetic tests pin the contracts:
`tests/test_ui_operator_parity.py`, `tests/test_plan_http_batch.py`,
`tests/test_server.py`, and `tests/events`-adjacent suites.

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
| `gate_start` / `gate_end` | apply gate, documented gate runner | `command`, `passed`, `rc`, `output_tail`; the documented runner also adds `gate`, `duration_s`, `timeout_s` |
| `escalation_rung` | escalation + gate | `rung`, `model` |
| `spend_check` | spend + panel | key view (`limit`, `remaining`, no label) and run view (`spent`, `ceiling`) |
| `bench_task` | bench | `phase` (start/end), `status` |
| `rankings_probe` | rankings | `model`, `phase` (start/end), `ok`, `cost` |
| `terminal` | apply gate | `status`, `cost`, `rounds`, `passed`, `gate_ran` |

Sink policy: `--events FILE` (JSONL, lazy-open, per-line flush). In-process
`add_sink` is what the Phase 2 server registers. A broken sink is dropped with
one warning, never raised into a lane.

## Run kind coverage (DF-UI-1 / DF-UI-5)

`POST /api/runs` and `RUNNERS` now cover every kind the UI can drive. Each
pane below uses `POST /api/runs` -> `/api/runs/{id}/result` (with the event
stream for progress) and adds no runner of its own.

| Kind | Pane | Notes |
|---|---|---|
| `chat` | Chat tab (default) | full stepper/event UI, `/script` consent cards, script posture badge |
| `verify` | Verify pane | prompt, judge, panel, max_cost |
| `continue` | Continue pane | state file path, instruction, verify, max_rounds |
| `apply` | Apply pane | target file, instruction, verify command, backend, ceilings |
| `dogfood` | Dogfood pane | claims/source/definitions files, rounds, ceiling |
| `plan` | Plan pane | goal, workspace, candidate files, execute opt-in, workers, ceilings; renders DAG nodes and execution summary |
| `bench` | Bench pane | manifest file **or directory** (the CLI-documented shapes), workspace, max_rounds; renders per-task statuses |
| `gate` | Gates pane | gate name from `GET /api/gates`; optional `phase` (jev-phase) and bounded timeout; returns exit code + output tail |

`gate` is a run kind rather than a synchronous POST so a long `unittest` or
`audit` gate is pollable and cannot hold an HTTP handler thread. The runner
pins `cwd` to this checkout, tokens come from `gate_runner.GATES`, and
`run_gate` remains the one shell-free runner. Cancelling before the gate
starts prevents it; cancelling mid-gate cannot kill the child process (the
runner has no cancel seam), and the pane states that limit instead of
claiming the gate stopped.

### Operator parity panes (DF-UI-5)

| Pane | Face | Owner | Notes |
|---|---|---|---|
| Plan | `POST /api/runs {"kind":"plan"}` | `run_plan_task` -> `compose_plan`/`PlanExecutor` | preview by default; Execute is an explicit checkbox |
| Bench | `POST /api/runs {"kind":"bench"}` | `run_bench_task` | manifest validation now accepts a file or directory |
| Gates | `GET /api/gates`, `POST /api/runs {"kind":"gate"}` | `gate_runner.GATES` / `run_gate` | list includes the platform-resolved argv, command and default timeout |
| Ledger | `GET /api/ledger/verify|tail|report|defer-stats`, `POST /api/ledger/repair` | `AutonomyLedger` | repair requires the exact body `{"confirm":"repair"}` and returns before/after chain verdicts |
| Models & Cost | `GET /api/spend|cost|capabilities|models|rankings|economics` | existing read owners | no config writes, no probes; live catalog load is opt-in |
| Missions | `GET /api/missions[/{id}]`, `POST /api/missions` | `mission_record.build_mission_spec` + `init_mission_pack` | pack root is confined under `./missions`; listed/detail reads stay read-only |
| Provision | `GET /api/provision/probe`, `POST /api/provision/plan` | `provision.probe_host` / `plan_provision` | plan only; mutating execution stays `harness provision apply --execute` |

The shell also exposes `/api/status`'s resolved `scripting` posture
(`enabled`, `timeout_s`, `max_output_bytes`, `allow_network`) as a header badge,
and remembers up to eight recent workspace paths in a `datalist`. The workspace
path stays typed: a browser page cannot hand the server a native folder path,
and neither the HTTP server nor the pywebview shell wires a host folder dialog
today. Recents are the browser-safe affordance until one does.

## DF-UI-2: read-mostly HTTP/MCP faces

Thin faces over existing owners -- no second implementation, same auth guard
(`X-Harness-Auth` / loopback) as every other `/api` route.

| Face | Owner | Notes |
|---|---|---|
| `GET /api/jev-phase[?phase=ID][&repo_root=.][&min_score=85]` | `harness.jev_completion` (`dogfood_phase`/`score_all_phases`) | Always local-only: `settings=None`, `use_live_jev=False` -- same guarantee `harness jev-phase --local-only` and the MCP `jev_phase` tool give (never a live Jev judge). No `phase` scores the whole board. |
| `GET /api/cost[?last=][&by_tier=][&by_model=][&savings=]` | `AutonomyLedger.cost_report` | Same call `harness cost` makes; flags mirror the CLI's `--last`/`--by-tier`/`--by-model`/`--savings`. |
| `GET /api/missions[?root=missions][&limit=25][&offset=0]` | `harness.mission_record` | Compact summaries, stable offset pagination, `limit` 1-100; each page omits receipt/evaluation history and variable-length resume/request fields. `total` counts pack directories containing `mission.yaml`; corrupt selected packs are skipped. Missing/empty root returns an empty page. |
| `GET /api/missions/<id>[?root=missions]` | `harness.mission_record` | Read-only full pack summary; does not regenerate `STATUS.md` or `INDEX.md`. The CLI/MCP `mission_status` commands retain their existing refresh behavior. |
| `POST /api/missions` | `harness.mission_record` | Same `build_mission_spec` + `init_mission_pack` path as `harness mission init`; root is jailed by `resolve_missions_root`; existing ids are refused. |
| MCP `jev_phase` | `harness.jev_completion` | Same local-only semantics as the HTTP face above; `{repo_root, phase, all, min_score}`. |

The former "`plan` stays CLI/MCP-only" note is retired: `run_plan_task` is a
real server runner (bench precedent), and the Plan pane is its face. The
operator ruling for `GAP-plan-http` is recorded by the landing PR.

## UX acceptance (human-drivability)

The GUI is judged by whether a person can discover and finish a lane without
the CLI:

1. **Every lane in this document has a labelled pane** with a one-sentence
   purpose, a form that names its fields, and a submit/run control. None of the
   lanes listed above is API-only.
2. **Every pane has empty, loading, success and error states.** The Ledger
   shows a placeholder while the chain loads; Gates shows elapsed seconds while
   a gate runs; each dispatch area replaces its placeholder with the terminal
   envelope or the exact error.
3. **Destructive actions are explicit.** Ledger repair requires typing
   `repair` (the server also requires the JSON word), and the button stays
   disabled until it matches. Plan Execute is an unchecked checkbox by
   default. Provision never executes mutating steps from a browser plan.
4. **Terminal evidence is visible, not inferred.** Gate cards show exit code,
   duration and output tail; bench shows per-task status and pass rate; plan
   shows DAG nodes and execution status; ledger shows the chain verdict and
   both sides of a repair.
5. **Secrets stay presence-only.** `/api/settings` and `/api/status` never
   return keys; the script badge reports posture, not source or credentials.
6. **Keyboard and screen-reader basics.** Panes use native buttons, inputs,
   labels, `<details>` and tables; the tab bar is a `tablist` of buttons, and
   every control is reachable by Tab. There is no custom drag-only widget.
7. **One browser limitation is stated, not hidden.** The workspace path is
   typed (with recent-path completion) because a web page cannot give the
   server a native folder handle; no picker exists yet on either surface.

## Deliberate non-goals in this slice

- Mutating host provisioning from the browser: the Provision pane probes and
  plans, then offers a JSON download; `harness provision apply --execute` stays
  a typed operator action with its lifecycle/reconciliation owner.
- `mission run` / `mission resume --run` from the browser: the pane initializes
  and reads packs; the HUL driver remains a CLI/API lane.
- MCP `tools/call` streaming: the MCP lane keeps its ledger-based reporting.
- Ledger entry *content* schema changes: the ledger is hash-chained; UI reads
  it as-is (the tail envelope carries `chain` for integrity display).
- Readiness `defer`-then-deferral: the deferral path emits `rotation` with
  `reason=readiness_defer`; a distinct "all models deferred" event can be
  added when a UI needs it.
