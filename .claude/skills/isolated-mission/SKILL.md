---
name: isolated-mission
description: Mission loop — a scout extracts scope, a plan runs only when justified, execute works from injected context only, a separate verifier plus the Jev bar grade it, and the loop continues until the bar passes or limits hit. Runs inside Freebuff, where every phase shares one session model. State lives in a Harness HUL mission pack.
argument-hint: "[--plan] [--scout-only] [--rounds N] [--bar PHASE_ID] [--model LABEL] [--budget USD] [--scope-lock] [--max-scope KB] [--inject-file PATH] [--continue ID] <mission>"
disable-model-invocation: true
allowed-tools: Bash(python .claude/skills/isolated-mission/state.py *), Bash(.venv/Scripts/python.exe .claude/skills/isolated-mission/state.py *), Bash(python -m harness.cli jev-phase *), Bash(.venv/Scripts/python.exe -m harness.cli jev-phase *), Bash(python -m unittest *), Bash(python -W error::ResourceWarning -m unittest *), Bash(python -m ruff check *), Bash(python audits/self/audit.py *), Write, Read, Glob, Grep, Bash(git status *), Bash(git diff *), Bash(git log *), Bash(git worktree list *)
---

# Isolated Mission

Mission: **$ARGUMENTS**

You (the main session) are the orchestrator and the only place strategic
judgment happens. Every other phase runs on this session's model, receives only
the context injected for it, and returns structured data.
The verifier is a separate *step* with its own written verdict, not necessarily
a separate *judge* — see what an `inline` boundary guarantees below.

## Freebuff execution model

Freebuff is the only runtime for this skill. It exposes no subagent API and no
headless one-shot prompt runner, so phases are **phase boundaries inside this
session**, not child processes:

- **One isolation mode: `inline`.** Every phase is a boundary inside this
  session, and `--isolation inline` is recorded on every receipt so a pack
  reader always knows the independence level of the grade they are reading.
  Freebuff cannot spawn a task from inside a task, so this skill does not
  pretend to orchestrate one; see the note below for what that costs.
- **`inline` is a narrow input, not a clear head.** What it guarantees: the
  phase works from a written brief rather than from the conversation, it runs
  after the gates rather than during the work, and it produces a written verdict
  with evidence attached. What it does **not** guarantee: an independent judge.
  The same session that wrote the code grades it, and the executor's reasoning
  is still in that session's head. No amount of input-narrowing removes it.
- **A genuinely independent grade is the operator's move, not this skill's.**
  Only a Freebuff task opened by a human starts with none of the builder's
  history. When a verdict matters more than convenience — a contested bar, a
  security- or money-touching change, or any moment you catch yourself grading
  the *intent* instead of the diff — say so in the verify receipt and let the
  operator open a new task with the verify brief. Do not claim independence you
  did not have; record `inline` and let the receipt disclose it.
- **Discipline that works under `inline`:** grade from the written evidence
  alone — the spec, the diff, the raw gate tails, the bar output.
- **One model per session.** Freebuff picks the model when the session opens; it
  cannot be switched between phases. Every phase therefore runs on the same
  model, and tier discipline is a decision about which session you open, not a
  knob you turn mid-mission.
- **Say which you used.** Every phase receipt carries a `--model` label (the
  model that actually ran) and `--isolation inline`, the only value there is.
  The two are separate fields on purpose: the label never carries the mode, and
  the mode never rides in `model`.
- **Provider or quota unavailable is `blocked`,** never a silent pass: record
  the exact command and its output in FINDINGS and close the mission.

## Mandatory sequence (every mission, including verification-only ones)

Do these in order; do not skip a step because the mission looks small.

1. `state.py init` (or `state.py show` for `--continue`) — no pack, no mission.
2. SCOUT → artifact + receipt. Never skip it, and never widen scope past what
   the scout named.
3. PLAN only if the rules in §2 say so → artifact + receipt.
4. EXECUTE from injected context only → receipt.
   For verification-only missions the executor is the gate run in step 5;
   record `--phase execute` for it.
5. GATES: run the scout/plan gate commands **yourself**, each as ONE plain
   command from the repo root: `python -m unittest <dotted.modules>` (this repo
   uses unittest, never pytest), `python -m ruff check ...`,
   `python audits/self/audit.py`, `python -m harness.cli jev-phase ...`.
   Never prefix `cd ... &&`.
6. VERIFY as a separate step, grading from the raw gate output + diff → receipt
   recording `--isolation`, so the pack states how independent that grade was.
7. LOOP decision → `round-<N>.json` artifact + `--phase loop` receipt.
8. `state.py terminal` with FINDINGS, then report pack path + outcome + cost.

Run every phase in the **foreground** and finish it before starting the next:
a mission ends when this turn ends, and anything left running is orphaned. If
a gate command cannot run, or is **denied** on a host that has a permission
layer, record `blocked` with the exact command and output in FINDINGS and close
the mission — never report a pass you did not observe. (Freebuff has no
permission layer, so a *denial* cannot arise here; on Freebuff the reachable
case is a gate that fails or is missing, and the same rule applies.)

