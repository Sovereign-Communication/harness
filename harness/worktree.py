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


def _declared_relpaths(declared, *roots):
    """Declared targets as paths relative to the worktree.

    Lanes differ: the agent's plan carries repo-relative targets while the
    MCP lane's planned nodes carry ABSOLUTE ones. Either form has to become a
    worktree-relative pathspec, or git rejects it as "outside repository" and
    the audit compares it against nothing -- flagging the node's own declared
    write as undeclared. Anything outside the tree is dropped (never a way to
    commit stray paths).
    """
    prefixes = [str(r).replace("\\", "/").rstrip("/") + "/"
                for r in roots if r]
    out = []
    for raw in declared or []:
        p = str(raw).replace("\\", "/")
        for prefix in prefixes:
            if p.startswith(prefix):
                p = p[len(prefix):]
                break
        if p.startswith("./"):
            p = p[2:]
        if p and p != "." and not p.startswith("../") and p != "..":
            out.append(p)
    return out


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

    def _declared(self, handle, declared):
        """This node's declared targets, worktree-relative (see
        :func:`_declared_relpaths`)."""
        return _declared_relpaths(declared, handle.get("path"), self.repo)

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
        declared_norm = set(self._declared(handle, declared))

        def generated_by_verification(path):
            # ``py_compile`` is the default Python gate. Its __pycache__ and
            # .pyc outputs are verification artifacts, not model writes; if
            # they enter the undeclared-write set, every isolated Python node
            # is rejected after its gate has actually passed.
            parts = path.split("/")
            return "__pycache__" in parts or path.endswith(".pyc")

        return sorted(c for c in changed
                      if not generated_by_verification(c)
                      and not any(c == d or c.startswith(d + "/")
                                  for d in declared_norm))

    def commit(self, handle, paths=None):
        """Commit the node's write INSIDE its worktree, so the branch
        actually carries it. Returns True when a commit was made.

        A worker writes files; a branch only carries what the worktree
        *committed*. Merging an uncommitted worktree reports "Already up to
        date": the node's edit silently never reaches the tree while the
        node still reports ok and its gate passed inside the worktree --
        exactly the fake success this lane exists to prevent. The audit has
        already run, so "declared paths" (or, when a caller names none,
        everything except the ``.harness/`` scaffolding) is precisely the
        node's declared work.
        """
        specs = self._declared(handle, paths)
        if not specs:
            specs = [".", ":(exclude).harness"]
        dirty = _git(handle["path"], "status", "--porcelain", "-uall",
                     "--", *specs)
        if not dirty.strip():
            return False
        # -f: a declared target may legitimately be ignored (e.g. tmp/x.py);
        # the audit already refused anything the node did not declare.
        _git(handle["path"], "add", "-A", "-f", "--", *specs)
        _git(handle["path"], "-c", "user.name=harness",
             "-c", "user.email=harness@local", "commit", "-q", "--no-verify",
             "-m", f"harness node {handle.get('node_id', '')} isolated work")
        return True

    def merge(self, handle, paths=None):
        """Merge the node's branch into the starting tree, committing the
        worktree's declared work first (``paths``: the node's target files --
        see :meth:`commit` for why an uncommitted worktree merges as
        nothing). Raises HarnessError on conflict (the caller discards;
        never force-merge)."""
        self.commit(handle, paths)
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
