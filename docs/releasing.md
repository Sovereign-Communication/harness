# Releasing

1. Start from a clean checkout and review the complete diff.
2. Confirm no credentials, local paths, raw responses, temporary logs, or
   generated build directories are tracked.
3. Install development tooling:

   ```bash
   python -m pip install -e '.[dev]'
   ```

4. Run the complete validation suite:

   ```bash
   python -m ruff check harness tests audits
   python -m compileall -q harness tests
   python -W error::ResourceWarning -m unittest discover -s tests -v
   python audits/self/audit.py
   python -m build
   python -m twine check --strict dist/*
   ```

5. Smoke-test the installed artifacts outside the repository, including both
   console scripts and module entry points.
6. Run `harness capabilities --check-shipped` with a live key before publishing
   model-pool changes.
7. Perform live verification and gated apply tests in a disposable checkout.
8. Review `THREAT_MODEL.md`, `docs/security.md`, and the changelog for current
   behavior and residual risks.
9. Commit and tag only after CI passes on every supported Python version.
   Historical note: CI was silent during PR #8 (2026-09-15, suspected private-repo Actions minutes; that merge rested on the documented merge-basis pattern -- full local battery green on every available interpreter). CI fires and is enforced as of the PR #9 merge (2026-09-16, run 35136528041 green at 185d827), so this gate is again a real CI gate.

The project intentionally keeps runtime dependencies at zero. Build and lint
packages are development-only extras.

## Publishing to PyPI

Publishing is automated and credential-free. `.github/workflows/publish.yml`
builds the distribution on a `v*` tag and uploads it to PyPI with
[pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish)
using **OIDC trusted publishing**. There is no PyPI API token in this
repository's secrets, and there is never a need to add one: the workflow's
only elevated capability is `id-token: write`, and pypi.org exchanges the
run's OIDC identity for a short-lived upload token.

### Release procedure

1. Bump `version` in `pyproject.toml` and land that change on `main` through
   a reviewed PR. `harness.__version__` is derived from `pyproject.toml`, so
   this single edit is the whole version bump.
2. Wait for `ci` to be green on `main`. The `package` job builds the same
   artifacts this workflow will publish.
3. Tag the merge commit and push the tag:

   ```bash
   git tag v0.5.0          # must equal the pyproject version, with the v
   git push origin v0.5.0
   ```

   The tag is what triggers the publish. The workflow refuses to upload if
   the tag and the `pyproject.toml` version disagree, because PyPI does not
   allow a released version to be overwritten: a mismatch would permanently
   publish a version number that the distribution's own metadata contradicts.

4. Watch the `publish` run. It builds, runs `twine check --strict`, verifies
   three-way version parity (pyproject, the importable `harness.__version__`,
   and the tag), smoke-installs the built wheel in a clean virtualenv, and
   only then uploads.

### Rehearsing without publishing

`workflow_dispatch` runs the entire chain and stops before the upload. Use it
to verify the release path without consuming a version number, and to
rehearse a tag you have not pushed yet:

- **`dry_run` is `true` by default.** Build, metadata check, version parity,
  and the clean-venv wheel install all run; the upload step is skipped. A
  green dry run is evidence for the whole path, not just the build.
- Set **`tag`** to rehearse a specific version. The tag-parity check then
  runs against that value. Leave it empty to skip tag parity and verify only
  the artifacts.

### One-time owner step: register the publisher

Trusted publishing requires a publisher entry on the project at pypi.org,
and **that entry must be created by the account owner** — it cannot be done
from a workflow or from CI credentials. Until it exists, a real tag push
runs every step and then fails at upload with a 403 from pypi.org. Nothing
earlier in the workflow fails, so a 403 at the last step means exactly one
thing: the publisher is not registered.

Register under the `sovereign-harness` project at pypi.org, using these exact
values:

| Field | Value |
|---|---|
| PyPI project | `sovereign-harness` |
| Provider | GitHub Actions |
| Repository | `Sovereign-Communication/harness` |
| Workflow name | `publish.yml` |
| Environment | *(leave empty)* |

The workflow sets no GitHub `environment`, so the Environment field must stay
empty — a mismatch there is the most common cause of a 403 that looks
otherwise correct. After registering, run a dry run first, then push a real
tag.

## Readiness snapshot - 0.5.0 (2026-10-23)

Audited against the `0.5.0` tree. `https://pypi.org/pypi/sovereign-harness/json`
returns **404**: no version has ever been published, so the first upload claims
the name and nothing can be overwritten afterwards.

Verified locally on that tree (Windows, CPython 3.14, setuptools 84):

