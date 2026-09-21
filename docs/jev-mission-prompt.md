# Freebuff Mission — paste prompts

**Canon:** [jev-roadmap.md](jev-roadmap.md) only.

Trust **origin/main** STATUS. A dirty local copy that still says “P1 incomplete” or “P2 in progress / blocked” is stale.

---

## RE-LAUNCH after P2 merge (use this now)

**Label:** `RELAUNCH — P3 / HUL-A / HG composition (P2 complete)`

```text
RE-LAUNCH. Reading is not enough. This run must produce file edits and a commit.

TRUTH (trust origin/main, not dirty local STATUS):
- P0 + P1 + P2 COMPLETE — PR #34 / #35 / #36 (405bbc1) merged; post-merge main CI green.
- JEV-P2-jury remains DEFERRED — do not invent a jury to claim extra completeness.
- Do NOT redo P0–P2. Do NOT edit Harness-jev-p1 or Harness-jev-p2 (locked).
- Open tracks in canon STATUS: JEV-P3, JEV-P4, JEV-P5 issue-sort, HUL-A..D,
  HG hourglass composition, MS cheapest-capable + context condensation.

WORKTREES (pick the track you are assigned; do not invent parallel plans):
- P3 utilization: C:\Users\SCM\Documents\GitHub\Harness-jev-p3
  branch feat/jev-p3-utilization
- HUL-A mission pack: C:\Users\SCM\Documents\GitHub\Harness-hul-a
  branch feat/hul-a-mission-pack
- Hourglass composition: C:\Users\SCM\Documents\GitHub\Harness-hg-remain
  branch feat/hourglass-composition
- Docs/STATUS: C:\Users\SCM\Documents\GitHub\Harness-jev-next
  branch feat/jev-mission-next

CANON: docs/jev-roadmap.md sections Tracker + implementer notes for your track.

RULES (every pass):
1) Extend ONE policy owner (jev_policy / spend / waist) — no forks, no second Jev client.
2) Hermetic tests named in canon MUST exist and pass; no coverage_baseline gaming.
3) No provider brand hardcoding in phase code — resolve via router/ladders/MS-*.
4) Live dogfood only when the lane is user-facing; use cheap paid rungs
   (HARNESS_USE_FREE=false; apply deepseek-v4.1-flash class; judge glm-5.3-flash class).
5) Gates green locally + audit BAR MET → push branch → PR → merge ONLY when CI green
   → update STATUS with evidence → continue next incomplete row.
6) If blocked: STATUS blocked + exact command/output. Never "will do".

FORBIDDEN: fake complete; merge red CI; brand mandates in phase DoD; parallel plans
outside canon STATUS; redoing shipped phases.
```

**savedMissions JSON**

```json
{
  "label": "RELAUNCH — P3 / HUL-A / HG composition (P2 complete)",
  "prompt": "RE-LAUNCH. File edits + commit required. TRUTH origin/main: P0+P1+P2 COMPLETE PR #34/#35/#36 (405bbc1). JEV-P2-jury deferred. Do NOT redo P0-P2 or edit locked P1/P2 worktrees. Open tracks: JEV-P3 utilization (Harness-jev-p3 / feat/jev-p3-utilization), HUL-A mission pack (Harness-hul-a / feat/hul-a-mission-pack), HG composition (Harness-hg-remain / feat/hourglass-composition), plus P4/P5/HUL-B..D/MS when scheduled. CANON docs/jev-roadmap.md only. Implement your assigned track: extend one policy owner; hermetic named tests green; no coverage_baseline gaming; no brand hardcoding; live dogfood cheap-paid only when user-facing; local gates + audit BAR MET → push → PR → merge only when CI green → STATUS evidence → next row. Blocked = exact command/output. Forbidden: fake complete, merge red, parallel plans."
}
```

---

## Full mission (operator loop)

**Label:** `Harness mission — remaining product tracks`

```text
Harness mission. Read docs/jev-roadmap.md only.
Trust origin/main STATUS. Execute every incomplete STATUS row until Exit:
phase playbook → local gates + audit → commit → push → PR → merge only when
CI green → post-merge verify → update canon STATUS → immediately continue.
Dogfood user-facing lanes. Paid cheap rungs for live evidence. No brand
hardcoding in phase code. Stop only if blocked (STATUS + exact evidence) or
Exit rows complete (Jev P4 + HUL product/open pack + FRP process).
```

---

## Obsolete prompts (do not use)

- `RELAUNCH P1 — edit or report blocked` — P1 shipped (PR #35).
- `RELAUNCH P2 repair — unblock PR #36` — P2 shipped (PR #36 `405bbc1`).
