# DISK RECLAIM LANE — Harness

**Lane owner:** Harness · **Repo + 8 worktrees, 34 dirty files, 1 stash**
**Packet written:** 2026-09-29 ~11:50 HST · **Status:** WIP, idle (newest edit 05:17)

> This packet is self-contained. Backup and reclaim steps are inline.

---

## 0. OPENING CHECK (mandatory)

This repo was **actively committing during triage** — it went from 7 dirty to
clean mid-session. Re-read status before acting.

```bash
cd /c/Users/SCM/Documents/GitHub/Harness
git status --porcelain
git rev-parse --abbrev-ref HEAD
```

---

## 1. BACKUP — lane handoff files to GitHub

**Risk: none.** Commits and pushes only.

### Key structural fact

**All eight `Harness-*` worktrees are already `MERGED_INTO_MAIN` — 0 commits
ahead of `origin/main`.** Their branch *history* is safe on GitHub. Only the
uncommitted delta is at risk.

**Consequence: no merge, no rebase, no cherry-pick is needed for any worktree
in this lane.** A plain commit-to-branch captures everything. This matters —
`SCMessenger` has 24 commits separating its lane branch from main, and a rebase
there would collide with active work. That risk does not apply here.

### State captured at packet time

| Worktree | Dirty | Newest edit | Branch state |
|---|---|---|---|
| `Harness-jev-hourglass-driver` | 19 | 2026-09-23 19:23 | `freebuff/jev-hourglass-driver-20260923` |
| `Harness-hv-2` | 6 | 2026-09-25 13:44 | `feat/hv-2-brief` |
| `Harness-l3-117` | 4 | 2026-09-28 10:43 | **detached HEAD** |
| `Harness-l3-118` | 2 | 2026-09-28 10:17 | **detached HEAD** |
| `Harness-l3-118b` | 1 | 2026-09-28 09:46 | **detached HEAD** |
| `Harness-l3verify` | 1 | 2026-09-28 13:28 | **detached HEAD** |
| `Harness-verify-panel-hg` | 1 | 2026-09-23 08:25 | `fix/verify-panel-hg-preview` |
| `Harness-jev-log` | 1 | 2026-09-21 12:04 | `feat/jev-p2-escalation-lifecycle-canon` |
| `Harness` (main) | untracked `Harness-jev-d10-d12/` | 2026-09-29 | `add-civicscope-completion-contract`, **no upstream** |

Full `HANDOFF/` sets are present in `Harness-hv-2`,
`Harness-jev-hourglass-driver`, and `Harness-jev-log`:
`BOD_STATE.md`, `CEO_STATE.md`, `CTO_STATE.md`,
`CTO_HANDOFF_HARNESS_JEV_FREEBUFF_AUDIT_2026-09-21.md`,
`FREEBUFF_TRANSITION.md`, `JEV_LOG_PROMOTION_DRAFT.md`.

### 1.1 Named-branch worktrees

For each of `jev-hourglass-driver`, `hv-2`, `jev-log`, `verify-panel-hg`:

```bash
cd /c/Users/SCM/Documents/GitHub/Harness-<name>
git switch -c wip/<name>-20260929
git add -A
git commit -m "handoff: preserve <n> uncommitted files from <name>"
git push -u origin wip/<name>-20260929
```

### 1.2 Detached-HEAD worktrees — READ THIS CAREFULLY

`l3-117`, `l3-118`, `l3-118b`, `l3verify` are on **detached HEAD with no
upstream and no branch recording where they belong**. This is precisely how
detached handoff work gets orphaned.

**Branch off the current SHA *before* switching to anything else.**

```bash
cd /c/Users/SCM/Documents/GitHub/Harness-<name>
git rev-parse HEAD                    # <-- record it FIRST
git switch -c wip/<name>-20260929     # branches off that exact SHA
git add -A
git commit -m "handoff: preserve <n> uncommitted files from <name> (detached HEAD)"
git push -u origin wip/<name>-20260929
```

