# Harness #439 draft + #433 assessment — 2026-10-31

Scope: sandbox copy `~/workspace/harness-439`, branch `sandbox/439-draft`. No pushes anywhere. The copy is not the lane's canonical checkout; the lane adopts by applying the patch or cherry-picking the branch commits.

## State found in the copy (read first — it shapes the patch)

The copy arrived at HEAD `ffeb4d2` with **uncommitted #438 draft work in the tree** (docs/driver.md, driver_core/cli.py, config.py, server.py, tests/test_driver_core/test_auth.py, plus untracked tests/test_driver_core/test_headless_provisioning.py). That state is not part of #439. It was committed first, untouched, as base commit `6499b05` ("carried-in base"), and the #439 patch below is the diff from that base — so the patch contains #439 changes only. Untracked runtime artifacts (harness/data/*, harness/ui/panes/) were left untracked.

## #439 — run_probe read-only but refused without write authority

### What was wrong

`run_probe` is declared `READ_ONLY` in `driver_core/actions.py`, but no executor was registered for it in any build, and the registry's write-policy note (`WRITE_DISABLED_NOTE`) was appended to *every* "not registered" refusal — so with `allow_write=false` the vocabulary listed a read-only action as refused for a write-gate reason, and a verifier (writes off, unkeyed — the correct posture) could not run deterministic probes at all.

### The fix (commits ce12f95, dba42f0, 9694436 on sandbox/439-draft)

- **Declaration** (`driver_core/config.py`, +63): new `Settings.probes` — `(name, command)` pairs declared by the operator, parsed from `DRIVER_PROBES` (`name=command` entries separated by `;`). Malformed entries, unusable names, empty commands, and duplicate names raise `ConfigError` at settings load rather than being skipped. Parallel in shape to `cli_command`.
- **Wiring** (`driver_core/wiring.py`, +17): `configured_probes(settings)` splits each declared command with the existing `parse_argv` (whitespace tokenisation, quotes group, no expansion — the same rule as `DRIVER_CLI_COMMAND`), returning `{name: argv tuple}`. An unparseable declaration raises.
- **Driver** (`driver_core/driver.py`, +3/−1): the default registry build now receives `probes=configured_probes(self.settings)`.
- **Executor registry** (`driver_core/executor_registry.py`, +98): 
  - `_run_probe` handler: the `command` parameter only *names* a declaration; the argv that runs is the declared argv, unchanged, through `osal.run` (argv list, `shell=False`) — there is no composition surface for a caller to exploit. An undeclared name is refused, naming the declared set. A **non-zero exit is returned as an answer** (`exit_code`, `stdout`, `stderr` in the output); a probe that could not be asked (missing binary, timeout — `ProcessResult.reason` set) is refused, because it carries no real exit code.
  - `_freeze_probes` validates declarations at build time; an argv given as a single string is refused outright (a string argv is the shell path `osal.run` exists to prevent).
  - `build_read_only_registry(..., probes=None)` / `build_driver_registry(..., probes=None)`: `run_probe` is registered whenever a non-empty declaration exists, **independent of `allow_write`**. With no declaration there is no probe executor, and the refusal says so.
  - `executability()`: the write-policy note is now attached only when the missing executor is a write executor (`WRITE_EXECUTORS`). Read-only refusals (undeclared probe, `call_read_tool`, `read_dom`) no longer name `DRIVER_ALLOW_WRITE` as their reason; write refusals keep the note verbatim.
- **Executor** (`driver_core/executor.py`, +15/−…): the same scoping applied to the unregistered-executor `ExecutorError` raised by `Executor.execute`, so the execution-time refusal agrees with the vocabulary output.
- **Docs** (`docs/driver.md`, +24/−…): executor inventory corrected (run_probe no longer listed as executor-less), `DRIVER_PROBES` row in the Configuration table, and a "Declaring read-only probes" subsection stating the operator's obligation (declaring asserts the command is read-only; the driver cannot verify that).
- **Audit chain**: no change needed — `Executor.execute` already appends a `KIND_ACTION` record for every execution; probe executions land in the chain identically, and the result's output carries stdout/exit/stderr. Pinned by test.

