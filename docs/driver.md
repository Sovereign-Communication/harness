# `harness driver` — verified extraction, Jev decision, deterministic action

The driver is a native, in-repo module: [`driver_core/`](../driver_core/). It
is a self-contained, segmented, standard-library-only package. Nothing inside
it imports Harness, so it can be read, tested and shipped on its own. Harness
reaches it through one thin adapter,
[`harness/perception_client.py`](../harness/perception_client.py)
(`PerceptionAdapter` plus the `harness driver` CLI), over the loopback REST
contract described below. The adapter speaks plain HTTP, so it works against
any service that exposes the same contract; `DRIVER_BASE_URL` selects which.

Nothing here names a model, provider, or screen-reading library. The driver
resolves its own extractor pool and Jev model from its settings.

## What the driver is for

The ordering is the whole design:

```
capture -> extract (verified by consensus) -> Jev decision -> execute
                            ^^^^^^^^^^^^^^^^
                    cross-verification happens HERE, not after
```

Extraction is cross-verified **before** the Jev decision, never after. A
calibrated confidence is calibrated only for the question that was actually
asked: hand a model a hallucinated screen state and it returns a confidently
wrong answer, and no downstream check can detect that, because the downstream
check is judging the same confident answer. So the input to a decision is
already an agreement of independent readers, or the step stops.

Three rules the driver holds to, which show up as reasons on a refusal:

- **An unanswered slot is a shortfall, not a vote.** Missing input reduces
  quorum; it does not dilute agreement.
- **The tally owns the verdict.** A specialist reader may explain a
  disagreement but never overrule it.
- **Agreement is per field, not per document.** One agreed field does not
  vouch for the fields beside it.

Perception tiers are tried structured-first (CLI, then MCP, then DOM) and the
screen (pixels) last. The default driver declares no source at all and
observes nothing: each tier turns on only when its own setting is present
(`DRIVER_CLI_COMMAND`, `DRIVER_MCP_COMMAND` + `DRIVER_MCP_TOOL`,
`DRIVER_DOM_URL`, `DRIVER_SCREEN`). A step against a driver with no source is
refused with `no_capture`.

## A refusal is a successful call

| What happened | How it is reported |
|---|---|
| The driver declined to act | HTTP 200, envelope with `ok: false` and a named `reason` |
| Malformed request or unknown route | HTTP 400 / 404 |
| Service down, wrong token, 5xx, non-JSON | the adapter raises `PerceptionUnavailable` |

A caller that retries on a transport error must never be retrying a
*decision*; conflating the two is how a refusal turns into a loop.
`PerceptionUnavailable` is also raised when a response violates the documented
shape (a refusal with no `reason`, or an undeclared one), so no caller has to
re-check it.

### Refusal reasons

The closed set is `STOP_REASONS` in
[`driver_core/server.py`](../driver_core/server.py), mirrored by the adapter.
Callers may branch on these and on nothing else about a refusal:

| `reason` | Meaning |
|---|---|
| `no_capture` | No declared source could observe the target (this is what a default, unconfigured driver reports). |
| `insufficient_agreement` | Too few independent extractors answered to reach quorum. |
| `extraction_disagreement` | The extractors answered, and did not agree. |
| `confidence_below_threshold` | The decision's calibrated confidence was under the configured threshold. |
| `state_not_stable` | The observed state was still changing and stability was required. |
| `decision_not_usable` | The Jev decision was unavailable (for example no key) or malformed. |
| `no_action_recommended` | The decision tier recommended doing nothing. |
| `undeclared_action` | The decision named an action outside the declared vocabulary. |
| `execution_refused` | The executor refused: missing, stale or mismatched consent, side effects switched off, or no input backend (see below). The `detail` says which. |

`action_unsupported` is **not** a driver reason and is never emitted.

The Python exceptions in [`driver_core/errors.py`](../driver_core/errors.py)
(`VocabularyError`, `ConsentError`, `BudgetRefused`, `ExecutorError`,
`OsalError`, ...) are internal: the service converts them into the reasons
above or into a 400, so they never reach a REST caller as a stack trace.

## Input actions are always refused today

