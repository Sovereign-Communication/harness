---
name: isolated-request
description: Run one analysis prompt in a fresh, history-free Freebuff task, read-only by default, and return just the result. Use for clean-context extraction, scans, and reviews that should not pollute the current session.
argument-hint: "[--model LABEL] [--write] <prompt>"
disable-model-invocation: true
allowed-tools: Write, Read, Glob, Grep
---

# Isolated Request

Runs `$ARGUMENTS` against a context that never saw this conversation: the
project's AGENTS.md/CLAUDE.md read fresh, no session history, no carry-over.
Only the result comes back.

## Execution model

Freebuff is the only runtime, and it cannot spawn a task from inside a task, so
this skill runs `inline`: you answer from the prompt file in the session you are
already in. That is a narrow input, not a clear head — the answering session
already knows whatever the caller knows. `/isolated-mission` § **Freebuff
execution model** owns the full statement of what that does and does not
guarantee.

When the answer must be genuinely independent of the conversation that asked
it, this is the operator's move: they open a new Freebuff task in this worktree
and hand it the prompt file. Do not claim an independence you did not have —
report `inline`.

## Procedure

1. **Split flags from the prompt.** Leading flags in `$ARGUMENTS`:
   - `--model LABEL` — a provenance label naming the model the fresh task runs
     on, for the report. It does **not** route: the model is chosen when the
     task opens, so choose the model in Freebuff's own task UI, then label what
     you picked. With no label, record whatever Freebuff reports.
   - `--write` — allow edits in the fresh task. Without it the task is
     read-only: it reports, it does not modify.
   Everything after the flags is the prompt, verbatim.

2. **Write the prompt to a file** with the file-write tool (never inline it in the
   shell — no quoting hazards): `tmp/claude/isolated-request-<short-slug>.md`
   (`tmp/` is gitignored; Write creates the folder; never write under
   `.claude/` — it is a protected config dir). Put every fact the task needs in
   it: paths, ids, constraints. Append this line so the task returns data, not
   chat: `Return only the requested result. No preamble.`

3. **Answer.** Re-read the file and answer from it alone.

4. **Report.** Show the result (formatted), then one line: model label,
   isolation mode (`inline`), write mode, and cost when the task reports it. An
   `inline` answer has no independent cost — record 0, do not invent a figure.

5. **Mission receipts.** When called from `/isolated-mission`, the caller
   records the result as a receipt in the mission pack; this skill does not
   write state.

## Examples

```
/isolated-request list every public function in harness/jev_packs.py with its one-line docstring as JSON
/isolated-request scan tracked files for hardcoded secrets or API keys; list file, line, severity (mask values)
/isolated-request --write rename the jev pack phase ids to snake_case across harness/ and tests/
```

## Notes

- Put every fact in the prompt file: an `inline` answer cannot see why the
  question is being asked, and nothing in the file will tell it.
- A quota wall, or a task that never starts, is `blocked`: report the exact
  command and its output. Never report a pass you did not see. A *denied*
  command is `blocked` too, but only on a host that has a permission layer —
  Freebuff has none, so that case cannot arise here.
- Read-only is the default because an unreviewed edit in a fresh context is
  expensive to unwind. `--write` is opt-in for the same reason.
