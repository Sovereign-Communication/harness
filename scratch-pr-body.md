Closes the one element of HV-1 that had no implementation: **"avoid redundant calls when no decision is needed."** Every other element was already satisfied on `main`; this PR adds the missing one and repairs an honesty gap it exposed.

**Not merging.** Awaiting Lane 3's verification comment.

## Per-element HV-1 table

Evidence in the "already on `main`" column is read off `origin/main` (`b2c87e5`), not inferred. Test counts are from `tests/test_hourglass_jev_integrations.py` on each side.

| # | Contract element | Already on `main` — evidence | This PR |
|---|---|---|---|
| 1 | Five selectable typed dimensions | `HOURGLASS_STAGE_DIMENSIONS` (`jev_packs.py:672`) declares exactly `context_intake`, `plan_soundness`, `execution`, `consent`, `restart_target`. Pinned by `test_exactly_the_five_declared_dimensions_exist` and `test_every_dimension_declares_its_signals_and_matching_questions` (`StagePackDeclarationTests`, **4** tests) | — |
| 2 | Stable capability/site name + pack/version | `HOURGLASS_STAGE_SITE="hourglass_stage"`, `HOURGLASS_STAGE_PACK_ID="harness-hourglass-stage-v1"`, `HOURGLASS_STAGE_PACK_VERSION="hourglass-stage-v1"` (`jev_packs.py:659-661`); every envelope carries all three | — |
| 3 | Typed input/output schema, operator-declared criteria | `hourglass_stage_question_pack()` emits declared `_noul`/`choice` questions with criteria + instructions; read through official answer shapes only | — |
| 4 | No arbitrary plugin code / model-created category | `test_an_unknown_dimension_is_refused_not_invented` — unknown dimension raises rather than inventing a question set | Re-pinned for the new function: `stage_judgment_requirement` refuses the same inputs, so a caller cannot invent a dimension just to reach the skip path |
| 5 | Authority / fallback / confidence rules; never promote a fallback | `test_partial_answers_are_all_or_nothing`, `test_out_of_range_and_non_numeric_signals_are_refused`, `test_malformed_live_response_is_never_native_but_still_settled` | — |
| 6 | Budget/preflight + ledger evidence (one reservation, one settlement, one `jev_eval`) | `test_context_intake_keys_signals_and_settles_exactly_once`, `test_unkeyed_is_all_none_and_still_ledgered`, `test_transport_failure_is_all_none_and_ledgered_once` | **Skip path adds zero of all four** — no reservation, no dispatch, no settlement, **no `jev_eval` event**, because nothing was evaluated. Pinned by `test_a_suppressed_call_reserves_nothing_and_spends_nothing` and `test_every_dimension_can_be_suppressed` across all 5 dimensions |
| 7 | Declared restart-target choice among context/planning/execution | `HOURGLASS_STAGES` (`jev_packs.py:666`), `declared_restart_targets()`, `normalize_restart_target()`; pinned by `test_restart_vocabulary_is_the_three_declared_stages`, `test_restart_target_yields_only_a_declared_key`, `test_out_of_vocabulary_choice_is_reported_not_snapped` | — |
| 8 | Code enforces transitions, preserves completed work, renews consent | `validate_restart_request()` (`jev_packs.py:802`) — `RestartTransitionGuardTests`, **7** tests | — |
| 9 | Never a completion or readiness claim | `test_no_signal_is_ever_a_completion_or_readiness_claim` | Skip reports `judgment_required=False` + `result_state="not_required"`, never a signal |
| 10 | Name code-owned facts vs JEV semantic judgment | Partial: the dimension pack declares criteria/signals, but nothing decided *whether* to ask | **`HOURGLASS_STAGE_REQUIREMENTS` + pure `stage_judgment_requirement()`** (`jev_packs.py`) — the decision is declared data, not per-caller logic. `StageRequirementDeclarationTests` (**5** tests) |
| 11 | **Avoid redundant calls when no decision is needed** | **Nothing on `main`.** The only pre-preflight exit was the unkeyed path, so a keyed caller always paid a call per stage boundary | **The guard.** Conservative default (`required=True`); only two explicit code-owned facts suppress. `subject_supplied=False` → *"no subject state was supplied"*; `superseded=True` → *"an earlier judgment for this subject is still current"*. The skip returns **before** the preflight |
| 12 | Hermetic contract tests | **22** tests on `main`, all passing unchanged | **+12** → **34** |

## The honesty gap element 11 forced open

Building the guard surfaced a real defect in the pre-existing envelope: the unkeyed and pre-dispatch-refusal paths never said whether a call left the machine, so **an unavailable run was indistinguishable from one that was never needed**. Both now report `judgment_required=True, dispatched=False`; only the live path sets `dispatched=True`. A judgment that was required and could not be made is a different fact from one that was asked and answered.

Verifying through the real owner (not just tests) then showed `result_state` was only on the skip path's envelope, so `unavailable` and `judged` both read as `None` to a caller branching on `structural`. All three paths now report it, matching the value their ledger event already computed — which also removes a place where envelope and ledger could disagree.

Verified through `JevPolicy.evaluate_hourglass_stage` directly:

| | `result_state` | `judgment_required` | `dispatched` | calls | reservations | ledger |
|---|---|---|---|---|---|---|
| suppressed | `not_required` | `False` | `False` | 0 | 0 | `[]` |
| unkeyed | `unavailable` | `True` | `False` | 0 | 0 | `['jev_eval']` |
| judged | `judged` | `True` | `True` | 1 | 1 | `['jev_eval']` |

## Why the two facts are parameters, not state-dict lookups

The `state` shape is caller-owned and free-form, so inferring "is this unchanged?" from it would make the guard **silently inert for every real caller** — the failure mode where a check passes because it never fires. The caller states the fact; this owner only decides. That is also why the default is permissive: a caller that knows nothing about its own state still gets judged.

## Mutation-checked

Each mutation caught by its intended test, and only that test:

| Mutation | Caught by |
|---|---|
| guard never fires (`if False`) | 4 tests |
| skip claims `dispatched: True` | 5 tests |
| skip settles a `jev_eval` (claims it spent) | `test_a_suppressed_call_reserves_nothing_and_spends_nothing` |
| default flipped to skip (inert guard) | `test_the_default_is_required` |

## Deliberately not in this PR

- **Wiring into the calling code is Lane 2's** (#119). This owner exposes the decision and reads it; it does not decide when a real caller's facts hold. The `HV-1` canon row stays **in progress** for exactly this reason, alongside the live dogfood that is still unclaimed.
- Choosing a replacement for the delisted `inclusionai/ling-3.0-flash-fin:free` — a shipped-defaults/`MS-*` decision reported to Lane 3 on #120, and the reason the suite must be run hermetically below.

## Gates

- `tests/test_hourglass_jev_integrations` — **34 pass**; the **22 pre-existing pass unchanged**, verified by running the file on this branch and on `origin/main` and comparing per-class counts (`4 + 7 + 11` identical).
- Full suite in two halves, run **hermetically as CI runs it** (no ambient key): `test_[a-j]*.py` → **1356 OK** (62s), `test_[k-z]*.py` → **1016 OK** (160s). **2372 total**, each half inside the 600s budget.

One honest note on how those halves were run: with an ambient `OPENROUTER_API_KEY` the suite makes real network calls and exceeded the 590s cap on half A. CI's `hermetic suite` jobs carry no key, so running it that way is both the real gate and the fast path; the keyed live-catalog check is opt-in and is Lane 3's open item.
