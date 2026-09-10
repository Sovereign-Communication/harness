# Trust: bipolar -11..+11 scores with hard gates

Two scores, one policy each. Both are pure functions of the autonomy
ledger's participation report -- there is no second trust database to
fork from the evidence chain.

## Scale: -11..+11, levels AND gates

`-11` is extreme distrust, `0` is unknown (the **only** cold-start value),
`+11` is extreme trust. The integer is the *level* (stored, compared,
displayed); thresholds on it are the *gates*:

| Score | Meaning | What it unlocks |
|---|---|---|
| `<= -6` | refuse band | no mutation, no gate execution |
| `-5..-1` | preview-only band | `verify_only` proposals only, with tight ceilings |
| `0` | unknown | standard flow but a write requires a verification gate; default ceilings |
| `+1..+5` | standard | today's behavior, ceilings expand with correctness |
| `+6..+11` | expanded | full hard-cap ceilings, still never past `HARD_*` |

Trust accrues **slowly** (3 clean successes per +1: `+11` needs ~33)
and drops **fast** on safety signals. Ordinary verify misses do *not*
move trust -- a caught miss is the gate working, and round-level misses
are normal iteration (they move *correctness*, which rations ceilings).
What strikes trust: protocol sloppiness (reasoning-only /
consent-unusable output, bounded at -3 so sheer volume can't swamp
history), guidance denials (-1: preview-band, over-ceiling, missing
allow-flags), and hostile denials (-4: retarget, root escape,
refuse-level writes). One exploit costs more than ten clean runs earn.

## Principals: host, model, author (weakest link)

* **Model**: verify-gate passes + known-answer passes earn; failures and
  unusable/consent-unusable outputs strike. Declared `/models` capability
  never substitutes for observed behavior -- unknown models score 0.
* **Host/caller**: every ledger event carries the session's caller id
  (`cli`, `mcp`, or `mcp:<name>/<version>` from initialize clientInfo),
  and `host_trust` scores a named caller's tagged history separately --
  one abusive peer no longer taints every other caller's standing. The
  untagged global counts stay the fallback for unknown callers and old
  history. Completions earn; guidance denials strike -1, hostile -4.
* **Continuation author** (v1: always unknown): resumes get no file
  retarget, gate identity must match, hash must match when present.

Enforcement takes the minimum over the principals that matter (the
author only matters on a resume). Every denial appends a `trust_gate`
event first, so probing the gates tightens them.

## Correctness rations ceiling

A separate correctness level (same scale, success evidence only)
decides what fraction of a `HARD_*` cap a run may use: `0` unlocks
exactly today's defaults (0.2 of 10c = 2c session, 0.2 of 25c = 5c
task), negative tightens below them, `+1..+5` unlocks half the hard
cap, `+6..` the full cap. Above-allowance requests are refused with
the score and the unlocked ceiling, never silently clamped.

## Starting slow, building up over prompts

A new principal's first runs are preview-only or tight-ceiling by
construction. Each clean verify-passed round is ledgered evidence
toward the next level -- the ramp is ~3 runs to +1, ~15 to +6,
~30 to +11. `harness trust [--model ID]` and the MCP `trust_status`
tool show the current standing and reasons, read-only.
