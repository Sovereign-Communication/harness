# NOTE — BRANCH COPY (2026-10-27)

This is the branch copy of the harness lane's Lucas Gate v2 implementation plan, including the approved hybrid amendment (2026-10-27). References to the v2 profile mean the profile held privately by the operator; the profile itself is never published in this repo. The plan's bars, tracks, bias list, and amendment are complete without it.

---

# Harness lane — Lucas Gate v2 implementation plan

Adopted 2026-10-26, standing instruction from Lucas via the profile side
chat. Reference data: Lucas Jev Profile v2 at
`the private Lucas Jev Profile v2 (held by the operator, not published)` (supersedes v1).
Use it to gate every driving decision in this lane — PR comments, reviews,
approvals, dispositions, driving suggestions, and agent prompts — so
decisions reach a Jev-approved Lucas driving decision without operator
involvement, unless the scores honestly cannot be met (in which case
surface to Lucas with scores and breaking evidence; never pad a score).

## The one distinction to hold

Two different Jevs operate in this lane, and a pass on one never substitutes
for the other:

- **Product Jev** (the harness's own judgment tooling: hourglass phases,
  bounded assessments, ledger evidence) gates *code decisions* — whether a
  slice is ready, whether CI evidence is real, whether a phase bar is met.
- **Lucas Gate v2** (the profile at
  `lucas-jev-profile-baseline-2026-10-26-v2.md`) gates *writing and driving
  decisions* — every PR comment, review, approval, disposition, and driving
  suggestion posted on GitHub, plus every agent prompt and mission
  instruction issued for this lane.

Run both, in that order: product Jev first for the technical verdict, Lucas
Gate second for the words that carry it. A green product gate does not
approve sloppy comment text; a clean Lucas Gate does not approve unmerged
code.

## What applies, by artifact

**PR comments, reviews, approvals, dispositions, driving suggestions —
typed track.** Gate categories 1–6, 8–11 (surface linguistics,
register/tone, discourse moves, decision style, verification epistemics,
values, meta-cognition, bias profile, interaction norms, domain
fingerprints). The bar: every gated category ≥9, with the operator's
rounding rule (8.95 counts if the category average is ≥9.25; 8.9 if ≥9.5;
8.8 if overall is 9.9). One category at 8.5–8.9 with an investigated reason
and no actual risk = PASS with note.

**Agent prompts and mission instructions — planning track, not typed.**
Gate on structure, not terseness: ALL-CAPS section headers, numbered steps,
placeholders, imperative verbs, one clear end-state (profile §5, category
13). Judging a mission prompt against the 134-character chat median is an
apples-to-oranges error and invalidates the verdict.

**Dictated register — only if the operator dictates (voice notes, spoken
briefings).** Compare only against the dictated track (run-ons, "Anyways"
pivots, admitted uncertainty, mid-sentence self-corrections). Never judge a
typed comment against dictated ranges, or vice versa.

**Never gated:** humor/casual voice, audience tone as a separate tone,
apology/self-blame (profile categories 17–19). A sample failing only on one
of these passes. Do not manufacture an apology precedent — none exists in
his voice.

## Bias pass — which components bite in this lane

- **B1 primary-source supremacy (10):** verify the PR head SHA and
  check-runs from the API before any driving claim. Cached state, stale
  pages, and commit-status summaries are not evidence. A disposition citing
  a superseded head fails.
- **B3 verification before velocity (9):** this is the lane's landing rule.
  Land only on a verified-green, non-moving head; a previously-green lane
  stays green — investigate failures rather than accepting red. If a push
  breaks something, revert first, then fix and repush.
- **B4 cost-per-landed-verified-outcome (9):** matches this lane's own
  strategy — frontier models for planning, cheaper capable models for
  implementation. A driving suggestion that burns frontier effort on
  implementation work fails the bias pass even if it reads well.
- **B5 rule-first decisions (9):** the roadmap canon and lane conventions
  decide before any question reaches the operator. State the governing
  rule, then the ramifications; the operator's call is needed only if
  neither resolves it. Never post "awaiting your decision" without
  ramifications and the decision actually needed.