```
SCOUT ──► PLAN (only if justified) ──► EXECUTE ──► EVALUATE (verifier + Jev bar)
      ▲                                                                                 │
      └──────────── loop: bar improvements become next round's scope ◄─────────────────┘
```

## 0. Parse and set up

Flags (all optional; anything else is the mission text):

| Flag | Meaning |
|---|---|
| `--plan` | force the plan phase, whatever the scout's effort estimate says |
| `--scout-only` | run scout and stop. The pack is left **open** at the scout, so it is resumable later with `--continue ID`; nothing is terminated |
| `--rounds N` | how many rounds the §5 loop may run before it must stop. Default 3; **`--rounds 1` is the single-round mission**. The loop is always on — §5 decides each round — so this is the only round control |
| `--bar PHASE_ID` | the canon STATUS id this mission must satisfy (e.g. `JEV-BAR`, `MS`); the Jev bar is then the completion gate |
| `--model LABEL` | provenance label recorded in each receipt, naming the model that actually ran. **It does not route** — no phase can move to another model than the session's |
| `--isolation inline` | how the phases were isolated. `inline` is the only mode Freebuff offers, and it is required on every receipt so a pack reader can never mistake this grade for an independent one |
| `--budget USD` | mission max cost for paid lanes (default 2.00; reserve 10%) |
| `--scope-lock` | a constraint on the briefs you write, not an enforced gate: from round 2 on, do not widen the scoped file list, even when the bar's next-round-scope asks for it. Nothing checks it — the execute receipt's summary is the only place it shows |
| `--max-scope KB` | the size budget you keep each phase brief under, and the threshold §5 calls scope drift. A discipline you apply while writing the brief; nothing refuses a brief that overshoots |
| `--inject-file PATH` | replace the scout phase's work with a scope you already agreed, given as the same scout JSON (§1). Record it as the `scout-r<N>.json` artifact plus a scout receipt, so the pack still shows what was agreed |
| `--continue ID` | resume mission ID from its pack (`state.py show`) |

`$PY` below means the literal word `python` (Harness is pure stdlib and
`state.py` imports this checkout's own `harness`; use `.venv/Scripts/python.exe`
only if `python` is missing — Glob cannot see the gitignored `.venv`). Write it
out literally and run each helper call as ONE plain command: no `&&`, pipes,
`cd`, or shell variables, so the command in the transcript is exactly the
command that ran and nothing is hidden behind a substitution. Write
JSON/Markdown inputs with the file-write tool under `tmp/claude/`, never under
`.claude/` (protected config dir) and never via shell redirection.
Mission id: `m-<yyyymmdd>-<3-word-slug>` unless `--continue`.

```bash
$PY .claude/skills/isolated-mission/state.py init --id <ID> --request "<mission>" \
  --success "<one-line definition of done; include the bar if --bar>" \
  --max-cost <budget> --reserve <10% of budget> --in-scope "<comma list or empty>"
```

For `--continue ID`: run `state.py show --id ID`, read `resume`, `last_receipts`,
`last_bar`, and resume at the next phase/round. Never redo completed rounds.

**Every write refuses to lose evidence.** A `receipt`, `bar`, or `terminal` on
an already-terminal mission exits nonzero instead of overwriting, and an
`artifact` whose `--name` is taken refuses the same way; `--force` is the
deliberate override. Each command's own section below gives the exact case and
message (§1 scout, §4 bar, §5 terminal). `init` needs no flag — a second
`init` on the same id already refuses with "mission pack already exists".

## 1. SCOUT — always (skip only with `--inject-file`)

Open a scout boundary: write the mission, the canon pointer, and the JSON shape
below to `tmp/claude/<ID>-scout-brief.md`, then answer **only** from that file
and the repo. Ask for **only** this JSON:

```json
{"files": [{"path": "...", "why": "...", "bytes": 0}], "patterns": ["..."],
 "dependencies": ["a -> b"], "canon_rows": ["STATUS ids touched"],
 "gates": ["exact test/audit commands that prove this work"],
 "scope_summary": "...", "out_of_scope": ["..."],
 "estimated_effort": "Small|Medium|Large|XLarge", "context_size_estimate": "~5K|~50K|~500K"}
```

Save it: write to `tmp/claude/<ID>-scout.json`, then
`state.py artifact --id <ID> --name scout-r<N>.json --file tmp/claude/<ID>-scout.json` and
`state.py receipt --id <ID> --phase scout --round <N> --model <label> --isolation inline --status ok --summary "<files> files, <effort>"`.
Both refuse a write that would overwrite (an artifact name already taken, a
terminal mission) — add `--force` only when redoing a phase is the intent.

`--scout-only`: show the scope and stop there. The pack stays open at the
scout — no execute, no terminal — so the operator can review the scope and
`--continue` it later.

`--inject-file PATH` replaces the scouting, not the phase: the file holds the
same scout JSON above, and you then take the same two steps — record it as the
`scout-r<N>.json` artifact and record a scout receipt whose summary says the
scope was injected. The pack therefore still shows what was agreed, which is
the whole point of the flag.

