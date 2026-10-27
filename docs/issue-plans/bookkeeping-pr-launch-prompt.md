# DRIVING PROMPT — LAUNCH THE #288 BOOKKEEPING PR (receipt-only, fully verified)

## OBJECTIVE

Open, verify, and land the #288 bookkeeping PR: the receipt-only change
that makes the books true after PR #411. #288 is already CLOSED by #411;
this PR changes NO issue state. It is verified fully before it is called
done: counts from the live API, rendered page checked, PR text scanned,
#306 untouched, post-merge main CI green.

## STEP 0 — CONFIRM THE GATE RUNNER FIRST

1. Confirm the native Lucas Gate v2 typed-track runner is callable in
   this session. If it is NOT, execute Step 0 of
   `docs/issue-plans/unified-main-prompt.md` (build the minimal runner)
   BEFORE opening any PR or posting any comment. Do NOT bypass the gate,
   do NOT post PR text ungated, do NOT hold the PR open ungated.
2. Confirm disk headroom (stop below 1 GiB free) and that GUI/MCP smoke
   checks pass or their outage is named exactly.

## STEP 1 — VERIFY LIVE STATE FROM THE API (never copy a stale count)

1. Read from the API, fresh: main HEAD SHA, open issue count, #288 state
   (must be closed by #411), #306 state (must be open and blocked with
   its exact blocker wording), #407 comment count and whether the
   revocation decision is recorded.
2. Recompute queue counts from that live state. The Oct 26 true state
   was 10 complete / 54 queued / 1 blocked (65 rows) — re-derive it now;
   if live state differs, use the live state and say exactly what
   changed.

## STEP 2 — MAKE THE RECEIPT-ONLY CHANGE (one branch, one PR)

1. Branch from current main. Edit ONLY: `docs/issue-plans/queue.json`
   (#288 status "queued" → "complete"; counts to the Step 1 values),
   the issue-plans README, the results page (same counts, #288 listed
   complete), and a new receipt doc for #288's landing (PR #411, merge
   commit, main CI run).
2. Add the #295 tracker comment covering BOTH the #410 merge (`6988979`)
   and the #411 merge (`4ccee99`), with CI receipts and the corrected
   counts.
3. NO issue-state changes. NO new behavior, NO code changes, NO
   unrelated edits.

## STEP 3 — SCAN THE PR TEXT BEFORE OPENING (fully verified, not asserted)

1. Scan the PR title AND body for close keywords: close, closes,
   closed, fix, fixes, fixed, resolve, resolves, resolved — including
   negated forms ("does not close/fix/resolve #N"). The scan MUST return
   zero hits. If the shadow-gate script
   (`scripts/driving-hygiene.py`, or its staged copy) is available,
   run it on the PR text and record its output; otherwise run the
   manual scan and record the exact command and result.
2. Scan the changed files for credential material (secret-named
   assignments with non-placeholder values). The scan MUST return zero
   hits. Record the command and result.
3. Render the results page locally and confirm the displayed counts
   match the Step 1 values exactly. Record the render check.

## STEP 4 — OPEN, WAIT FOR FULL CI, GATE EVERY ACTION

1. Open ONE PR. Wait for ALL required CI jobs green (the receipt-only
   #410 PR ran the full 14-job matrix; expect the same shape here).
2. Jev gate v1 + Lucas Gate v2 typed track on the PR text before
   opening, and again on the exact merge action before merging.
   Below-bar → auto-investigate per policy; only actual risk blocks.
3. NEVER merge on partial CI, NEVER merge on a red job, NEVER claim
   green that is not green.

## STEP 5 — MERGE, THEN PROVE NOTHING ELSE MOVED

1. Merge with a merge commit. Then verify, from the API: #306 is STILL
   open and blocked with its blocker wording unchanged; NO issue gained
   an auto-close event from this PR; the merge commit is on main.
2. Wait for post-merge main CI fully green (all jobs; note the
   changed-file typecheck skips on main by design) and the site
   workflow result (record whether deploy ran or was skipped and why —
   never claim a deploy that did not happen).
3. Final report: main SHA before/after, counts before/after, #306 state
   before/after, #407 status, CI run IDs, scan outputs, and the next
   issue (#293, unless the canonical prompt says otherwise).

## STANDING CONSTRAINTS

1. One PR at a time, green CI, merge commits on main. Keep main green.
2. Do NOT print, log, or commit any key value. Do NOT loosen any
   audit/Jev bar, baseline, or pinned score. Do NOT raise any ceiling.
   Do NOT re-run paid attestation. Do NOT claim a landing, merge, or
   deploy that did not happen.
3. If any verification item fails, STOP that step and report exactly
   what failed — do not merge around it, do not weaken a scan to pass.

## END-STATE

Books true everywhere (queue, README, results page, #295 tracker);
#288 complete in every surface; #306 open and blocked, untouched;
main green; zero unintended issue closures; a final report with the
evidence above. Only then is the bookkeeping PR done.
