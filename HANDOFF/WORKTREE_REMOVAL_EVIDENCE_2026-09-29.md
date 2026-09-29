# WORKTREE REMOVAL EVIDENCE — Harness

**Date:** 2026-09-29 12:34 HST · **Operator tree:** `C:\Users\SCM\Documents\GitHub\Harness`
**Remote:** `https://github.com/Sovereign-Communication/harness.git`
**Remote `main` at audit time:** `10f14e0` (PR #140, `codex/hv5-planning-invocation-rebased`)
**Method:** read-only. `git fetch origin --no-tags` (no `--prune`, so no ref was
removed), then `git worktree list --porcelain`, then `git ls-remote` for every
branch. **Nothing was removed, pruned, or deleted. No worktree was created,
removed, or modified by this pass** other than the single scratch worktree used
to write this file, which is itself listed in the table below.

Every verdict below is grounded in a remote hash comparison, never a local
tracking ref.

---

## 0. HEADLINE: ONE COMMIT EXISTS ONLY ON THIS DISK

**`.freebuff/worktrees/9531ead7-ccad-4d47-bb73-3b909123fff8` is checked out at a
DETACHED HEAD `3c3fc5a350a58de3d35a5b76ba6b32b421381ab7`, and that commit is on
nothing.**

- not on `origin` (`git branch -r --contains 3c3fc5a` → empty)
- not on any **local** branch either (`git for-each-ref --contains 3c3fc5a` → empty)
- exactly 1 commit unique to it versus every remote ref:
  `git rev-list 3c3fc5a --not --remotes` → `3c3fc5a`

Its content is real work, not an artefact:

```
3c3fc5a  WIP: freebuff/chat-topic-title-request-9531ead7-ccad-4d47-bb73-3b909123fff8
         harness/jev_packs.py  | 114 ++++++++++++++++++++++++-------
         harness/jev_policy.py  |   5 ++-
         2 files changed, 103 insertions(+), 16 deletions(-)
```

The work adds `validate_log_pack`, `validate_phase_completion_pack`,
`phase_completion_score_100` (the only sanctioned TypeSafe level-index → 0-100
map), `phase_completion_level_for_index`, `phase_completion_question_pack`, and a
shared `_validated_score_block`, wired into `jev_policy.py` via
`PHASE_COMPLETION_SITE`.

**Removing that worktree destroys this commit.** A detached HEAD with no ref
pointing at it is unreachable and is a prime candidate for `git gc` pruning. This
is exactly the condition this exercise exists to catch, and it is the only one
found.

**Required before any removal:** create a branch at `3c3fc5a` and push it, e.g.

```bash
git -C .freebuff/worktrees/9531ead7-ccad-4d47-bb73-3b909123fff8 \
    checkout -b freebuff/chat-topic-title-request-9531ead7
git push origin freebuff/chat-topic-title-request-9531ead7
```

then confirm with `git ls-remote`. **No action has been taken on this** — the
audit pass was read-only by instruction.

---

## 1. VERDICT TABLE

`remote` column is the `git ls-remote` result for that exact branch. "contained in"
means the HEAD commit is an ancestor of a remote branch even when the branch name
itself does not exist on the remote.

| # | Worktree | Branch | HEAD | Working tree | Untracked entries | Remote | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | `.codex/…/df-audit5-fix/Harness` | `codex/hv1-hourglass-integration` | `bbfa7f3` | clean | none | branch **absent**; HEAD contained in `origin/main`, `origin/codex/hv5-planning-invocation-rebased`, `origin/freebuff/mission-b2d7eb8e…` | **SAFE TO REMOVE** |
| 2 | `.codex/…/lane3-backlog/Harness` | `codex/lane3-jev-phase-local-accounting-notice` | `6269636` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 3 | `.codex/…/oc-handoff-jev-gate/Harness` | `codex/oc-handoff-jev-gate` | `87a672e` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 4 | **operator tree** `…/Harness` | `add-civicscope-completion-contract` | `163f103` | **not clean** | `Harness-jev-d10-d12/` — a **registered nested worktree**, not noise | `== HEAD` | **NEEDS ACTION FIRST** — see §2 |
| 5 | `…/Harness-hv-2` | `feat/hv-2-brief` | `aae1b1c` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 6 | `…/Harness-jev-hourglass-driver` | `freebuff/jev-hourglass-driver-20260923` | `c72e74c` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 7 | `…/Harness-jev-log` | `feat/jev-p2-escalation-lifecycle-canon` | `5b254df` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 8 | `…/Harness-l3-117` | `codex/l3-117-publish-workflow-verification` | `fcf7bc8` | not clean | `.l3venv/` (1 779 files, 51 MB) + `.l3-verify/` (probe jsonl + lock) — **both regenerable noise** | `== HEAD` | **SAFE TO REMOVE** (noise only; re-creating the venv costs a `pip install`) |
| 9 | `…/Harness-l3-118` | `codex/l3-118-verification-battery` | `49e9c78` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 10 | `…/Harness-l3-118b` | `codex/l3-118b-audit-score-refresh` | `8445583` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 11 | `…/Harness-l3verify` | `codex/l3verify-coverage-baseline-refresh` | `528fdf5` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 12 | `…/Harness-verify-panel-hg` | `fix/verify-panel-hg-preview` | `8668669` | not clean | `.venv/` only — **noise** (430 tracked files, 0 tracked modifications) | `== HEAD` | **SAFE TO REMOVE** |
| 13 | `.freebuff/…/2978d197` | `feat/hv4-stage-composition` | `3f12163` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 14 | `.freebuff/…/2c3868e7` | `freebuff/isolated-mission-…` | `b10a06b` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 15 | `.freebuff/…/4e01c2ef` | `feat/hv1-redundancy-guard` | `e8538f9` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 16 | `.freebuff/…/5a7406c2` | `freebuff/please-find-the-last…` | `001b392` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 17 | `.freebuff/…/704d1bdf` | `feat/hv3-budget-wiring` | `5272dcd` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 18 | **`.freebuff/…/9531ead7`** | **DETACHED HEAD** | **`3c3fc5a`** | clean | none | **nothing contains it** | **NEEDS ACTION FIRST** — see §0 |
| 19 | `.freebuff/…/b2d7eb8e` | `freebuff/mission-b2d7eb8e…` | `f875685` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 20 | `.freebuff/…/ef2fdc7d` | `mission/lane3-verify-infra` | `ddb0b4d` | not clean | `.l3-scratch/` (probe jsonl + lock) — **noise** | `== HEAD` | **SAFE TO REMOVE** |
| 21 | `.freebuff/…/fc321fcd` | `feat/hv5-execution-handoffs` | `bff19c9` | clean | none | `== HEAD` | **SAFE TO REMOVE** |
| 22 | **nested** `…/Harness-jev-d10-d12` | `jev-d10-d12` | `29c28de` | clean | none | `== HEAD` | **SAFE TO REMOVE** — see §3 |
| 23 | `…/wt-harness-canary` | DETACHED HEAD | `6961c09` | clean | none | not any remote *tip*; contained in `origin/main` + 3 others | **SAFE TO REMOVE** |
| 24 | `…/wt-harness-canonical` | DETACHED HEAD | `ad4a300` | clean | none | not any remote *tip*; contained in `origin/main` + 3 others | **SAFE TO REMOVE** |
| 25 | scratch worktree for this document | `handoff/worktree-removal-evidence-2026-09-29` | this file | clean | none | this branch | **SAFE TO REMOVE** once merged |

**Totals: 22 SAFE TO REMOVE · 2 NEEDS ACTION FIRST · 0 UNCERTAIN.**

### Why "absent on origin" is not itself disqualifying

Row 1's branch `codex/hv1-hourglass-integration` does not exist on the remote,
which looks alarming. It is not: its HEAD `bbfa7f3` is an ancestor of
`origin/main`, so the commits are reachable from GitHub under other names. The
correct test is reachability from **some** remote branch, not tip equality on the
worktree's own branch name. Rows 23 and 24 are the same case for detached heads.

---

## 2. THE NESTED WORKTREE (the easiest thing to destroy by accident)

`Harness-jev-d10-d12/` sits **inside** the operator tree and is a fully
registered worktree. It was deliberately left untracked in the operator tree and
deliberately never committed or gitignored, so it appears in the operator tree's
own `git status` as `?? Harness-jev-d10-d12/`.

**Verified against the remote:**

- path `…/Harness-jev-d10-d12`, branch `jev-d10-d12`, HEAD `29c28de`
- working tree clean, 0 untracked entries
- `git ls-remote origin refs/heads/jev-d10-d12` → `29c28de` — **`origin == HEAD`**
- contained in `origin/jev-d10-d12`

So its work **is** on GitHub and it is individually safe to remove.

**The hazard is structural, not about its content.** Because it lives inside the
operator tree's working directory, any recursive delete aimed at the operator
tree takes the nested worktree with it. Two consequences for whoever runs the
cleanup:

1. Removing the operator tree requires first deciding about the nested worktree —
   it cannot be swept up silently. That is why row 4 is NEEDS ACTION FIRST.
2. `git worktree remove` on the operator tree refuses while nested worktrees are
   registered; a plain filesystem delete would not. Prefer the git command.

Also note the operator tree itself is on `add-civicscope-completion-contract`,
which is **broken at runtime** and must not be mistaken for a merge candidate: it
carries `wip(jev): preserve operator-tree token-budget and final-alignment work`,
where `harness/apply_gate.py:58` reads `req.token_budget` but the frozen
`ApplyRequest` in `harness/apply_state.py` declares no such field, so that path
raises `AttributeError` whenever `require_diff_authorization` is set (reachable
from `harness/agent.py` lines 1044, 1082, 1102, 1123).

---

## 3. THE DISAPPEARED WORKTREE — RESOLVED, NOTHING LOST

`.codex/worktrees/harness-finalize/Harness` vanished from
`git worktree list --porcelain` during the preservation pass. It was not removed
by that pass, which ran no `worktree remove`, `prune`, or delete against that
path; it was the same worktree observed going from 2 dirty entries to clean
earlier in the session, so another lane was active there.

Re-verified now:

- the directory still exists on disk but is **empty of repository content**
  (`ls -A …/Harness` → 24 entries, no `.git`; `git -C` → "not a git repository"),
  and there is no admin record under `.git/worktrees/`
- its last known HEAD `bff19c9f210cd47255b63a037bec5433b0449f9a` is **still on the
  remote**: `git ls-remote origin refs/heads/feat/hv5-execution-handoffs` →
  `bff19c9`
- `git rev-list bff19c9 --not --remotes` → **0 commits**

**No work was lost.** Its committed history is on GitHub, and it was clean at last
observation so there was nothing uncommitted to lose. The lingering directory is
empty and can be reclaimed, but that is a cleanup action, not an audit action.

---

## 4. A DISK-ONLY RISK CLASS NO WORKTREE CHECK WILL FIND: STASHES

Stashes live in `refs/stash`, which is never pushed. Every one of the four
currently holds real parked work:

| Stash | Commit | Unique vs remotes | Content |
|---|---|---|---|
| `stash@{0}` | `b6afa3b9` | **3 commits** | On `codex/hv5-planning-invocation-rebased`: "preserve HV-5 implementation while gating HV-4 planning invocation" — 16 files, +930/−119 |
| `stash@{1}` | `50471532` | **3 commits** | On `codex/hv5-planning-invocation`: "preserve mixed HV-1-HV-6 candidate before phase rebase" — 41 files, +4 114/−346 |
| `stash@{2}` | `9c617277` | 0 commits | On `feat/hv4-waist-composition`: "planning waist refactor (**broken**: 2 ruff errors, 4+16 test failures) — parked by takeover 2026-09-28" — 1 file, +265/−190 |
| `stash@{3}` | `5966d9b9` | 0 commits | On `feat/hv1-typed-integrations`: "snapshot in-progress HV-1 work before rebuilding from pristine main" — 5 files, +420/−38 |

Roughly **5 700 insertions** of work that exists only on this disk. Note that
`stash@{0}` and `stash@{1}` sit on `codex/hv5-planning-invocation*` branches whose
work has just been merged into `main` as PR #140 — those branches are now prime
cleanup candidates, which makes the stashes *more* exposed, not less, because the
stash is the only place the parked deltas still live.

`git stash drop`, or any `git gc` that prunes unreachable stash commits, destroys
these permanently. `stash@{0}` and `stash@{1}` should be turned into branches and
pushed before any branch-level cleanup.

---

## 5. CONCURRENCY: ANOTHER LANE IS PRESENT BUT CURRENTLY IDLE

Evidence gathered at 12:34–12:36 HST:

- **No non-noise file was modified in any worktree in the ~2 hours before the
  audit.** The newest non-noise mtimes are `docs/jev-roadmap.md` 08:48 and
  `harness/jev_completion.py` 08:54 in the operator tree; everything else is
  older. (The 11:56 and 12:26 commits in the operator tree were this session's
  own preservation commits.)
- **No new remote activity**: remote head count is 121, identical to the count
  after the preservation pass; `main` is still `10f14e0`.
- **No git locks**: `find .git -name '*.lock'` → none, so no operation is
  mid-flight.
- **Processes present**: multiple `git.exe` and `python.exe`/`node.exe`, consistent
  with editor tooling and possibly an idle agent host. Process presence alone is
  not proof of live work.
- `.git/worktrees/*` admin directory mtimes all fall within the audit window
  (12:32–12:34), which is explained by `git worktree list`/`git status` touching
  them, not by a checkout.

**Assessment: another lane existed and did unregister `harness-finalize` earlier
in the session, but nothing has touched the repo in the last ~2 hours.** That is
"probably idle", not "proven idle". Deleting a worktree another lane is actively
using destroys work exactly as surely as an unpushed commit does, so the cleanup
should re-run the §0 check immediately before each removal rather than trusting
this snapshot.

---

## 6. WHAT THIS PASS DID NOT DO

- No worktree was removed, pruned, or deleted.
- No branch, tag, or ref was deleted or force-moved; the fetch ran **without**
  `--prune`.
- Nothing was committed on any lane branch, and `3c3fc5a` was **not** rescued —
  §0 documents it and leaves the decision deliberate.
- The four stashes were not dropped, popped, or converted.
- No upstream was configured on the branch carrying this document, matching the
  convention used by the preservation pushes.

## 7. RECOMMENDED ORDER OF OPERATIONS

1. Branch and push `3c3fc5a` (§0). **Until this is done, worktree 18 must not be
   removed.**
2. Branch and push `stash@{0}` and `stash@{1}` (§4), the two with unique commits.
3. Decide the nested `Harness-jev-d10-d12` worktree separately from the operator
   tree (§2); it is individually safe, but it must not be swept up by a recursive
   delete.
4. Re-run the §0 reachability check immediately before each removal.
5. Then, and only then, remove the 22 SAFE TO REMOVE worktrees, the empty
   `harness-finalize` directory, and this document's scratch worktree.
