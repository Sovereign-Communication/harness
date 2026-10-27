**SUPERSEDED — DO NOT FOLLOW THIS DOC. Canonical: `docs/issue-plans/unified-main-prompt.md` (same branch). See `docs/issue-plans/CANONICAL.md`.**

This doc is kept for history only. Corrected 2026-10-27 for #333 DONE (PR #398), #306 BLOCKED, and #288 DONE (PR #411), but the canonical prompt still wins on any disagreement.

---

# Full burndown prompt — every open issue, all aspects fully flushed out

Objective: complete the ENTIRE harness burndown — all 59 open issues plus
the new visibility-site issue (60 total) — each one fully done: saved,
landed, tested, confirmed, no regressions. Work in dependency order, one PR
at a time, green CI, merge commits on main. No issue counts as done with
any aspect left open. The mission ends only when every issue is landed or
explicitly blocked with a named unblocker — not when the session ends.

## What "fully flushed out" means for EVERY issue

Each issue gets all of these aspects, no exceptions, no partial credit:
1. SPEC — read the full issue body and thread; if the spec is ambiguous
   or contradicted, stop that issue and report the ambiguity with the
   evidence — do not guess.
2. IMPLEMENTATION — the complete behavior the issue body specifies, not a
   slice of it.
3. TESTS — hermetic regression tests proving the behavior through the real
   invocation path (real CLI, real surface), not unit-level assertions
   about a mock.
4. VERIFICATION — local gates green, audit green, Jev gate v1 green, PR
   head CI green (all required checks), main CI green after merge.
5. RECEIPT — a plan doc (`docs/issue-plans/<n>.md`) recording commands,
   outputs, exit codes, and evidence.
6. DISPOSITION — a comment on the issue naming the merge commit and the
   exact evidence, then the issue closed by the merge.
7. BOOKKEEPING — #295 tracker, queue.json, and the results page updated as
   each issue lands, so public counts never lie.

## Phase 0 — Services up (every session)

GUI alive (`GET http://127.0.0.1:8800/api/status` → 200) and MCP answering
a stdio smoke call (30 tools) before work and after every session. If
either is down, start it and re-verify before working. A dead service is a
named transport failure, never a silent fallback. Leave both running —
other lanes depend on them.

## The full burndown (dependency order — work top to bottom)

Landed already (not open): #339, #330, #347, #327, #278, #276, #331, #324, #333 (PR #398), #288 (PR #411), #279, #287, #282.
#295 is the tracker and stays open by design. The visibility-site issue
(VIS) is filed first in Phase 3 — it is not in the 59 yet.

READY NOW (no unmet prerequisites — start here):
- #333 ledger existence check — DONE, CLOSED by PR #398 (merge `919d2acf`, 2026-10-26): (b) ledger-backed validation across ALL writers is LANDED. Do NOT rebuild. Its prerequisites are satisfied for #318, #336, #285.
- #306 Fireworks key — BLOCKED: one-shot probe returned HTTP 200 with `contains_ok=false`; the marker prohibits another call. NEVER rerun the probe, NEVER delete the marker, NEVER mark #306 complete. Only unblocker: operator decision on a non-repeating remedy or another already-existing valid receipt. NEVER print, log, or commit any key value.
- #277 dogfood round 2 — prereqs ALL complete; runs LAST as the closing
  verification round (Phase 5), not first.
- #293 edge lines ·
  #301 driver_core ruff · #310 provision split · #311 local-fit CI ·
  #289 uv migration · #290 triage runbook · #325 v1 assessment gate
- #282 Playwright smoke · #284 tool-description lint · #286 structured
  logging · #299 POST cap · #300 check_batch docstring · #302 refusal
  screen · #303 scheme gate · #328 version surface · #329 CLI polish ·
  #332 fallback labeling · #334 log_judgment envelope · #335 MCP
  annotations · #345 spend attribution · #377 session allowlist · #380
  Jev-free backend

