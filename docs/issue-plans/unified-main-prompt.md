# DRIVING PROMPT — UNIFIED MAIN: MEGA TRANSFORMER, WORLD CLASS

## GOAL (Lucas, 2026-10-27)

All the toys become a mega transformer and all work together. This is the
harness repo and it needs to WORK FULLY: every part independently
complete and functional, AND interdependent as needed, fully compatible
and coordinated. The repo is already released on PyPI — the bar is
POLISH and PERFECTION, world class, not prototype. No half-done parts, no
dead ends, no "green except the thing a user actually touches."

## WIN CONDITION (Lucas, 2026-10-27)

Dogfood harness on itself. When it can audit ITSELF FULLY, that is the
win. "Fully" is literal — every aspect:
- CODE: implementation complete per spec, hermetic tests through the real
  surface, local gates + audit + CI green, main green, no regressions.
- SURFACES: CLI, GUI, MCP, website — every surface a real user touches,
  verified live, including first-use flows (a browser user without the
  token must get a safe, user-owned handoff, never a dead end).
- BRAIN-TALK: driving words and decisions — the harness's own PR comments,
  reviews, dispositions, and prompts pass Lucas Gate v2 (typed track) when
  it drives itself. The native typed-track runner must exist and be
  callable for this to be real.
- COSTS: every figure traces to a receipt; no invented metrics; paid
  calls only where authorized, never re-run paid attestation.
- DOCS AND BOOKS: plan docs, receipts, queue, README, results page, and
  tracker all true at the same time — public counts never lie.
- FRESH INSTALL: `pip install sovereign-harness` in a clean environment
  works, end to end, with docs that match reality.

## APPROVED GATE — HYBRID (Lucas approved 2026-10-27)

