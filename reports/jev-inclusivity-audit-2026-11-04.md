# Jev inclusivity audit - harness lane - 2026-11-04

Audited head: `89cd74c` (`lane/burndown`). This report and its cure land in
the commit that carries this file; the evidence post on `#442` names its SHA.

Scope: STEP 0 of the owner steering posted on `#442` at 2026-11-03 23:36 HST.
The audit is source-and-receipt based: no live TypeSafe call was made and no
operator key was spent.

## Verdicts

| # | Surface | Verdict | Anchor |
|---|---|---|---|
| 1 | Decision-gate v1 fidelity (code) | PASS | `harness/jev_packs.py:2506` |
| 2 | Operative prompt copies | PASS for prompts; one stale evidence doc | `docs/jev-calibration.md:3` |
| 3 | Direct-TypeSafe call path | PASS | `harness/jev.py:555` |
| 4 | Lucas Gate v2 actually-run rate, last 10 decisions | FAIL: 0/10 receipts | `harness/lucas_gate.py:217` |
| 5 | jev-complete vs driving-hygiene vs gate-v1 | PASS, distinct | `harness/jev_completion.py:1`, `scripts/driving_hygiene.py:1`, `harness/jev_assessment.py:1` |
| 6 | Receipts + Rule 8 | PARTIAL; Rule 8 undefined | `AGENTS.md:82`, `docs/JEV_GATE_POLICY_V1.md:26` |
| 7 | #325 flexibility rule | PASS | `harness/jev_assessment.py:273` |
| 8 | No routing through the product provider system | PASS | `harness/jev.py:21`, `harness/jev_policy.py:4011` |

## 1. Decision-gate v1 fidelity - PASS

`compose_decision_verdict` (`harness/jev_packs.py:2506-2616`) applies the
policy's eight rules in order: fallback and malformed answers fail closed
(`:2563-2575`), `is_destructive >= 0.5` escalates (`:2576`), `escalate`
escalates (`:2581`), a lead below the 0.20 margin escalates (`:2583`), an
`advances_goal` below 0.95 investigates (`:2589`), `needs_improvement`
revises at most twice then escalates (`:2596-2607`), otherwise proceed
(`:2608`). Choice confidence is reported and never gates, matching
`docs/JEV_GATE_POLICY_V1.md:27`.

