**SUPERSEDED — DO NOT FOLLOW THIS DOC. Canonical: `docs/issue-plans/unified-main-prompt.md` (same branch). See `docs/issue-plans/CANONICAL.md`.**

This was a point-in-time resume doc (2026-10-26 baseline). Its steps 1–2 live on as canonical Steps 1–2. The canonical prompt's baseline and order supersede this doc.

---

# DRIVING PROMPT — RESUME BURNDOWN (post-#288)

## VERIFIED GITHUB BASELINE (checked from the API, 2026-10-26)

- Main is `4ccee99` (PR #411 merge). Main CI is GREEN: 14 jobs success,
  changed-file typecheck skipped on main by design.
- 0 open PRs. 55 open issues.
- #288 is CLOSED by #411. #333 is CLOSED by #398 (merge `919d2acf`, in
  main, 41 commits behind main's head) — the (b) ledger-backed build
  LANDED. DO NOT rebuild #333.
- #306 is OPEN and BLOCKED (see step 5). #407 is OPEN with ZERO comments
  — the revocation decision is NOT recorded on GitHub yet.
- queue.json ON MAIN IS STALE: it still shows #288 as "queued" and counts
  9 complete / 55 queued / 1 blocked. True state after #288: 10 complete /
  54 queued / 1 blocked. #295's tracker comment is behind by the #410 and
  #411 merges. Fixing this staleness is step 1.

## STEP 0 — CONFIRM DISK HEADROOM BEFORE BUILDING

Check free space first. The laptop was at 2.02 GiB after the last cleanup.
If free space is below 1 GiB, STOP and report — do not start builds, do
not run suites. The two empty directory shells
(`.freebuff\worktrees\e208075a\Harness`, `.freebuff\worktrees\7a5cf39f\Harness`)
stay in place: e208075a IS the active workspace and cannot be removed
while the session is open; remove 7a5cf39f only after its lock releases,
and only if it is still empty.

## STEP 1 — #288 BOOKKEEPING PR (receipt-only, do this FIRST)

#288 is already closed by #411. This PR only makes the books true:
1. queue.json: #288 status "queued" → "complete"; counts to 10 complete /
   54 queued / 1 blocked.
2. issue-plans README and results page: same counts, #288 listed complete.
3. #295 tracker comment covering BOTH the #410 merge (`6988979`) and the
   #411 merge (`4ccee99`), with CI receipts and the corrected counts.
4. Receipt doc for #288's landing (PR #411, merge commit, main CI run).
5. NO issue-state changes. NO close keywords for ANY issue in the PR body
   or title — the #409 auto-close accident (a negated "does not close
   #306" phrase still triggered GitHub's parser) must not repeat. Scan the
   PR text for "close", "closes", "fix", "fixes", "resolve" before opening.
6. One PR, full required CI green, merge commit, post-merge main CI green.

## STEP 2 — RECORD THE #407 REVOCATION DECISION ON #407

Post a comment on #407 (Jev v1 + Lucas Gate v2 typed track first):
"Decided by Lucas 2026-10-26: revoking opt-in STOPS future submissions;
contributions already included in published aggregates STAY as they are."
This decision was given in chat and is nowhere on GitHub yet — record it
before #407 reaches the build queue, so the build spec carries it.

## STEP 3 — NEXT BUILD ITEM: #293

#293 ([tests] Cover the 6 suite-unexecuted edge lines left by #274) is
OPEN, its prerequisite #276 is landed, and it is the first unfinished item
in burndown order. Build it with ALL aspects: spec read from the full issue
body and thread; implementation; hermetic regression tests through the
real surface; local gates + audit + Jev gate green; one PR; all required
CI green; merge commit; post-merge main CI green; receipt doc;
disposition naming merge commit + evidence; queue/README/results/#295
updated in the same PR cycle.

## STEP 4 — THEN #301 (unlocks the driver chain)

#301 (driver_core ruff scope; prerequisite #278 landed) is next. Same
full-aspect cycle as step 3. Landing #301 unblocks #292 → #270 → #272 →
#271 → #273 → #275. Do NOT start #270 before #292 lands.

## STEP 5 — #306 STAYS BLOCKED, DO NOT TOUCH THE PROBE

#306 remains OPEN and BLOCKED. Its blocker is exact: the one-shot
Fireworks probe returned HTTP 200 with `contains_ok=false`, and the marker
prohibits another call. NEVER rerun the probe, NEVER delete the marker,
NEVER mark #306 complete. Its only unblocker is an operator decision on a
non-repeating remedy or another already-existing valid receipt. Carry the
blocker wording forward unchanged in every bookkeeping update.

## STEP 6 — CONTINUE IN BURNDOWN ORDER

After #293 and #301, continue from `docs/issue-plans/burndown-prompt.md`
(all 60 issues, all aspects) with ONE correction: #333 is DONE (step 0
baseline) — skip it. #277 runs LAST as the closing verification round over
everything built. #407 builds when it reaches the queue, carrying the step
2 revocation semantics. Public deploy still needs the operator's
CF_API_TOKEN + D1/KV IDs — report exactly what is missing if asked to
deploy; never claim a deploy that did not happen.

## STEP 7 — BACK UP THE LAPTOP'S LOCAL-ONLY HARNESS STATE (Harness only)

The last laptop audit found Harness work product NOT on GitHub: a stash
(13 changed paths; `git stash show --include-untracked` fails on a
duplicate path in audits/self/dogfood/DRV1_DOGFOOD_RECEIPT.md), a Codex
snapshot (6 changed paths, 659-entry checkpoint tree), 16 local tag refs
whose targets are already reachable, and 7,670 ignored files (mostly
caches/runtime, but 269 tmp files outside cache subtrees plus
dogfood_matrix_results.json need a work-product check).
1. Inspect the stash and snapshot contents first. If reviewable AND
   credential-free, snapshot each to its own `wip-snapshots/...` branch
   with `[skip ci]`, after verifying the branch will not match any push
   workflow filter — then verify the exact remote commit before touching
   local state.
2. If any content is NOT reviewable, report exactly what is unverifiable
   and LEAVE IT. Never push unknown content, never force-push, never touch
   main, never run local CI for this step.
3. Ignored caches/runtime files stay local. Only genuine work product gets
   snapshotted.

## STANDING CONSTRAINTS

- One PR at a time, green CI, merge commits on main. Keep main green.
- Jev gate v1 before every GitHub comment, review, approval, or
  disposition; Lucas Gate v2 (typed track for GitHub text, planning track
  for prompts) on every driving artifact. Below-bar → auto-investigate per
  policy; only actual risk blocks. Never pad a score, never shop a verdict.
- Do NOT print, log, or commit any key value. Do NOT loosen any audit/Jev
  bar, baseline, or pinned score. Do NOT raise any ceiling. Do NOT claim
  a landing, merge, or deploy that did not happen.
- If a spec is ambiguous or contradicted, stop that issue and report it
  with the evidence — do not guess.

## SESSION CLOSEOUT (every session)

GUI (8800 /api/status 200) and MCP (stdio smoke, 30 tools) verified up and
left running; main SHA, CI state, issues landed this session, and next
issue stated plainly; 0 open PRs at session end, or the open PR named with
its exact CI state.

## END-STATE

#288 bookkeeping landed and the books true (10/54/1); #407 revocation
decision recorded on the issue; #293 landed with receipt (and #301 landed
if the session reaches it); #306 still open and blocked with its unblocker
named; laptop-only stash/snapshot either snapshotted to verified remote
refs or explicitly reported unverifiable; main green; a final report
naming what landed, what remains, and who owns each remaining item.