Results land in the audit chain like other actions (acceptance 2), write-class actions remain refused under `allow_write=false` (acceptance 3, first half), and the vocabulary output no longer lists a read-only action as refused for a write-gate reason (acceptance 3, second half). Acceptance 1 and 4 are covered by the handler + regression tests below.

### Tests (tests/test_driver_core/test_run_probe.py, 271 lines, 17 tests)

- Probe executes with writes off through a full `Driver` built from unkeyed settings (`keyed == False` asserted) with probes declared via `DRIVER_PROBES`; recorded argv is exactly the declared argv; one `action` record for `run_probe` with `ok=True` in the audit chain.
- Undeclared command shapes refused and nothing runs: `"rm -rf /"`, `"health_check; rm -rf /"`, a near-miss name, and a declared name with appended flags. The fake `osal.run` records zero calls.
- Non-zero exit (3) returns as a successful execution with `exit_code`/`stderr` in the output; a `not_found` probe is refused with the reason named; dry-run executes nothing; a string-argv declaration raises at build time.
- One real end-to-end probe: declared argv `(sys.executable, "-c", ...)` prints and exits 3 — stdout and exit code pass through. Uses `sys.executable`, never a bare `python` (see pre-existing failures below).
- Write gate unchanged: `write_file`/`delete_file` still raise the `DRIVER_ALLOW_WRITE` refusal with writes off and probes declared; with writes on, all `WRITE_EXECUTORS` plus `run_probe` are registered and a consented `write_file` really writes.
- Vocabulary surface (`Service.handle("vocabulary")`): declared probe is `executable` with `needs == []` under writes off; `run_probe`/`call_read_tool`/`read_dom` refusals contain no `DRIVER_ALLOW_WRITE`; `click`'s refusal still does. Same scoping asserted directly on `executability()`.
- Config: env parsing, malformed-entry `ConfigError`, unusable declarations `ConfigError` at `Settings` build, and `configured_probes` quote handling (`"C:\Program Files\tool.exe" --check` splits into two argv entries).

### Test evidence

- New suite: **17/17 pass** (`python3 -m unittest tests.test_driver_core.test_run_probe`).
- Touched-area suite (`tests/test_driver_core/`, unittest discover): **426 tests, 5 failures — the identical 5 that fail on the pre-change baseline** (409 tests, same 5). No regressions; the 17-test delta is the new file.
- The 5 pre-existing failures, named precisely — all environmental, all present before any #439 change, none caused by it: they spawn the bare **`python`** executable, which does not exist on this sandbox's PATH (only `python3`):
  - `test_declarations.OsBoundaryTests.test_a_command_that_actually_runs_returns_its_output`
  - `test_declarations.OsBoundaryTests.test_a_non_zero_exit_is_an_answer_not_a_failure`
  - `test_tier_chain.OsalInputTests.test_a_child_really_receives_stdin`
  - `test_tier_chain.OsalInputTests.test_a_child_that_ignores_stdin_still_works`
  - `test_wiring.LaunchDiagnosticTests.test_a_timeout_is_not_dressed_up_as_a_missing_binary` (failure text: `command did not run: not found: python`)
- No consumers of the changed builders exist outside `driver_core` + tests (grep across harness/, bench/, examples/, scripts/: one unrelated string mention of the file path in harness/jev_completion.py). `compileall` on driver_core is clean.

### LoC

Patch: 7 files, **+483 / −8** (662-line diff). Of that: tests 271, registry 98, config 63, docs 24, wiring 17, executor 15, driver 3. Production code + docs ≈ +212/−8.

### Patch