## 2. PLAN — only when justified

Run when `--plan`, or effort is Large/XLarge, or the previous evaluate returned
`needs_replan`. It earns its place by narrowing scope, not by running on a
bigger model: answer the plan boundary yourself and keep it to strategy.
Output JSON:
`{approach, phases: [{id, work, files, gate}], context_injection_points,
risk_factors, validation_checkpoints, confidence}`. In Harness, the plan must name the
canon STATUS row it updates, extend one policy owner (no parallel modules),
and name hermetic gate tests. Save as `plan-r<N>.json` artifact + receipt.

## 3. EXECUTE — injected context only

Write those five inputs to `tmp/claude/<ID>-execute-brief.md` and answer
**only** from it:

1. the scoped file list (kept under `--max-scope`; with `--scope-lock`, no files
   beyond round 1's list),
2. patterns + dependency map from scout,
3. the plan phase(s) for this round, verbatim,
4. scope boundaries (in/out) and the canon rules that apply,
5. last round's bar `improvements` (declared buckets + suggested actions) as the work queue.

Writes happen in a git worktree / feature branch, never on `main`.
Receipt: `--phase execute --model <label> --isolation inline`.

## 4. EVALUATE — separate verifier + Jev bar

Run the gates scout and plan named yourself (sequence step 5), then grade as a
**separate step**: write the success definition, the raw gate tails and the
diff range to `tmp/claude/<ID>-verify-brief.md`, and reach a verdict from that
brief and the diff alone — not from memory of why you wrote each line.

That is a fresh *input*, not a fresh *mind*: you graded code you also wrote, in
this session. Say so in the receipt (`--isolation inline`) rather than implying
independence you did not have. If the grade needs to be independent, stop and
ask the operator to open a new Freebuff task with the verify brief — that is
their call, not something this skill arranges.

Either way it re-runs the gates it can, reviews the diff, and returns
`{verdict: pass|fail, gates: [{cmd, ok, tail}], issues: [...], drift: bool}`,
recorded with `--phase verify`. A verifier that could not observe a gate result
must return `fail`.

If `--bar PHASE_ID` (or the mission maps to a canon row), run the Jev bar:

```bash
$PY -m harness.cli jev-phase --phase <PHASE_ID> --repo-root . --local-only --json --out tmp/claude/<ID>-bar-r<N>.json
$PY .claude/skills/isolated-mission/state.py bar --id <ID> --file tmp/claude/<ID>-bar-r<N>.json
```

(Drop `--local-only` only when the operator wants a live, paid Jev judgment.)
A `bar` record added after the mission is already terminal refuses unless
`--force`, so a closed mission's bar history stays the one the auditor read.
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

The loop runs by default, up to `--rounds N` rounds (3 by default). Continue when
the verifier failed or the bar has improvements AND rounds/budget remain. Stop
on: bar pass + verifier pass (`complete`), budget reserve refusal
from `state.py` (`blocked` — the dual budget said no), 2 rounds with no new
evidence (`stalled`), or scope drift past the `--max-scope` budget
(`needs_replan` → back to PLAN once, then stop). Close with:

```bash
$PY .claude/skills/isolated-mission/state.py terminal --id <ID> --outcome <complete|failed|blocked|stalled> --findings-file tmp/claude/<ID>-FINDINGS.md
```

Re-closing an already-terminal mission refuses unless `--force`: it would
overwrite both FINDINGS.md and the recorded outcome. **That correction is
legitimate** — a wrong findings document, or an outcome recorded before the last
round's evidence landed — so re-run the command with `--force` and state in
FINDINGS.md that it is a correction. What must not happen is the correction
arriving silently.

FINDINGS.md = what changed, gate evidence (raw tails), bar result, open
improvements, cost, and the isolation mode each phase actually ran in.
Then report to the user: outcome, pack path, and the remaining improvements if
any, with cost split two ways — (a) the pack budget (paid Harness lanes and
Jev calls recorded via `--cost`), and (b) what this session actually cost.
Inline phases share this session and have no independent cost: record them as 0
and say so rather than inventing a per-phase figure. Never report (a) as the
mission's total cost.

## Rules

- Freebuff only. No other agent runtime, no headless prompt runner, no second
  coding agent; `/isolated-request` is the one place isolation is handed off.
- One model per session; no phase routing. `--model` labels what ran, it does
  not choose; `--isolation` records how the phase was isolated. Grading is a
  separate step from building — and `--isolation` says whether it was also a
  separate mind, which under `inline` it was not.
- Each phase gets only its injection; never forward the whole conversation.
- 0-hallucination: bar buckets/actions come from the operator pack; never invent new ones.
- Fail ≠ approve. Blocked = exact command + output in FINDINGS, never "will do".
- Paid Harness lanes during a mission: cheap paid rungs, `HARNESS_USE_FREE=false`,
  and a private `HARNESS_LEDGER` when phases run concurrently.
