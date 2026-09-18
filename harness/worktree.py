"""Git-worktree isolation for parallel DAG execution (M3).

Partition rule (frontier consensus, 2026-09-17 — see
docs/hourglass-micro-requests.md): nodes executing CONCURRENTLY in a stage
each get an isolated ``git worktree`` + branch; nodes executing SERIALLY
share the working tree under the per-path mutex. Isolated branches merge
back in topological order (node-id tiebreak — the executor's stage order
is already topological, and within a stage ids sort deterministically);
on merge conflict the node's result becomes ``merge_conflict`` and its
changes are discarded, never force-merged. Before accepting, the
worktree's changes are audited against the node's declared target files:
undeclared writes reject the node (fail-closed), because declared
disjointness cannot establish independence when a model writes outside
its declaration. PR/forge creation is NOT here — branches stay local;
opt-in forge flow is a separate future concern.

Git is host tooling invoked via subprocess (stdlib only). When git or the
repo is unavailable, isolation degrades loudly to shared-tree mutex
execution — the caller decides whether that is acceptable; planning may
degrade, spending may not (no cost semantics live here).
"""
import os
import subprocess

from .errors import HarnessError

GIT_TIMEOUT = 30


def _git(repo, *args):
    """One git subprocess in ``repo``; HarnessError on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True,
            timeout=GIT_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"git {' '.join(args[:2])} failed: {exc}") from None
    if proc.returncode != 0:
        raise HarnessError(
            f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


class WorktreeIsolation:
    """Create/audit/merge/discard per-node git worktrees (one per node)."""

    def __init__(self, repo=None):
        self.repo = os.path.abspath(repo or os.getcwd())

    def available(self):
        """True when the tree is a git repo and git runs at all."""
        try:
            _git(self.repo, "rev-parse", "--is-inside-work-tree")
            return True
        except HarnessError:
            return False

    def create(self, node_id):
        """Isolated worktree + branch for one node. Returns the handle
        (``base`` pins the branch point so the audit sees committed work
        relative to it, not just a post-commit-clean status)."""
        safe_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in node_id)
        branch = f"harness/{safe_id}"
        path = os.path.join(self.repo, ".harness", "wt", safe_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            _git(self.repo, "worktree", "remove", "--force", path)
        base = _git(self.repo, "rev-parse", "HEAD").strip()
        _git(self.repo, "worktree", "add", "-b", branch, path)
        return {"node_id": node_id, "path": path, "branch": branch,
                "base": base}

    def audit(self, handle, declared):
        """Undeclared changes in the worktree (paths relative to the repo
        root, forward-slash normalized): the branch's committed diff against
        its base PLUS uncommitted/untracked leftovers. Empty means the node
        wrote only what it declared."""
        changed = set()
        diff = _git(handle["path"], "diff", "--name-only", handle["base"], "HEAD")
        changed.update(line.strip().replace("\\", "/")
                       for line in diff.splitlines() if line.strip())
        por = _git(handle["path"], "status", "--porcelain", "-uall")
        for line in por.splitlines():
            if not line.strip():
                continue
            rel = line[3:].strip().strip('"').replace("\\", "/")
            if rel.startswith(".harness/"):
                continue
            changed.add(rel)
        declared_norm = {str(d).replace("\\", "/").lstrip("./") for d in declared}
        return sorted(c for c in changed
                      if not any(c == d or c.startswith(d + "/")
                                 for d in declared_norm))

    def merge(self, handle):
        """Merge the node's branch into the starting tree. Raises
        HarnessError on conflict (the caller discards; never force-merge)."""
        _git(self.repo, "merge", "--no-ff", "--no-edit", handle["branch"])

    def discard(self, handle):
        """Remove the worktree and its branch (best-effort cleanup)."""
        try:
            _git(self.repo, "worktree", "remove", "--force", handle["path"])
        except HarnessError:
            pass
        try:
            _git(self.repo, "branch", "-D", handle["branch"])
        except HarnessError:
            pass
