# Harness owner handoff — three JEV-P6 / Jev-client defects measured during a Jev-only audit run

> **All three fixed in #96 (merged 2026-09-27). One measurement outstanding.**
> See "Status as of 2026-09-27" below. Kept open only for that measurement.

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/Harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

This is an assist-only handoff. The owning repository retains all decisions, edits, merges, and publication authority.

## Status as of 2026-09-27 — all three defects fixed; one measurement still outstanding

The three findings below are **fixed on `main`**. They were implemented by the
owning lane in pull request **#96** (`fix/jev-p0-fixes`), merged
**2026-09-27T03:20:20Z**, and independently re-verified against the merged code
on `main` at `02d5f4286ba6064bc9ae84b32cbc1504e7b97f74`. This section was added
after the fact; the finding text below is preserved exactly as measured.

| # | Defect | Fix | Verified against the code |
|---|---|---|---|
| 1 | Score/expectation tolerance rejected correct provider output | #96 | Tolerance is now derived from the pack-declared `score_probability_precision`; the original pinning test is retained and still rejects; a new test still rejects above the new tolerance; a fully-keyed non-fallback live assessment returned `assessed` |
| 2 | Billed-but-discarded answers, 94.1% on a 7-criteria axis | #96 | A `probabilities` subset that normalises onto the declared criteria is recoverable, unmatched options recorded as `None` and never as an invented `0.0`; an undeclared option, a non-normalising subset, and a score-legend mismatch all remain fatal; `coverage.billed_but_discarded` and `spend.discarded_cost_usd` / `discarded_input_tokens` are reported and rendered |
| 3 | Any `HarnessError` reported as `stop_reason="run_budget"` | #96 | A spend refusal still reports `run_budget`; anything else reports `stop_reason="error"` with `stop_detail` carrying the exception type and message; `rate limit exceeded`-style errors stay out of the budget bucket |

The full suite on that merged `main` is green: **2,163 tests, OK, 46 skipped,
0 failures.**

**What is not yet done.** Requested action 4 below — a keyed re-run at
comparable volume reporting the post-fix billed-but-discarded rate — has **not**
happened, so the improvement is currently demonstrated at the mechanism level
and asserted at the rate level. The only keyed evidence committed since the fix
is a single live vision-assessment call (`HV0_LIVE_PILOT_2026-09-26.md`) and a
1,323-input-token self-audit aggregate; no committed artifact renders the
"billed but discarded" line, so no post-fix measured rate exists in the
repository. Requested actions 1, 2 and 3 are satisfied: the fixes are minimal,
each lives in the module that owns the behaviour, they landed through the normal
reviewed pull request path, and each carries the regression test this document
asked for. The regression test for Finding 2 pins the *accounting* of the rate,
which is what a hermetic test can pin; the *observed* rate on real traffic is
what remains to be measured.

These are findings about a revision, not about the product. The measurements
were taken on `2cf24b5`; re-check rather than trusting this table if `main` has
moved again.

## Context and measurement basis

- Measured on an isolated detached checkout of the current release candidate, commit `2cf24b569d9d73afaf78d489561ca0b79fc5490a`, which passed the full local suite (2,079 tests, OK, 46 skipped).
- The defects below were all observed during a large, fully-keyed, native-Jev run: **7,279 dispatched calls, 7,361,767 input tokens, 1,841,763 free output tokens, $0.030919 billed**, with every call recorded in the hash-chained ledger and cost arithmetic recomputed and matched exactly at the published rate of $0.0042 per million input tokens. No fallback model and no secondary provider participated in any call.
- Because the run was large, these are not anecdotes: each defect below has a measured rate over thousands of calls.

## Finding 1 — the Score/expectation tolerance rejects real provider responses, blocking HV-0

- `harness/jev_packs.py:326-331` in `validate_vision_assessment_answers` requires `abs(float(score) - expected_score) > 1e-4` to be rejected.
- The provider returns the `score` value rounded to roughly two decimals alongside separately rounded probabilities, so the identity `score == sum(level * p)` does not hold on real responses. The check therefore rejects correct provider output.
- **Measured impact.** The live Hourglass vision assessment on commit `2cf24b5` returned `status=unassessed`, `model=jev-1.13.0`, `usage_source=actual`, `input_tokens=14397`, `cost_usd=0.0000604674`, and failed with `assessment response is invalid: assessment Score differs from its probability distribution: modularity`. A native, non-fallback, correctly billed call was made and its answer thrown away. HV-0 cannot complete on this release.
- **The test pins the defect.** `tests/test_jev_vision_assessment.py:216-220` (`test_answer_rejects_score_inconsistent_with_distribution`) deliberately asserts the strict 1e-4 rejection, so a tolerance change must update that test in the same change.
- **Requested action.** Introduce a tolerance derived from the provider's documented output precision, keep a rejection path for genuinely inconsistent distributions, and update the pinning test with a case that still rejects a real inconsistency at the new tolerance. Confirm against a live call before marking HV-0 complete.

