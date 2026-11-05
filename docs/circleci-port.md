# CircleCI port — the check mapping of record (TRACK B)

Status: **mapping recorded; `.circleci/config.yml` not written yet.** This file
is the port's contract, not a claim that a port exists. The verification rail is
dark today: hosted Actions answers every job with zero steps and the annotation
"The job was not started because your account is locked due to a billing issue"
(run `42001069443` on lane head `0f9716e`, read 2026-11-05). The local battery is
the interim rail until this port activates.

## What the port must preserve

The merge bar read from the API (`GET /branches/main/protection`, 2026-11-05):
11 required contexts, `strict: true`, `enforce_admins: true`. A port that
changes these names is a **branch-protection change** and stays owner-reserved;
the port is not allowed to relax it as part of moving CI vendors.

| # | Required context (must keep this exact string until protection moves) | GitHub job id | Planned CircleCI job | Gate commands it must run |
|---|---|---|---|---|
| 1 | `Checkout hygiene` | `checkout-hygiene` | `checkout-hygiene` | clean-tree check; `python scripts/check_driver_core_drift.py --remote` |
| 2 | `audit` | `audit` | `audit` | `python -m ruff check audits`; `python audits/self/audit.py` |
| 3 | `hermetic suite (macos-latest, py3.11)` | `test` | `hermetic-macos-311` | `ruff check harness tests examples/oc_handoff scripts`; `compileall`; `python -m harness.repo_cards --check`; `python -W error::ResourceWarning -m unittest discover -s tests -v` |
| 4 | `hermetic suite (ubuntu-22.04, py3.9)` | `test` | `hermetic-ubuntu2204-39` | same gate set |
| 5 | `hermetic suite (ubuntu-latest, py3.11)` | `test` | `hermetic-ubuntu-311` | same gate set |
| 6 | `hermetic suite (ubuntu-latest, py3.13)` | `test` | `hermetic-ubuntu-313` | same gate set |
| 7 | `hermetic suite (windows-latest, py3.11)` | `test` | `hermetic-windows-311` | same gate set |
| 8 | `installed package (macos-latest)` | `package-installed` | `package-installed-macos` | wheel install into a clean venv; CLI smoke |
| 9 | `installed package (ubuntu-latest)` | `package-installed` | `package-installed-ubuntu` | wheel install; `harness serve` + MCP handshake smoke |
| 10 | `installed package (windows-latest)` | `package-installed` | `package-installed-windows` | wheel install into a clean venv; CLI smoke |
| 11 | `package` | `package` | `package` | build sdist/wheel; `python -m twine check dist/*` |

### Lane-only jobs that are NOT in the required set

These exist in `ci.yml` and must be ported for parity, but they do not carry a
required context today. Adding one to protection is a separate owner decision:

| Job | GitHub job id | Planned CircleCI job |
|---|---|---|
| `Change scope` | `changes` | `change-scope` |
| `changed-file typecheck (pyright)` | `typecheck-changed` | `typecheck-changed` |
| `Driving hygiene (deterministic)` | `driving-hygiene` | `driving-hygiene` |
| `UI Playwright smoke (single worker)` | `ui-playwright-smoke` | `ui-playwright-smoke` |
| `handoff scope (advisory)` | `handoff-scope` | `handoff-scope` |
| `hermetic suite (ubuntu-latest, py3.14)` | `test` | `hermetic-ubuntu-314` |

The py3.14 leg is new on the lane (#443) and is deliberately not required yet.

## Activation preconditions (none of them are satisfied today)

1. The owner connects CircleCI VCS to `Treystu/harness` (the org is gone; this is
   a personal repository).
2. The `jev-gates` context exists and carries `TYPESAFE_API_KEY`, before any
   ported gate job runs.
3. The Linux legs run on the machine runner on the Oracle box; macOS and Windows
   hermetic legs use CircleCI hosted classes, and the hermetic matrix is planned
   around macOS concurrency 1 (so the macOS leg must not be split into parallel
   jobs it cannot run).
4. The ported pipeline is green on `main` **before** protection is switched to
   the CircleCI contexts; the Actions workflows retire only after that flip.

Until all four hold, `.circleci/config.yml` would be inert, so it is not worth
writing ahead of them: an unvalidated pipeline is not a rail. What can be
verified now is this mapping, which `tests/test_circleci_port_mapping.py` pins
against `ci.yml` so a rename cannot silently stale it.

## Reproducing the reads in this file

- Required contexts and mode: `gh api repos/Treystu/harness/branches/main/protection`
- Dark rail: `gh api repos/Treystu/harness/actions/runs/42001069443/jobs` (every
  job `steps=0`) and the check-run annotation for one of them
- Job ids, names, matrix legs and gate commands: `.github/workflows/ci.yml`