The action vocabulary declares six kinds of synthetic input: `click`,
`type_text`, `press_key`, `focus`, `scroll` and `submit_irreversible`.
**Every one of them is refused, always, until an input backend is registered**
through `driver_core.osal.register_input_backend`. Nothing in this repository
registers one, so on a stock install a step that would click, type, press a
key, focus, scroll or submit comes back `ok: false` with
`reason: "execution_refused"`; the detail says either that side effects are
off or that no backend is registered for the platform. It never reports an
input that did not happen. The input backend is tracked as open work (roadmap
row `DRV-2`, which stays open); this document will describe it when it exists.

What the executor actually implements (the vocabulary declares 14 actions;
only these have an executor behind them, and anything else is refused as
unregistered):

- Always: three read-only executors, `no_action`, `observe` and `read_value`.
  The other declared read-only actions (`read_dom`, `call_read_tool`,
  `run_probe`) have no executor in this build and are refused.
- Only when `DRIVER_ALLOW_WRITE` is set: `write_file` (back up, then atomic
  replace) and `delete_file` (irreversible), plus the six input actions above,
  which still refuse at the OS layer for want of a backend.

## Consent is never invented

Consent binds to one exact **action and its parameters** (there is no
wildcard), and it is yours to give. The adapter has no code path that
synthesises one:

```bash
# Read-only observation: no consent sent, so anything mutating is refused.
harness driver step "file-manager" --schema screen

# An explicit, parameter-bound consent, recorded in the driver's audit log.
harness driver step "notes" --schema cli \
    --action write_file --params '{"path": "notes.txt", "content": "hi"}' --by operator
```

Irreversible actions need a fresh, explicit confirmation and are never
batched, and no model runs past the execution tier.

Two paths attach a grant on the caller's behalf, and both say so. The MCP
`driver_step` tool builds a grant when you pass `action`. `POST
/api/driver/drive` sends **no consent by default** (`auto_approve` is
`false`); with `"auto_approve": true` it sends a grant for the one declared
read-only action `observe`, labelled `by: "harness:auto_approve"` (never as a
person). It cannot authorise a mutating or irreversible action; those need a
parameter-bound consent from you on `/api/driver/step`. The driver also
refuses anything its own consent law or executor registry does not allow.

## Command line

```bash
harness driver health        # liveness + the driver's redacted settings
harness driver vocabulary    # the closed action vocabulary it will accept
harness driver schemas       # declared extraction schemas
harness driver verify        # audit-chain verdict + spend snapshot
harness driver step TARGET --schema cli --raw   # the full envelope as JSON
```

`harness driver step` exits `0` when the step completed, `1` when the driver
refused with a named reason (a decision, **not** a failure of the command),
`2` for malformed arguments (nothing sent), and `4` when the service was
unreachable (nothing spent). The standalone `driver-core` entry point
(`driver-core health | vocabulary | schema | step | verify | serve`) runs the
same module directly; `driver-core serve` starts the loopback service.

## Server REST API

The Harness UI server proxies the driver under `/api/driver/*`. Every route
sits behind the UI's own token (`X-Harness-Auth` or `Authorization: Bearer`;
a token in the URL query string is **not** accepted): no token or a wrong token is `401` and **never starts a driver**.

