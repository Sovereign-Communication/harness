# Hermetic / audit triage runbook

Use this guide when a fresh checkout or local audit reports a red gate. It
extends the #276 measurement dispositions and #278 tool-resolution contract
for #290. Capture the command, exit code, and first useful error before
changing anything.

The audit driver can use a configured Jev key for its final judgment. Run the
audit probes below from an unkeyed profile with an empty config home; do not
use a live-key environment for triage.

**Authority:** CI is authoritative. A failure in the CI `audit` job or
hermetic test matrix is a defect until reproduced and explained. A local-only
failure may depend on the machine, but a green CI result or an environment
label alone does not explain it.

## Triage order

1. Identify the failing gate and whether its process actually started.
2. Run the matching probe below and compare its evidence with both outcomes.
3. Correct the named environment problem and repeat the same probe. If the
   failure remains in a hermetic run, fix the code or test that owns it.

## Bare `python` command does not start

**Signature:** the shell reports `python: command not found` (or on Windows,
`'python' is not recognized`), before Harness prints gate output. A Harness
gate preflight can instead report `no Python interpreter found: sys.executable
is unusable and python, python3, py are not on PATH`.

**Distinguishing check:** try the interpreter launchers available on the
machine, then list or run the documented gate with the working one:

```text
python --version
python3 --version
py -3 --version
python3 -m harness.cli gates
python3 -m harness.cli gates --run unittest
```

On Windows, substitute `py -3` for `python3` when that is the working
launcher. Gate commands containing bare `python` are resolved by
[`harness.tools`](../harness/tools.py): `HARNESS_PYTHON`, the running
`sys.executable`, then `python`, `python3`, and `py`. The runner therefore
does not require a shell `python` alias when it already has a usable
interpreter.

- **Environment-broken:** the bare shell probe cannot start, but `python3` or
  `py -3` prints a Python version and can launch the gate list. Use that
  launcher for the outer CLI, or set `HARNESS_PYTHON` to the installed
  interpreter. If no launcher works, install Python or fix the explicit
  `HARNESS_PYTHON` override. A failed command that never started is not a
  failing test.
- **Code-broken:** an interpreter starts Harness, and the gate reaches a test
  that prints a named `FAIL`, `ERROR`, or traceback. The gate ran; investigate
  that assertion or traceback instead of changing PATH.

## S5 — Ruff resolution

**Probe:** `python audits/self/audit.py --dim SM` (use the working interpreter
launcher above). S5 runs `ruff check harness tests` through the shared resolver.

**Signatures:** `No module named ruff`, `ruff: command not found`, or the
named S5 failure below. The legacy module-not-found text is a tool-resolution
symptom; it is not itself a lint finding.

- **Environment-broken:** the S5 evidence begins `S5 ruff unresolved:` and
  names the missing Ruff tool or an invalid `HARNESS_RUFF` override. Install
  the development extra with `python -m pip install -e '.[dev]'`, put
  Ruff on `PATH`, or correct/unset `HARNESS_RUFF`.
- **Code-broken:** Ruff resolves and S5 evidence includes lint output such as
  a file, line, rule, or Ruff exit status. Fix the reported lint finding.
  S5 is enforced; do not skip it or call a resolved lint finding infrastructure.

## R13 — hermetic test suite and environment leaks

**Probe:** `python audits/self/audit.py --dim R`. R13 runs the unit suite with
`ResourceWarning` treated as an error, removes routing and provider-key
variables from the child environment, and gives it a fresh empty home.

- **Environment-broken local reproduction:** a normal local test run is red,
  but R13 reports `unittest: Ran … tests … OK (env clean; empty home)`. The
  difference points to ambient configuration or files in the normal run.
  Reproduce with the isolated setup before deciding; do not copy credentials
  into logs or the runbook.
- **Code-broken hermetic run:** R13 reports a failing test or a
  `ResourceWarning` / `unclosed file` signature. The leak signature fails R13
  even when the unittest summary says `OK`. Treat this as a real test or
  resource-lifecycle defect; CI's hermetic result is authoritative.

