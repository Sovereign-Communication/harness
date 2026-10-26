# Worldwide push — condensed build prompt (with full burndown)

Objective: finish the open fronts and ship the public face of the tool —
#306 Fireworks key found and installed, #333 built as (b) ledger-backed
validation, a separate public visibility site on sanitized aggregated
opt-in stats driving downloads, then deploy. Iterate ALL phases fully, in
order, landing each on main green before the next. If a phase blocks, name
the blocker exactly and continue with the next unblocked phase. Never mark
done what is not on main green.

## Phase 0 — Services up

Before work and after every session: GUI alive (`GET
http://127.0.0.1:8800/api/status` → 200) and MCP answering a stdio smoke
call (30 tools). If either is down, start it and re-verify before working.
A dead service is a named transport failure, never a silent fallback.
Leave both running at session end — other lanes depend on them.

## Phase 1 — #306: find the Fireworks key, install it

The key was placed and used by a previous session but is not at the
canonical path now — find it on this machine: `~/.config/harness/fireworks.env`,
`~/.config/scmorc/fireworks.env`, prior session worktrees and session
artifact dirs, any other harness config location. NEVER print, log, or
commit the value; presence-only checks via `resolve_fireworks_key()`. When
found: install to `~/.config/harness/fireworks.env` (dir 700, file 0600),
verify, then finish #306 (probe marker/receipt state first — receipt
present is the evidence; marker without receipt is reported, never deleted;
neither → run the EV-8 probe once, max $0.01 — then receipt PR + EV-8 canon
flip + `harness jev-phase --phase EV-8`). If it cannot be found after a
thorough search, report exactly where you searched and stop this phase.

## Phase 2 — #333: (b) ledger-backed validation, all writers — DECIDED

DECIDED (Lucas 2026-10-25, Jev unanimous, conf 1.0, non-destructive 0.06):
(b) ledger-backed validation. No shape question remains. Why (b): MCP
servers are per-client stdio, so a memory-only registry evaporates between
sessions; the ledger is the durable truth, so the check lives there.

The bug: `defer_work({"task_id": "nope"})` returns success and writes a
`defer_midtask` event for a task that never existed. The fix, ALL aspects:
validate task existence against the ledger before appending ANY event, on
EVERY writer — `offer_work` records minted IDs; `defer_work`,
`log_judgment`, and any other event writer reject unknown task_ids with an
explicit error and NO ledger write. A fix covering only `defer_work` is not
complete. Verify: `nope` repro errors; hermetic regression tests per writer;
participation and `jev_calibration` consumers clean of phantoms; #336's
never-prune list absorbs existing phantoms. PR green, merge, main green,
disposition on #333 with merge commit and evidence.

## Phase 3 — Public visibility site (worldwide model stats)

No separate worldwide visibility site exists today (the current
`site/public/results` page is the campaign's own receipts, not a user-stats
hub). So: (1) FILE AN ISSUE to track the new site first — that issue is
this phase's tracker. (2) Build a SEPARATE public site showing models and
their stats worldwide: roster, usage, success rates, cost per successful
task, latency. (3) Ingestion from opt-in users ONLY, AGGREGATES ONLY —
never raw ledger rows, keys, tokens, paths, or user-attributable data;
sanitization/aggregation rules documented and hermetic-tested; opt-in
explicit and revocable. (4) Drive downloads: prominent `pip install
sovereign-harness` path, console scripts, docs link, and the promo case
study featured — the 0.23¢ driving visual audit receipt, honestly presented
(cheap AND evidence-backed, including what it did not assess). (5) No
invented metrics: every figure traces to a receipt.

## Phase 4 — Deploy

Cloudflare is already set up locally (operator-confirmed). Deploy through
the local Cloudflare setup, verify the public URL serves the new visibility
site (and the results page), record the URL in the repo. If the local setup
is missing or broken, report exactly what is missing — never claim a deploy
that did not happen.