| Check | Result |
|---|---|
| `python -m build` | sdist 1.9 MB + wheel 982 KB, `py3-none-any`, version `0.5.0` |
| `python -m twine check --strict dist/*` | PASSED on both artifacts |
| Wheel metadata | Metadata-Version 2.4, `License-Expression: MIT`, `Requires-Python: >=3.9`, no runtime `Requires-Dist`, four extras intact, supported-version classifiers and Repository/Documentation/Changelog URLs present |
| Wheel package data | all five `harness/ui/*` files plus the three runtime `harness/data/*` files (`experience.md`, `refine_propose_prompt.md`, `task_families.json`) |
| sdist contents | 424 entries; operator-lane docs excluded; no `__pycache__`/`.pyc`; no operator-home command lines |
| README | no relative Markdown links; install note states the `sovereign-harness` name vs `harness` import conflict and frozen model tiers in a wheel |
| Clean-venv wheel install | `harness`, `harness-mcp`, `harness-desktop`, `driver-core` all `--help` cleanly; `pip check` clean; reports `0.5.0` |
| Installed-wheel server | `/`, `/app.js`, `/app.css`, `/panes.js`, `/panes.css`, `/api/status`, `/api/settings`, `/api/capabilities` all 200 |
| Clean-venv sdist install | builds, imports, reports `0.5.0`, runtime data present; `pip check` clean |
| Release state | version `0.5.0` in `pyproject.toml` and the fallback literal; `[Unreleased]` is `Nothing yet.`; dated `[0.5.0]` changelog section present |
| Lint / compile | `ruff check harness tests audits` clean; `compileall` clean |
| Full hermetic battery | 5,006 tests, `OK (skipped=72)`, under `-W error::ResourceWarning` |
| Self-audit | **BAR MET**: A/R/SM/SD all `10.0`; Jev 95% gate PASS; D12 changed-line coverage pass |
| Credential scan of both artifacts | no keys/tokens; only redaction patterns and `sk-...` test fixtures |

Only one blocker remains, and it is owner-only:

1. **Trusted publisher not registered.** Register under the
   `sovereign-harness` project at pypi.org with the exact values in the table
   above (workflow `publish.yml`, repository
   `Sovereign-Communication/harness`, Environment left empty). Until that
   entry exists, a tag push runs every build/verify step and fails at upload
   with a 403.

The release edits themselves are prepared in the tree. Land them on `main`
through a reviewed PR with green CI, confirm the post-merge CI run is
completed/success, then push `v0.5.0`. The workflow performs the same build,
strict metadata check, three-way version parity, clean-venv wheel smoke, and
OIDC upload. Do not tag before the merge.

The publish workflow was reviewed line by line and matches this document: one
interpreter, one artifact of each kind, three-way version parity,
`twine check --strict`, clean-venv wheel smoke with `pip check`, and OIDC
trusted publishing with no stored token.

## Readiness re-check — 2026-10-25

Checked against `origin/main` at `45412ff` (merged PR #388) and a local build of
the current tree.

| Item | State |
|---|---|
| Package | `sovereign-harness`, version `0.5.0` in `pyproject.toml` and `harness.__version__` |
| PyPI | `https://pypi.org/pypi/sovereign-harness/json` still returns **404**; no project and no published versions |
| Git tags | `v0.4.1` is the newest local **and** remote tag; no `v0.5.0` |
| GitHub releases | newest is `v0.4.1` (2026-09-22); no `v0.5.0` release |
| `main` CI | run `40178792082` on `45412ff` completed **success**, all 12 jobs green (hermetic matrix, audit, package, installed package) |
| Publish workflow | active; rehearsal run `36466908639` on `b2c87e52` (2026-09-28) completed **success** with every build/verify step green and the upload step `skipped` |
| Repository credentials | no Actions secrets, no Actions variables, no GitHub environments; the only elevated publish capability is `id-token: write` |
| Branch protection | **not enabled** on `main` (GitHub returns "Branch not protected"); the reviewed-PR rule is currently convention, not enforcement |
| Build | `python -m build` produced `sovereign_harness-0.5.0.tar.gz` (1.9 MB) and `sovereign_harness-0.5.0-py3-none-any.whl` (988 KB, `py3-none-any`) |
| Metadata | `python -m twine check --strict dist/*` **PASSED** on both artifacts |
| Wheel | Metadata 2.4, `License-Expression: MIT`, `Requires-Python: >=3.9`, no runtime `Requires-Dist`, four extras, all five `harness/ui/*` and three `harness/data/*` files present |
| sdist | 450 entries; no `__pycache__`/`.pyc`; operator-lane docs (`docs/jev-roadmap.md`, `docs/claude-context.md`, `docs/jev-dogfood.md`) excluded |
| Clean-venv wheel | installs with no runtime dependencies, reports `0.5.0`, `harness`/`harness-mcp`/`driver-core --help` exit 0, `pip check` clean |
| Paid attestation | default-attestation `plan --execute` green on the real CLI with the paid judge (`allow`), JEV `supported=0.97`, final gate `add-ok`, $0.004061; the verifier budget fix is recorded in `audits/self/dogfood/SWEEP_2026-10-25_SEAMS.md` |

The only release blocker remains the owner-only trusted-publisher entry. A
pending publisher cannot be read from the repository (PyPI exposes it only in
the project's publishing settings), so it must be confirmed at pypi.org before
the first tag. If it is absent, register it with the exact values in the table
above; if it is present, `v0.5.0` is ready to tag after this tree is merged and
`main` CI is green. Enabling protection on `main` (required PR review plus the
CI status checks) is recommended before that tag, because the repository is
public and no protection currently enforces it.
