# Agent-desktop audit — 2026-11-01, completed 2026-11-04

Status: **complete** (B6 completed and Parts C/D written on 2026-11-04, lane/burndown).
Scope: read-only. No source, config or desktop state was mutated by this audit.

This report exists because the 2026-11-01 audit session died mid-B6 (disk pressure),
so B1-B5 were measured but B6, Part C, Part D and this file were never produced. The
B1-B5 rows below are the session's recorded measurements, carried forward verbatim in
substance and labelled as such. Everything in the "verified today" column was
re-checked against this repository at the time of writing, and the report's own path
and line references are guarded by an executable stale-ref test
(`tests/test_agent_desktop_audit_report.py`) so they cannot rot silently.

## Part A — snapshot and method

| Item | Value |
|---|---|
| Audit object | agent-desktop **v1.0.2** (pinned install, archive checksum matched, SLSA attestation verified — 2026-11-01 session; lane-reported) |
| Host | operator laptop, Windows; single FreeBuff Desktop window |
| Method B1-B2 | pinned install + process/window enumeration |
| Method B3 | accessibility-tree dump (Electron AX flag on, 30s settle) |
| Method B4 | screenshot capture through the screen source |
| Method B5 | one `type_text` path against Notepad, headless and headed |
| Re-run 2026-11-04 | **not re-run.** The audit tool bundle (`.tools/`: binary, tarball, checksums, one PNG) is not present in this worktree, so B1-B5 cannot be re-derived here. They are recorded evidence, not fresh measurement. |

## Part B — measurements

| Step | Result | State |
|---|---|---|
| B1 | Pinned agent-desktop v1.0.2 installed; archive checksum matched; SLSA attestation verified. | recorded 2026-11-01 |
| B2 | FreeBuff visible to the tool: one window, not focused. | recorded 2026-11-01 |
| B3 field exposure (core measurement) | The Electron accessibility tree exposes window chrome only: **13 nodes, 3 refs**, including with the accessibility flag on and a 30s settle. **All eight launch-gate fields are absent** from the tree on this build: `app_identity`, `backend`, `model`, `thinking`, `repo`, `build_mode`, `mission_queued`, `start_state`. | recorded 2026-11-01 |
| B4 | Screenshot fallback works on this machine (1800x1131 PNG). | recorded 2026-11-01 |
| B5 (Notepad only) | `set-value` delivered and verified headless, readback exact, pointer unmoved — but the Notepad window took the foreground (measured focus leak). Both type paths refused cleanly: `POLICY_DENIED` headless, `ACTION_FAILED` headed, no partial write. | recorded 2026-11-01 |
| B6 stale-ref test | **Completed 2026-11-04.** See "Part B6" below. | this pass |

### Part B6 — stale references

The audit was chartered to end with a test that keeps its own evidence honest. Two
findings, both now pinned:

1. **The audit's evidence bundle was local-only.** `.tools/` held the v1.0.2 binary,
   tarball, checksums and one PNG; that directory is not in this worktree and was
   never committed (it was a named, bounded audit exception). The B1-B5 rows are
   therefore recorded evidence and this report says so rather than implying they can
   be re-run. The amendment in Part D turns that into a forward rule: an audit that
   cites a pinned artifact must land the checksums (not the binary) under `audits/`.
2. **The report's own references are now executable.** `tests/test_agent_desktop_audit_report.py`
   fails if a cited `path:line` no longer exists, if any of the eight fields disappears
   from the Part C mapping, if a placeholder marker returns, or if a Part D amendment
   loses its target file. This is the B6 deliverable: staleness is detected by the
   suite, not by a reader.

## Part C — fit mapping (launch-gate fields to observable surfaces)

The question Part C answers: for each launch-gate field, which surface can actually
observe it on agent-desktop v1.0.2, and what does that mean for S3?

