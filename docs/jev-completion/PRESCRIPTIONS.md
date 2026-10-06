# harness Phase-2 Prescriptions — Jev-selected implementation patterns

Audited SHA 0676d642 (origin/main), 7,589 functions, model `jev-1.13.0`. 3797 prescription judgments, 2989 context-needs judgments. 0 API errors.
Tables `prescriptions`, `context_needs`, `deepened`, `battery_security`, `battery_testplan`, `battery_concurrency`, `pinpoints`, `pattern_fits` in `audit.db`.

## Execution-ready (>=0.95, production-path, non-test)

- **0.98** `task_runner` (harness/executor.py:540) [security] -> **validate-guard**
- **0.98** `run_bench_task` (harness/server.py:349) [wiring] -> **dependency-injection**
- **0.97** `verify_signature` (harness/jev_completion.py:863) [wiring] -> **dependency-injection**
- **0.96** `reverse_str` (bench/tasks/reverse/reverse.py:1) [wiring] -> **return-value**
- **0.95** `resolve_jev_key` (harness/config.py:535) [security] -> **secrets-safe**

## Strong signal (0.80–0.95, production-path, non-test) — lane review required

- 0.94 `run_node` (harness/executor.py:518) [security] -> validate-guard
- 0.93 `name` (harness/providers.py:24) [wiring] -> return-value
- 0.93 `_rankings_reports` (harness/server.py:110) [wiring] -> dependency-injection
- 0.93 `read` (harness/waist.py:2129) [errors] -> propagate-exception
- 0.90 `_refuse` (harness/mcp.py:506) [security] -> fail-closed
- 0.90 `_http_get` (harness/web.py:78) [security] -> validate-guard
- 0.89 `note` (harness/repo_items.py:260) [wiring] -> dependency-injection
- 0.89 `run_dogfood_task` (harness/server.py:394) [security] -> validate-guard
- 0.88 `install_event_sink` (harness/server.py:806) [errors] -> propagate-exception
- 0.88 `build` (harness/server.py:1448) [errors] -> propagate-exception
- 0.87 `scan_repo` (harness/repo_cards.py:239) [errors] -> propagate-exception
- 0.86 `do_GET` (driver_core/server.py:314) [security] -> validate-guard
- 0.85 `_warn` (harness/local_fit/dispatch.py:135) [errors] -> suppress-documented
- 0.84 `_envelope` (driver_core/adapters.py:228) [errors] -> propagate-exception
- 0.83 `sm_no_dead_code` (audits/self/audit.py:891) [errors] -> propagate-exception
- 0.83 `_discard` (driver_core/osal.py:529) [errors] -> suppress-documented
- 0.83 `defer_step` (harness/provision.py:1357) [errors] -> propagate-exception
- 0.83 `uninstall_event_sink` (harness/server.py:811) [errors] -> propagate-exception
- 0.82 `demotion` (harness/capability.py:670) [wiring] -> dependency-injection
- 0.82 `maybe_order_pool` (harness/local_fit/dispatch.py:144) [wiring] -> dependency-injection
- 0.81 `axis_verification` (harness/jev_packs.py:1927) [wiring] -> dependency-injection
- 0.81 `gather_web_context` (harness/web.py:346) [security] -> validate-guard
- 0.80 `_recipe_venv_pip` (harness/provision.py:1966) [security] -> validate-guard
- 0.80 `write_card` (harness/repo_cards.py:304) [wiring] -> propagate-exception

(29 total at >=0.80 excluding test code; top 40 shown.)

## Batch by pattern x facet (non-no-change, production-path)

### wiring
- dependency-injection: 100 functions (avg conf 0.45)
- handle-local-fallback: 64 functions (avg conf 0.39)
- guard-clauses: 49 functions (avg conf 0.35)
- exception-vs-sentinel: 40 functions (avg conf 0.29)
- propagate-exception: 15 functions (avg conf 0.44)
- return-value: 10 functions (avg conf 0.5)
- split-function: 9 functions (avg conf 0.44)
- context-manager: 3 functions (avg conf 0.36)

### errors
- propagate-exception: 206 functions (avg conf 0.48)
- exception-vs-sentinel: 122 functions (avg conf 0.4)
- handle-local-fallback: 111 functions (avg conf 0.39)
- log-and-continue: 37 functions (avg conf 0.45)
- suppress-documented: 37 functions (avg conf 0.5)
- result-object: 23 functions (avg conf 0.37)
- fail-closed: 14 functions (avg conf 0.47)
- domain-exception: 1 functions (avg conf 0.45)

### security
- validate-guard: 39 functions (avg conf 0.58)
- secrets-safe: 17 functions (avg conf 0.46)
- fail-closed: 7 functions (avg conf 0.62)
- authz-check: 3 functions (avg conf 0.46)
- redact-logging: 1 functions (avg conf 0.78)


## Veto-context records

- 4510 veto/escalation verdicts (confidence < 0.95) each carry a structured veto record: function id + file:line, full facet score vector, top driving facets computed in code, pinpointed call site, and a template-assembled plain-language why (never Jev prose).
- Vetoes with empty why: 0 (all passed validation; none queued for re-judgment).

## Aspect coverage (targeted batteries, iteration 1)

- 10 repo aspects derived from the actual file layout: spend-economics, jev-policy, cli-contracts, state-persistence, server-transport, consent, provider-adapters, driver-orchestration, dag-workflow, apply-gate.
- Coverage audit found 0% aspect-specific coverage on iteration 0; one targeted-battery iteration brought 979/979 production-path functions in named aspects to 100% (2,245 aspect judgments, all noul — no prose).

## Battery headlines

- Security: input not validated: 88
- Security: secrets risk: 2
- Security: fail-open risk: 30
- Testplan: hard-to-test: 1
- Testplan: critical priority: 0
- Concurrency: bug likely+: 1
- Context: critical need: 272
- Deepened: still critical after expansion: 21