## Finding 2 — malformed answers are billed and then discarded, and the failure rate scales with the number of choice criteria

- `harness/jev.py:150-175` (`_parse_answer`) enforces a strict structural contract, notably that a `choice` answer's `probabilities` key set must equal the declared `criteria` key set exactly (`harness/jev.py:162-163`). `JevEvaluator.evaluate` catches the resulting `ValueError` and, at `harness/jev.py:200-210`, still records the provider's reported `usage`, so the call is **billed in full** while its answer is discarded and the caller silently degrades to the code-owned keyword fallback.
- **Measured impact, 7,279 calls.** 333 calls (4.6%) were billed and discarded, consuming $0.001369 of input tokens (4.4% of all spend) for zero signal. The rate is not uniform, and it tracks the size of the choice question:

  | choice criteria in the pack | calls | billed-but-discarded |
  |---|---|---|
  | 3 axes of 5-6 criteria (repo summary) | 7,276 | 4.3% |
  | 1 axis of 7 criteria (log triage pack) | 51 | 94.1% |

  A single 7-criteria choice discarded 48 of 51 answers. This is the most operationally significant finding in this handoff: **the stricter the declared vocabulary, the more reliably the tool pays for answers it throws away**, which inverts the intuition that a larger declared set is safer.
- **Secondary cost.** Each discarded call also produces a row whose axes come from the keyword matcher rather than the model, with an empty `axis_confidence` map. A consumer reading only the aggregate cannot distinguish a keyword classification from a model classification without checking the `is_fallback` flag, and the 94% case in particular would present as a nearly clean report.
- **Requested action.** Decide, per answer type, which mismatches are genuinely fatal and which are recoverable, and make the two behaviours explicit. Concretely: treat a `probabilities` key set that is a subset of, or normalizes onto, the declared criteria as a recoverable shape with the unmatched options recorded as `None` — the same honest-unmatched discipline the axis path already uses — rather than a hard rejection. Separately, decide whether a billed-but-unparseable response should be surfaced as a counter so the run reports its own effective coverage. Add a regression test that pins the current high-discard rate so the improvement is measurable.

## Finding 3 — the repo-summary driver labels any `HarnessError` as `stop_reason="run_budget"` and discards the message

- `harness/repo_summary.py:165-179` has three sites that set `stop_reason = "run_budget"`: a genuine `make_policy` budget refusal, a `_budget_refusal`, and a bare `except HarnessError:` that catches **any** error raised by `policy.evaluate_repo_summary` and records it as budget exhaustion.
- **Measured impact.** A run configured with a $0.25 cumulative run budget and a $0.10 per-governor ceiling stopped after 2,315 of 3,638 elements having spent **$0.0107**, and the envelope reported `stop_reason: run_budget` with 1,323 elements pending. The budget was 95.7% unspent, so the reported reason was demonstrably wrong and the actual error was unrecoverable from the artifact.
- The stop is not itself a data-loss bug: the judgment is never persisted, the append-only resume state stays valid, and re-invoking the same command resumed and completed the remaining 1,323 elements. The defect is diagnostic, and it is the kind that costs a great deal of time when a run is long.
- **Requested action.** Narrow the `except` clause to the specific refusal types, record the exception type and message in the envelope for any other case, and use a distinct `stop_reason` such as `error` so an operator can tell "the money ran out" from "something broke". A test that injects a non-budget `HarnessError` and asserts the envelope does not claim `run_budget` would pin this.

## Requested owner action

**Actions 1, 2 and 3 are complete** and are recorded above with the PR that
discharged them. One action remains:

1. Re-run a keyed audit at comparable volume after the fixes and report the
   resulting billed-but-discarded rate, so the improvement is demonstrated
   rather than asserted. The envelope now reports this figure per run, so the
   number should come from a normal artifact rather than a separate tally. A run
   in the same order of magnitude as the original (thousands of calls, not tens)
   is what makes the comparison meaningful; the 7-criteria axis that discarded
   94.1% of its answers is the case worth reporting, since a larger declared
   vocabulary is where the defect was worst.

## Stopping condition

Stop after one independent owner verdict per finding. The measurements above are
reproduced evidence, not a merge recommendation. The assist lane attempted no fix
of its own; the fixes recorded above are the owning lane's work in #96. This
document stays open only for the single outstanding measurement above, and can
be closed once that rate is reported.

## Handoff boundary

No repository changes are requested by this document. Findings outside this repository must be split into their own owner-scoped handoff before transfer.
