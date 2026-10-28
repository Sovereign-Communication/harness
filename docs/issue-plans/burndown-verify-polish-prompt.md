HARNESS BURNDOWN / VERIFY / POLISH - FRESH LANE PROMPT (2026-10-27)
Repo: Sovereign-Communication/harness. You are the lane on the operator's machine. Work in {{LANE_WORKTREE}} (your own checkout; never use any other machine's paths). This prompt plus the mission submitted with it are your complete inputs. Do not wait for other context.

END-STATE (ONE)
Every burndown item below is landed on main with green CI or explicitly parked with an owner decision on its issue; the #277 full self-audit is filed as the win proof; main is green with 0 open PRs; GUI and MCP are verified up and left running or the outage is named exactly.

STARTING STATE - VERIFY FRESH, DO NOT TRUST THIS SNAPSHOT
Snapshot: main 4ccee99 (PR #411 merge); CI green, 14 jobs; 0 open PRs; 63 open issues; plans branch PR #397 head c3ab6b7a.
1. VERIFY main SHA, CI state, and open issue count from the GitHub API before starting work. If main moved, re-derive the order from the new state and report the delta first.
2. INVENTORY your local worktree before writing code: list every unpushed branch and uncommitted change. Known unlanded work from the last session: #293 branch, #301 commit, #288 bookkeeping branch, capability fallback fix (capability.py + test_capability.py, 47 tests locally). If any of these is missing from your worktree, STOP that item and report it missing; do not rebuild from memory.

STEP 0 - GATE RUNNER (UNBLOCKER, DO FIRST)
3. MAKE the native Lucas Gate v2 typed-track runner callable from your lane ({{RUNNER}}). NO GitHub text (issue, comment, review, PR body) is posted without a gate pass. Product Jev first, Lucas Gate second; neither substitutes for the other.
4. NOTE: the 2026-10-27 session-audit comments are ALREADY POSTED on #418, #419, #342, #280, #319. DO NOT re-post them. DO NOT run scripts/post_session_audit_comments.ps1 -Apply (it would duplicate). Retire that script or leave it unrun.

STEP 1 - BOOKKEEPING FIRST
5. LAND the #288 bookkeeping PR from the bookkeeping launch prompt on the plans branch (docs/issue-plans/bookkeeping-pr-launch-prompt.md + mission). Re-derive live counts; do not trust the Oct 26 10/54/1 figure. Receipt-only edits. Full CI. Gated merge. Post-merge proof: #306 untouched, no auto-closures, main green.

STEP 2 - RECORD THE #407 DECISION
6. CHECK #407 for the recorded revocation decision (revoking opt-in STOPS future submissions; contributions already in published aggregates STAY). If absent, post ONE gated comment recording it. If present, move on; do not duplicate.

STEP 3 - LAND EXISTING LOCAL WORK, ONE PR AT A TIME
7. LAND #293, then #301, in that order. One PR at a time, green CI, merge commits on main. Verify each PR head is green and non-moving before merge.
8. LAND the capability fallback fix as its own PR: a nonempty stale capability snapshot survives provider errors and empty catalogs; an empty registry is retried, never saved as fresh; cancellation and provider spend-limit errors still propagate. Full capability suite + lint + wheel build. This fix is NOT on main until this PR merges; do not claim otherwise.

STEP 4 - BURNDOWN IN DEPENDENCY ORDER
9. WORK the remaining issues in dependency order (chains below); skip #333 (LANDED via PR #398, ledger-backed validation - do not rebuild) and #288 (Step 1). Chains: #345 -> #346 -> #342 -> #343/#344 -> #308 (needs #346+#342), #309 (needs #342); #345 -> #340 -> #280 (also #302) -> #318 (also #333, satisfied) -> #319, #337 (also #336); #340 -> #281 (also #277), #294 (also #282+#277), #304, #313; #345 -> #336 -> #337; #284 + #335 -> #285; #301 -> #292 -> #270 -> #272 -> #271 -> #273 (also #299) -> #275 (also #342+#344); #325 -> #291 (also #340), #314 (also #309); #377 -> #378 -> #382, #379; #380 -> #381.
10. DRIVE #418 (restart recovery: durable run records, checkpoints, safe resume/restart) and #419 (separate local health from backend/request status) with the evidence already posted in their comments. Demand-driven catalog staleness and freshness display (captured_at/refreshed hidden in UI/CLI) are part of this drive-down, not separate issues.
11. COMPLETE the #412-#417 laptop packet BEFORE the #277 report: owner-authenticated /api/status 200 (default port 8765, not 8800); live-browser fresh-profile first-use incl. recovery with no stranded user; authenticated driver loopback round-trip; MCP stdio count VERIFIED (do not trust the prior 30); one live GUI model/Jev approval with spend recorded; test re-run; #277 checklist report.

STEP 5 - SHADOW GATE
12. RUN the shadow gate rollout (plans branch docs/issue-plans/shadow-gate/): Stage A advisory PR -> Stage B full verification checklist -> Stage C enforce ONLY after all Stage B passes + negative control on a scratch PR. If any Stage B item fails, STAY ADVISORY; never weaken the check. Deterministic sub-checks only (negated-close scan, close-keyword scan, credential scan, planning-structure check). Voice categories + bias pass stay OUT of CI.

STEP 6 - #306 BLOCKED, UNTOUCHED
13. DO NOT touch #306. Blocker: one-shot Fireworks probe returned HTTP 200 with contains_ok=false; the marker prohibits another call. NEVER rerun the probe, NEVER delete the marker, NEVER mark #306 complete. Only unblocker: operator decision on a non-repeating remedy or another already-existing valid receipt.

STEP 7 - #277 LAST (VERIFY)
14. FILE the #277 full self-audit as the win proof: code, tests, all four surfaces (CLI/GUI/MCP/website), brain-talk, costs, docs/books, fresh pip install in a clean environment, GUI loopback-browser path, installed MCP runtime identity via real-client check, live model/Jev approvals, GUI first-use auth handoff. If the self-audit is not full, the win is not claimed.

STEP 8 - #407 BUILD + POLISH
15. BUILD #407 (opt-in only, aggregates only, sanitization documented + hermetic-tested, revocation semantics as recorded in Step 2), visibility site, deploy. Deploy needs operator CF_API_TOKEN + D1/KV IDs; report exactly what is missing; never claim a deploy that did not happen.
16. RUN the world-class polish pass: the product is already released on PyPI - polish to that bar, fully verified, not just green lights.

STANDING RULES (ALL STEPS)
17. ONE PR at a time, green CI, merge commits on main; keep main green.
18. NO close keywords in any PR body/title unless that close is intended and declared (negated forms also auto-close - the PR #409 lesson).
19. DO NOT print/log/commit any key value. DO NOT loosen any audit/Jev bar, baseline, or pinned score. DO NOT raise any ceiling. DO NOT re-run paid attestation. DO NOT claim a landing, merge, or deploy that did not happen.
20. IF a spec is ambiguous or contradicted, STOP that issue and report with evidence.
21. EVERY driving artifact (comments, reviews, approvals, dispositions, driving suggestions, agent prompts, mission instructions) passes Jev gate policy v1 AND Lucas Gate v2 first. Below-bar -> auto-investigate per policy; only actual risk blocks. Never pad a score, never re-run to shop a verdict, never fail on a not-applicable category.
22. HELD owner-decision items: post Jev-flushed decision briefs, never decide: #294 router flag, #270-273 debut scope, #275 Hourglass philosophy, #291 runner-vs-manual, #325 aggregation rule, #280 strike policy + weights, #281 schedule/venue/bar, #282/#283 rescope, #314 parity bars, #336 never-prune list, #337 checklist criteria, #303 enforce-vs-disclose, public deploy.

DO-NOT-DECIDE (NOT THIS SESSION)
#306 unblocker; held items in rule 22; any new ceiling, bar, or scope change; any model selection (choose at tab creation, never in this prompt).

SESSION CLOSEOUT (EVERY SESSION, EXACT)
GUI and MCP verified up and left running or the outage named exactly; main SHA; CI state; issues landed; next issue; 0 open PRs or the open PR named with exact CI state. End at a report to the harness tracking thread.
