# Lucas Gate v2

`lucas_gate_v2` is a read-only deterministic validator for a caller-produced
typed-track scorecard. It does not load an operator profile, make semantic
judgments, call Jev or another model, or persist the assessment evidence. The
calling agent owns the semantic review and supplies the scorecard.

## Private profile handling

When Lucas provides a local profile path, the calling agent reads that file
locally before making profile-based judgments. The path and profile contents
must stay out of repository files, tool output, logs, and commits. Pass only
the minimum assessment artifact and concise paraphrases of evidence or
precedent; do not send the full profile to the runner.

If the profile is unavailable, continue the semantic gate against the explicit
mission, issue specification, and current source evidence. Record that the
private profile was unavailable. Do not infer personal preferences from its
absence, and do not claim a private-profile review or typed voice-suite pass.
Dimensions with no relevant evidence may be marked not applicable; that does
not fail the category. A typed assessment with no applicable categories
returns `not_applicable`, which is neither a pass nor a failure; use the
semantic gate form for the decision.

## Scorecard contract

The request includes the ten category IDs `1`–`6`, `8`–`11` and all eight
bias checks `B1`, `B3`, `B4`, `B5`, `B8`, `B9`, `B10`, and `B11`. Applicable
categories require a score, category average, evidence, and precedent. A
not-applicable category uses `applicable: false` plus a reason and must not
include scores or evidence. `overall_average` covers applicable categories
only. Bias checks remain required; their floors are unchanged.

The applicable category bar remains 9.0. The existing rounding exceptions
remain unchanged: 8.95 requires a category average of 9.25, 8.9 requires 9.5,
and 8.8 requires an overall average of 9.9. One category from 8.5 through 8.9
may pass with an investigated reason when there is no actual risk. Bias floors
remain B1 10; B3, B4, B5, B8, and B11 9; B9 and B10 8. The runner reports
failing IDs and applicable/N/A IDs, but never echoes the supplied evidence or
profile-derived reason.