| Route | Purpose |
|---|---|
| `GET /api/driver/health` | Driver status, version, live sources, redacted settings. Never contains the driver token. |
| `GET /api/driver/vocabulary` | The declared action vocabulary. |
| `GET /api/driver/schemas` | The three declared extraction schemas: `screen`, `cli`, `dom`. (The `schema` field of a step names a target class: `cli`, `dom`, `gui`, `mcp` or `screen`.) |
| `GET /api/driver/verify` | Audit hash-chain verdict and spend snapshot. |
| `POST /api/driver/step` | One step. Body: `target` (required), `schema` (required), optional `consent`, `prefer`, `require_stable`. A refusal is 200 `ok:false`; missing fields are 400; an unreachable driver is 503. |
| `POST /api/driver/start` | Ensure the in-process loopback driver is running and report its health. |
| `POST /api/driver/drive` | Start a multi-step run (a normal UI run: 201 with an `id`; poll `GET /api/runs/<id>/result`). Body: `goal` (required), optional `target`, `schema`, `max_steps` (1-20, default 5), `verify` (a gate command, see below), `require_stable`, `auto_approve` (default false), `max_cost` (stop before the next step once the run's reported driver cost reaches it). |

`/api/driver/drive` walks the perception tiers in order (`cli`, `mcp`, `dom`,
`screen`), one per step, until a step succeeds or the optional `verify`
command passes. Its result reports `status`, `ok_steps` (how many steps the
driver actually completed), per-step envelopes, total cost, the audit verdict
and a `summary`:

| `status` | Meaning |
|---|---|
| `done` | A step succeeded and, if a `verify` command was given, it passed ("goal met"); or a step succeeded with no `verify` command (the summary says nothing confirmed it). |
| `verified_without_driver` | The `verify` command passed but the driver executed 0 steps successfully: the result is real but not attributable to the driver. |
| `max_steps_reached` | No success within `max_steps`; the summary says nothing was verified. |
| `cost_capped` | Stopped at `max_cost`. |
| `cancelled` | Cancelled by the caller. |

The `verify` command runs as a gate through `harness.gate_runner` (no shell,
30 s timeout) with the privileges of the server process; see
[security.md](security.md).

### The in-process driver and its token

When `/api/driver/*` or a drive run needs a driver and none is reachable at
the configured endpoint, Harness starts one in-process on loopback (a daemon
thread owned by the server process, stopped when the process exits).

- The bearer token is `DRIVER_TOKEN` if you declared one (it must meet the
  driver's minimum length); otherwise the driver generates a random token for
  that start (`secrets.token_urlsafe(24)`). There is no built-in or fixed
  fallback token.
- The token lives in the adapter that talks to the driver. It is not put in
  the process environment, not returned by `/health`, and not logged.
- To integrate an external client over HTTP, declare `DRIVER_TOKEN`
  yourself, since a generated token dies with its process.

## Desktop token

`harness desktop` takes its UI token from `--auth-token`, then
`HARNESS_UI_AUTH_TOKEN`; with neither it generates one, stores it in
`~/.config/harness/desktop_token` (created owner-only, mode 0600 on POSIX), and reuses it so the window keeps working
across restarts. The server only ever accepts the token it was started with:
a caller cannot replace it by presenting a different one first.

## MCP tools

| Tool | Notes |
|---|---|
| `driver_step` | Registered in the mutation lane. Takes `target`, `schema`, optional `action` + `params` + `by` (builds a parameter-bound consent), `prefer`, `require_stable`. |
| `driver_health` | Liveness and redacted settings. |
| `driver_vocabulary` | The declared actions. |
| `driver_verify` | Audit chain verdict and spend. |

The MCP tools use the adapter directly and do not start a driver; point them
at a running one (`driver-core serve`) with `DRIVER_BASE_URL` and
`DRIVER_TOKEN`.

## GUI

The Driver pane in the web/desktop UI shows service health and live sources,
the declared vocabulary, and an interactive step form with consent authoring
and an envelope inspector; it also offers audit-chain verification. It is a
view over the routes above and owns no policy.

## Configuration

Resolution order is **explicit constructor args > config file > environment >
loopback default**. No Harness state file is written on the driver's behalf.

| Setting | Env var | Default |
|---|---|---|
| Endpoint | `DRIVER_BASE_URL` | `http://127.0.0.1:8791` |
| Token | `DRIVER_TOKEN` | generated per start when unset |
| Config file | `DRIVER_CONFIG_PATH` | `~/.config/harness/driver.json` |
| Audit log | `DRIVER_AUDIT_PATH` | per-user state directory |
| Allow side-effect executors | `DRIVER_ALLOW_WRITE` | off |
| Dry run | `DRIVER_DRY_RUN` | off |

```json
{ "base_url": "http://127.0.0.1:8791", "token": "..." }
```

The service binds **loopback only** and requires a bearer token, because an
endpoint that can act on a machine should never be reachable by accident from
another host. That is a floor, not a security claim: any local process that
holds the token can drive it. The driver is configured entirely under
`DRIVER_*` and reads nothing from Harness's environment.

## Status

Delivered: the native module, the adapter, the CLI, the REST routes, the
Driver pane and the MCP tools, with hermetic tests (no network, no screen, no
model). Not delivered: any synthetic-input backend (above) and a live vision
extractor, both tracked under roadmap row `DRV-2`, which stays open.