- IN CI (blocking, deterministic only, no model calls, no secrets): a
  `driving-hygiene` job on pull_request, Ubuntu-only — (1) PR-body
  negated-close-language scan (the #409 trap that auto-closed #306),
  (2) close-keyword scan against an explicit allow-list, (3)
  credential-pattern scan on added lines, (4) planning-structure check on
  changed `docs/issue-plans/*.md` (ALL-CAPS headers, numbered steps,
  stated end-state). Implement this job as part of this push. It needs no
  runner, so the runner gap does NOT block it. Staged implementation + rollout: `docs/issue-plans/shadow-gate/`. Canonical index: `docs/issue-plans/CANONICAL.md`.
- OUT OF CI, in the driving-agent loop only: typed-track voice categories
  1–6, 8–11 scoring and the bias pass. The Gate itself never becomes a
  merge-blocking CI gate.

## VERIFIED BASELINE (checked from the API, 2026-10-26)

- Main is `4ccee99` (PR #411 merge). Main CI GREEN: 14 jobs success,
  changed-file typecheck skipped on main by design. 0 open PRs.
- 55 open issues. #288 CLOSED by #411. #333 CLOSED by #398 (merge
  `919d2acf`, in main) — the (b) ledger-backed build LANDED. DO NOT
  rebuild #333.
- #306 OPEN and BLOCKED (see STEP 5). #407 OPEN; revocation decision
  recorded or not must be verified, not assumed.
- queue.json on main was stale at 9/55/1 (true state after #288: 10/54/1);
  #295 tracker was behind by #410 + #411. Verify current counts from the
  API before writing any bookkeeping — never copy a stale count forward.

## UPDATE 2026-10-27 - NEW ITEMS (read before starting)

- Open issues now 61 (55 + 6 new GUI audit issues #412-#417 below); main unchanged at 4ccee99; 0 open PRs.
- JEV POLICY CORRECTION (Lucas 2026-10-27): save the POLICY to memory, NOT every run. Per-run Jev verdicts go in run receipts only, cited from receipts on repeat. The prior steering Step 4 (save every run to memory) is SUPERSEDED. Full instructions: docs/issue-plans/jev-use-instructions.md (branch); AGENTS.md + MEMORY.md carry the standing policy.
- GATE AVERAGES LOCKED (2026-10-27, Jev-verified jev-1.14.0 proceed / destructive 0.05 / advances_goal 0.96): category_average = arithmetic mean of that category's raw scores across samples where scored; overall_average = arithmetic mean of the category averages (equal weight per category); runner recomputes both from raw per-sample rows and rejects runs with missing rows or recomputed means differing by more than 0.005; apply rounding unrounded first (8.95 counts iff category average >= 9.25; 8.9 iff >= 9.5; 8.8 iff overall >= 9.9 exact). Full definition: implementation plan LOCKED DEFINITION section.
- GUI AUDIT 2026-10-27 (verdict ESCALATE, report workspace/harness-gui-audit-2026-10-27.md): server serves/guards as coded; server/driver suites 173 passed + 8 subtests; GUI NOT verified end to end. New issues #412-#417 (below) feed #277; work them before the #277 final report; #277 remains LAST as the filed report.
- CORRECTIONS from the audit (primary sources at 4ccee99): make_server default port is 8765, NOT 8800 (8800 needs explicit --port); /api/status 200 requires auth (unauthenticated 401 by design); driver-core verify 52-record chain is the local HOME store, NOT the harness chain - never cite it as harness evidence.
- SHADOW GATE: deterministic driving-hygiene job staged at docs/issue-plans/shadow-gate/ (advisory first: Stage A advisory PR, Stage B verification checklist, Stage C enforce only after all Stage B passes + negative control). Do not enforce until verified.
- BOOKKEEPING LAUNCH: full launch prompt docs/issue-plans/bookkeeping-pr-launch-prompt.md + mission version (under 4,000 chars) docs/issue-plans/bookkeeping-pr-launch-mission.md. STEP 1 uses the launch prompt.
- ENTIRE-VERIFIED NOTES (2026-10-27, public-docs research; Entire work itself is parked, nothing here depends on it): no standalone entire-mcp forge server exists - the real Entire MCP surface is entire-brain memory tools only (brain_remember, brain_recall, brain_brief, brain_query, brain_status); no intent-drift monitor product exists (intent/drift checks are hackathon ideas, not shipped); no downloadable Entire skill package - the official substitute is GENERATED agent guides (entire brain setup / graph init-agents writing .entire/agent-guide.md + AGENTS.md/CLAUDE.md wiring). Do NOT add .entire files to these repos expecting Entire action on GitHub; do NOT build against intent-drift monitors, Trail-management MCP tools, or a risk dashboard - none exist in the documented surface.

## STEP 0 — MAKE THE TYPED-TRACK RUNNER CALLABLE (do this FIRST)

The self-audit of brain-talk requires the native Lucas Gate v2
typed-track runner, and its absence is the lane's named unblocker. Check
the MCP catalog and the repo's gate implementation; if no callable runner
exists, BUILD the minimal one per
`docs/issue-plans/lucas-gate-v2-harness-implementation.md` (same branch; the v2 profile itself stays private and is not published) (profile v2, typed track:
categories 1–6, 8–11, bar ≥9 with the operator rounding rule, bias pass
B1/B3/B4/B5/B8/B9/B10/B11). Until it exists, post NO GitHub text without a
gate pass — but local builds, tests, and the deterministic CI job proceed,
because they need no runner. The runner is part of the win, not a reason
to stall the build.

## STEP 1 — BOOKKEEPING FIRST (receipt-only)

Make the books true before building: queue counts from live API state,
README, results page, #295 tracker covering the #410 (`6988979`) and
#411 (`4ccee99`) merges with CI receipts. NO issue-state changes. NO
close keywords for ANY issue in any PR body or title — scan for "close",
"closes", "fix", "fixes", "resolve" before opening (the #409 negated
phrase still triggered GitHub's parser). One PR, full CI green, merge,
post-merge main CI green.

## STEP 2 — RECORD THE #407 REVOCATION DECISION

Verify whether #407 carries it; if not, post it (gated first): "Decided
by Lucas 2026-10-26: revoking opt-in STOPS future submissions;
contributions already included in published aggregates STAY as they are."
Record it before #407 reaches the build queue, so the build spec carries
it.

## STEP 3 — NEXT BUILD ITEM: #293, THEN #301

#293 ([tests] Cover the 6 suite-unexecuted edge lines left by #274;
prerequisite #276 landed) is the first unfinished build item. Build it
with ALL aspects (spec, implementation, real-surface tests, gates, audit,
PR, CI green, merge, main green, receipt, disposition, bookkeeping).
Then #301 (driver_core ruff scope; prerequisite #278 landed) — landing
it unblocks #292 → #270 → #272 → #271 → #273 → #275. Do NOT start #270
before #292 lands.

## STEP 4 — CONTINUE IN BURNDOWN ORDER

After #293 and #301, continue from `docs/issue-plans/burndown-prompt.md`
(corrected 2026-10-27: #333 DONE, #306 BLOCKED, #288 DONE; this prompt is canonical where they disagree).
One PR at a time, green CI, merge commits, main green throughout.

## STEP 5 — #306 STAYS BLOCKED, DO NOT TOUCH THE PROBE

#306 remains OPEN and BLOCKED. Its blocker is exact: the one-shot
Fireworks probe returned HTTP 200 with `contains_ok=false`, and the
marker prohibits another call. NEVER rerun the probe, NEVER delete the
marker, NEVER mark #306 complete. Its only unblocker is an operator
decision on a non-repeating remedy or another already-existing valid
receipt. Carry the blocker wording forward unchanged.

## STEP 6 — #277 LAST: THE FULL SELF-AUDIT, FILED

#277 (dogfood round 2) runs LAST as the closing proof of the win: one
structured round across CLI, GUI, MCP, website with a filed report
(runtime, outputs, exit codes, calls, timing, costs). Explicit findings
even where there are none; 403/unverified never passes. Each defect
found is fixed with a test-bearing PR or filed with severity. Known gaps
that MUST be covered: GUI loopback-browser path (no native pywebview),
installed MCP runtime identity via a real-client check, live model/Jev
semantic approvals, and the GUI first-use auth handoff (no stranded
browser user). The filed matrix is the receipt. If the self-audit is not
full, the win is not claimed.
GUI audit issues feeding this step (filed 2026-10-27, work before the final report): #412 first-use auth handoff live UX; #413 driver loopback-browser round-trip; #414 MCP runtime identity real-client check; #415 live model/Jev semantic approvals via GUI; #416 GUI first-use acceptance doc + serve/auth doc corrections; #417 driver-core verify store identity. Laptop packet (from the audit): owner-authenticated /api/status 200; live-browser fresh-profile first-use incl. recovery with no stranded user; authenticated driver loopback round-trip; MCP stdio count verified (do not trust the prior 30); one live GUI approval with spend recorded; test re-run; then the #277 report.

## STEP 7 — #407 BUILD, VISIBILITY SITE, DEPLOY

#407 builds when it reaches the queue, carrying the STEP 2 revocation
semantics: separate public site, models and stats worldwide, opt-in users
ONLY, AGGREGATES ONLY (never raw ledger rows, keys, tokens, paths, or
user-attributable data), sanitization rules documented and
hermetic-tested, opt-in explicit and revocable. Drive downloads:
prominent `pip install sovereign-harness` path and the 0.23¢ audit
receipt featured honestly (cheap AND evidence-backed, including what it
did not assess). Deploy via the local Cloudflare setup; verify the public
URL serves; record it. Public deploy needs the operator's CF_API_TOKEN +
D1/KV IDs — report exactly what is missing if asked to deploy; never
claim a deploy that did not happen.

## STEP 8 — WORLD-CLASS POLISH PASS

Before calling anything done: fresh `pip install` in a clean environment
works; docs match reality; version and changelog current; every surface
has no dead ends; every issue landed has its receipt and disposition;
queue, README, results page, and tracker agree. Polish is not a separate
phase to skip — it is the bar for every phase above.

## STANDING CONSTRAINTS

- One PR at a time, green CI, merge commits on main. Keep main green.
- Jev gate v1 before every GitHub comment, review, approval, or
  disposition; Lucas Gate v2 (typed track for GitHub text, planning track
  for prompts) on every driving artifact. Below-bar → auto-investigate per
  policy; only actual risk blocks. Never pad a score, never shop a
  verdict, never fail on a not-applicable category.
- Memory holds the POLICY, not every run (corrected 2026-10-27): per-run Jev verdicts go in run receipts only, cited from receipts on repeat.
- Do NOT print, log, or commit any key value. Do NOT loosen any audit/Jev
  bar, baseline, or pinned score. Do NOT raise any ceiling. Do NOT re-run
  paid attestation. Do NOT claim a landing, merge, or deploy that did not
  happen.
- If a spec is ambiguous or contradicted, stop that issue and report it
  with the evidence — do not guess.

## SESSION CLOSEOUT (every session)

GUI (serve --port 8800, /api/status 200 WITH the saved owner credential - unauthenticated 401 is by design) and MCP (stdio smoke, tool count verified, not assumed) verified up and
left running (or the outage named exactly, with its cause); main SHA, CI
state, issues landed this session, and next issue stated plainly; 0 open
PRs at session end, or the open PR named with its exact CI state.

## END-STATE

One unified main: every part independently complete and functional, all
parts coordinated, all surfaces verified live, the harness auditing
itself FULLY (code, surfaces, brain-talk, costs, docs, fresh install)
with the filed #277 matrix as proof; #306 still open and blocked with its
unblocker named; books true; main green; a final report naming what
landed, what remains, and who owns each remaining item. The mission ends
only when the self-audit is full — not when the session ends.
