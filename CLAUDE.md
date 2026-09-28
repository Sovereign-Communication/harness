# CLAUDE.md — Harness

@AGENTS.md

Everything above is the tool-neutral agent context (canon pointers, current
mission truth, model policy, rules). This file adds only how Claude Code works
here. Canon STATUS lives in `docs/jev-roadmap.md` — update its rows; never start
a parallel plan document.

## Model tiering (cost policy)

The main session (Opus) is for orchestration, design, canon edits, and final
review. Everything else runs cheaper; the cost lever is **which model you open
the session on**, because Freebuff picks the model when a session or task opens
and cannot switch it between mission phases.

`--model` on `/isolated-mission` and `/isolated-request` is a provenance label
recorded in the pack's receipt, not a router: it names the model that ran and
cannot move a phase onto a different one. Both skills run their phases `inline`
— a fresh input, not a fresh mind — and a genuinely independent grade means the
operator opens a new Freebuff task. The `.claude/agents/` delegation tiers are
not wired into these skills.

## Missions and the Jev bar

- `/isolated-mission [--bar PHASE_ID] <mission>` — scout → plan only when
  justified → execute from injected context → separate verifier + Jev bar → loop.
  Phases are in-session boundaries; an independent grade is the operator's
  move, not something the skill arranges.
  State is a Harness HUL mission pack under `tmp/claude/missions/<id>/`
  (`python .claude/skills/isolated-mission/state.py show --id <id>`).
- `/isolated-request [--model label] <prompt>` — one analysis prompt in a fresh
  Freebuff task, read-only by default, returns just the result.
- **Jev bar**: `python -m harness.cli jev-phase --phase <ID> --repo-root . --local-only`
  (add `--all` for the whole board). STATUS may say complete only on bar pass;
  its `improvements` list (declared sentiment buckets + suggested actions) is the
  next work queue. Drop `--local-only` only for an operator-approved live Jev call.

## Gates (paste raw tails in every PR)

```bash
.venv/Scripts/python.exe -m ruff check harness tests audits
.venv/Scripts/python.exe -m compileall -q harness tests
.venv/Scripts/python.exe -W error::ResourceWarning -m unittest discover -s tests
.venv/Scripts/python.exe audits/self/audit.py        # must print "bar met"
.venv/Scripts/python.exe -m harness.cli jev-phase --phase <ID> --repo-root . --local-only
```

`audits/self/audit.py` rewrites `audits/self/round2_scores.json`; commit it
only with a BAR MET result. D5 reads installed metadata: after a version bump
run `.venv/Scripts/python.exe -m pip install -e ".[dev]"` or D5 fails locally.

## Harness MCP in Claude Code

Registered per machine (local scope, observe-only by default):

```bash
claude mcp add harness --scope local -e HARNESS_MCP_ALLOWED_ROOTS=<repo path> -- <repo>/.venv/Scripts/python.exe -m harness.mcp
```

Writes (`apply_edit`) and paid verification stay refused unless
`HARNESS_MCP_ALLOW_WRITE` / `HARNESS_MCP_ALLOW_VERIFY` or per-call confirmation
is given. See `docs/mcp.md`.

## Working conventions

- Branch or worktree per phase PR (`git worktree add ../Harness-<slug> -b <branch> origin/main`);
  never commit on `main`. PRs land as merge commits (repo convention), only with
  local gates + CI green.
- Windows: the Bash tool is Git Bash; the venv python is `.venv/Scripts/python.exe`.
- Live Harness runs: `HARNESS_USE_FREE=false` (cheap paid rungs), and a private
  `HARNESS_LEDGER=<path>` when several agents run lanes concurrently so the
  hash-chained ledger never interleaves.
- Skill scratch goes under `tmp/claude/` (gitignored). `.claude/` is protected
  config — do not write scratch there.
- Headless sessions ignore `.claude/settings.json` allow rules until the
  workspace trust dialog has been accepted once interactively.
