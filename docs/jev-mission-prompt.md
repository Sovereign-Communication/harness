# Freebuff Mission — paste prompts

**Canon:** `docs/jev-roadmap.md`

---

## RE-LAUNCH (use this — forces real edits)

**Label:** `RELAUNCH P1 — edit or report blocked`

```text
RE-LAUNCH. Reading is not enough. This run must produce file edits and a commit.

CANON: docs/jev-roadmap.md — section “P1 implementer playbook”.
STATUS: 0.0+P0 complete. P1 incomplete. That is the ONLY work this turn.

DO THIS NOW (in order; no planning essay):

1) Open worktree/branch for P1:
   C:\Users\SCM\Documents\GitHub\Harness-jev-p1
   branch feat/jev-p1-policy-and-lanes (base origin/main / d042d70).
   If the worktree is missing, create a fresh branch off origin/main and copy
   any needed WIP — do not wait.

2) IMPLEMENT P1 DoD from the playbook — concrete code edits required:
   - Keep harness/jev_policy.py as the ONE owner (do not fork it).
   - One gate story: if engine.jev_policy is set → agent does NOT re-run Jev;
     if engine has no jev_policy → agent post-gate may heal-retry ONCE and
     final status stays ok after a successful heal apply.
   - Wire waist.compose_plan/confirm to policy.evaluate_plan(site="waist").
   - CLI + MCP envelopes include structural via policy/aggregate_structural.
   - spend.preflight_jev + record_actual; no double-bill.
   - Tests that MUST exist and pass before PR:
       tests/test_jev_policy.py
       tests/test_jev_lane_parity.py
       tests/test_jev_ledger_spend.py
     Also green:
       tests.test_jev
       tests.test_agent.TestHourglassLane
       (especially test_apply_node_jev_structural_evaluation_retry)

3) RUN GATES (show output):
   $env:PYTHONPATH="<worktree>"; python -m unittest tests.test_jev
     tests.test_agent.TestHourglassLane tests.test_jev_policy
     tests.test_jev_lane_parity tests.test_jev_ledger_spend -v

4) COMMIT + PUSH + PR:
   git add -A && git commit -m "feat(jev): JEV-P1 policy owner, lanes, tests"
   push -u origin feat/jev-p1-policy-and-lanes
   open PR titled feat(jev): JEV-P1-…
   Update docs/jev-roadmap.md STATUS on that branch (P1 evidence + PR URL).

5) If you cannot edit (permissions/worktree locked): STOP and write to the
   operator which path is blocked. Do not say “will do” — either edit files
   or name the blocker.

Forbidden: re-implement P0; new planning docs; provider brand hardcoding;
claiming done without commits + green tests.
```

**savedMissions JSON**

```json
{
  "label": "RELAUNCH P1 — edit or report blocked",
  "prompt": "RE-LAUNCH. Reading is not enough. This run must produce file edits and a commit. CANON: docs/jev-roadmap.md section P1 implementer playbook. STATUS: 0.0+P0 complete; P1 incomplete — ONLY work this turn. 1) Worktree C:\\Users\\SCM\\Documents\\GitHub\\Harness-jev-p1 branch feat/jev-p1-policy-and-lanes (or fresh branch off origin/main if missing — do not wait). 2) Implement P1 DoD: keep harness/jev_policy.py ONE owner; one gate story (engine.jev_policy set → agent does not re-run Jev; no policy → agent post-gate once, heal apply stays ok); waist evaluate_plan site=waist; CLI+MCP structural envelope via aggregate_structural; spend preflight_jev + record_actual no double-bill; ADD tests/test_jev_policy.py, test_jev_lane_parity.py, test_jev_ledger_spend.py and keep test_jev + TestHourglassLane green (structural retry). 3) Run those unittest modules and paste results. 4) Commit, push -u origin feat/jev-p1-policy-and-lanes, PR feat(jev): JEV-P1-…, update canon STATUS on the branch. 5) If blocked, name the exact blocker — no 'will do'. Forbidden: redo P0; new plans; brand hardcoding; done without commit+green tests."
}
```

---

## Simple mission (full track — after P1 PR exists)

**Label:** `Harness mission — jev+HUL`

```text
Harness mission. Read docs/jev-roadmap.md only.
Execute every incomplete STATUS row until Exit: finish current phase per its
implementer playbook/notes → gates → commit → push → PR → merge → verify
origin/main → update canon STATUS → next phase immediately.
Do not stop after one green PR. Do not fake done. No provider brands in phase
work (canon § Model selection). Stop only if blocked (STATUS+reason) or
P4+HUL complete / honest terminal with FINDINGS.md.
```
