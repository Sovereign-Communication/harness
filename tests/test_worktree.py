"""Git-worktree isolation + executor integration (M3, hermetic: local git only).

Pins the MR-5 partition rule as implemented: concurrent nodes in a stage run
in isolated worktrees + local branches; undeclared writes reject the node;
merge conflicts become merge_conflict (never force-merged); serial stages
share the tree under the mutex. Skips cleanly when git is unavailable.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from harness.dag import DAGNode, TaskDAG
from harness.errors import HarnessError
from harness.executor import ConcurrentExecutor
from harness.worktree import WorktreeIsolation

GIT = shutil.which("git") is not None


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                   check=True)


@unittest.skipUnless(GIT, "optional deps: git not available")
class WorktreeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        repo = self.dir.name
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        with open(os.path.join(repo, "base.txt"), "w") as f:
            f.write("base\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "base")
        self.iso = WorktreeIsolation(repo=repo)
        self.repo = repo

    def tearDown(self):
        wt_root = os.path.join(self.repo, ".harness", "wt")
        if os.path.isdir(wt_root):
            for name in os.listdir(wt_root):
                self.iso.discard({"path": os.path.join(wt_root, name),
                                  "branch": f"harness/{name}"})
        self.dir.cleanup()

    def test_available_in_repo(self):
        self.assertTrue(self.iso.available())

    def test_create_and_audit_undeclared_writes(self):
        h = self.iso.create("task_1")
        declared = ["base.txt"]
        with open(os.path.join(h["path"], "declared.txt"), "w") as f:
            f.write("x\n")
        self.assertEqual(self.iso.audit(h, declared), ["declared.txt"])

    def test_audit_accepts_declared_and_nested(self):
        h = self.iso.create("task_2")
        os.makedirs(os.path.join(h["path"], "pkg"))
        declared = ["base.txt", "pkg/mod.py"]
        for rel in ("base.txt", "pkg/mod.py"):
            with open(os.path.join(h["path"], rel), "a") as f:
                f.write("changed\n")
        self.assertEqual(self.iso.audit(h, declared), [])

    def test_merge_applies_changes_to_repo(self):
        h = self.iso.create("task_3")
        with open(os.path.join(h["path"], "base.txt"), "a") as f:
            f.write("isolated change\n")
        _git(h["path"], "add", ".")
        _git(h["path"], "commit", "-q", "-m", "node work")
        self.iso.merge(h)
        with open(os.path.join(self.repo, "base.txt")) as f:
            self.assertIn("isolated change", f.read())

    def test_conflicting_merge_raises(self):
        # Mirror the executor's real semantics: BOTH stage worktrees are
        # created up front (same branch point), so same-line changes on the
        # two branches genuinely diverge and the second merge conflicts.
        h1 = self.iso.create("task_4a")
        h2 = self.iso.create("task_4b")
        with open(os.path.join(h1["path"], "base.txt"), "a") as f:
            f.write("from a\n")
        _git(h1["path"], "add", ".")
        _git(h1["path"], "commit", "-q", "-m", "a")
        self.iso.merge(h1)
        with open(os.path.join(h2["path"], "base.txt"), "w") as f:
            f.write("base conflict\n")
        _git(h2["path"], "add", ".")
        _git(h2["path"], "commit", "-q", "-m", "b")
        with self.assertRaises(HarnessError):
            self.iso.merge(h2)
        self.iso.discard(h2)

    def test_discard_removes_worktree(self):
        h = self.iso.create("task_5")
        path = h["path"]
        self.assertTrue(os.path.isdir(path))
        self.iso.discard(h)
        self.assertFalse(os.path.isdir(path))


@unittest.skipUnless(GIT, "optional deps: git not available")
class ExecutorIsolationTests(WorktreeIsolationTests):
    """execute_dag's parallel stages isolate, audit, merge, and settle."""

    def _two_node_dag(self):
        n1 = DAGNode(node_id="task_1", instruction="write a",
                     target_files=("a.txt",))
        n2 = DAGNode(node_id="task_2", instruction="write b",
                     target_files=("b.txt",))
        return TaskDAG(nodes={"task_1": n1, "task_2": n2})

    def test_parallel_stage_isolates_and_merges(self):
        seen_cwds = []

        def worker(node, gate_cwd=None):
            base = gate_cwd or self.repo
            target = node.target_files[0]
            path = os.path.join(base, target)
            seen_cwds.append(gate_cwd)
            with open(path, "w") as f:
                f.write(f"{node.node_id} wrote {target}\n")
            if gate_cwd:
                _git(gate_cwd, "add", ".")
                _git(gate_cwd, "commit", "-q", "-m", node.node_id)
            return {"status": "ok", "cost": 0.0}

        results = ConcurrentExecutor(max_workers=2).execute_dag(
            self._two_node_dag(), worker, isolator=self.iso)
        self.assertEqual(results["task_1"]["status"], "ok")
        self.assertEqual(results["task_2"]["status"], "ok")
        self.assertTrue(all(seen_cwds))
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(self.repo, name)) as f:
                self.assertIn("wrote", f.read())

    def test_undeclared_write_rejects_node(self):
        def worker(node, gate_cwd=None):
            base = gate_cwd or self.repo
            # Declared target...
            with open(os.path.join(base, node.target_files[0]), "w") as f:
                f.write("declared\n")
            # ...plus an undeclared write (the audit must reject this node).
            with open(os.path.join(base, "sneaky.txt"), "w") as f:
                f.write("undeclared\n")
            if gate_cwd:
                _git(gate_cwd, "add", ".")
                _git(gate_cwd, "commit", "-q", "-m", node.node_id)
            return {"status": "ok", "cost": 0.0}

        sneaky = DAGNode(node_id="sneaky_node", instruction="x",
                         target_files=("a.txt",))
        other = DAGNode(node_id="task_other", instruction="y",
                        target_files=("b.txt",))
        dag = TaskDAG(nodes={"sneaky_node": sneaky, "task_other": other})

        def other_worker(node, gate_cwd=None):
            return {"status": "ok", "cost": 0.0}

        def worker_dispatch(node, gate_cwd=None):
            return worker(node, gate_cwd=gate_cwd) if node.node_id == "sneaky_node" \
                else other_worker(node, gate_cwd=gate_cwd)
        results = ConcurrentExecutor(max_workers=2).execute_dag(
            dag, worker_dispatch, isolator=self.iso)
        self.assertEqual(results["sneaky_node"]["status"], "fatal")
        self.assertIn("undeclared", results["sneaky_node"]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "sneaky.txt")))

    def test_audit_error_sets_fatal(self):
        from unittest.mock import patch

        def worker(node, gate_cwd=None):
            return {"status": "ok", "cost": 0.0}

        with patch.object(self.iso, "audit",
                          side_effect=HarnessError("audit blew up")):
            results = ConcurrentExecutor(max_workers=2).execute_dag(
                self._two_node_dag(), worker, isolator=self.iso)
        self.assertEqual(results["task_1"]["status"], "fatal")
        self.assertIn("audit blew up", results["task_1"]["error"])

    def test_failed_node_result_passes_through_without_merge(self):
        def worker(node, gate_cwd=None):
            base = gate_cwd or self.repo
            with open(os.path.join(base, node.target_files[0]), "w") as f:
                f.write("unmerged work\n")
            return {"status": "verify_failed", "cost": 0.0}

        results = ConcurrentExecutor(max_workers=2).execute_dag(
            self._two_node_dag(), worker, isolator=self.iso)
        self.assertEqual(results["task_1"]["status"], "verify_failed")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_merge_conflict_sets_merge_conflict_status(self):
        # Both stage worktrees branch from the same commit (created up
        # front), so two nodes adding the same file with different content
        # genuinely conflict: exactly one node merges, the other fails as
        # merge_conflict -- never force-merged.
        n1 = DAGNode(node_id="task_1", instruction="x",
                     target_files=("a.txt",))
        n2 = DAGNode(node_id="task_2", instruction="y",
                     target_files=("a.txt",))
        dag = TaskDAG(nodes={"task_1": n1, "task_2": n2})

        def worker(node, gate_cwd=None):
            with open(os.path.join(gate_cwd, "a.txt"), "w") as f:
                f.write(f"written by {node.node_id}\n")
            _git(gate_cwd, "add", ".")
            _git(gate_cwd, "commit", "-q", "-m", node.node_id)
            return {"status": "ok", "cost": 0.0}

        results = ConcurrentExecutor(max_workers=2).execute_dag(
            dag, worker, isolator=self.iso)
        statuses = sorted(r["status"] for r in results.values())
        self.assertEqual(statuses, ["merge_conflict", "ok"])

    def test_reserver_bounds_concurrent_dispatch(self):
        from harness.spend import NodeReserver, SpendGovernor
        from tests._fake import FakeTransport, m

        fake = FakeTransport(models=[m("cheap/x", prompt="0.000005",
                                         completion="0.00001")])
        gov = SpendGovernor(fake, "sk-test", max_cost=0.01)
        routes = {"task_1": None, "task_2": None}
        reserver = NodeReserver(gov, routes, 0.006)

        def worker(node):
            return {"status": "ok", "cost": 0.006}

        results = ConcurrentExecutor(max_workers=2).execute_dag(
            self._two_node_dag(), worker, reserver=reserver)
        statuses = sorted(r["status"] for r in results.values())
        # With per-node worst case 0.006 and ceiling 0.01, at most one node
        # can hold a reservation: the other must fail closed, and spend
        # never breaches.
        self.assertIn("fatal", statuses)
        self.assertLessEqual(gov.spent + gov.outstanding, 0.01 + 1e-9)


if __name__ == "__main__":
    unittest.main()
