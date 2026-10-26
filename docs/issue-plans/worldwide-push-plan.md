# Worldwide push — build plan (massive prompt)

Objective: finish the three open fronts and ship the public face of the tool —
(1) the #306 Fireworks key, found and installed; (2) the #333 ledger
existence check, built in the shape Lucas picks; (3) a separate public
visibility site showing models and their stats worldwide, fed by sanitized,
aggregated opt-in user ledger stats, driving users to download the tool;
then deploy it. Iterate through ALL phases, fully, in order. Land each phase
on main green before moving to the next; if a phase blocks, name the blocker
exactly and continue with the next unblocked phase — never mark done what is
not on main green.

## Phase 0 — Services up (standing steering)

Before any work, and after every session: GUI alive
(`GET http://127.0.0.1:8800/api/status` → 200) and MCP answering a stdio
smoke call (`harness-mcp` / `python -m harness.mcp`, 30 tools). If either is
down, start it and re-verify before working. A dead service is a named
transport failure, never a silent fallback; restore any lost draft, restart,
re-verify, retry. Leave both running at session end — other lanes depend on
them.

## Phase 1 — #306: find the Fireworks key and install it

State: the key was placed and used by a previous session but is not at the
canonical path now. It was saved somewhere on this machine — find it.

1. Search this machine's harness locations: `~/.config/harness/fireworks.env`,
   `~/.config/scmorc/fireworks.env`, prior session worktrees and session
   artifact dirs, and any other harness config location on this machine.
2. NEVER print, log, or commit the key value, in any form. Verify
   presence-only (booleans from `resolve_fireworks_key()`), never the value.
3. When found: install to `~/.config/harness/fireworks.env` (dir 700, file
   0600) and verify via `resolve_fireworks_key()`.
4. Then finish #306: check probe marker/receipt state FIRST (receipt present
   → that is the evidence; marker without receipt → report it, never delete
   the marker; neither → run the EV-8 probe once, max $0.01), then land the
   receipt PR + EV-8 canon flip + `harness jev-phase --phase EV-8`.
5. If the key cannot be found after a thorough search of the machine's
   harness locations, report exactly where you searched and stop this phase —
   do not invent a key, do not silently request a new one. That is the
   blocker, named.

## Phase 2 — #333: ledger existence check (Lucas's pick)

The bug: `defer_work({"task_id": "nope"})` returns success and writes a
`defer_midtask` ledger event for a task that never existed, polluting
participation metrics and `jev_calibration` input. The fix: validate task
existence before appending ANY ledger event.

- `offer_work` must record the IDs it mints.
- `defer_work` / `log_judgment` must reject unknown task_ids with an explicit
  error and NO ledger write.
- The shape is Lucas's call — (a) in-process registry of offered IDs
  (simpler, memory-only, dies with the process) or (b) ledger-backed
  validation (survives restarts, consistent with the hash-chained design,
  more code). DO NOT decide the shape yourself. Implement the shape recorded
  on #333 or in-thread by Lucas. If no pick is recorded, complete all other
  phases and leave #333 as the single surfaced decision — do not stall the
  whole push on it.
- Verification: the `nope` repro must error, not fabricate; hermetic
  regression test pinning that; #336's never-prune list absorbs the existing
  phantom entries. PR green, merge, main green, disposition on #333 naming
  the merge commit and evidence.

## Phase 3 — Public visibility site (worldwide model stats)

First, confirm: no separate worldwide visibility site exists today. The
current `site/public/results` page is the campaign's own receipts, not a
user-stats hub. So:

1. FILE AN ISSUE to track the new site before building (scope below,
   acceptance criteria, owner). That issue is the tracker for this phase.
2. Build a SEPARATE public site (a new surface, not a tweak of the campaign
   results page) showing models and their stats for worldwide visibility:
   model roster, usage, success rates, cost per successful task, latency —
   everything a visitor needs to see the tool works, and everything that
   drives them to download it.
3. Ingestion from opt-in users ONLY: users who opt in report ledger stats;
   the pipeline collects and displays AGGREGATES ONLY — never raw ledger
   rows, never keys, tokens, paths, or user-attributable data. Sanitization
   and aggregation rules are documented and hermetic-tested. Opt-in is
   explicit and revocable; no data from anyone who did not opt in.
4. The site must drive downloads: prominent install path
   (`pip install sovereign-harness`, console scripts, docs link) and the
   promo case study featured — the 0.23¢ driving visual audit receipt,
   honestly presented (cheap AND evidence-backed, including what it did not
   assess).
5. No invented metrics, ever: every figure on the page traces to a receipt.
   SEO/promo copy uses real numbers only.

## Phase 4 — Deploy

Cloudflare is already set up locally (operator-confirmed). Deploy through
the local Cloudflare setup (Pages/Worker as configured) and verify the
public URL serves the new visibility site (and the results page). Record the
public URL in the repo. If the local Cloudflare setup turns out missing or
broken, report exactly what is missing instead of inventing a deploy path —
do not claim a deploy that did not happen.

## Standing constraints (all phases)

- One-PR-at-a-time, green-CI merges; main branch protection is active
  (11 required checks, 0 required approvals).
- Do NOT decide #333. Do NOT touch #306's key value beyond presence checks.
- Do NOT loosen any audit/Jev bar, baseline, or pinned score. Do NOT raise
  any spending ceiling. Do NOT re-run paid attestation.
- Do NOT start #277 (dogfood round 2) in this push — it stays unstarted
  until explicitly started.
- Every comment, review, approval, disposition, or driving suggestion posted
  on GitHub passes full Jev gate policy v1 first
  (`docs/JEV_GATE_POLICY_V1.md`); below-bar → auto-investigate per policy;
  only actual risk blocks.
- Keep main green throughout. Keep the #295 tracker, queue.json, and results
  page true as work lands.

## Done looks like

- #306 installed + EV-8 receipt landed (or the key-search blocker named
  with the searched locations).
- #333 implemented in Lucas's picked shape (or surfaced as the single
  decision with everything else done).
- Visibility-site issue filed, site built, deployed publicly at a recorded
  URL, fed by sanitized aggregated opt-in stats, driving downloads.
- Main green, 0 open PRs for this push, inventory updated.