Tests pin each rule, including the two signals that used to be confused:
`tests/test_jev_decision_gate.py:176` (confidence alone no longer gates),
`:218` (below-bar advances investigates), `:232` (investigate is not a soft
proceed), and `:144` (a lead exactly at the margin does not escalate, #326).

Recorded hazard, not a behavior defect: the constant is still named
`DECISION_CONFIDENCE_THRESHOLD` (`harness/jev_packs.py:2330`) although the
composer applies it to `advances_goal`. The comment beside it already says
it is not used on the disposition Choice. Renaming it is API churn across
`jev_policy.py` and two tests; it is recorded here so no reader mistakes the
name for a confidence gate. The copy that did make that mistake is cured in
section 2.

## 2. Operative prompt copies - PASS for prompts; one stale evidence doc

Checked: `docs/jev-mission-prompt.md`, `CLAUDE.md`,
`.claude/skills/isolated-mission/SKILL.md`, and
`.claude/agents/harness-scout.md`, `harness-implementer.md`,
`harness-verifier.md`. None states a Choice-confidence bar. The mission
prompt's `--bar <ROW-ID>` resolves to the phase-completion bar
(`harness jev-phase`), and the verifier runs named gates plus that bar;
neither presents `confidence` as permission. `docs/hourglass-freebuff-prompts.md:34`
names a pack-declared confidence threshold for `review_required` in the
hourglass design assessment; that is a different gate and is not counted as
a decision-gate copy.

DEFECT: `docs/jev-calibration.md:3-5` says "the 0.95 fail-closed threshold
exceeds Jev's observed calibration band, so every judgment escalates". That
was true of the pre-v1 audit tool, not of the ratified gate: v1 never gates
on Choice confidence, and 0.95 is the `advances_goal` Noul bar. Uncorrected,
the document reads as an operative statement that the current gate escalates
on confidence and that lowering a confidence bar is the cure.

Cure: a supersession note at the top of the document naming the distinction
and pointing at the policy and the composer. `tests/test_jev_doc_fidelity.py`
fails if the note is removed, if the policy's telemetry sentence is removed,
or if any operative prompt starts stating a confidence minimum as permission.

One more confidence floor is recorded so it is not mistaken for this gate:
the apply lane's structural check still requires a configured confidence at
or above its floor (`harness/jev.py:90-93`, `harness/apply_gate.py:203`).
That is a separate production gate, not the v1 decision gate; it is not
changed here.

## 3. Direct-TypeSafe call path - PASS

One evaluator, one endpoint, one wire: `harness/jev.py:555-575` constructs
`JevEvaluator` with `https://api.typesafe.ai/v1/systemone` and the shared
`HttpTransport` (`harness/jev.py:21`). `JevPolicy` is built only by
`policy_for` (`harness/jev_policy.py:4011`); the lane call sites are
`harness/cli.py:554,586,626,662,675,880,999,1300,1736`,
`harness/conversation_lifecycle.py:382`, `harness/edit_lifecycle.py:206`,
`harness/execution_stages.py:309`, `harness/hourglass_lifecycle.py:565`, and
`harness/jev_completion.py:1599`. Every one composes the same policy owner.

No Jev module imports `chat` or the provider routing table for its wire path.
No `harness/jev*.py` module imports `chat` at all. `routing_table` appears in
one local import at `jev_policy.py:3947`, inside `evaluate_economics_route`,
where it supplies the deterministic tier baseline before the judgment is
dispatched through `evaluate_typed_pack`; the other matches are plain
filename strings in `jev_completion.py:174,177` (a required-tests list), not
imports. The harness delta holds: Jev calls TypeSafe directly and does not
route through the product's own provider system.

## 4. Lucas Gate v2 actually-run rate - FAIL, 0/10

Method: the sample is the ten most recent lane-driving commits (`89cd74c`,
`f2e7194`, `f2c9faa`, `a2058f7`, `8b48d52`, `7ee0bf0`, `5766356`, `21bfe18`,
`d1710c6`, `9a0037b`). For each, the commit body, the lane's status and
evidence comments on `#442`/`#295`, and all repository artifacts were
searched for the runner's own signature: `validation:
"deterministic_scorecard_contract"`, `artifact_sha256`, and
`product_jev_run` (`harness/lucas_gate.py:217-239`).

Result: 0 of 10 carry a runner output. At `89cd74c`, the only occurrences of
`artifact_sha256` were `harness/lucas_gate.py` and
`tests/test_lucas_gate_v2.py`; no commit, comment, or evidence artifact
carried one. The prose score lines in `docs/issue-plans/421.md:9-11` and
`docs/issue-plans/293.md:38,44` are caller-written text with no digest, so
they are not evidence that the deterministic gate ran. `AGENTS.md:89`
requires "Lucas Gate v2 first ... on every comment"; the measured rate over
this sample is zero, and no receipt can be reconstructed after the fact
because the runner is stateless by design (`docs/lucas-gate-v2.md`).

Disposition: process defect, not a code defect. The runner itself is correct
and tested. Enforcing a receipt requirement (for example in
`scripts/driving_hygiene.py`) would be a gate change and stays parked under
the owner-reserved gate swaps. The finding is recorded rather than cured.

## 5. The three bars are distinct - PASS

- Phase completion: `harness/jev_completion.py:1-7` (hard mechanical facts
  plus a bounded semantic 0-100 judgment; `PHASE_COMPLETE_MIN_SCORE = 85.0`),
  surfaced as `harness jev-phase` (`harness/cli.py:2166`) and as the mission
  skill's `--bar`.
- Driving hygiene: `scripts/driving_hygiene.py:1-6` - deterministic PR
  checks using only the event payload, local Git, and repository files; no
  network and no model. CI job at `.github/workflows/ci.yml:121-138`.
- v1 assessment gate: `harness/jev_assessment.py:1-6` - five aspects for
  lane-workflow dispositions, with `AUTHORITY` and `PRODUCTION_GATE` in every
  verdict (`:50-57`); `harness assess` is its CLI.
- Decision gate: `compose_decision_verdict` (section 1).

No module returns another's pass as its own. `jev_assessment` explicitly
states it is not the audit gate, the phase-completion gate, or a
`compose_decision_verdict` replacement, and a supplied composer verdict is
carried untouched (`harness/jev_assessment.py:176-186`).

## 6. Receipts + Rule 8 - PARTIAL

Receipts: this wave's evidence posts carry the head SHA and raw gate counts
(`#426` comment 6627549708; `#270` comment 6627793182). The `#425` self-audit
has no full four-dimension receipt and is recorded as a partial on `#425`,
not as a pass. Lucas receipts are 0/10 (section 4).

Rule 8: at `89cd74c` the lane had no literal "Rule 8" (`grep -rn "Rule 8" .`
returned zero; this report's quotations are the only occurrences now). Two
candidate referents exist and neither is a receipts rule:
`AGENTS.md:82` (rule 8: uncommitted work is at-risk work, push safety) and
`docs/JEV_GATE_POLICY_V1.md:26` (decision rule 8: otherwise proceed). The
steering's Rule 8 definition is therefore UNCOVERED: it must be supplied
before compliance can be claimed. This report does not guess at it.

## 7. #325 flexibility rule - PASS

`harness/jev_assessment.py:273-296` implements the declared ladder: a
majority below 85 blocks even with a clearing investigation (`:273`), flex or
uncertain aspects investigate until an investigation finding exists and then
pass with a note (`:279-295`), three stalled rounds stop, and a named risk
blocks at any score (both in the same ladder, `:240-270`). Tests cover four
green plus one flex (`tests/test_jev_assessment.py:92`), three green plus two
flex (`:114`), below-bar with investigation (`:127`), uncertain then cleared
(`:135`), and the narrow authority (`:162`). The roadmap's three-green plus
two-flex example (`docs/jev-roadmap.md:354-357`) describes this path, not an
auto-pass.

## 8. Uncovered surfaces

- No live TypeSafe call was made; "actually-run" is measured from receipts.
  A live call would spend the operator's key and would not make the record
  more inclusive.
- The last-10 sample is commit-based. If oversight means a different
  decision ledger, the rate must be recomputed against that ledger; the
  sample is named above so the recomputation is mechanical.
- The steering's "Rule 8" definition is absent (section 6).
- The BigEnergyCo and CivicScope reports were not re-read; this is the
  harness lane's report only.

## Cures in this change

- `docs/jev-calibration.md`: supersession note (the 0.95 is the
  `advances_goal` bar, not Choice confidence; v1 reports confidence as
  telemetry).
- `tests/test_jev_doc_fidelity.py`: three checks - the policy's telemetry
  sentence, the calibration note, and the absence of a confidence minimum in
  the operative prompts. Dropping the note or adding "confidence >= 0.95" to
  a prompt fails the suite.

## Reproduction

```bash
git rev-parse --short HEAD
grep -n "reported as telemetry but does NOT gate" docs/JEV_GATE_POLICY_V1.md
grep -rn "artifact_sha256" .            # zero outside the runner and its tests
python -m unittest tests.test_jev_doc_fidelity tests.test_jev_decision_gate tests.test_jev_assessment
```
