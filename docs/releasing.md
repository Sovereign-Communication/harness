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
   python -m twine check dist/*
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
   git tag v0.4.2          # must equal the pyproject version, with the v
   git push origin v0.4.2
   ```

   The tag is what triggers the publish. The workflow refuses to upload if
   the tag and the `pyproject.toml` version disagree, because PyPI does not
   allow a released version to be overwritten: a mismatch would permanently
   publish a version number that the distribution's own metadata contradicts.

4. Watch the `publish` run. It builds, runs `twine check`, verifies
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
