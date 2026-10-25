# Seam sweep — 2026-10-25

A real, non-CLI pass over the surfaces the previous full-surface sweep did not
drive: the HTTP server API, the MCP stdio tool face, and the `plan`, `continue`
and trust/ledger report paths. Run in the Freebuff worktree off `main` `92f188a`
(PR #376). Raw stdout/stderr and JSON receipts live under
`tmp/sweep-2026-10-25/receipts/` in the run's worktree (ignored by git); this
file records what was observed.

## MCP stdio seam

Server started the documented way, `python -m harness.mcp`, with
`HARNESS_MCP_ALLOW_WRITE=true`, `HARNESS_MCP_ALLOW_VERIFY=true`, and a dedicated
`HARNESS_MCP_ALLOWED_ROOTS` fixture tree. The client performs the real
`initialize` / `notifications/initialized` / `tools/call` exchange over a pipe.

| Surface | Observed | Receipt |
|---|---|---|
| `initialize`, `tools/list` | protocol 2025-06-18, 30 tools listed | `mcp-seam.json` |
| `trust_status`, `participation_report`, `ledger_status`, `spend_status` | unknown-trust host, empty participation, ledger chain verified, spend ceiling reported | `mcp-seam.json` |
| `panel_verify` | one panelist answered; free judge returned HTTP 429 twice, so the verdict is explicitly raw panel output with `consensus=inconclusive` / `judge_fallback=true`; cost $0.000003 | `mcp-seam.json` |
| `apply_edit` (passing gate) | `status=ok`, one verify round passed, target changed `a + b` → `a * b` | `mcp-seam3.json`, `ledger15.jsonl` |
| `apply_edit` (failing gate) | `status=verify_failed`, `gate_ran=true`, continuation object returned | `mcp-seam3.json` |
| `continue_work` | `status=ok`; rounds `verify_failed` then `ok` after the continuation fixed `mul`; gate passed | `mcp-continue2.json`, `ledger16.jsonl` |
| `dogfood` | `status=ok`; ground ok, verify ok (claim `c1` converged `"real"` by 1/1), apply ok; target changed `a - b` → `a + b` | `mcp-dogfood.json`, `ledger17.jsonl` |
| `plan_and_execute` (preview) | `status=planned`, one-node DAG, tier 0, cost ceiling 0 | `mcp-seam3.json` |

## HTTP server seam

Started with `python -m harness.cli serve --host 127.0.0.1 --port 8813
--auth-token …` and called with the real HTTP API (no in-process handler mocks).

| Surface | Observed | Receipt |
|---|---|---|
| Auth boundary | missing token → **401**; foreign Host → **403** | `http-seam.json` |
| `GET /api/status` | 200, `auth_required: true` | `http-seam.json` |
| `GET /api/trust`, `/api/ledger/verify`, `/api/ledger/report`, `/api/spend` | 200; ledger chain verified; participation report and spend envelope returned | `http-seam.json` |
| `POST /api/runs` kind `verify` | dispatch 201 `running` → final 200 `done` with panel result | `http-seam.json` |
| `POST /api/runs` kind `apply` | dispatch 201 → final 200 `ok`, target changed and gate passed | `http-seam.json` |
| `POST /api/runs` kind `plan` | dispatch 201 → final 200 `planned`, DAG returned | `http-seam.json` |
| `POST /api/runs` kind `dogfood` (operator ledger) | final 200 `error`: trust gate refused at combined trust **-6** (operator ledger history), no write | `http-seam.json` |
| `POST /api/runs` kind `dogfood` (fresh fixture ledger) | final 200 `ok`, three phases | `http-seam2.json` |
| `POST /api/runs` kind `continue` | dispatch 201 → final `error`: trust gate refuses at combined **-1** preview-only after the deliberately failing apply in the same fresh ledger | `http-seam2.json` |

The two dogfood runs are the same code path on different trust histories: the
operator ledger is at refuse level and correctly refuses, while a fresh fixture
ledger completes all three phases. The `continue` refusal is likewise the trust
policy, not a transport defect; the mechanism itself is proven on the MCP and
CLI faces below.

## CLI plan, continue, ledger paths

| Surface | Command (shape) | Observed | Receipt |
|---|---|---|---|
| `plan` (preview) | `harness plan --goal "Make mul multiply" --file … --no-decompose-llm --no-confirm` | exit 0, `status=planned`, one node, tier 0, ladder returned, no spend | `cli-plan.out` |
| `continue` | `harness continue --state … --instruction …` | `verify_failed` on the first gate, then `ok` after the continuation; target changed `a + b` → `a * b` | `continue/result.json`, `continue/continued.json` |
| `ledger verify` / `ledger report` | `harness ledger verify`, `harness ledger report` | chain verified, participation report emitted | `ledger-verify.out`, `ledger-report.out` |

## Findings and fixes

1. **Fixed — a gate launched by the MCP server inherited the JSON-RPC stdin
   pipe.** `osal.run` passed `stdin=None`, so every gate child inherited the
   server's stdin. Under MCP that handle is the stdio protocol channel, and the
   gate blocked until the 300 s verify timeout: `apply_edit` with the trivial
   gate `python check.py` returned `verify_failed` with
   `command timed out after 300s`, and even `python -c "print('gate ok')"`
   timed out. `osal.run` now passes `subprocess.DEVNULL` when no `input_text` is
   supplied (the same isolation `run_tree` already documents), keeping the
   explicit-input path intact. Regression test:
   `tests/test_osal.py::OsalRunTest::test_gate_never_inherits_the_callers_stdin`.
   Re-verified through MCP: the same `apply_edit` is `status=ok` in 18 s
   (`ledger14`), and `continue_work` / `dogfood` then pass.

2. **Fixed — the HTTP plan runner called the composer with a shape it does not
   accept.** `run_plan_task` passed a positional goal and a `settings=` kwarg to
   the keyword-only `compose_plan(..., opts_goal=...)`, so every real HTTP plan
   failed before planning. It now passes `opts_goal`, the session ledger,
   `execute`, `use_free`, and the escalation/token options, mirroring the CLI
   and MCP shapes. Test:
   `tests/test_plan_http_batch.py::TestPlanTaskRunner` now asserts the accepted
   kwargs. Re-verified through HTTP: final `planned`.

3. **Fixed — HTTP/MCP dogfood crashed with `max_lines=None`.** The non-CLI
   faces do not pass `max_lines`; `service.run_dogfood` forwarded `None` to the
   engine, which compares it with a line count. It now defaults to the
   documented `MAX_FILE_LINES` (500), matching the CLI. Test:
   `tests/test_dogfood.py::DogfoodVerifySeamContractTests::test_run_dogfood_defaults_max_lines_for_non_cli_faces`.
   Re-verified through HTTP (final `ok`) and MCP (`status=ok`).

## Honest limits

- HTTP `continue` success was not reached: the trust gate refused at combined
  -1 after the intentionally failing apply. The continuation mechanism is
  proven on MCP (`continue_work` `ok`) and CLI (two rounds, second `ok`).
- `panel_verify` synthesis is unavailable when the free judge is rate-limited
  (HTTP 429); the tool correctly returns raw panel output and an explicit
  `inconclusive` consensus rather than inventing a verdict.
- `harness plan --execute` was not run; only preview mode was exercised. The
  planned result is real, but a full DAG execution through that CLI face
  remains unexercised in this sweep.

No audit or JEV bar, coverage baseline, pinned score, or global ceiling was
changed. #277, #340, #304, #333 and #306 were not started or decided.
