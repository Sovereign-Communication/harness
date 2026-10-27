# DRIVING PROMPT — STAGE, VERIFY, THEN ENFORCE THE SHADOW GATE

## OBJECTIVE

Land the deterministic shadow gate (`driving-hygiene`) in CI in three
stages: ADVISORY first, VERIFY against fixed cases, then ENFORCE
(blocking). The shadow gate is deterministic only: no model calls, no
secrets, Ubuntu-only. It NEVER judges voice, tone, or bias — those stay
in the driving-agent loop (Lucas Gate v2). Any proposal to add judgment
to this job needs a fresh explicit decision from Lucas; do not expand
its scope on your own.

## STAGE A — ADVISORY PR (do this FIRST)

1. Move `docs/issue-plans/shadow-gate/driving-hygiene.py` to
   `scripts/driving-hygiene.py` unchanged, except fixing the target path
   references in its docstring if any.
2. Merge the job snippet from
   `docs/issue-plans/shadow-gate/driving-hygiene-job.yml` into
   `.github/workflows/ci.yml` as a new `driving-hygiene` job, WITH
   `continue-on-error: true` (advisory). Confirm the job runs on the PR
   itself (workflows from the PR head run in that PR's checks).
3. Open ONE PR for this stage only. Its body must contain NO close
   keywords for any issue it does not intend to close — scan it with the
   new script before opening, and record the scan output in the receipt.
4. Wait for full required CI green on the other jobs; the hygiene job
   itself may report findings without blocking while advisory.

## STAGE B — VERIFY (all items must pass before enforcing)

1. NEGATED-CLOSE: feed the script a body containing the exact #409 trap
   phrase ("does not resolve or close #306") — it MUST fail with a
   negated-close finding.
2. CLEAN BODIES PASS: feed bodies with (a) a declaration-line close
   ("Fixes #123" on its own line), (b) no keywords at all, (c) prose
   mentioning an issue without a close keyword — all MUST pass.
3. UNDECLARED-CLOSE: feed a body with mid-sentence "this PR fixes #123"
   — it MUST fail as undeclared, proving authors must use declaration
   lines or remove the reference.
4. CREDENTIAL SCAN: feed a diff with a real-looking assignment in a
   non-test file — MUST fail; and a diff with placeholder values
   (test/example/mock), `${{ secrets.X }}` references, and
   `os.environ` reads — MUST pass.
5. PLANNING STRUCTURE: run the script with this rollout prompt and
   `docs/issue-plans/unified-main-prompt.md` as the changed files — both
   MUST pass. (Older plans are grandfathered: the check runs only on
   changed files in a PR, the same changed-file-only precedent as #288's
   typecheck job. Do NOT rewrite old plans to satisfy it.)
6. LIVE ADVISORY RUN: the Stage A PR's hygiene job ran, reported a
   summary, and did not block the merge. Record its runtime (target
   under 2 minutes; record the actual).
7. NO SECRETS, NO MODEL CALLS: confirm the job uses no secrets and makes
   no network calls beyond checkout and the git diff.

## STAGE C — ENFORCE (only after every Stage B item passes)

1. Open a follow-up PR removing `continue-on-error: true` from the job
   (one-line change + receipt). This is the enforcement flip.
2. NEGATIVE CONTROL: on a scratch PR, include an intentional
   negated-close body — the hygiene job MUST go red and block. Then
   remove the control, confirm green, and close the scratch PR without
   merging it. This mirrors #288's negative-control proof pattern.
3. Merge the enforcement PR only when all required checks are green and
   the negative control is recorded.
4. ANNOUNCE: record the enforcement in the #295 tracker comment (job
   name, blocking status, scope limits, link to the staged script) so no
   one mistakes it for a voice gate.

## STANDING CONSTRAINTS

1. One PR at a time, green CI, merge commits on main. Keep main green.
2. Jev gate v1 before every GitHub comment, review, approval, or
   disposition; Lucas Gate v2 (typed track for GitHub text) on every
   driving artifact. Below-bar → auto-investigate per policy; only
   actual risk blocks. Never pad a score, never shop a verdict.
3. Do NOT print, log, or commit any key value. Do NOT claim a landing,
   merge, or enforcement that did not happen.
4. If any Stage B item fails, STAY ADVISORY and report exactly what
   failed — do not enforce on a failing check, do not weaken the check
   to make it pass.

## END-STATE

The `driving-hygiene` job is blocking on main PRs, verified by a
recorded negative control, with its scope limits documented in the #295
tracker. Voice, tone, and bias remain outside CI, in the driving loop
only. A final report names the advisory PR, the verification evidence,
the enforcement PR, and the job's current blocking status.