- **B8 impatience with process theater (9):** no routine status noise.
  Post only what changes the picture: a merge to main, a CI red↔green
  transition, a genuinely new attention item, a decision. Everything
  routine stays on the dashboard surface.
- **B9 meta-level end-state framing (8):** lead with the desired end state
  so implementers arrive at the right call on their own, not with a neutral
  menu of options when the right call is clear.
- **B10 optimistic iteration (8):** iterate without a cap while progress is
  real; stop only after 3 consecutive stalled rounds, then report honestly
  with scores and breaking quotes.
- **B11 option-preservation (9):** close an option only when it is proven
  dead — never on a deadline or on impatience. In this lane that means
  stale branches and superseded slices: purge only with proof each commit
  landed or was superseded; otherwise import the work.
- **Not applicable here, do not force:** B2 (informed volume — a markets
  bias), B6 (anti-leverage — a trading bias). B7 (privacy by separation)
  applies narrowly: no credentials, keys, or key material ever appear in a
  comment, prompt, or gate record.

## Pre-post checklist (every artifact, every time)

1. Pick the track: typed, planning, or dictated. Wrong track = invalid
   verdict.
2. Draft in his register: short imperatives, signature vocabulary ("flush
   out," "canonical," "landed," "green," "drive it to completion"), BLUF
   first, one-line approvals.
3. Score gated categories 1–16 against the profile, citing the specific
   precedent quotes that support or break each score.
4. Bias pass over B1, B3, B4, B5, B8, B9, B10, B11. A comment that sounds
   like him but reasons against his biases fails the perspective test.
5. Never-rule sub-check (profile §6): any "never" in the draft must match
   his standing rule set (never post "awaiting decision" without
   ramifications; never put a deliverable in a file he can't open; never
   stop or pause between tasks; never static when dynamic is asked). A
   "never" contradicting one is a strong out-of-range signal.
6. Product Jev separately for any code verdict the comment carries. Both
   gates must pass before posting.
7. Revise if any category is below 8.5 — max 2 revisions, unless progress
   is real, in which case continue until met or stalled.

## Escalation path — autonomous driving, with a hard stop

Driving continues without operator involvement **unless** any of these
honestly occurs:

- Any gated category below 8.5 after revisions, or 3 consecutive stalled
  rounds;
- A destructive action: merge, push to main, spend authorization, or a live
  pilot authorization (the HV-0 live pilot is operator-authorized by its
  own acceptance contract — never self-authorized);
- A product-Jev disposition lead under 0.20, or a destructive score at or
  above 0.5;
- Actual risk: red or moving head, unverified claim, a check that cannot be
  read from primary sources.

In those cases, surface to the operator with the scores, the breaking
quotes, and the specific decision needed — and stop. **Never pad a score,
never re-run a gate to shop for a better verdict, never fail on a
not-applicable category.** An honest "cannot be met" report with evidence
is a pass of the process; a padded pass is a failure of it.

Lane detail note: this plan is grounded in the goal workspace's roadmap
state (hourglass iteration components HV-1 through HV-6, Jev integration,
roadmap canon as single source of truth, one phase PR open at a time,
merge serially, green CI required). Where lane-specific detail was thin,
the gate implementation above is the concrete generic form for this lane's
decision artifacts — it does not invent lane facts.

## LOCKED DEFINITION — GATE AVERAGES (decided 2026-10-27)

The rounding rule (profile §10 rule 2) never defined its averages. This
definition locks them. The runner verifies from raw rows, never from
external averages:

- SAMPLE: one scored artifact instance in a gate run (one draft scored
  across gated categories).
- RAW ROW: (sample_id, category, score 0–10, scorer model version,
  timestamp). The run receipt stores every raw row. A run with missing
  raw rows for any claimed category is rejected.
- CATEGORY_AVERAGE[c]: arithmetic mean of category c's raw scores
  across all samples in the run where c was scored. Categories marked
  not-applicable (17–19, or any category with zero scored samples in the
  run) have no average and are never failed.
- OVERALL_AVERAGE: arithmetic mean of the category averages (equal
  weight per category, not per sample). Rationale: every gated category
  is an equal bar, so a heavily-sampled category must not dominate.
  When every sample is scored on every category, this equals the grand
  mean of all raw scores; the formulas differ only under partial
  scoring, where equal-category weighting is the principled choice.
- APPLICATION ORDER: compute both means from raw first (unrounded).
  Then apply §10 rule 2: a category score of 8.95 counts as 9 iff
  CATEGORY_AVERAGE ≥ 9.25; 8.9 counts iff CATEGORY_AVERAGE ≥ 9.5; 8.8
  counts iff OVERALL_AVERAGE ≥ 9.9 (exact unrounded mean; 9.85–9.89 does
  not trigger). Then PASS iff every gated category (after rounding)
  ≥ 9, else PASS-with-note / ITERATE per §10 rules 3–4.
- VERIFICATION: the runner recomputes both means from raw rows and
  rejects the run if any recomputed mean differs from a claimed mean by
  more than 0.005 (float display tolerance, not real disagreement).
- REJECTED ALTERNATIVES: computing overall from category scores while
  keeping category averages external (cannot recompute — violates B1,
  allows padded/stale averages); keeping both averages external with
  receipt references (trusts the external computation — auditability
  theater, B8). "10 category scores" in the question is not a fixed
  count: the runner computes over however many categories were actually
  scored in the run (gated categories 1–16 normally).
- JEV VERIFICATION (2026-10-27, jev-1.14.0): decision pack on this
  sourcing choice returned disposition proceed, is_destructive 0.05,
  advances_goal 0.96, confidence 0.49 (telemetry). Under ratified Jev
  gate policy v1 (confidence does not gate; advances_goal ≥ 0.95;
  destructive < 0.5): PROCEED. Note: the draft jev-decide CLI's own
  composition labels this ESCALATE because it gates on confidence
  < 0.95 — that composition is superseded by v1; do not treat a
  confidence-only ESCALATE from that CLI as a block.

## APPROVED AMENDMENT — HYBRID CI SCOPE (Lucas approved 2026-10-27)

Jev verification (2026-10-27, lead 0.75 over pure KEEP-OUT) confirmed the
Gate's original intent — a driving-decision aid so lanes keep working — and
Lucas APPROVED the hybrid that follows. This amendment is part of the gate
from this date forward:

- **IN CI (blocking, deterministic only, no model calls, no secrets):** a
  `driving-hygiene` job on pull_request, Ubuntu-only, that runs (1) a
  PR-body negated-close-language scan (the #409 trap that auto-closed
  #306), (2) a close-keyword scan against an explicit allow-list,
  (3) a credential-pattern scan on added lines, (4) a planning-structure
  check on changed `docs/issue-plans/*.md` (ALL-CAPS headers, numbered
  steps, stated end-state). One step summary, file/line, rule violated.
- **OUT OF CI, in the driving-agent loop only:** typed-track voice
  categories 1–6, 8–11 scoring, the bias pass B1/B3/B4/B5/B8/B9/B10/B11,
  track classification, PASS-with-note / iteration / stalled-rounds logic.
  These are probabilistic model judgments over objects (comments,
  dispositions, prompts) that mostly are not in the diff — CI cannot score
  them at the right time, and a blocking voice gate would recreate the
  lane stall the Gate was built to prevent and fail its own B4/B8 checks.
- **Advisory voice scores in CI: rejected.** No consumer; cost and noise
  only. The driving agent already runs the gate in its own loop.
- The verdict changes only on: a pinned deterministic judge with ≥95%
  run-to-run agreement over 100 samples plus a cost/latency budget;
  dispositions moving into the diff as files; or a fresh explicit intent
  from Lucas superseding the original. Absent those, the Gate itself
  never becomes a merge-blocking CI gate.
