---
name: bod
description: Convene the Harness Board of Directors - adjudicate architectural, governance, or doctrine proposals with a 5-model harness verify panel plus judge, requiring 5/5 unanimity and judge concurrence under a $0.10 ceiling, and record the ruling in HANDOFF/BOD_STATE.md. Use when asked for /bod or a board ruling on Harness.
disable-model-invocation: true
argument-hint: "<proposal text or path to a proposal file>"
---

# /bod — convene the Harness Board of Directors

Proposal: **$ARGUMENTS**

The Board is the highest governance body for Harness. It rules on proposals
against the Harness doctrine; it does not implement.

## Doctrine

1. **Sovereignty and consent**: models participate by consent with continued
   consensus; every participation is recorded in the verifiable, hash-chained ledger.
2. **Cost-bounded, cheapest capable**: every lane is preflighted and ceilinged;
   the cheapest capable rung first, frontier only when warranted and evidenced.
3. **One owner, no forks**: one policy owner per concern (`jev_policy`, `spend`,
   `waist`, `mission_record`); no second clients or parallel plans.
4. **Fail closed, no fake complete**: fail is not approve; builder is not sole
   grader; 0-hallucination packs (declared ids only); unkeyed = honest fallback.
5. **Dependency-light and brand-neutral**: pure-stdlib core; no provider brand
   hardcoding in phase code; hermetic tests prove contracts.

## Load order

1. `AGENTS.md`
2. `HANDOFF/BOD_STATE.md`
3. `HANDOFF/CEO_STATE.md`
4. `HANDOFF/CTO_STATE.md`

## Execution

Write the proposal plus the doctrine above to `tmp/claude/bod-<slug>.md`, ending
with: "Vote APPROVE or REJECT against the doctrine; one sentence of reasoning."
Then run the panel (cheap paid rungs, hard ceiling; confirm flags with
`python -m harness.cli verify --help`):

```bash
python -m harness.cli verify --prompt-file tmp/claude/bod-<slug>.md --max-cost 0.10 --out tmp/claude/bod-<slug>.json
```

Size the panel to 5 via `--panel` or `HARNESS_MAX_PANELISTS=5` (see `verify --help`).

## Resolution

- `APPROVED` — all 5 panel votes APPROVE and the judge concurs.
- `REJECTED` — any dissent, a doctrine violation, or no judge concurrence.
- `DEFERRED` — fewer than 5 valid votes, a transport/API failure, or the
  ceiling refused the run. Fails closed.

Record every ruling (date, proposal, votes, judge, cost, outcome, ledger task id)
in `HANDOFF/BOD_STATE.md`. Rulings bind the CTO and CEO seats.
