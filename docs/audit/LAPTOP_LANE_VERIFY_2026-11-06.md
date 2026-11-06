# Harness laptop lane — verification record, 2026-11-06

Record of the laptop-side queue executed from inside a FreeBuff Desktop
session on the machine where FreeBuff runs. Filed so the work is resumable
from GitHub alone. The authoritative, full filings are on the issues named
per section; this file is the repo-side pin of what was run and read.

Tree under test: `828474615e2a23c4d8bfffd4d7d0e0c777354039` (= `origin/main`).
Host: Windows, Python 3.14.6 (`C:\Python314\python.exe`).

## 1. Harness reachability from inside a FreeBuff session

Full filing: tracker #295, comment 6696109810.

| Surface | Verdict | Method |
|---|---|---|
| MCP tools | UNREACHABLE | the configured `harness-oc` server is an SSH tunnel to a remote EC2 host; `ssh: connect to host 13.217.204.112 port 22: Connection timed out` |
| CLI | REACHABLE | `python -m harness.cli` from the tree under test (exit 0); `harness doctor` through the canonical clone's venv |
| GUI | REACHABLE | HTTP 200 plus a rendered local-browser read of the served UI; landed tree served on an alternate port |

Notes that change how lanes should verify:

- No `harness` executable is on PATH, and `python -m harness` fails
  (`No module named harness.__main__`). The invocation that resolves the tree
  under test is `python -m harness.cli`.
- The installed editable package (`pip show sovereign-harness`, 0.5.1) points
  at FreeBuff worktree `138f0570`, not the tree under test and not the
  operator clone. Version and tree identity must be printed, not assumed.
- The local MCP server itself is healthy (`harness doctor` runs its stdio
  smoke and lists 30 tools). What is broken is the desktop's remote-tunnel
  configuration for MCP.

## 2. Live-keyed dogfood under `jev_primary_mode`

Full filing: #453, comment 6696156857.

`jev_primary_mode` does not exist on landed `main` (zero grep hits); it lives
only on the sandbox seat's branch `lane/jev-primary-s1` at `1f144359`. The run
therefore executed against a read-only export of that head (`git archive`),
with nothing pushed to that branch and no worktree of theirs created.

Result: a service-level `verify` under `HARNESS_JEV_PRIMARY_MODE=1` with real
keys routed **`jev_primary`** with `is_fallback: false` on a live
`jev-1.14.0` evaluation, and the Jev cost carried as its own `jev:claims` line
apart from both LLM models. Ceiling identity held:
`0.05 - 0.000188472714 - 0.0 = 0.049811527286` working remaining.

Recorded findings, not smoothed:

- The mode names the route; it does not own the verdict. Jev claim-support
  reported the claim supported at 0.83 while the deterministic panel tally
  read `not_real` on the same specimen.
- Live Jev cost was about 5.5x the hermetic S5 figure, as S5 predicted.
- The CLI `verify` path passes no terminal reserve, so the dual-envelope
  reserve identity is exercised there with 0.0; only callers that pass a
  reserve exercise the non-zero branch.

## 3. S3 calibration inputs (session-start specimens)

Full filing: #452, comment 6696257682.

Observed on this machine: physical 1920x1200, logical desktop 1536x960 (125%
scaling), and `capture_screen()` returns a 1536x960 PNG — the driver's pixels
are logical, so a corpus must pin its scale factor. The FreeBuff window is
`Freebuff: The free coding agent`, class `Chrome_WidgetWin_1`, rect
`[48, 5, 1488, 910]`, exe `Freebuff.exe` FileVersion `0.0.201`.

`describe_screen()` returns only title/app/class with an empty description
slot, so of the eight launch-gate fields in `driver_core/vision_verify.py`
only `app_identity` is fully readable today; `backend`, `model`, `thinking`,
`repo` and structured `mission_queued` have no read path yet. The app also
exposes a token-gated loopback JSON API (401 without a token) and no Chrome
DevTools endpoint, which is the cheaper structured read once a token is
provisioned — an owner act.

Three of the eight visible windows share `Chrome_WidgetWin_1` with FreeBuff,
so per-field negatives cannot be generated from window class alone.

## What is deliberately not claimed

No CI result, no merge, no readiness. Nothing here was pushed to `main` or to
`lane/jev-primary-s1`, and no per-field calibration bar is asserted.

GATE: local pass (decision-signal) - record of executed probes and runs, README/docs only - checks 1-5 clean.