| Launch-gate field | Accessibility tree (B3, measured) | Screenshot + Jev vision (#272) | CLI/config surface | Modelled in this repo? |
|---|---|---|---|---|
| `app_identity` | absent | candidate: window title / chrome is visible in a screenshot | no | **no** |
| `backend` | absent | candidate: rendered label, if the UI shows it | no | **no** |
| `model` | absent | candidate: rendered label, if the UI shows it | no | **no** |
| `thinking` | absent | candidate: rendered toggle/state | no | **no** |
| `repo` | absent | candidate: rendered selector value | no | **no** |
| `build_mode` | absent | candidate: rendered selector value | no | **no** |
| `mission_queued` | absent | candidate: rendered queue state | no | **no** |
| `start_state` | absent | candidate: rendered Start control state | no | **no** |

Reading of the mapping, stated plainly:

- The **accessibility-tree route is not a viable verification path** on this build:
  8 of 8 fields absent, with 13 nodes / 3 refs total. This is a measured result, not
  an inference from a failed selector.
- The **vision route is the only candidate** for reading these fields, which is why
  S3 (#272) exists (screenshot + Jev, threshold 0.85, advisory when the flag is off).
  A candidate is not a proof: no vision run over these eight fields has been recorded,
  and #272's acceptance criteria remain open.
- **None of the eight field names is modelled anywhere in this repository** (checked
  across `*.py`, `*.md`, `*.json` on 2026-11-04). The launch gate currently lives in
  the consuming desktop flow, not here, so the driver can verify these fields only
  through pixels until a schema exists. That is a finding, not a defect in this repo.
- The `driver_core` vocabulary has `focus`, `click` and `type_text`
  (`driver_core/actions.py:167`, `:169`, `:171`) but **no `launch_app`** yet — S2
  (#270) is the open prerequisite for anything that must observe a freshly launched app.

## Part D — amendment list (proposed, not applied)

The interim record is explicit that "amendments are a separate, later direction". This
is therefore a list of exact proposals with their targets, not an applied change. Each
target file exists on this tip and is checked by the B6 test.

1. **`docs/jev-roadmap.md`** — in the S3 (#272) row, add one sentence to the
   verification note: "The accessibility-tree route was measured non-viable on
   agent-desktop v1.0.2 (8/8 launch-gate fields absent, 13 AX nodes / 3 refs); the
   vision route is the verification path, and the eight fields are not yet modelled
   in this repository." Rationale: prevents a future pass from re-attempting AX
   extraction as if it were untried.
2. **`docs/driver.md`** — add a short "Verification path" note after the keyless/keyed
   table: "Screenshot verification must follow any `type_text`, because the measured
   B5 result shows the typed-into window can take the foreground; assert on a fresh
   capture, never on the pre-typing frame." Rationale: a concrete, measured focus
   leak that would otherwise silently invalidate a vision check.
3. **#272 (issue text, not a file)** — add the eight field names to the acceptance
   criteria as the required vision fields, so the gate is scored per field rather
   than on a single "is it visible" question. Rationale: the S3 bar is per-field 95.
4. **#270 (issue text, not a file)** — record that `launch_app` is a prerequisite for
   observing `start_state` in the same run, so S2 and S3 are ordered, not parallel.
5. **Future audit rule (no file yet)** — land pinned artifact *checksums* under
   `audits/` for any audit that cites a specific binary version, so the evidence
   survives the worktree. Rationale: the B1 pin is currently unreproducible here.

## Limitations

- B1-B5 were not re-run on 2026-11-04; the tooling is absent from the worktree. Any
  statement about the desktop build in this report is recorded evidence from
  2026-11-01, labelled as such in every row.
- No vision (Jev) call was made while writing Part C: it would spend the owner's
  credit and there is no captured frame on this host to score.
- Part D is a proposal list. Nothing in `docs/` was amended by this pass, by the
  audit's own terms.

## Reproducibility

- Executable guard: `python -m unittest tests.test_agent_desktop_audit_report`.
- Repository references used above, re-verified at write time:
  `driver_core/actions.py:167` (`focus`), `driver_core/actions.py:169` (`click`),
  `driver_core/actions.py:171` (`type_text`), `harness/perception_client.py:182`
  (driver `health` client), `pyproject.toml:40` (`harness-desktop` entry point),
  `docs/driver.md:300` (headless key provisioning), `docs/driver.md:313` (keyless vs
  keyed table).
