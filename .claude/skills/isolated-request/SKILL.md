---
name: isolated-request
description: Run one analysis prompt in a fresh, history-free headless Claude session (claude -p) on the cheapest capable model, read-only by default, and return the result plus its cost. Use for clean-context extraction, scans, and reviews that should not pollute the current session.
argument-hint: "[--model haiku|sonnet|opus] [--write] [--budget USD] [--allow RULE] <prompt>"
disable-model-invocation: true
allowed-tools: Bash(python .claude/skills/isolated-request/run.py *), Bash(.venv/Scripts/python.exe .claude/skills/isolated-request/run.py *), Bash(.venv/bin/python .claude/skills/isolated-request/run.py *), Write, Read
---

# Isolated Request

Runs `$ARGUMENTS` in a brand-new `claude -p` process: no session history, the
project's CLAUDE.md/AGENTS.md loaded fresh, `.claude/settings.json` permissions
applied, session not persisted. Only the final result (and its cost) comes back
into this conversation.

## Procedure

1. **Split flags from the prompt.** Leading flags you may see in `$ARGUMENTS`:
   - `--model haiku|sonnet|opus` — default **sonnet**. Pick the cheapest capable
     tier yourself when the user gave none: `haiku` for extraction / listing /
     grep-shaped work, `sonnet` for analysis and review, `opus` only when the
     user asks for it or the task is architectural judgment.
   - `--write` — allow edits (`acceptEdits`). Without it the session is
     read-only (`dontAsk`: anything not pre-approved by the project allowlist is
     refused, never prompted).
   - `--budget USD` — spend cap for the session (default 1.0).
   - `--allow 'RULE'` — extra permission rule, repeatable, e.g.
     `--allow 'Bash(.venv/Scripts/python.exe -m unittest *)'`.
   Everything after the flags is the prompt, verbatim.

2. **Write the prompt to a file** with the Write tool (never inline it in the
   shell — no quoting hazards): `tmp/claude/isolated-request-<short-slug>.md`
   (`tmp/` is gitignored; Write creates the folder; never write under `.claude/` — it is a protected config dir). Append this line to the prompt so the
   isolated session returns data, not chat: `Return only the requested result. No preamble.`

3. **Run the helper** from the repo root as ONE plain command — no `&&`, pipes,
   `cd`, or shell variables, or the pre-approved permission rule will not match.
   Interpreter: plain `python` (the helper is stdlib-only; fall back to
   `.venv/Scripts/python.exe` only if `python` is missing):

   ```bash
   python .claude/skills/isolated-request/run.py --model <tier> [--write] [--budget N] [--allow RULE]... --prompt-file tmp/claude/isolated-request-<slug>.md
   ```

   Use a Bash timeout of 600000 ms. For work expected to run longer than ~9
   minutes, run it with `run_in_background` and wait for the notification.
   `--dry-run` prints the exact `claude` command without running it.

4. **Report.** The helper prints one JSON object:
   `{ok, result, model, cost_usd, num_turns, duration_ms, session_id, permission_mode, is_error, error}`.
   Show the user `result` (formatted), then one line: model, cost, turns,
   permission mode. If `ok` is false, show `error` verbatim — do not retry with
   broader permissions unless the user asks.

5. **Mission receipts.** When called from `/isolated-mission`, the caller records
   the JSON as a receipt in the mission pack; this skill does not write state.

## Examples

```
/isolated-request --model haiku list every public function in harness/jev_packs.py with its one-line docstring as JSON
/isolated-request scan tracked files for hardcoded secrets or API keys; list file, line, severity (mask values)
/isolated-request --allow 'Bash(.venv/Scripts/python.exe -m unittest *)' run tests.test_jev_completion and report failures with the first traceback line
```

## Notes

- Isolation is process-level: the child cannot see this conversation, so put
  every fact it needs (paths, IDs, constraints) in the prompt.
- Cost comes from the child's own `total_cost_usd`; it is an estimate on
  subscription plans.
- Read-only sessions can still run commands the project allowlist pre-approves
  (hermetic gates, read-only git/gh) — see `.claude/settings.json`.