`~/workspace/lane-sweep-2026-10-31/harness-439.patch` = `git diff 6499b05..HEAD` (the three #439 commits only; the carried-in #438 base commit is the diff's parent and is excluded). Adoption: apply the patch on the lane branch, or cherry-pick ce12f95, dba42f0, 9694436 after the base state. One sequencing note: the patch touches `driver_core/config.py` and `docs/driver.md`, which the #438 draft also touches — apply #438 first (or expect small context overlap in those two files) so the two drafts do not collide.

## #433 — Lucas Gate v2 runner unlanded (assessment only; no code attempted)

### What exists where (checked fresh 2026-10-31)

- **Main**: `harness/lucas_gate.py` returns **404** via the GitHub API today — the runner is not on main. No Lucas Gate files exist in either sandbox checkout (`~/workspace/harness`, `~/workspace/harness-439`), committed or uncommitted.
- **The validator build**: issue #433 records that a deterministic typed-track validator (`lucas_gate.py`, `mcp.py`, `mcp_schemas.py`, `test_lucas_gate_v2.py`; 6 focused tests passing) was built in a prior lane session but left **uncommitted with no PR**. It is **not present in any checkout reachable from this sandbox** — it lives, if anywhere, in that laptop lane session's working tree. From the evidence available here its current location is unconfirmed; locating it is a lane-side inventory step, not something this sandbox can settle.
- **The plan**: PR #397 is **closed, not merged** (verified today); its branch `plans/worldwide-push-2026-10-25` still exists and is the only home of the v2 implementation plan.
- **The profile**: the v2 profile is the owner's private material. A source exists outside the repo in the owner's oversight environment (its contents were not read for this assessment). It is **absent from the lane and from its documented config location**, and per the issue its loading must stay private: never committed, never pasted into GitHub text, never printed. **Provisioning it into the config location is the owner's act** — no lane or agent step can substitute, and no approximation of the profile was created or attempted here.
- **MCP catalog**: issue-reported (2026-10-28) that the connected catalog lists 30 tools without `lucas_gate_v2` while a fresh stdio launch lists 31 including it. Not re-verifiable from this sandbox; treat as reported, pending a lane-side catalog check.

### What blocks landing

1. **The profile** (owner-gated, hard blocker for the acceptance's core): until the owner provisions the real profile at the documented config location, the runner cannot perform a semantic pass against it, and the acceptance explicitly states a synthetic scorecard pass does not count. Zero gate-dependent GitHub writes can pass before this.
2. **The validator's location**: the only build of the runner is uncommitted and unlocated from here. If the laptop session tree still holds it, landing is a commit-and-PR step; if not, the runner is rebuilt from the plan on the plans branch — a materially larger step, and the fork between those two is the first thing to settle.
3. **Catalog gap**: even with code on main, the connected MCP catalog must expose `lucas_gate_v2`; the stdio-vs-connected discrepancy needs a catalog refresh/registration step on the serving side.

### Efficient landing sequence (once the profile is provisioned)

1. Locate the uncommitted validator in the laptop lane tree (or record its absence and rebuild from the plans branch — decide this first; everything else is downstream).
2. Commit the validator + MCP tool + schemas + its 6 tests on the lane branch; open the PR; land on main with the focused tests green (repo rule: local gates + CI audit).
3. Document profile loading in the same PR (location, privacy constraints) — documentation only, never the profile itself.
4. Owner provisions the profile at the documented config location (owner's act, outside the PR).
5. Refresh the connected MCP catalog so `lucas_gate_v2` appears alongside the stdio listing; verify by listing tools on the connected surface.
6. Run one real semantic gate pass against the provisioned profile in the driving loop and record it — that, not a synthetic scorecard, is the acceptance evidence. Then close #433.

Until step 4 happens, steps 1–3 and 5 can still proceed — the code, docs, and catalog work does not depend on the profile; only the final verification (step 6) waits on it.