## Standing constraints

One PR at a time, green-CI merges; main protection active (11 required
checks, 0 approvals). Do NOT reopen #333's shape. Do NOT print/log/commit
any key value. Do NOT loosen any audit/Jev bar, baseline, or pinned score.
Do NOT raise any ceiling. Do NOT re-run paid attestation. Do NOT start
#277 in this push. Jev gate v1 before every GitHub comment, review,
approval, or disposition; below-bar → auto-investigate; only actual risk
blocks. Keep main green; keep #295, queue.json, and the results page true.

## Done

#306 installed + EV-8 receipt (or blocker named with searched locations);
#333 built as (b) across all writers, all aspects verified; visibility-site
issue filed, site built, deployed publicly at a recorded URL on sanitized
aggregated opt-in stats, driving downloads; main green; inventory true.

## Appendix — full burndown (59 open issues, main 874befef, 2026-10-25)

Landed already (not open): #339, #330, #347, #327, #278, #276, #331, #324.
The visibility-site issue from Phase 3 is NOT in this list yet — the lane
files it, making 60. #295 is the tracker and stays open by design.

READY NOW (prerequisites met or none):
- #277 dogfood round 2 — prereqs #276/#331/#327/#330 ALL complete; next chain item, needs only an explicit start (NOT started in this push)
- #333 ledger existence check — decided (b), ready to build
- #306 Fireworks key — operator machine, ready to execute (Phase 1)
- #279 doctor (prereqs done) · #287 CI cancel/paths · #288 pyright · #293 edge lines · #301 driver_core ruff · #310 provision split · #311 local-fit CI · #289 uv migration · #290 triage runbook · #325 v1 assessment gate (prereq #324 closed)
- #282 Playwright smoke · #284 tool-description lint · #286 structured logging · #299 POST cap · #300 check_batch docstring · #302 refusal screen · #303 scheme gate · #328 version surface · #329 CLI polish · #332 fallback labeling · #334 log_judgment envelope · #335 MCP annotations · #345 spend attribution · #377 session allowlist · #380 Jev-free backend

CHAINS (in dependency order):
- #345 → #346 → #342 → #343 / #344 → #308 (needs #346+#342) · #309 (needs #342)
- #345 → #340 → #280 (also needs #302) → #318 (also needs #333) → #319 · #337 (also needs #336)
- #340 → #281 (also needs #277) · #294 (also needs #282+#277) · #304 · #313
- #333 + #345 → #336 → #337
- #284 + #335 + #333 → #285
- #301 → #292 → #270 → #272 → #271 (prereq #324 closed) → #273 (also needs #299) → #275 (also needs #342+#344)
- #325 → #291 (also needs #340) · #314 (also needs #309)
- #377 → #378 → #382 (also needs #377) · #379
- #380 → #381

HELD (not merely queued — need a named decision or operator action):
- #333 — DECIDED (b) by Lucas 2026-10-25; build it (Phase 2)
- #306 — operator machine: key find + install + probe + receipt (Phase 1)
- #294 — behind prerequisites, then Lucas's router-flag decision
- #270–273 — Lucas's debut-scope decision gates the driver/NL-control line
- #275 — Hourglass philosophy decision
- #291 — runner-vs-manual decision
- #325 — aggregation-rule decision (then #291/#314 unblock)
- #280 — strike policy + apply-lane weights decision
- #281 — schedule/venue/bar decision
- #282/#283 — rescope decision (served UI, not Electron; Jest IPC inapplicable as written)
- #314 — parity-bars decision
- #336 — never-prune list decision (after #333 lands)
- #337 — checklist go/no-go criteria decision
- #303 — enforce-vs-disclose decision (Jev-escalated, held)
- #347-area follow-ups #377–382 — tracking/low-priority lane items
- Public deploy — needs operator's CF_API_TOKEN + D1/KV IDs (Phase 4)

Everything else is queued behind the chains above, not held.
