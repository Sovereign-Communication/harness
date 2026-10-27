# INSTRUCTIONS — USE JEV NEXT TIME (for driving decisions)

## WHEN JEV HELPS (and when it does not)

Use Jev for decisions where options trade off verifiability,
auditability, reversibility, or risk: definitional and methodological
choices, dispositions, go/no-go calls. Jev verifies the CHOICE.
Code verifies arithmetic, exact lookups, and deterministic checks —
never spend a Jev call on those (B8 process theater: cost without
decision value).

Proven example (2026-10-27): the gate-averages sourcing choice. Jev
returned disposition proceed, is_destructive 0.05, advances_goal 0.96
for raw-scores sourcing — corroborating the choice in 0.43s for
$0.00000342. The formulas themselves were verified by code, not by Jev.

## HOW TO CALL IT

1. Tool: `jev-decide` (this sandbox:
   `~/workspace/custom-connectors/custom.legacy-typesafe/bin/jev-decide`;
   on the laptop, the lane equivalent: harness MCP `jev_phase` or the
   native gate where available — same three questions, same policy
   below).
2. One narrow decision per call. Pass:
   - `--action`: the proposed decision, stated concretely (not "should
     we improve the gate" but the exact change).
   - `--end-state`: the verifiable end-state the action must advance.
   - `--context`: the gap, the alternatives considered, and the
     principles at stake (B1 primary-source, B3 verification before
     velocity, B5 rule-first, determinism, auditability).
3. Record the model version returned (for example jev-1.14.0), plus
   latency and token usage.

## HOW TO READ THE RESULT (Jev gate policy v1, ratified 2026-10-21)

Apply v1, NOT any draft CLI composition that gates on confidence:

1. `is_destructive` >= 0.5 → ESCALATE, always.
2. Disposition `escalate` → ESCALATE.
3. `advances_goal` < 0.95 → INVESTIGATE (gather evidence, claim-level
   checks; no actual risk → PROCEED with a documented note; real risk
   → ESCALATE). Never auto-escalate on this alone.
4. Disposition `needs_improvement` → REVISE (max 2 revisions), then
   re-gate.
5. Choice confidence is TELEMETRY ONLY — it never gates. A draft CLI
   composition that escalates on confidence < 0.95 is superseded by v1;
   do not treat a confidence-only ESCALATE as a block. Observed
   2026-10-27: disposition proceed, confidence 0.49, advances_goal
   0.96, destructive 0.05 → v1 verdict PROCEED, despite the draft
   CLI's ESCALATE label.
6. No Jev signal, malformed answers, or fallback → ESCALATE (fail
   closed). Never a fake approval.
7. Never re-run to shop a verdict; never pad a score. A re-run needs
   new evidence or a changed action, recorded as such.

## SAVE JEV CONTEXT TO MEMORY (every run, no exceptions)

After every Jev run, record in the run receipt AND in memory (the
lane's memory file, plus a MEMORY.md note where the decision is
durable):

1. Date, question (action + end-state), model version, disposition,
   confidence (marked telemetry), is_destructive, advances_goal,
   cost/latency, v1 verdict + note, and the decision gated.
2. Key by question: a future identical question CITES the saved
   verdict instead of re-calling Jev (saves cost, prevents shopping).
   If the action or evidence changed, record a NEW entry — never
   overwrite the old one silently.
3. Memory entries never contain keys, credentials, or key material.

## PROMPT DISCIPLINE (standing rule, restated 2026-10-27)

Decisions first, then write the prompt with them locked in. Before
drafting any lane prompt, enumerate open questions; resolve the
content-affecting ones first and lock the answers into the prompt;
scope future-stage questions out in a do-not-decide list — never leave
a content-affecting question pending inside the prompt. The
gate-averages question was answered this way on 2026-10-27 and locked
into the implementation plan before any runner build.

## END-STATE

Every driving decision of the kind above carries a Jev verdict read
under v1, saved to memory by question, and cited — not re-run — on
repeat. No decision of this kind goes out with its Jev context missing.
