# Live MCP isolation baseline — 2026-10-03

Recorded at the start of the WIP-recovery merge train so every phase can prove the
shared, user-registered `harness` MCP server was never restarted or repointed.

## Baseline

| PID | Started | Command |
|---|---|---|
| 11904 | 12:40:49 | `Harness\.venv\Scripts\python.exe -m harness.mcp` |
| 11388 | 12:40:49 | `uv\python\cpython-3.9\...\python.exe -m harness.mcp` (child) |

These match the PIDs the earlier Claude session reported as "respawned at 12:40 PM,
now running the merged code". They predate the merge train.

**Correction, 2026-10-04 (after PR #162):** the parent PID was first recorded here as
`4206208`. That was wrong — `4206208` never existed as a live `harness.mcp` process.
Re-reading the process table via `Get-CimInstance Win32_Process` gives the real parent
as **11904**, sharing the child's exact start time `12:40:49`. The invariant that
matters is therefore **start time, not PID**: a `-m harness.mcp` process whose start
time has moved off `12:40:49` means the server was restarted, and the isolation rule
was broken. Verify with:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*harness.mcp*' } |
  Select-Object ProcessId, CreationDate, CommandLine
```

## Why these must not move

The package is installed **editable**, and the install hardcodes the operator tree:

```
__editable__.sovereign_harness-0.4.2.pth
  -> MAPPING: {'harness': 'C:\\...\\Harness\\harness',
                'driver_core': 'C:\\...\\Harness\\driver_core'}
```

The finder is **appended** to `sys.meta_path` (`sys.meta_path.append(_EditableFinder)`),
so the current working directory wins resolution. A worktree therefore imports its
own `harness` package with no install step — verified:

```
cd C:\...\Harness-p1-fixups && ..\Harness\.venv\Scripts\python.exe -c "import harness; print(harness.__file__)"
  -> C:\...\Harness-p1-fixups\harness\__init__.py
```

## Rules for the merge train

Forbidden until Phase H:

- `pip install -e .` / `uv pip install` / any reinstall of `sovereign_harness` — this
  rewrites the `.pth` and repoints the **running** server at whatever tree it was run from.
- Killing or respawning the `-m harness.mcp` processes (11904 / 11388).
- Editing MCP config or re-registering the `harness` server.
- Changing the loopback port, auth token, or any config the live server reads.
- `harness serve` / `harness desktop` against the live instance.
- Edits in the operator tree (`C:\...\Harness`) while the server is live.

Required:

- All validation runs from a phase worktree, using the operator venv's interpreter
  (`C:\...\Harness\.venv\Scripts\python.exe`). The cwd-wins behaviour makes this correct.
- Every concurrent run uses a **private `HARNESS_LEDGER`**, so the live ledger is untouched.
- Process-touching tests (R13 network guard, socket timeouts, driver server) run only in
  hermetic CI or an isolated env with its own temp ledger and `HOME`.

## Per-phase check

After each phase's merge, re-read the PID table. If the `-m harness.mcp` pair is gone or
either start time has moved off `12:40:49`, the isolation rule was broken: stop, and
restore before continuing. **Never `taskkill` by PID on a guess** — match on the command
line, because a stale PID in a note will happily land on an unrelated process.