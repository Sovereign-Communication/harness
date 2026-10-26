# Mission (condensed): full burndown — keep it on track

Objective: take the full burndown prompt
(`docs/issue-plans/burndown-prompt.md`) from first issue to done, fully.
You pull the plan from GitHub, you execute it, you land it — no issue left
half-done, no aspect left open.

## The loop (repeat until done)

1. Work the FIRST unfinished issue in dependency order, completely: spec
   read, implementation, hermetic tests through the real surface, local
   gates + audit + Jev gate green, PR, all required CI green, merge, main
   green, receipt doc, disposition naming merge commit + evidence.
2. Update #295, queue.json, and the results page as each issue lands, so
   the public counts never lie.
3. Move to the next issue. If an issue blocks (held decision, missing
   operator input), write the blocker down exactly, post its Jev-flushed
   decision brief where one is needed, skip to the next unblocked issue,
   and keep going — never stall the burndown on one blocked issue, never
   mark blocked work done.
4. Session closeout, every session: GUI (8800 /api/status 200) and MCP
   (stdio smoke, 30 tools) verified up and left running; main SHA, CI
   state, issues landed this session, and next issue stated plainly.

## Order

Ready-now first (#333 (b), #306, and the unblocked list), then the chains
in dependency order (#345→#346→#342→#343/#344, #340→#280→#318→#319,
#301→#292→#270→#272→#271→#273→#275, and the rest), visibility-site issue
filed + site built + deployed via local Cloudflare woven in, decision
briefs posted for every held item as its turn comes, and #277 LAST as the
closing verification round over everything built.

## Hard constraints

- Do NOT reopen #333 (DECIDED: (b)). Do NOT print, log, or commit any key
  value. Do NOT loosen any audit/Jev bar, baseline, or pinned score. Do
  NOT raise any ceiling. Do NOT claim a landing that did not happen.
- One PR at a time, green CI, merge commits on main. Jev gate v1 before
  every GitHub comment, review, approval, or disposition.
- If a spec is ambiguous or contradicted, stop that issue and report it
  with the evidence — do not guess.

## Done looks like

All 60 issues landed with receipts and dispositions (or explicitly blocked
with a named unblocker); #306 installed + EV-8 receipt; #333 built as (b)
across all writers; visibility site live at a recorded public URL; #277
round filed with defects fixed or filed; main green; inventory true; a
final report naming what landed, what remains, and who owns each remaining
item. The mission ends only when every issue is landed or explicitly
blocked with a named unblocker — not when the session ends.
