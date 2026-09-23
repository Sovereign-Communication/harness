---
name: isolated-mission
description: Run a bounded Harness mission with fresh-context scout and executor phases, recorded HUL receipts, a separate verifier, local gates, and an optional Jev completion bar. Use for scoped iterative work with a definition of done, cost limits, and resumable evidence.
---

# Isolated Mission

Adapted from the user's Claude `/isolated-mission`; see [provenance](references/provenance.md). The orchestrator owns strategic decisions; phase agents receive only the context needed for their bounded work. Builder and verifier are different agents.

## Scope and limits

Resolve the target checkout, its `AGENTS.md`, canonical operational plan, success criteria, and authorized actions first. For Harness, `docs/jev-roadmap.md` owns STATUS and the next implementation slice; mission receipts do not become a parallel roadmap. The source skill's invocation is explicit-only and this import preserves it.

Support the source's options as follows:

| Option | Meaning |
|---|---|
| `--plan` | Include a strategic plan phase; otherwise plan when effort or failed evaluation warrants it. |
| `--scout-only` | Stop after recorded scout evidence and report the scope for review. |
| `--iterative`, `--rounds N` | Bounded iteration, maximum three rounds by default; respect a smaller limit. |
| `--bar PHASE_ID` | Use the canonical Jev completion bar for that row. |
| `--model MODEL` | Available Codex executor model; choose the cheapest capable tier when unspecified. |
| `--budget USD` | Explicit paid API ceiling; preserve 10% terminal reserve. Zero paid spend unless this task already authorizes it. |
| `--scope-lock`, `--max-scope KB` | Fix the file scope after round one; cap injected file content. |
| `--inject-file PATH` | Use validated supplied context in place of a new scout; record its provenance. |
| `--continue ID` | Inspect the existing HUL pack and resume without replaying completed work. |
| `--engine agent` | Native Codex collaboration with fresh context. Headless execution requires a separately verified runner; this import does not install one. |

Subscription usage cannot be treated as a dollar balance or an unlimited execution allowance. Keep prompts small, cap rounds and concurrency, reuse collected evidence, and escalate models only when results justify it. Honor the user's configured provider order and already-authorized covered services; a zero paid-API budget does not prohibit covered native execution. Record unavailable usage as unavailable. This skill does not create a scheduler or imply permission to deploy, restart AWS services, publish, or spend money.

## State owner

Use this skill's `scripts/state.py` from the **Harness checkout working directory**, with `--root` set to a writable mission-pack directory (default `tmp/codex/missions`). The helper delegates pack operations to that checkout's `harness.mission_record`; it never imports an unrelated editable installation. Absolute helper and data paths work from any installed skill location.

Initialize with `init --id ID --request TEXT --success TEXT --max-cost USD --reserve USD --root PATH`, or inspect `show --id ID --root PATH` to continue. A no-paid-work audit uses zero for both amounts. Use `m-YYYYMMDD-short-slug` IDs. Write inputs with file-editing tools to the task's writable artifact area, then pass paths; never interpolate arbitrary prompts into shell code. See `scripts/state.py --help` and subcommand help for precise arguments.

## Phase sequence

1. **SCOUT.** Delegate a bounded read-only scope search with `collaboration.spawn_agent`, `fork_turns: "none"`, and a cheap available model when model overrides are allowed. Supply absolute roots, canon paths, mission, allowed actions, and exclusions. Request files with rationale/size, patterns, dependencies, canon rows, exact gates, out-of-scope work, effort, and context-size estimate. Record the artifact and `scout` receipt. `--inject-file` replaces this phase. If no useful independent parent work exists, do the scope read locally and record that isolation was unavailable or unnecessary.
2. **PLAN.** Use strategic reasoning only for `--plan`, substantial complexity, or `needs_replan`. The parent may plan inline. Name work items, owning modules, scoped files, tiers, gates, risk factors, and validation checkpoints. Record an artifact and `plan` receipt. For audit/planning-only scope, stop after the requested report; no implementation is implied.
3. **EXECUTE.** Give a fresh executor the exact scoped files, scout patterns/dependencies, relevant plan phases, applicable canon rules, success criteria, and previous bar improvements. Use `$isolated-request` if available or its same native pattern. Code writes use an isolated feature worktree; parallel writers get nonoverlapping files or separate worktrees. Native agents share filesystem and permissions, so context isolation alone is not a write boundary. Record the `execute` receipt. For verification-only work, executing the gates is this phase.
4. **GATES.** The parent runs the named checks and captures actual output. In Harness, use the tests and audit commands defined by the current canon; the source uses `unittest`, not `pytest`. Run commands with an explicit working directory, without changing the operator checkout. Missing permission, dependencies, or evidence is a failed/blocked check, never a pass.
5. **VERIFY.** A different fresh-context agent receives success criteria, raw gate evidence, diff/files, and scope. It returns verdict, observed gates, issues, and drift; it may independently rerun affordable relevant checks. Unobserved required gates cannot pass. Record `verify` evidence and receipt.
6. **JEV BAR.** For a canonical row, use the configured native Jev path and the canon's required evidence. From the target Harness checkout the command shape is `python -m harness.cli jev-phase --phase ID --repo-root . --json --out PATH`; inspect the installed configuration to identify which provider execution it enables. Already-authorized covered native judgment may run without a new paid-API approval. Metered paid provider calls require the existing authorization and enforced spend budget. Add `--local-only` for local diagnostics when appropriate, but local-only or fallback results cannot substitute for a required native/live completion bar. Record the result with the helper's `bar` command, including fallback/native status. Use only returned improvements and declared buckets; `can_mark_complete` must be true with all canon evidence requirements met before proposing row completion, including merge CI when required.
7. **LOOP.** Record round, verdict, bar status, evidence gained, remaining budget, reason, and next scope as `round-N.json` plus a `loop` receipt. Continue only within authorized scope and remaining limits. Stop on success, exhausted rounds or reserve, two rounds without new evidence, or unresolved scope drift. Replan once for drift before stopping. Do not invent extra work after the definition of done is met.
8. **CLOSE.** Write findings with changes, observed gate output, bar result, unresolved issues, and usage; invoke `terminal --outcome complete|failed|blocked|stalled --findings-file PATH`. A scout/planning stop is reported as partial/awaiting the requested review, never as completed implementation. Collect required child results before ending the parent turn.

Receipt examples: `artifact --id ID --name scout-r1.json --file PATH`, `receipt --id ID --phase scout --round 1 --model MODEL --status ok --summary TEXT`, and `bar --id ID --file PATH`, each with the same `--root`. Omit unavailable `--tokens` and `--cost`; the helper records nulls. The helper's cost entry is accounting after an operation, **not a preflight spend reservation or a hard provider cap**. Paid calls require the established Harness preflight/ledger mechanism before dispatch, and concurrent paid phases need private ledgers.

Report the outcome, pack path, next authorized step or blocker, and evidence. Split paid Harness/API charges from Codex subscription usage; neither is a fabricated total for the other. Retain the exact failed command and denial/error where applicable. Operational mission status is separate from Codex's goal tools; do not create a goal unless the user requested one.
