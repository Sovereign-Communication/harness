# MISSION: LAUNCH THE #288 BOOKKEEPING PR

## FULL SPEC (pull this first, follow it exactly)

Pull `docs/issue-plans/bookkeeping-pr-launch-prompt.md` from branch
`plans/worldwide-push-2026-10-25` and execute it fully. This mission text
is steering only; the full prompt on the branch governs every step. If
this text and the full prompt disagree, the full prompt wins.

## OBJECTIVE

Open, fully verify, and land the #288 bookkeeping PR. #288 is already
CLOSED by PR #411; this PR changes NO issue state. It only makes the
books true after PR #411.

## STEPS

1. GATE RUNNER FIRST: confirm the native Lucas Gate v2 typed-track
runner is callable. If NOT, build it first per the canonical prompt
(`docs/issue-plans/unified-main-prompt.md` Step 0). Never post PR text
ungated. Confirm disk headroom (stop below 1 GiB free).
2. LIVE STATE FROM THE API: read fresh main SHA, open issue count,
#288 state (must be closed by #411), #306 state (must be open and
blocked, exact blocker wording), #407 comment status. Recompute queue
counts from live state; never copy a stale count. Oct 26 true state was
10 complete / 54 queued / 1 blocked (65 rows) - re-derive now.
3. RECEIPT-ONLY CHANGE: branch from current main. Edit ONLY queue.json
(#288 "queued" to "complete", counts to Step 2 values), issue-plans
README, results page (same counts, #288 complete), a new receipt doc
(PR #411, merge commit, main CI run), and the #295 tracker comment
covering BOTH #410 (6988979) and #411 (4ccee99) merges with CI receipts.
NO code changes, NO unrelated edits.
4. SCAN BEFORE OPENING: scan PR title AND body for close keywords
(close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved),
including negated forms ("does not close #N"). MUST return zero hits.
Run `scripts/driving-hygiene.py` if available (or its staged copy) and
record output; else record the manual scan command and result. Scan
changed files for credential material; MUST return zero hits. Render
the results page locally; displayed counts MUST match Step 2 exactly.
5. OPEN AND GATE: open ONE PR. Wait for ALL required CI jobs green
(expect the full 14-job matrix, as #410 had). Jev gate v1 + Lucas Gate
v2 typed track on the PR text before opening, and again on the exact
merge action. NEVER merge on partial or red CI.
6. MERGE AND PROVE: merge with a merge commit. Then verify from the API
that #306 is STILL open and blocked with wording unchanged, NO issue
gained an auto-close event from this PR, and the merge commit is on
main. Wait for post-merge main CI fully green (typecheck skips on main
by design) and record the site workflow result (deploy ran or skipped,
and why - never claim a deploy that did not happen).

## LOOP AND STOP RULES

Keep working until the END-STATE below is verified. Stop only when the
outcome exists and is verified, or on a concrete owner blocker (name it
exactly: what, why, what unblocks it). Never mark blocked work done.
Resume note on every handoff: exact head SHA + verified/not verified +
remaining gap.

## CONSTRAINTS

One PR at a time, green CI, merge commits, main stays green. No key
values printed, logged, or committed. No audit/Jev bar loosened, no
ceiling raised, no paid attestation re-run. No landing, merge, or deploy
claimed that did not happen. If any verification item fails, STOP that
step and report exactly what failed - do not merge around it, do not
weaken a scan to pass.

## END-STATE (all must be true)

Books true everywhere (queue, README, results page, #295); #288 complete
in every surface; #306 open and blocked, untouched; main green; zero
unintended issue closures; final report with main SHA before/after,
counts before/after, #306 before/after, #407 status, CI run IDs, and
scan outputs. Only then is the bookkeeping PR done.
