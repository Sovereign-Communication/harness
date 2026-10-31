# Canonical Jev Gate Policy v1

Modeled on harness's `compose_decision_verdict` (`harness/jev_packs.py`), with flexibility for iterated work.
Ratified: 2026-10-21 (Lucas). Replaces the absolute 0.95 Choice-confidence bar.

## Core Principle

**Gate on risk, not on numbers.** A low score starts investigation. Only actual risk blocks.
Choice confidence is distribution concentration (telemetry), not permission. Noul probabilities
are real probabilities but are still subject to evidence-thoroughness bias — a low Noul triggers
investigation, not automatic escalation.

## Decision Gate (should we take this action?)

Rules in order:
1. No Jev signal (fallback) → **escalate**. Fail closed.
2. Missing or malformed answers → **escalate**. Fail closed.
3. `is_destructive` ≥ 0.5 → **escalate**. Destructive always escalates, at any confidence.
4. Disposition == "escalate" → **escalate**.
5. Disposition lead < 0.20 → **escalate**. Ambiguity test: no clear front-runner.
6. `advances_goal` < 0.95 → **investigate** (do NOT auto-escalate).
   - Gather more evidence, run claim-level verification.
   - If investigation finds no actual risk → **proceed** with documented note.
   - If investigation finds real risk → **escalate**.
7. Disposition == "needs_improvement" → **revise** (caller fixes and re-gates, max 2 revisions).
8. Otherwise → **proceed**.
- The disposition's `confidence` is reported as telemetry but does NOT gate.

## Assessment Gate (5-aspect suite on work product)

- Full 5 aspects: completeness, confidence, correctness, no-regression, evidence quality.
- Relevance-first: irrelevant aspects marked IRRELEVANT with reason, never scored.
- **Flexibility rule**: If 4/5 aspects ≥90 and the 5th is 85–89:
  - Auto-investigate the low aspect (do not ask).
  - If no actual risk found → **PASS** with documented note explaining the investigation.
  - If real risk found → **BLOCK** until fixed.
- **Single-point rule**: A single aspect at 85+ never blocks alone if investigation clears it.
- Any aspect below 85 → block, fix, re-assess.
- 3 consecutive stalled rounds (no progress) → report honestly with scores.

## Below-Bar Auto-Investigation (standing)

When any score comes back below bar:
1. Identify the uncertainty drivers (which aspect? what evidence is missing?).
2. Gather the missing evidence (read the diff, check CI, verify claims).
3. Re-assess with claim-level verification where possible.
4. Only escalate to human if investigation surfaces ACTUAL RISK, not just low confidence.

## Local Verification (2026-10-21)

Tested on harness PR #312:
- Destructive: 0.19 (pass, < 0.5)
- Disposition: proceed, lead 0.98 (pass, > 0.20 margin)
- Advances_goal: 0.87 (below 0.95 → investigate)
- Investigation: claim-level verification confirmed backward compatible, zero secrets,
  tested (89-line test file), 12/12 CI green, non-destructive.
- **Verdict under v1: PROCEED** (investigation cleared, no actual risk).
- Under old policy: ESCALATE on 0.74 Choice confidence (methodological artifact).

The v1 policy produces the correct verdict where the old policy escalated on noise.

## Code counterpart (2026-10-29, issue #325)

The assessment gate is implemented in `harness/jev_assessment.py` and runs as
`harness assess --input <judgement.json>` (JSON in, verdict out; exit 0 pass,
1 investigate/block/stop, 2 malformed). It answers for lane-workflow
dispositions only: `authority` in every verdict says so, and
`production_gate` says the audit and phase-completion bars are unchanged.

**Aggregation rule (declared, because the policy's point scores and a live
Jev distribution are different objects):** an aspect is in a band when that
band holds a strict majority (> 0.5) of the declared distribution. A tie or
distribution with no strict majority is `UNCERTAIN` and is investigated; it is never converted
into a point score, and the full distribution stays in the verdict.

**Disposition ladder, in order:**

1. three stalled rounds (`stalled_rounds >= 3`) -> `STOP`, scores reported;
2. any named actual `risk` -> `BLOCK`, at any score;
3. a majority `<85` -> `BLOCK` (fix and re-assess; investigation does not
   excuse a below-bar aspect);
4. a majority `85-89`, or `UNCERTAIN` -> `INVESTIGATE` until the aspect
   carries an investigation finding; then `PASS_WITH_NOTE` when no risk was
   named. Four green plus one flex and three green plus two flex take the
   same path, which is the flexibility rule and the single-point rule;
5. five green -> `PASS`.

Relevance-first is enforced in the input: an irrelevant aspect must carry a
reason, is excluded from the disposition, and cannot be scored. All five
aspects must be present -- an unjudged aspect is a refusal, not a smaller
denominator. A `composer` object supplied by the caller is carried through
untouched under its own key; a v1 `PASS` never rewrites it, so a lane can
publish both outputs without claiming the production composer proceeded.