Running `git switch`, `git checkout`, or `git reset` **before** branching off
abandons the SHA. There is no reflog you should rely on here.

### 1.3 Main `Harness` repo + the stash

```bash
cd /c/Users/SCM/Documents/GitHub/Harness
git switch -c wip/harness-civicscope-20260929
git add -A            # captures untracked Harness-jev-d10-d12/
git commit -m "handoff: preserve civicscope lane WIP + Harness-jev-d10-d12 (2026-09-29)"
git push -u origin wip/harness-civicscope-20260929
```

**Then capture the stash in this same session:**

```bash
git stash branch wip/harness-stash-20260929
git push -u origin wip/harness-stash-20260929
```

> **Never `git stash pop`. Never `git stash drop`.**
> `Harness` is the only repo in this audit with a stash. "Leave it in place" is
> **not** safe: the next person (or you, next week) runs `pop`, the applied
> changes conflict, they run `drop`, and the original is gone. Capture it to a
> branch now, while you know what it is.

### Exit gate — do not proceed to Step 2 until every target passes

```bash
# for each of the 8 worktrees + main:
git status --porcelain          # expect: empty
git ls-remote --heads origin | grep 'wip/.*-20260929'   # expect: a line
```

**A branch whose push silently failed still orphans on worktree removal.**
Verify the remote listing, not just the local branch.

---

## 2. RECLAIM — build and runtime caches

Do **not** run before Step 1 passes.

| Target | GB | Risk | Mitigation |
|---|---|---|---|
| `C:\Users\SCM\.gradle\caches` | 1.14 | Build slowdown on next build | `gradle --stop` first; confirm no build in flight |
| `C:\Users\SCM\.gradle\wrapper` | 0.14 | **Breaks offline builds** | **Retain `wrapper/dists`.** Only prune stale dist versions, never all of them |
| `C:\Users\SCM\.gradle\daemon` | 0.07 | None — logs and PID files | Regenerates on next build |
| `.cache\codex-runtimes\codex-primary-runtime` | 1.28 | Codex re-downloads on next launch; **fails if offline** | Confirm network reachability first. **Keep the parent `.cache\codex-runtimes` dir** so it re-populates cleanly |
| `.konan\kotlin-native-prebuilt-windows-x86_64-1.9.20` | 0.27 | Kotlin/Native **compiler toolchain**, not a cache | No project references found. Confirm no Kotlin/Native project exists anywhere before removing |

**Lane reclaim ceiling: 2.90 GB, all conditional on verification.**

### Why `wrapper` needs care but `caches` does not

`.gradle\caches` is pure derived output — losing it costs a slower build, full
stop. `.gradle\wrapper\dists` holds the **Gradle distribution binaries**. If
they are gone and the machine is offline, *no Gradle build can start at all*.
Prune stale dists by version; do not clear the directory.

### Precondition for the `.konan` purge

`.konan` contains only `kotlin-native-prebuilt-windows-x86_64-1.9.20`
(downloaded 2026-07-02). No project in `Documents` references it. But it is a
**toolchain**, so removal means a full re-download:

```bash
grep -rl "kotlin.native.prebuilt" ~/Documents 2>/dev/null   # expect: no matches
```

If anything matches, **hold** — a cached toolchain beats a blocked build.

---

## 3. NOT IN THIS LANE — do not touch

| Path | GB | Reason |
|---|---|---|
| `wt-harness-canary`, `wt-harness-canonical` | — | Clean but **detached HEAD, no upstream** — "is this pushed?" is unanswerable. Treat as WIP. `wt-harness-canonical` last commit 2026-09-21 |
| All 19 `wt-*` worktrees | — | WIP. Withdrawn from reclaim entirely |
| `C:\pagefile.sys` | 18.5 | Left alone per your decision |

---

## 4. RELATED LANE

`Documents/GitHub/REPO_STALE_HANDOFF_2026-09-29.md` covers `OxAlphaAPI`,
`ComfyUI`, and `RepoGraph` — all siblings in `Documents\GitHub`. Run that lane
independently; nothing in it touches `Harness`.
