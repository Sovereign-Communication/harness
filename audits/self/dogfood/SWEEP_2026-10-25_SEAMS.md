# Seam sweep — 2026-10-25

A real, non-CLI pass over the surfaces the previous full-surface sweep did not
drive: the HTTP server API, the MCP stdio tool face, and the `plan`, `continue`
and trust/ledger report paths. The first pass ran in the Freebuff worktree off
`main` `92f188a` (PR #376); the parallel-DAG slice below ran off `main`
`f4fde10` after PR #387. Raw stdout/stderr and JSON receipts live under
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

## Plan `--execute`, after the first seam pass

The first CLI plan preview exposed a second real defect: the preview's nodes
had `local_gate: null`, because `_plan_compose` did not pass the tree root to
the composer. On execute, the write-time trust gate then refused every node as
"a write with no verification gate" at unknown trust (`plan-exec1.out`, exit 2,
0/2 nodes, final gate run against untouched files). The CLI now passes its CWD
as the plan root (`root=os.getcwd()`), so `plan_task` derives each node's gate
exactly as the HTTP and MCP faces already did. Regression test:
`tests/test_cli.py::PlanAllowHeuristicPreviewWiringTests::test_plan_composition_is_rooted_at_the_cli_cwd`.

Re-verified with a real two-node chain, goal "1. fix add in add.py\n2. fix mul
in mul.py", candidate files `add.py` and `mul.py`:

| Stage | Observed | Receipt |
|---|---|---|
| Preview after fix | `planned`; batches `[[task_1], [task_2]]`; `task_2` depends on `task_1`; each node has a `py_compile` gate | `plan-preview-gated.out` |
| Execute, default attestation | fail-closed: `diff verifier (google/gemma-4-31b-it:free) returned no usable verdict`; no files changed | `plan-exec2.out` |
| Execute, `--no-attestation` | exit 0; `status=ok`, 2/2 nodes, both node gates passed, final gate `ok`; `add` changed `a - b` → `a + b`, `mul` changed `a + b` → `a * b`; cost `$0.000286` | `plan-exec3.out` |
| Ledger | 2 `dispatch_start`, 2 `verify_round`, 2 `complete`, 5 `jev_eval` | `plan-exec/ledger3.jsonl` |

The executed results are ordered but do not repeat the DAG `node_id`; each
carries its own `task_id` and `file`. The attestation run is the honest
default: a free judge that returns no verdict refuses the write rather than
skipping the check.

## Parallel DAG with worktree isolation

A second two-node plan proved the concurrent arm: two independent nodes, no
dependency edges, one file each, so the preview's batch is
`[[task_1, task_2]]` and both nodes are worktree-isolated. Files
`tmp/parallel-dag-fixture/{alpha,beta}.py` each contained the same defect
(`return a - b`); the goal was the single change `change return a - b to
return a + b`, and the final gate imported both edited modules and asserted
both results.

| Stage | Observed | Receipt |
|---|---|---|
| Preview | `planned`; one batch `[[task_1, task_2]]`; no dependencies; each node has its own `py_compile` gate rooted at the CLI CWD | `parallel/preview-green.json` |
| Concurrency | a 0.15 s worktree watcher took 32 samples; **30 samples showed both per-node worktrees existing at once**, at `task_1-22284-0a842fe8` and `task_2-22284-982764d0`, both based on `4e9a33d` | `parallel/wt-samples.log` |
| Execute | exit 0; `status=ok`, **2/2 nodes**, both node gates `passed=true`, final gate `ok` (`both-ok`) | `parallel/execute4.json`, `parallel/execute4.log` |
| Roots | `alpha.py` was edited in the task-1 worktree and `beta.py` in the task-2 worktree; each node's `py_compile` command names that same worktree path, so the gate compiled the edited copy, not the source copy | `parallel/execute4.json` |
| Merge back | two merge commits, `86bf7a8` (`alpha.py`) and `7e98e24` (`beta.py`), each changing exactly its one declared file; combined `HEAD~2..HEAD` diff is two files | `git log`/`git show --stat` on the run branch |
| Source tree | after the run both functions return `a + b`; no worktree remained and neither `harness/task_*` branch from this run survived | `git worktree list`, `git branch` |
| Ledger | 15 rows: 2 `dispatch_start`, 2 `model_result`, 2 `readiness`, 2 `verify_round`, 2 `complete`, 5 `jev_eval`; both applies passed JEV at `supported=0.98`; total cost `$0.001699` | `parallel/ledger4.jsonl` |

Fail-closed was exercised first. With a goal naming both files' different
changes, each node's instruction described both edits while its diff made only
one; JEV correctly refused both writes (`supported` 0.29–0.59 across the
attempts), the worktrees
were discarded, the source files stayed at `return a - b`, and the final gate—run
against that real, untouched tree—reported `verify_failed`. The green run used
a goal whose single change each node fully implemented. No JEV threshold, cost
ceiling, or audit bar was changed to make it pass; a direct probe confirmed the
difference is instruction content (bare single-change instruction `0.98–0.99`,
combined two-change instruction `0.26–0.49`).

The ledger rows are attributable to a node by their unique `task_id`
(`dcb7975c`, `c876d964`), model, `passed` flag and cost, and by the
`file`/worktree path in the result envelope and merge commit. They do not carry
the DAG `node_id`—the apply ledger schema predates planned nodes and still owns
only a task id—so the node name is recoverable from the result envelope and the
merge commit, not from the ledger alone. That shape is unchanged.

## HTTP `continue` — policy, not wiring

A controlled run on a fresh HTTP ledger answered the open question. The HTTP
continue lane builds the same engine through `apply_session` and reaches the
same `apply_batch(continuation=...)` call as MCP and the CLI; no settings or
seam is missing. The earlier refusal was trust arithmetic:

- With `task_max_cost=0.5` on a fresh ledger, the first successful apply also
  recorded a **guidance denial** (the request exceeded the `0.4` task ceiling
  unlocked by correctness 0). Host trust became `-1`, and the next apply was
  correctly refused at preview-only trust (`http-trust-continue.json`).
- With `task_max_cost=0.3`, a successful apply, then a deliberately failing
  apply, left trust at `0` with no denials. The HTTP `continue` run then
  returned final `200 status=ok result=ok` (`http-trust-continue2.json`).

So the earlier `-1` was correct fail-closed policy on that ledger, not an HTTP
wiring defect. No HTTP continue change is warranted.

## Remaining limits

- HTTP `continue` has no success receipt from the first sweep because that
  fixture asked for a task ceiling the fresh ledger had not unlocked; the
  controlled run above now proves success.
- `panel_verify` synthesis is unavailable when the free judge is rate-limited
  (HTTP 429); the tool correctly returns raw panel output and an explicit
  `inconclusive` consensus rather than inventing a verdict.
- Plan execution used `--no-attestation` for the green DAG run; the default
  attestation path was exercised and failed closed when the free judge returned
  no verdict. A paid-judge attestation run remains unexercised.

No audit or JEV bar, coverage baseline, pinned score, or global ceiling was
changed. #277, #340, #304, #333 and #306 were not started or decided.

## Surface exhaustion verdict

Proven end-to-end by this sweep, with receipts above:

| Surface | Evidence |
|---|---|
| CLI `apply` / `continue` / `dogfood` | green writes, rewinds, continuations and gates, including a legitimately failing gate |
| CLI `plan` preview | grounded, tiered, rooted nodes with derived `py_compile` gates |
| CLI `plan --execute`, serial | a real dependency chain: 2/2 nodes, both gates, final gate green |
| CLI `plan --execute`, parallel | a real two-node independent stage: two simultaneous worktrees, per-node gates in the right roots, two one-file merges, final gate green |
| CLI `ledger verify` / `report` | chain verified and participation report emitted |
| MCP stdio | initialize/tools/list plus trust, participation, ledger, spend, `panel_verify`, `apply_edit` pass/fail, `continue_work`, `dogfood`, plan preview |
| HTTP API | auth boundary, status/trust/ledger/spend, and runs for verify, apply, plan, dogfood and continue |
| JEV write gate | semantic pass and fail-closed refusal both observed; fail-closed never wrote |

Still unexercised, with the reason:

- **Green default attestation.** The default path was exercised and refused
  closed when the free judge returned no usable verdict; a paid-judge green run
  was not bought for this sweep. This is a cost/availability limit, not a
  wiring gap.
- **Live merge conflict and live undeclared-write rejection.** Both are covered
  hermetically (`tests/test_worktree.py`) but deliberately not induced here;
  inducing a malicious worker or a conflicting write against the real tree
  would be destructive and is outside a read/receipt pass.
- **Parallel DAG execution through HTTP/MCP.** Their planning faces were
driven and all three lanes share `PlanExecutor`, but only the CLI parallel
  execution was run end-to-end.
- **`EV-8` Fireworks.** Open only on a live Fireworks API key against the two
  named models.
- **`OC-HANDOFF` / PR #73** remains the experimental OpenClaw lane; **`GAP-jury`
  / `JEV-P2-jury`** remain deferred by operator ruling; **`CIVICSCOPE-COMPLETION`**
  is an external sibling repo.

Every lane exercised here works end-to-end; the items above are deliberate
scope limits or external blocks, not broken lanes. This run's worktree branches
were removed, `main` was green, and the published issue queue still matches the
live open set at 64 rows / 59 open issues.
