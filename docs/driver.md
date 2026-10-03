# `harness driver` — verified extraction, Jev decision, deterministic action

Harness talks to **driver-core** (a separate sibling service,
`Sovereign-Communication/driver-core`) through a thin stdlib adapter,
[`harness/perception_client.py`](../harness/perception_client.py). It is the
same shape as [`harness media`](media.md): harness resolves an endpoint and
carries an envelope, and everything else stays service-side.

Nothing here names a model, provider, or screen-reading library. driver-core
owns its ladders and its extractor policy; this adapter owns none of it.

## What the driver is for

The ordering is the whole design, and it is the part worth understanding
before using it:

```
capture -> extract (verified by consensus) -> Jev decision -> execute
                            ^^^^^^^^^^^^^^^^
                    cross-verification happens HERE, not after
```

Extraction is cross-verified **before** the Jev decision, never after. This
is not a stylistic preference. A calibrated confidence is calibrated only for
the question that was actually asked: hand a model a hallucinated screen
state and it returns a confidently wrong answer, and no amount of checking
downstream can detect that, because the downstream check is judging the same
confident answer. So the input to a decision is already an agreement of
independent readers, or the step stops.

Three rules the driver holds to, which show up as reasons on a refusal:

- **An unanswered slot is a shortfall, not a vote.** Missing input does not
  dilute agreement; it reduces quorum.
- **The tally owns the verdict.** A specialist reader may explain a
  disagreement but never overrule it.
- **Agreement is per field, not per document.** One agreed field does not
  vouch for the fields beside it.

## The one distinction that matters for callers

**A refusal is a successful call.**

| What happened | How the adapter reports it |
|---|---|
| The driver declined to act | `step()` returns an envelope with `ok: False` and a `reason` |
| The service is down / 4xx / 5xx / non-JSON | raises `PerceptionUnavailable` |

The service answers **HTTP 200** for every refusal; only malformed requests
and unknown routes are 4xx/5xx. That is deliberate. A caller that retries on
a transport error must never be retrying a *decision*, and conflating the two
is how a refusal turns into a loop. `harness driver step` therefore exits:

- `0` — the step completed
- `1` — the driver refused, with a named reason (this is **not** a failure of
  the command)
- `2` — your arguments were malformed; nothing was sent
- `4` — the service was unreachable (defer-style; nothing was spent)

`PerceptionUnavailable` is raised only for transport failures and for
responses that violate the service's documented shape — a refusal carrying no
`reason`, or an undeclared reason. Those are contract violations, surfaced at
the boundary so no future caller has to re-check them.

## Consent is never invented

Consent binds to an exact **action and its parameters**, and it is yours to
give. The adapter has no code path that synthesises one:

```bash
# Read-only observation. No consent is sent, so the driver refuses
# anything mutating on its own.
harness driver step "file-manager" --schema screen

# An explicit, parameter-bound consent, recorded in the driver's audit log.
harness driver step "file-manager" \
    --action open_window --params '{"path": "~/notes"}' --by operator
```

A step without `--action` is a read-only observation. Irreversible actions
are never batched, and no model runs past the execution tier.

## Other commands

```bash
harness driver health        # liveness + the driver's redacted settings
harness driver vocabulary    # the closed action vocabulary it will accept
harness driver schemas       # declared extraction schemas
harness driver verify        # audit-chain verdict + spend snapshot
harness driver step TARGET --raw   # the full envelope as JSON
```

## Native In-Repo Module & GUI Integration

`driver_core` is integrated natively into the Harness repository as a pure standard-library module (`driver_core/`), complete with CLI entrypoint `driver-core`, server loopback daemon management, GUI control pane, and MCP tools:

1. **Native Module (`driver_core/`)**:
   - Zero non-stdlib runtime dependencies.
   - Internal OSAL boundary (`driver_core/osal.py`) strictly isolated from Harness core.
   - Packaged and discoverable via `pyproject.toml` (`driver-core = "driver_core.cli:main"`).

2. **Web / Desktop GUI (`harness/ui/panes.js`, `harness/ui/panes.css`)**:
   - Interactive **Driver** pane in the Harness UI.
   - **Service Health & Sources**: Displays driver state, active perception tiers, schema definitions, and loopback connectivity.
   - **Interactive Step Dispatch**: Live execution form supporting target specification, extraction schema selection, parameter-bound consent authoring, and stability toggles.
   - **Execution Breakdown**: Live envelope inspector rendering step ID, reason, Jev confidence scores, extraction findings, and refusal diagnosis.
   - **Declared Vocabulary Table**: Interactive registry of accepted driver actions with parameter signatures and descriptions.
   - **Audit Chain Verification**: Real-time cryptographic ledger hash chain integrity checks.

3. **Server REST API (`harness/server.py`)**:
   - `GET /api/driver/health`: Driver status and service metadata.
   - `GET /api/driver/vocabulary`: Available action vocabulary.
   - `GET /api/driver/schemas`: Declared perception schemas.
   - `GET /api/driver/verify`: Cryptographic audit log verification.
   - `POST /api/driver/step`: Dispatches a step and returns the envelope.
   - `POST /api/driver/start`: Spawns or verifies the background loopback service daemon.

4. **MCP Tools (`harness/mcp.py`, `harness/mcp_lanes.py`)**:
   - `driver_step`: Dispatches perception, verification, and action steps (registered in `MUTATION_LANE`).
   - `driver_health`: Queries driver liveness and settings.
   - `driver_vocabulary`: Lists accepted actions.
   - `driver_verify`: Verifies audit log hash chain integrity.

## Configuration

No Harness state file is written on the driver's behalf. Resolution order is
**explicit constructor args > config file > environment > loopback default**:

| Setting | Env var | Default |
|---|---|---|
| Endpoint | `DRIVER_BASE_URL` | `http://127.0.0.1:8791` |
| Token | `DRIVER_TOKEN` | — |
| Config file | `DRIVER_CONFIG_PATH` | `~/.config/harness/driver.json` |

```json
{ "base_url": "http://127.0.0.1:8791", "token": "..." }
```

Start the service with `driver-core serve` (or let Harness server lazily start the loopback daemon). It binds **loopback only** and requires a bearer token, because an endpoint that can act on a machine should never be reachable by accident from another host. That is a floor, not a security claim.

The driver is configured entirely under `DRIVER_*` and reads nothing from
Harness's environment. This adapter is asserted to read no other namespace,
so the two projects stay separable in both directions.

## Implementation Status & Provider Backends

The adapter, module integration, GUI pane, and MCP tools are complete. The perception pipeline adheres strictly to structural tiers before screen capture (CLI → MCP → DOM → Screen). Synthetic input actions without an underlying system driver backend (such as low-level `click` / `type` / `key`) are safely declared and refused via HTTP 200 envelopes with structured closed reasons (`action_unsupported` or `decision_not_usable`), preserving deterministic non-crashing behavior across all caller lanes.