# Harness #438 draft — headless key provisioning for driver_core

**Deliverable:** [harness-438.patch](sandbox://workspace/lane-sweep-2026-10-31/harness-438.patch) — a first draft for the lane to adopt, built in the sandbox checkout (`~/workspace/harness`, HEAD `ffeb4d2aa05f3153d052aa64ceb141fd7c7fd255`, local branch `sandbox/438-draft`). Nothing pushed anywhere. The lane branch (`lane/burndown`, head `83bc0103`) was not touched; the patch is against the sandbox HEAD and will need a trivial rebase/apply check against the lane head when adopted.

## What already existed vs. what was missing

`driver_core` already had the mechanism's core: `load_settings()` reads `DRIVER_JEV_API_KEY` from the environment (the package reads only `DRIVER_*` variables, enforced by `assert_no_foreign_reads`), `Settings.keyed` reports state, and `JevClient.decide()` / the vision extractor refuse cleanly when unkeyed. The dogfood script (`scripts/run_driver_dogfood.py`) already bridges the Harness config-file key (`harness.config.resolve_jev_key()`) into the driver process as that env var.

What #438 actually lacked, verified in the code:

1. **Discoverability.** Nothing in the driver docs (`docs/driver.md` Configuration table) mentioned `DRIVER_JEV_API_KEY` at all, and no surface stated which commands are intentionally keyless vs. keyed.
2. **Health named no remedy.** `Service.health()` and `driver-core health` reported `keyed: false` and stopped there — a headless operator holding a valid key had no supported way to learn what to set.
3. **A silent failure shape.** A provisioning script that declares `DRIVER_JEV_API_KEY` blank was silently read as "keyless" (unlike `DRIVER_TOKEN`, where declared-but-blank is a named `ConfigError` — the precedent pinned by `test_a_blank_token_is_reported_not_traced`).

## What the draft implements

| File | Change |
|---|---|
| `driver_core/config.py` | `JEV_API_KEY_VARIABLE` constant (one spelling for loader, error, and health text). `Settings.provisioning_step()` — returns `None` when keyed, else the remediation sentence naming the variable and what it unlocks (Jev decisions + screen/vision extraction; health/vocabulary/schema/verify keyless). `load_settings()` now applies the token's presence rule to the key: declared-but-blank raises `ConfigError` naming the variable; unset stays keyless. |
| `driver_core/server.py` | `health()` gains a `key_provisioning` field (the `provisioning_step()` value; `null` when keyed). Additive; names a variable, never a value. |
| `driver_core/cli.py` | `driver-core health` text output prints `key : missing -- <step>` when unkeyed, beside the existing `keyed=False` line. Keyed output is byte-identical to before. |
| `docs/driver.md` | `DRIVER_JEV_API_KEY` row in the Configuration table, plus a "Provisioning the Jev key (headless installs)" section: keyless-vs-keyed surface table, the env-var provisioning command, the blank-is-an-error rule, the constructor-argument alternative, and the config-file bridge note (export the `resolve_jev_key()` value; driver-core never opens Harness config files). |
| `tests/test_driver_core/test_headless_provisioning.py` | **New, 13 tests** (below). |
| `tests/test_driver_core/test_auth.py` | One contract-pin updated: the health-shape assertion now includes `key_provisioning` (the test's intent — "a declared token does not move the wire" — is preserved; the wire itself intentionally gained the field). |

## LoC

327 insertions, 5 deletions across 6 files (`git diff HEAD --stat`): production code 52 lines changed (`config.py` 40, `server.py` 7, `cli.py` 5), docs 38, tests 242 (232 new + 10 in the auth pin).

## Test evidence

- **New tests: 13/13 pass.** They prove, in repo style (unittest, hermetic, dummy keys only):
  - unkeyed health (JSON via `Service.handle` and CLI text via `cmd_health`) names `DRIVER_JEV_API_KEY`;
  - keyed via env var (dummy key): `keyed` true, no remedy anywhere, key value absent from the serialized health body and from CLI text;
  - **end-to-end acceptance shape:** env-provisioned settings + the real `JevClient` over the package's fake transport → one `step` executes (`observe`), one transport call is made, and `verify` reports the step's spend ($0.000042 for 1,000 input tokens at the repo's stated price). The unkeyed contrast: same step refuses `decision_not_usable`, transport never called, spend $0;
  - no behavior change for pre-existing key channels: caller-supplied `jev_api_key=` is keyed with no remedy, an explicit argument still beats the environment, a directly built `Settings` is unchanged (the config-file channel reaches the driver through the env export, pinned by the env tests);
  - declared-but-blank key → `ConfigError` naming the variable; unset key → keyless, no error.
- **Touched-area suite:** `tests/test_driver_core` — 409 tests (396 baseline + 13 new), failures = the same 5 pre-existing environmental failures as the pristine tree (see reconciliation). The one test my change initially broke (the health-shape pin in `test_auth.py`) is updated in the patch, as described above.
- **Full suite on the branch:** 5,176 tests, failures = the same 5 environmental ones, nothing else.
- **Live CLI spot-check (no key):** `python -m driver_core health` prints the new `key : missing -- set DRIVER_JEV_API_KEY …` line; `--json health` carries `key_provisioning`; with a dummy key exported, health reports `keyed=True` and no remedy line.
- Not run: ruff (not installed in this sandbox) — the lane's lint gate should run on adoption. No live-key step was run; the end-to-end evidence is hermetic by design, and the live dogfood path is unchanged.

## Local-red vs CI-green reconciliation (the lane's open v3 question)

**Finding: the local red is environmental, not code — at this sandbox HEAD, local and CI are testing the same code and getting different results for one reason: the bare `python` executable.**

On the pristine sandbox checkout (`ffeb4d2`), `tests/test_driver_core` shows exactly 5 failures, and the full suite shows the same 5 and no others:

- `test_declarations.OsBoundaryTests.test_a_command_that_actually_runs_returns_its_output`
- `test_declarations.OsBoundaryTests.test_a_non_zero_exit_is_an_answer_not_a_failure`
- `test_tier_chain.OsalInputTests.test_a_child_really_receives_stdin`
- `test_tier_chain.OsalInputTests.test_a_child_that_ignores_stdin_still_works`
- `test_wiring.LaunchDiagnosticTests.test_a_timeout_is_not_dressed_up_as_a_missing_binary`

All five spawn a child process via `osal.run(["python", "-c", ...])`. This sandbox has only `python3` (`which python` → nothing; `which python3` → `/usr/bin/python3`), so the child never starts and the assertions fail (e.g. the timeout test gets `"command did not run: not found: python"` instead of `""`). **Proof:** with a `python` → `python3` shim on `PATH`, all 20 tests in the three affected classes pass. On CI runners and the operator's Windows machines `python` resolves, hence green there. If the lane wants the suite robust on python3-only systems, the fix is in the tests (use `sys.executable`, as other tests in this repo already do) — out of scope for this draft, flagged here as the reconciliation answer.

One methodological note, disclosed for honesty: my first full-suite baseline run was started in the background *before* I began editing, and it imported modules while edits were landing, so its log shows 2 extra failures (`CliTests.test_health_exits_zero`, `test_health_reports_whether_writes_are_allowed`) whose tracebacks show a torn mid-edit state (`AttributeError: 'Settings' object has no attribute 'provisioning_step'` — new CLI, old config, a combination that never existed as a tree). Those two are artifacts of that run, not of either tree: both pass on the pristine subset run and on the finished branch (subset and full). All numbers quoted above come from uninterrupted runs.

Caveat on scope: the sandbox checkout is at `ffeb4d2`, behind the lane head `83bc0103`. This reconciliation settles the question for the sandbox tree; if the lane's local-red was observed on a different tree, the same `python`-on-PATH check is the first thing to re-run there.

## Adoption notes for the lane

1. **One judgment call to review:** the declared-but-blank `ConfigError` is the only behavior change for existing installs. It mirrors the `DRIVER_TOKEN` precedent and its rationale (a provisioning typo should not masquerade as a product fault), but an operator who exports an empty `DRIVER_JEV_API_KEY` in a wrapper script will now get a startup error instead of a keyless start. If the lane prefers keyless-fallback, drop the `load_settings` hunk and keep the rest — the health/docs/tests around it are independent except `BlankKeyTests`.
2. **Wire change is additive only:** `key_provisioning` appears in health for keyed and unkeyed installs alike (stable schema). The Harness adapter passes health through untouched; no adapter or UI change is needed.
3. **Docs placement:** the entry went in `docs/driver.md` (the driver doc), not README — README's Jev row describes the *host* config precedence and was left alone. CHANGELOG untouched; add an entry per lane convention when the PR is cut.
4. **Untouched, as instructed:** #306 (operator-gated) and #294 — no files related to either; no pushes, no real keys anywhere (dummy keys only, in tests and one CLI spot-check).
5. The work sits on local branch `sandbox/438-draft` in `~/workspace/harness` with the new test file in intent-to-add state; the exported patch was verified to apply/reverse cleanly (`git apply --check --reverse`).