CHAINS (in order — a link never starts before its prerequisites land):
- #345 → #346 → #342 → #343 / #344 → #308 (needs #346+#342) · #309
  (needs #342)
- #345 → #340 → #280 (also needs #302) → #318 (#333 satisfied — landed) →
  #319 · #337 (also needs #336)
- #340 → #281 (also needs #277) · #294 (also needs #282+#277) · #304 ·
  #313
- (#333 satisfied — landed) #345 → #336 → #337
- #284 + #335 (+ #333 satisfied — landed) → #285
- #301 → #292 → #270 → #272 → #271 → #273 (also needs #299) → #275
  (also needs #342+#344)
- #325 → #291 (also needs #340) · #314 (also needs #309)
- #377 → #378 → #382 (also needs #377) · #379
- #380 → #381

## Held items — flush out the decision, don't stall on it

These need a named decision or operator action. For EACH, produce a
Jev-flushed decision brief and post it on the issue: the options, the
trade-offs with evidence, and a Jev-backed recommendation — so Lucas can
decide with one tap. Then build it the moment the decision lands. NEVER
decide these yourself; NEVER let a held item stall unblocked work — keep
working the ready chains while decisions are pending.
- #294 — router-flag decision (after its prerequisites land)
- #270–273 — debut-scope decision for the driver/NL-control line
- #275 — Hourglass philosophy decision
- #291 — runner-vs-manual decision
- #325 — aggregation-rule decision (unblocks #291/#314)
- #280 — strike policy + apply-lane weights decision
- #281 — schedule/venue/bar decision
- #282/#283 — rescope decision (served UI, not Electron; Jest IPC
  inapplicable as written)
- #314 — parity-bars decision
- #336 — never-prune list decision (after #333 landed via PR #398)
- #337 — checklist go/no-go criteria decision
- #303 — enforce-vs-disclose decision (Jev-escalated, held)
- #377–382 — tracking/low-priority lane items (build in chain order)
- Public deploy — needs operator's CF_API_TOKEN + D1/KV IDs

## Phase 3 (woven in, not separate) — Visibility site + deploy

- FILE the visibility-site issue (VIS) first: a SEPARATE public site
  showing models and their stats worldwide — roster, usage, success rates,
  cost per successful task, latency. Ingestion from opt-in users ONLY,
  AGGREGATES ONLY (never raw ledger rows, keys, tokens, paths, or
  user-attributable data); sanitization/aggregation rules documented and
  hermetic-tested; opt-in explicit and revocable. Drive downloads:
  prominent `pip install sovereign-harness` path, console scripts, docs
  link, and the 0.23¢ driving visual audit receipt featured honestly
  (cheap AND evidence-backed, including what it did not assess). No
  invented metrics: every figure traces to a receipt.
- DEPLOY via the local Cloudflare setup (operator-confirmed); verify the
  public URL serves the visibility site and the results page; record the
  URL. If the local setup is missing or broken, report exactly what is
  missing — never claim a deploy that did not happen.

## Phase 5 — #277 closing verification round

One structured round across all four surfaces — CLI, GUI, MCP, website —
with a filed report (runtime, outputs, exit codes, calls, timing, costs).
Explicit findings even where there are none; 403/unverified never passes.
Each defect found is fixed with a test-bearing PR or filed with severity.
Known gaps to cover: GUI loopback-browser path (no native pywebview),
installed MCP runtime identity via a real-client check, live model/Jev
semantic approvals. The filed matrix is the receipt.

## Standing constraints

- One PR at a time, green-CI merges; main protection active (11 required
  checks, 0 approvals). Keep main green throughout.
- Do NOT reopen #333's shape (DECIDED: (b)). Do NOT print/log/commit any
  key value. Do NOT loosen any audit/Jev bar, baseline, or pinned score.
  Do NOT raise any ceiling. Do NOT re-run paid attestation. Do NOT claim
  a deploy, a merge, or a completion that did not happen.
- Jev gate v1 before every GitHub comment, review, approval, or
  disposition; below-bar → auto-investigate per policy; only actual risk
  blocks. Choice confidence is telemetry only.
- If a spec is ambiguous or contradicted, stop that issue and report it
  with the evidence — do not guess, do not sprawl into a redesign.

## Done looks like

All 60 issues landed with receipts and dispositions (or explicitly blocked
with a named unblocker); #306 open and blocked with its named unblocker; #333 built as (b)
across all writers — DONE via PR #398; visibility site live at a recorded public URL on
sanitized aggregated opt-in stats; #277 round filed with defects fixed or
filed; main green; 0 open PRs for this push; #295, queue.json, and the
results page true; a final report naming what landed, what remains, and
who owns each remaining item.
