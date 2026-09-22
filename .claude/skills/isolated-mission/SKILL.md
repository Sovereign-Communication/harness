---
name: isolated-mission
description: Multi-tier mission loop — Haiku scout extracts scope, Opus plans only when justified, the cheapest capable model executes with injected context, a separate verifier plus the Jev bar grade it, and the loop continues until the bar passes or limits hit. State lives in a Harness HUL mission pack.
argument-hint: "[--plan] [--scout-only] [--iterative] [--rounds N] [--bar PHASE_ID] [--engine agent|headless] [--model haiku|sonnet|opus] [--budget USD] [--scope-lock] [--max-scope KB] [--inject-file PATH] [--continue ID] <mission>"
disable-model-invocation: true
allowed-tools: Bash(python .claude/skills/isolated-mission/state.py *), Bash(.venv/Scripts/python.exe .claude/skills/isolated-mission/state.py *), Bash(python .claude/skills/isolated-request/run.py *), Bash(python -m harness.cli jev-phase *), Bash(.venv/Scripts/python.exe -m harness.cli jev-phase *), Bash(python -m unittest *), Bash(python -W error::ResourceWarning -m unittest *), Bash(python -m ruff check *), Bash(python audits/self/audit.py *), Write, Read, Glob, Grep, Bash(git status *), Bash(git diff *), Bash(git log *), Bash(git worktree list *)
---

# Isolated Mission

Mission: **$ARGUMENTS**

You (the main session) are the orchestrator and the only place strategic
judgment happens. Every other phase runs in an isolated context on the
cheapest capable tier, receives only the context injected for it, and returns
structured data. Builder ≠ grader: the verifier is never the executor.

## Mandatory sequence (every mission, including verification-only ones)

Do these in order; do not skip a step because the mission looks small.

1. `state.py init` (or `state.py show` for `--continue`) — no pack, no mission.
2. SCOUT via the `harness-scout` agent (Haiku) → artifact + receipt. Do not scout yourself.
3. PLAN only if the rules in §2 say so → artifact + receipt.
4. EXECUTE via an executor agent (Haiku/Sonnet) → receipt. For verification-only
   missions the executor is the gate run in step 5; record `--phase execute` for it.
5. GATES: run the scout/plan gate commands **yourself**, each as ONE plain
   command from the repo root (pre-approved above, so they work in any permission
   mode): `python -m unittest <dotted.modules>` (this repo uses unittest, never
   pytest), `python -m ruff check ...`, `python audits/self/audit.py`,
   `python -m harness.cli jev-phase ...`. Never prefix `cd ... &&`.
6. VERIFY via the `harness-verifier` agent with the raw gate output + diff → receipt.
7. LOOP decision → `round-<N>.json` artifact + `--phase loop` receipt.
8. `state.py terminal` with FINDINGS, then report pack path + outcome + cost by tier.

Run phase agents in the **foreground** (never `run_in_background`): a headless
(`claude -p`) mission ends when your turn ends, and background agents would be
orphaned. If a gate command is denied, record `blocked` with the exact denial in
FINDINGS and close the mission — never report a pass you did not observe.

```
SCOUT (haiku) ──► PLAN (opus, only if justified) ──► EXECUTE (haiku|sonnet) ──► EVALUATE (verifier + Jev bar)
      ▲                                                                                 │
      └──────────── loop: bar improvements become next round's scope ◄─────────────────┘
```

## 0. Parse and set up

Flags (all optional; anything else is the mission text):

| Flag | Meaning |
|---|---|
| `--plan` | force the Opus plan phase |
| `--scout-only` | stop after scout; show scope for review |
| `--iterative` / `--rounds N` | loop until done (default max 3 rounds) / exactly N rounds max |
| `--bar PHASE_ID` | the canon STATUS id this mission must satisfy (e.g. `JEV-BAR`, `MS`); the Jev bar is then the completion gate |
| `--engine agent\|headless` | `agent` (default): Agent tool subagents. `headless`: each phase is a `/isolated-request` process (`claude -p`) — use for unattended runs or when phases must not share permissions |
| `--model TIER` | force the execute tier |
| `--budget USD` | mission max cost for paid lanes (default 2.00; reserve 10%) |
| `--scope-lock` | after round 1, never add files to scope |
| `--max-scope KB` | refuse to inject more than this much file content per phase |
| `--inject-file PATH` | skip scout; use this file as the scope/context injection |
| `--continue ID` | resume mission ID from its pack (`state.py show`) |

`$PY` below means the literal word `python` (Harness is pure stdlib and
`state.py` imports this checkout's own `harness`; use `.venv/Scripts/python.exe`
only if `python` is missing — Glob cannot see the gitignored `.venv`). Write it
out literally and run each helper call as ONE plain command (no `&&`, pipes,
`cd`, or shell variables) so the pre-approved permission rules match. Write
JSON/Markdown inputs with the Write tool under `tmp/claude/`, never under
`.claude/` (protected config dir) and never via shell redirection.
Mission id: `m-<yyyymmdd>-<3-word-slug>` unless `--continue`.

```bash
$PY .claude/skills/isolated-mission/state.py init --id <ID> --request "<mission>" \
  --success "<one-line definition of done; include the bar if --bar>" \
  --max-cost <budget> --reserve <10% of budget> --in-scope "<comma list or empty>"
```

For `--continue ID`: run `state.py show --id ID`, read `resume`, `last_receipts`,
`last_bar`, and resume at the next phase/round. Never redo completed rounds.

## 1. SCOUT — always Haiku (skip only with `--inject-file`)

