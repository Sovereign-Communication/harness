# Mission (condensed): worldwide push — keep it on track

Objective: take the condensed plan
(`docs/issue-plans/worldwide-push-plan.md`, including its full-burndown
appendix) from first phase to done, fully. You pull the plan from GitHub,
you execute it, you land it — no phase left half-done, no aspect left open.

## The loop (repeat until done)

1. Work the FIRST unfinished phase completely: implement, test, verify
   through the real surface (not assertions), Jev-gate, PR, green CI,
   merge, main green, disposition recorded.
2. Update #295, queue.json, and the results page as each phase lands, so
   the public counts never lie.
3. Move to the next phase. If a phase blocks, write the blocker down
   exactly (what, why, what unblocks it), skip to the next unblocked phase,
   and keep going — never stall the push on one blocked phase, never mark
   blocked work done.
4. Session closeout, every session: GUI (8800 /api/status 200) and MCP
   (stdio smoke, 30 tools) verified up and left running; main SHA, CI
   state, and next phase stated plainly.

## Phase order

0. Services up. 1. #306 — find the key, install it (0600), probe receipt +
   EV-8 flip. 2. #333 — build (b) ledger-backed across ALL writers
   (DECIDED, no shape question). 3. Visibility site — file the tracking
   issue, build the separate public stats hub on sanitized aggregated
   opt-in data, feature the 0.23¢ receipt and the download path. 4. Deploy
   via local Cloudflare; verify the public URL; record it.

## Hard constraints

- Do NOT reopen #333. Do NOT print, log, or commit any key value. Do NOT
  loosen any audit/Jev bar, baseline, or pinned score. Do NOT raise any
  ceiling. Do NOT start #277. Do NOT claim a deploy that did not happen.
- One PR at a time, green CI, merge commits on main. Jev gate v1 before
  every GitHub comment, review, approval, or disposition.
- If a phase's spec is ambiguous or contradicted, stop that phase and
  report the ambiguity with the evidence — do not guess.

## Done looks like

#306 installed + EV-8 receipt on main (or blocker named with searched
locations); #333 built as (b) across all writers, all aspects verified;
visibility site live at a recorded public URL; main green; inventory true;
a final report naming what landed, what remains, and who owns each
remaining item. The mission ends only when every phase is landed or
explicitly blocked with a named unblocker — not when the session ends.
