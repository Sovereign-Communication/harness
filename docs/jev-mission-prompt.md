# Mission prompts — Claude Code

**Canon:** [jev-roadmap.md](jev-roadmap.md) only. Trust **origin/main** STATUS;
a stale local copy is not evidence.

These run the canon through `/isolated-mission` (Haiku scout → Opus plan only
when justified → cheapest execute → separate verifier + Jev bar → loop). Mission
state lands in `tmp/claude/missions/<id>/` (HUL pack: receipts, dual budget,
Jev bar evals, FINDINGS). Context: [claude-context.md](claude-context.md).

---

## Next open STATUS row (use this now)

Interactive (paste into Claude Code at the repo root):

```text
/isolated-mission --iterative --rounds 3 --bar <ROW-ID> --budget 2
Implement canon row <ROW-ID> from docs/jev-roadmap.md "Next implementation slice".
Worktree off origin/main (feat/<slug>); extend the one owner named in the
tracker; hermetic tests named in canon; no provider brands in phase code;
cheap paid rungs for any live dogfood (HARNESS_USE_FREE=false). Loop until
`harness jev-phase --phase <ROW-ID> --local-only` passes the bar with the
verifier green; then open the PR and paste gate tails + the bar output.
```

Unattended (scheduled / cloud / CI shell):

```bash
claude -p --model opus --max-budget-usd 10 --permission-mode acceptEdits \
  "/isolated-mission --iterative --rounds 3 --bar <ROW-ID> --budget 2 Implement canon row <ROW-ID> ..."
```

Headless sessions ignore project allow rules until the workspace trust dialog is
accepted once; pass `--allowedTools` for gate commands otherwise.

---

## Full canon loop (operator)

```text
/isolated-mission --iterative --plan
Harness mission. Read docs/jev-roadmap.md only (origin/main). Execute every
open STATUS row in "Next implementation slice" order: worktree → gates +
audit BAR MET → Jev bar pass → PR → merge only when CI green → post-merge
verify → update canon STATUS with evidence → next row. Dogfood user-facing
lanes on cheap paid rungs. No brand hardcoding in phase code. Stop only when
blocked (STATUS + exact evidence) or every Exit row is true.
```

## Whole-board bar check (no mission)

```bash
python -m harness.cli jev-phase --all --repo-root . --local-only
```

Any phase listed under `false_complete` claims complete in STATUS but fails the
bar — its `improvements` are the work.

---

## Retired prompts

The Freebuff `RELAUNCH …` paste prompts and `savedMissions` JSON (P1, P2 repair,
P3/HUL-A/HG) are obsolete — every phase they targeted has merged. See git
history of this file for the originals.
