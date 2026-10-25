# Hermetic / audit triage runbook

This is the disposition record for audit-measurement noise landed with
[#276](https://github.com/Sovereign-Communication/harness/issues/276). It is a
starting point for
[#290](https://github.com/Sovereign-Communication/harness/issues/290), which
extends it to effective-settings and usage questions.

**Authority:** CI is authoritative: a failure in the CI `audit` job or the
hermetic test matrix is a defect until proven otherwise. A local-only failure
can be environment sensitive, but it is never dismissed by label. Reproduce it
with a scrubbed environment before deciding.

## Environment failure or code defect?

Ask these in order:

1. Does the same test fail in the CI hermetic suite? If yes, it is a code or
   test defect, not an environment signature.
2. Does it fail with an empty home, no provider keys, and no ambient
   `HARNESS_*` overrides? If it disappears under that setup, it is
   environment-sensitive.
3. Is the failure a missing external tool, not a finding? Use the named
   remediation below; do not skip or weaken the check.

## S5 — ruff resolution

**Signature:** `No module named ruff`, `ruff: command not found`, or an S5
failure that names the interpreter instead of a lint finding.

**Disposition (2026-10-25):** S5 is an enforced check, not a skipped one. It
resolves ruff through the one owner in `harness.tools.ruff_argv`:
`HARNESS_RUFF` first, then `ruff` on `PATH`, then `<interpreter> -m ruff`. A
missing ruff now produces one named failure beginning `S5 ruff unresolved:`.
That is an environment/setup error with a direct fix:

```bash
python -m pip install -e '.[dev]'
# or point at an existing binary:
export HARNESS_RUFF=/path/to/ruff
```

Do not add a per-PR skip and do not label a genuinely reported lint finding as
infrastructure. If ruff is present and S5 fails, it is a real lint defect.

## D12 — merge-base changed-line coverage

D12 no longer trusts a stored baseline SHA. At audit time it resolves the
runtime merge-base (`HARNESS_D12_BASE` in CI, otherwise `origin/main`), then
measures added executable `harness/` lines against coverage produced for the
current tree. If the committed fast-path coverage document does not name that
merge-base, the traced battery regenerates a temporary document. A manual
baseline re-point is not a pre-merge step.

- **No base or no coverage:** CI fails closed. Locally the audit prints a
  visible `SKIP (fail-open)` so an interactive run is not silently green.
- **Below 95%:** a real coverage gap; execute the added line with a real test
  or delete the unreachable line. Never lower the bar or hand-edit coverage.
- **Reproduce locally (Bash):**

  ```bash
  HARNESS_D12_BASE="$(git merge-base HEAD origin/main)" \
    python audits/self/audit.py --dim SD
  ```

  The traced battery is the slow part (roughly two to five minutes). To reuse
  a prepared document, set `HARNESS_D12_BASELINE` to a JSON file whose
  `commit` is the same merge-base.

## Local-only `test_dogfood` / `test_hourglass_end_to_end`

**Known signature:** `tests.test_dogfood` or
`tests.test_hourglass_end_to_end` fails on a developer box while the CI
hermetic suite is green.

**Disposition:** environment-sensitive until reproduced cleanly. These lanes
touch provider/config/ledger seams that can inherit a developer's ambient
keys or files. Run:

```bash
python -m unittest tests.test_dogfood tests.test_hourglass_end_to_end -v
```

under an empty home and without provider keys. If the failure follows that
environment, record it here rather than weakening the test. If it also fails
in CI, treat it as a real defect and fix the cause.

## Real-defect signs

- The same assertion fails in the hermetic CI matrix.
- S5 fails while ruff is resolvable.
- D12 reports a real ratio below 95% on current-tree coverage.
- A local failure survives a clean-home, no-key reproduction.

For those, file the defect against the owning module; do not relabel it as
audit infrastructure.
