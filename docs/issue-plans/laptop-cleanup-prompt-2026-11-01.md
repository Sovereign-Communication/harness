LAPTOP CLEANUP + UNIFY - HARNESS LANE (2026-11-01)
Owner-ordered in the harness tracking thread. Read AGENTS.md section LANE PROTOCOL first - mechanics outrank this prompt. Gates: Lucas Gate policy reference at docs/issue-plans/lucas-gate-policy-reference.md (plans branch); Jev per repo gate policy v1.

ENVIRONMENT
[FreeBuff laptop] - your local machine. This prompt targets THIS machine only: the canonical harness clone (your working folder), every other harness checkout, worktree, and folder on this machine, and every temp location on this machine. Nothing here concerns the oversight sandbox.
Owner machine audit, 2026-11-01: the C: drive is at 11 GB free. Disk pressure is now a session-killer - the 2026-10-31 session died mid-work with its Batch 0 changes unpushed. This cleanup is the fix.

END STATE (one)
The local harness workspace is cleaned up and unified, with every piece of work preserved on GitHub and proven there, so the local remainder is purge-ready: (1) exactly ONE local working branch held - lane/burndown - in the canonical clone, its tree clean or fully committed and pushed; (2) every other local branch, worktree, stash, and uncommitted change dispositioned with a written proof line (preserved at a named remote ref + SHA, or landed with patch-id/content proof) in the cleanup ledger; (3) every temp location swept - work product moved into the working folder, regenerable items listed with their proof, unknowns held in a manifest and NOT deleted; (4) the proven set purged locally and the GB freed measured; (5) CLEANUP-REPORT-2026-11-01.md in the working folder + a summary comment on draft PR #442, mirrored to tracker #295.

KNOWN STARTING FACTS (re-verify in Step 0; do not trust them blind)
1. [repo] Main 0780ea74, green. Draft PR #442 head 83bc0103, 17 of 17 check-runs success. 80 open issues. Remote branch census 255.
2. [FreeBuff laptop] The 2026-10-31 session's Batch 0 slice (a) work - the adopted sweep deltas for issues 438 and 439, 6 files changed, 147 focused tests passing - was NEVER PUSHED. It exists only in your local worktree. It is the highest-value unpushed work on this machine: preserve it first (Step 2, item 1).
3. [FreeBuff laptop] Temp scope is ALL temp locations: C:\tmp, C:\temp, %TEMP%, and any other scratch or staging directory. A root folder named tmp or temp on this machine is agent scratch by definition - nothing in it is assumed legitimate. (This supersedes any earlier note excluding C:\tmp.)
4. [repo] Clean Burndown v4 Batch 13 (the staged 67 remote heads) is ordinary v4 work and proceeds under v4. It is NOT part of this prompt's critical path - do not let it block the local end state, and do not count remote deletions toward the GB freed here.

STEP 0 - RE-VERIFY (before touching anything)
1. Record free disk space on C: (the before number).
2. Verify live repo state from primary sources: main SHA + check-runs, PR #442 head + check-runs, open-issue count by the pinned method. If reality differs from the facts above, the report says so and the fresh numbers govern.
3. Take the local census (the before picture, written to the cleanup ledger in the working folder - create it now): every harness clone/checkout/worktree on this machine with its path; in each, every local branch with head SHA and upstream state, every stash entry, and the dirty/untracked state of every working tree; every harness-related folder outside the canonical clone.

STEP 1 - INVENTORY THE TEMP LOCATIONS
1. List every temp location in scope (KNOWN STARTING FACTS item 3) and census its harness-related contents: gate notes, review evidence, sweep drafts, run records, prompt or plan drafts, patches, logs with unique content.
2. Classify each item in the ledger as exactly one of: WORK PRODUCT (unique content, must be preserved), REGENERABLE (rebuildable or re-downloadable, with the reason recorded), or UNKNOWN (cannot be classified with certainty - held, never deleted in this pass).

STEP 2 - PRESERVE EVERYTHING TO GITHUB (before any purge)
1. FIRST: commit the Batch 0 slice (a) work in the canonical clone as wip commits on lane/burndown and push. Verify the push landed (remote head == local head) before doing anything else.
2. Commit and push every other uncommitted change and untracked file of substance in every checkout, on its own branch. Nothing of substance stays local-only.
3. Push every unpushed local branch under its existing name. No force-push, no history rewrite, ever. If a push is rejected, park that branch in the ledger with the rejection and continue.
4. Convert every stash entry into a pushed branch (or a patch file committed to the working folder and pushed on lane/burndown) before dropping any stash; a stash is dropped only with its preservation receipt in the ledger.
5. Move every WORK PRODUCT item from the temp locations into the working folder; commit and push what belongs in the repo; the rest rides in the working folder and is listed in the report.
6. SECRET SCAN before every push: no key, token, credential, or .env value is ever committed or pushed. If a secret is found in any file, stop that file, record it in the ledger by name only (never the value), and list it in the report's QUESTIONS.

STEP 3 - PROVE PRESERVATION (per item, in the ledger)
For every local branch, worktree, stash, and moved temp item, write its proof line: the remote ref (or landed PR) + the SHA it preserves + how you verified it (ls-remote head match, patch-id, or content diff). An item without a complete proof line is NOT purge-eligible, whatever it looks like. Judgment classes (branches whose PR closed unmerged; branches with no PR) get individual proof lines and individual dispositions - never bulk treatment.

STEP 4 - UNIFY
1. Retire every non-canonical checkout and worktree: manifest-first (path, branch, head SHA, dirty state in the ledger), preserve per Steps 2-3, then remove the worktree registration / extra clone.
2. End with the canonical clone holding exactly ONE local branch: lane/burndown, checked out, tree clean or fully committed and pushed.

STEP 5 - PURGE THE PROVEN SET ONLY
1. Delete local branches whose proof lines are complete. Delete nothing else with a name.
2. Remove regenerable junk from the working folder and the temp locations (venvs, caches, node_modules, build output) - each class listed in the ledger as regenerable with its reason.
3. UNKNOWN items stay exactly where they are, listed in the manifest. Suspicion is not proof; nothing unproven is deleted.
4. Record free disk space on C: (the after number) and the GB freed.

STEP 6 - REPORT
1. Write CLEANUP-REPORT-2026-11-01.md in the working folder: census before/after (checkouts, local branches, stashes, temp items by class), the full preservation ledger with proof lines, the purge list with the proof each deletion relied on, the held/unknown manifest, GB freed, the single-branch confirmation, and a QUESTIONS section last (reserved items and genuine gaps only, each with the evidence already gathered).
2. Post the summary as a status comment on draft PR #442, mirrored to tracker #295, including the report's working-folder path.

RULES
1. Working folder only from here on: no temp or scratch location for any lane work - gate notes, evidence, ledgers, and intermediate work live in the working folder or the repo.
2. Lucas Gate FIRST on every disposition decision (decision-signal mode per the policy reference until the profile is provisioned); Jev v1 for any disposition you gate with Jev. Record verdicts in the report.
3. Never delete without a proof line. Never force-push. Never commit or push a secret. Never loosen any bar to make an item fit a class.
4. Questions never stop the work: park the item, take the next safe one, and put the question in the report's QUESTIONS section.
5. Push wip at every sub-step and post a status comment at every turn end on #442 per LANE PROTOCOL; this cleanup is disk-critical, so checkpoint more often than the minimum, not less.
6. Report DONE only when the END STATE holds in full; otherwise report the exact gap.
