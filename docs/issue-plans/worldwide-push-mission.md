# Mission: worldwide push — keep it on track

Objective: take the worldwide push plan
(`docs/issue-plans/worldwide-push-plan.md`) from first phase to done, fully,
iterating through all phases. You pull the plan from GitHub, you execute it,
you land it — no phase left half-done.

## The loop (repeat until done)

1. Read the plan. Work the FIRST unfinished phase, completely: implement,
   test, verify through the real surface (not assertions), Jev-gate, PR,
   green CI, merge, main green, disposition recorded.
2. Update the tracker (#295), queue.json, and results page as each phase
   lands, so the public counts never lie.
3. Move to the next phase. If a phase blocks, write the blocker down
   exactly (what, why, what unblocks it), skip to the next unblocked phase,
   and keep going — never stall the whole push on one blocked phase, never
   mark blocked work done.
4. Session closeout, every session: GUI (8800 /api/status 200) and MCP
   (stdio smoke, 30 tools) verified up and left running; main SHA, CI state,
   and next phase stated plainly.

## Phase order (from the plan)

0. Services up. 1. #306 — find the Fireworks key, install it (0600),
   probe receipt + EV-8 flip. 2. #333 — build Lucas's picked shape, or
   surface it as the single decision if he has not picked. 3. Visibility
   site — file the tracking issue, build the separate public stats hub on
   sanitized aggregated opt-in data, feature the 0.23¢ audit receipt and the
   download path. 4. Deploy via the local Cloudflare setup; verify the
   public URL serves; record it.

## Hard constraints

- Do NOT decide #333. Do NOT print, log, or commit any key value. Do NOT
  loosen any audit/Jev bar, baseline, or pinned score. Do NOT raise any
  ceiling. Do NOT start #277. Do NOT claim a deploy that did not happen.
- One PR at a time, green CI, merge commits on main. Jev gate v1 before
  every GitHub comment, review, approval, or disposition.
- If a phase's spec turns out ambiguous or contradicted, stop that phase
  and report the ambiguity with the evidence — do not guess.

## Done looks like

#306 installed + EV-8 receipt on main (or blocker named with searched
locations); #333 built per Lucas's pick (or the single surfaced decision);
visibility site live at a recorded public URL; main green; inventory true;
a final report naming what landed, what remains, and who owns each
remaining item. The mission ends only when every phase is landed or
explicitly blocked with a named unblocker — not when the session ends.