The R13 child intentionally scrubs known routing/key variables and overrides
`HOME`, `USERPROFILE`, `APPDATA`, and `XDG_CONFIG_HOME` with an empty temporary
home. If a provider key or routing override still reaches that child, inspect
the hermetic environment builder in [`audit.py`](../audits/self/audit.py):
that is an isolation defect, not a reason to dismiss the red run as ordinary
machine state.

## D12 — stale coverage baseline and changed-line measurement

**Probe:** `python audits/self/audit.py --dim SD`. Locally D12 resolves the
runtime merge-base from `origin/main`; CI supplies `HARNESS_D12_BASE` from the
event base. For a reproducible local run, set it to
`git merge-base HEAD origin/main` before invoking the probe.

A committed coverage snapshot is only a fast path when its commit equals the
runtime merge-base. **A stale snapshot is expected to trigger fresh traced
coverage for the current tree; it is not itself a failure and must not be
manually re-pointed.** A documentation-only change reports that there are no
executable `harness/` changes to measure and skips the traced battery.

- **Environment-broken measurement:** the checkout cannot resolve or diff the
  base, or current-tree coverage cannot be obtained. Locally the audit prints
  a visible `SKIP (fail-open): D12 cannot …`; CI prints `[FATAL] D12: …` and
  fails closed. Fetch `origin/main`, remove a bad `HARNESS_D12_BASE` override,
  or repair the unavailable coverage setup, then rerun. The local skip is not
  evidence that coverage passed.
- **Code-broken coverage:** D12 resolves a base and reports a measured ratio,
  for example `changed-line coverage 18/20 = 90% (bar 95%)`, with untested
  paths or line numbers. Add tests that execute those changed lines or remove
  unreachable code. Do not lower the 95% bar.

For the full measurement contract and its CI/local behavior, see
[#276](https://github.com/Sovereign-Communication/harness/issues/276) and the
[coverage-baseline notes in CONTRIBUTING](../CONTRIBUTING.md#coverage-baseline).

## Worked example — explicit `jev_disabled`

The #290 report used `jev_disabled/config.py:1031` as a historical symptom
anchor. The line number can move; the behavior belongs to `load_settings()`
(the #347 settings fix), and JevPolicy/JevEvaluator already honor a correctly
disabled `Settings` object. Do not diagnose this symptom as an evaluator
bypass.

**Safe distinguishing check:** run the focused, hermetic regression:

```text
python -m unittest tests.test_config_jev_overrides -v
```

For a new reproduction fixture, set the effective settings before dispatch
(e.g. `jev_disabled=True`, `jev_api_key=None`), isolate the config home and
environment, assert the resulting settings are disabled with no key, and make
`HttpTransport.post` fail if called. The check must not reach a live provider.

- **Environment/fixture-broken:** the fixture did not establish those effective
  settings or retained ambient config, so the preview tries the provider path.
  Fix the fixture and repeat with network prohibited. The historical wrapper
  reported `provider_usage_unknown` with CLI exit 1 while its containing
  process exited 0; that is an unknown-usage error, not a successful provider
  response or proof of a charge. The reported transport was blocked. For any
  run that could reach a provider, check its authoritative usage receipt
  before claiming either billed usage or zero usage.
- **Code-broken:** the isolated fixture asserts disabled settings and no key,
  but `load_settings()` returns an enabled/keyed object or the mocked transport
  is called. The focused test should fail with that assertion or mock-call
  failure. Fix the settings owner or dispatch path; do not weaken hermeticity.

## Local-only `test_dogfood` / `test_hourglass_end_to_end`

**Known signature:** `tests.test_dogfood` or
`tests.test_hourglass_end_to_end` fails on a developer box while the CI
hermetic suite is green.

**Distinguishing check:** run the focused tests under an empty home and without
provider keys:

```text
python -m unittest tests.test_dogfood tests.test_hourglass_end_to_end -v
```

If the failure disappears under that setup and R13 is green, the local run was
environment-sensitive. If the failure follows the clean setup or appears in
CI, fix the owning test or code.

## Real-defect signs

- The same assertion fails in the hermetic CI matrix.
- S5 fails while Ruff resolves and reports lint findings.
- D12 measures current-tree changed-line coverage below 95%.
- R13 fails in its scrubbed environment or sees a resource warning.
- A focused local failure survives the empty-home, no-key reproduction.
