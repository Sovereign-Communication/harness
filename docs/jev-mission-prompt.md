# Freebuff Mission — paste prompts

**Canon:** [jev-roadmap.md](jev-roadmap.md) (STATUS + **P2 repair playbook**)

Use this file for the **next** Freebuff pass. The old “P1 incomplete” relaunch prompt is **obsolete** — P1 is merged (PR #35). Running it will waste a pass redoing shipped work.

---

## RE-LAUNCH P2 repair (use this now)

**Label:** `RELAUNCH P2 repair — unblock PR #36 (no P1 redo)`

```text
RE-LAUNCH. Reading is not enough. This run must produce file edits and a commit
on the EXISTING P2 branch. Do not redo P1. Do not start P3.

TRUTH (trust this, not a dirty local main STATUS):
- P1 COMPLETE — PR #35 merged to origin/main (9d5ff14). Leave Harness-jev-p1 alone.
- P2 WIP — worktree C:\Users\SCM\Documents\GitHub\Harness-jev-p2
  branch feat/jev-p2-system-one-pillars (PR #36 OPEN), tip c50b22e.
- PR #36 CI: unittest jobs pass; AUDIT FAILS D12 changed-line coverage 29/43=67% (bar 95%).
- Operator local re-run: tests/test_jev_lane_parity.py FAIL x2
  (no canned chat response left / HARNESS_READY extra round) — tests are not hermetic
  against real machine settings.
- JEV-P2-jury (lean typed pre-gate) is DEFERRED — do not claim it complete.
- Status is "in progress / blocked on evidence" — NOT complete.

CANON: docs/jev-roadmap.md section "P2 repair playbook". If that section is missing
in your checkout, follow this prompt in full — it is the playbook.

ONLY WORK THIS PASS (ordered):

1) Hermetic lane parity
   Fix tests/test_jev_lane_parity.py so apply/batch envelope tests pass on a
   machine WITH real harness settings AND on clean CI.
   Force test settings: jev_api_key=None, consent off, no extra readiness chat
   under test, FakeTransport posts sized for every apply chat call.
   Do not weaken production fail-closed apply behavior to green one test.

2) D12 coverage — execute these changed lines with real tests
   apply_state.py:41
   consent.py:225,226,231
   jev_policy.py:227,231,233,235,239,241,242
   waist.py:1018,1019,1020
   Then run python audits/self/audit.py until BAR MET (D12 >= 95%).
   Do NOT game coverage_baseline.json to hide untested new lines.

3) Honest STATUS on the PR branch docs
   Tracker P2 = in progress/repair until merge; after merge only with audit green
   + gates green + jury still listed deferred.
   system-one M4: P1 complete via PR #35 (remove "not merged yet").
   M2/M3 [x] only when named tests green on merge tip.

4) Run and PASTE raw output:
   $env:PYTHONPATH = "C:\Users\SCM\Documents\GitHub\Harness-jev-p2"
   python -m unittest tests.test_jev tests.test_jev_smoke tests.test_jev_policy tests.test_jev_lane_parity tests.test_jev_ledger_spend tests.test_jev_triage tests.test_consent_confidence tests.test_min_confidence_gating tests.test_agent.TestHourglassLane tests.test_waist -v
   python audits/self/audit.py

5) Commit + push EXISTING branch feat/jev-p2-system-one-pillars to PR #36.
   Wait for CI. Merge ONLY when audit + all tests are green.
   Then update STATUS P2 complete (jury deferred noted) and stop — report ready for P3.

FORBIDDEN: redo P0/P1; new plans/docs outside canon STATUS; edit Harness-jev-p1;
mark complete while audit/local gates red; merge red; provider brand hardcoding;
claim done without commit + green tests + audit paste.

If blocked: STATUS blocked + exact failing command/output. No "will do".
```

**savedMissions JSON**

```json
{
  "label": "RELAUNCH P2 repair — unblock PR #36 (no P1 redo)",
  "prompt": "RE-LAUNCH. File edits + commit required on EXISTING P2 branch only. TRUST: P1 COMPLETE PR #35 origin/main 9d5ff14 — do NOT redo P1, do NOT touch Harness-jev-p1. WIP: C:\\Users\\SCM\\Documents\\GitHub\\Harness-jev-p2 branch feat/jev-p2-system-one-pillars PR #36 OPEN tip c50b22e. BLOCKERS: CI audit FAIL D12 29/43=67% bar 95%; local tests/test_jev_lane_parity.py FAIL x2 (HARNESS_READY extra chat / no canned response — not hermetic). JEV-P2-jury DEFERRED — never claim complete. PLAYBOOK (docs/jev-roadmap.md P2 repair playbook): 1) Make lane-parity hermetic (jev_api_key=None, consent off, no readiness chat under test, transport posts cover every apply call; do not weaken prod fail-closed). 2) Cover D12 lines apply_state.py:41; consent.py:225,226,231; jev_policy.py:227,231,233,235,239,241,242; waist.py:1018-1020 with real tests; audits/self/audit.py BAR MET; no coverage_baseline gaming. 3) Honest STATUS on PR branch (P2 in progress/repair; M4 P1 complete PR #35; jury deferred). 4) PYTHONPATH=Harness-jev-p2 run unittest tests.test_jev tests.test_jev_smoke tests.test_jev_policy tests.test_jev_lane_parity tests.test_jev_ledger_spend tests.test_jev_triage tests.test_consent_confidence tests.test_min_confidence_gating tests.test_agent.TestHourglassLane tests.test_waist -v AND python audits/self/audit.py — paste raw output. 5) Push same branch to PR #36; merge only when CI audit+tests green; then STATUS P2 complete with jury deferred; stop and report ready for P3. FORBIDDEN: redo P0/P1; P3/HUL; merge red; fake done; edit locked P1 worktree; brand hardcoding."
}
```

---

## Full mission (after P2 is merged)

**Label:** `Harness mission — jev+HUL`

```text
Harness mission. Read docs/jev-roadmap.md only (canonical STATUS + playbooks).
Trust origin/main STATUS, not a dirty local copy that still says P1 incomplete.
Execute every incomplete STATUS row until Exit: current phase playbook → gates
(local + CI audit) → commit → push → PR → merge only when green → verify
origin/main → update STATUS → next phase immediately.
Do not stop after one green PR. Do not fake done. No provider brands in phase
work. Stop only if blocked (STATUS + exact evidence) or P4+HUL complete /
honest terminal with FINDINGS.md.
```

---

## Obsolete prompts (do not use)

- `RELAUNCH P1 — edit or report blocked` — P1 shipped in PR #35; using this wastes a pass and can fight merged STATUS.