Delegate to the `harness-scout` agent (Agent tool, `subagent_type: "harness-scout"`)
or, with `--engine headless`, `/isolated-request --model haiku`. Prompt it with
the mission and ask for **only** this JSON:

```json
{"files": [{"path": "...", "why": "...", "bytes": 0}], "patterns": ["..."],
 "dependencies": ["a -> b"], "canon_rows": ["STATUS ids touched"],
 "gates": ["exact test/audit commands that prove this work"],
 "scope_summary": "...", "out_of_scope": ["..."],
 "estimated_effort": "Small|Medium|Large|XLarge", "context_size_estimate": "~5K|~50K|~500K"}
```

Save it: write to `tmp/claude/<ID>-scout.json`, then
`state.py artifact --id <ID> --name scout-r<N>.json --file tmp/claude/<ID>-scout.json` and
`state.py receipt --id <ID> --phase scout --round <N> --model haiku --tokens <subagent_tokens> --status ok --summary "<files> files, <effort>"`.

`--scout-only`: show the scope and stop.

## 2. PLAN — Opus, only when justified

Run when `--plan`, or effort is Large/XLarge, or the previous evaluate returned
`needs_replan`. If this session is already Opus, plan inline (that is the
strategic use of Opus); otherwise delegate with `model: "opus"`. Output JSON:
`{approach, phases: [{id, work, files, tier, gate}], context_injection_points,
risk_factors, validation_checkpoints, confidence}`. In Harness, the plan must name the
canon STATUS row it updates, extend one policy owner (no parallel modules),
and name hermetic gate tests. Save as `plan-r<N>.json` artifact + receipt.

## 3. EXECUTE — cheapest capable tier, injected context only

Tier: `--model` if given; else `haiku` for Small/mechanical, `sonnet` for
Medium+ (never Opus for execution unless the user forces it). Delegate to
`harness-implementer` (sonnet) or a haiku general-purpose agent. Inject exactly:

1. the scoped file list (respect `--max-scope`; with `--scope-lock` no new files),
2. patterns + dependency map from scout,
3. the plan phase(s) for this round, verbatim,
4. scope boundaries (in/out) and the canon rules that apply,
5. last round's bar `improvements` (declared buckets + suggested actions) as the work queue.

Writes happen in a git worktree / feature branch, never on `main`. Parallel
executors only for overlap-free file sets. Receipt: `--phase execute --model <tier>`.

## 4. EVALUATE — separate verifier + Jev bar

Run the gates scout and plan named yourself (sequence step 5), then delegate to
`harness-verifier` (never the executor) in the foreground with: the success
definition, the raw gate tails, and the diff range. It re-runs gates when its
permissions allow, reviews the diff, and returns
`{verdict: pass|fail, gates: [{cmd, ok, tail}], issues: [...], drift: bool}`.
A verifier that could not observe a gate result must return `fail`.

If `--bar PHASE_ID` (or the mission maps to a canon row), run the Jev bar:

```bash
$PY -m harness.cli jev-phase --phase <PHASE_ID> --repo-root . --local-only --json --out tmp/claude/<ID>-bar-r<N>.json
$PY .claude/skills/isolated-mission/state.py bar --id <ID> --file tmp/claude/<ID>-bar-r<N>.json
```

(Drop `--local-only` only when the operator wants a live, paid Jev judgment.)
The bar's `improvements` list — each a declared sentiment bucket with
`suggested_next_action`, axis, level and source — **is the next round's scope**.
A mission targeting a STATUS row is complete only when `can_mark_complete` is
true; never flip STATUS on prose.

## 5. LOOP decision (write it down every round)

Emit and record (as `round-<N>.json` artifact + `--phase loop` receipt):

```json
{"mission_id": "...", "round": 1, "status": "success|partial|needs_replan|complete|blocked|stalled",
 "bar": {"phase": "...", "pass": false, "score": 0, "top_improvements": ["bucket: action"]},
 "verifier": "pass|fail", "should_continue": true, "reason": "...", "next_round_scope": ["..."]}
```

Continue when the verifier failed or the bar has improvements AND rounds/budget
remain. Stop on: bar pass + verifier pass (`complete`), budget reserve refusal
from `state.py` (`blocked` — the dual budget said no), 2 rounds with no new
evidence (`stalled`), or scope drift beyond `--max-scope` (`needs_replan` →
back to PLAN once, then stop). Close with:

```bash
$PY .claude/skills/isolated-mission/state.py terminal --id <ID> --outcome <complete|failed|blocked|stalled> --findings-file tmp/claude/<ID>-FINDINGS.md
```

FINDINGS.md = what changed, gate evidence (raw tails), bar result, open
improvements, cost by tier. Then report to the user: outcome, pack path, and
the remaining improvements if any, with cost split two ways — (a) the pack
budget (paid Harness lanes and Jev calls recorded via `--cost`), and (b) Claude
spend by tier: subagent tokens from each receipt's `--tokens`, plus
`cost_usd` for headless phases. Never report (a) as the mission's total cost.

## Rules

- Opus only for orchestration and planning. Scout = Haiku. Execute = Haiku/Sonnet. Verify = Sonnet.
- Each phase gets only its injection; never forward the whole conversation.
- 0-hallucination: bar buckets/actions come from the operator pack; never invent new ones.
- Fail ≠ approve. Blocked = exact command + output in FINDINGS, never "will do".
- Paid Harness lanes during a mission: cheap paid rungs, `HARNESS_USE_FREE=false`,
  and a private `HARNESS_LEDGER` when phases run concurrently.
