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
   Standing deviation (2026-09-15): CI never fires on this repo (private-repo Actions minutes, falsified in PR #8), so this gate is satisfied by the documented merge-basis pattern -- the full local battery green on every available interpreter plus the branch's last fired CI run.

The project intentionally keeps runtime dependencies at zero. Build and lint
packages are development-only extras.
