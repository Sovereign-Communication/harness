"""Unit tests for concurrent executor and file-locking manager (harness/executor.py)."""
import os
import threading
import time
import unittest

from harness.batch import BatchOptions, run_batch
from harness.dag import DAGNode, TaskDAG
from harness.errors import HarnessError
from harness.executor import ConcurrentExecutor, FileLockManager, PlanExecutor


class FileLockManagerTests(unittest.TestCase):
    def test_canonical_path_mapping(self):
        mgr = FileLockManager()
        p1 = os.path.abspath("foo/bar.py")
        p2 = os.path.abspath("foo/../foo/bar.py")
        lock1 = mgr.get_lock(p1)
        lock2 = mgr.get_lock(p2)
        self.assertIs(lock1, lock2)

    def test_acquire_mutual_exclusion(self):
        mgr = FileLockManager()
        path = os.path.abspath("test_file.py")
        entered = []
        active_count = 0
        max_active = 0

        def worker():
            nonlocal active_count, max_active
            with mgr.acquire(path):
                active_count += 1
                max_active = max(max_active, active_count)
                time.sleep(0.01)
                entered.append(True)
                active_count -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(entered), 5)
        self.assertEqual(max_active, 1)

    def test_acquire_all_sorted(self):
        mgr = FileLockManager()
        p1 = os.path.abspath("b.py")
        p2 = os.path.abspath("a.py")
        with mgr.acquire_all([p1, p2, p1]):
            # Verify we can acquire locks across multiple files
            pass


class ConcurrentExecutorTests(unittest.TestCase):
    def test_plan_summary_does_not_treat_empty_execution_as_success(self):
        summary = PlanExecutor.summarize({})
        self.assertFalse(summary["all_ok"])
        self.assertEqual(summary["completed"], 0)

    def test_executor_invalid_workers(self):
        with self.assertRaises(HarnessError):
            ConcurrentExecutor(max_workers=0)

    def test_execute_files_ordering_and_parallelism(self):
        executor = ConcurrentExecutor(max_workers=4)
        files = ["f1.py", "f2.py", "f3.py", "f4.py"]
        thread_names = set()

        def worker(idx, fp):
            thread_names.add(threading.current_thread().name)
            time.sleep(0.01)
            return {"status": "ok", "cost": 0.001, "file": fp, "idx": idx}

        results = executor.execute_files(files, worker, keep_going=False)
        self.assertEqual(len(results), 4)
        # Verify ordering is preserved
        self.assertEqual([r["file"] for r in results], files)
        self.assertEqual([r["idx"] for r in results], [0, 1, 2, 3])

    def test_execute_files_fail_fast(self):
        executor = ConcurrentExecutor(max_workers=2)
        files = ["f1.py", "f2.py", "f3.py"]

        def worker(idx, fp):
            if idx == 0:
                return {"status": "verify_failed", "cost": 0.001, "file": fp}
            time.sleep(0.05)
            return {"status": "ok", "cost": 0.001, "file": fp}

        results = executor.execute_files(files, worker, keep_going=False)
        # In fail-fast, first failure cuts off subsequent results
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "verify_failed")

    def test_execute_files_keep_going(self):
        executor = ConcurrentExecutor(max_workers=3)
        files = ["f1.py", "f2.py", "f3.py"]

        def worker(idx, fp):
            if idx == 1:
                return {"status": "verify_failed", "file": fp}
            return {"status": "ok", "file": fp}

        results = executor.execute_files(files, worker, keep_going=True)
        self.assertEqual(len(results), 3)
        self.assertEqual([r["status"] for r in results], ["ok", "verify_failed", "ok"])

    def test_execute_files_exception_handling(self):
        executor = ConcurrentExecutor(max_workers=2)

        def worker(idx, fp):
            if idx == 0:
                raise RuntimeError("crash")
            return {"status": "ok", "file": fp}

        results = executor.execute_files(["f1.py", "f2.py"], worker, keep_going=True)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "fatal")
        self.assertIn("crash", results[0]["error"])

    def test_isolation_creation_failure_cleans_prior_handles(self):
        class FailingIsolation:
            def __init__(self):
                self.created = []
                self.discarded = []

            def create(self, node_id):
                if node_id == "B":
                    raise HarnessError("worktree unavailable")
                handle = {"node_id": node_id, "path": node_id}
                self.created.append(handle)
                return handle

            def discard(self, handle):
                self.discarded.append(handle)

            def audit(self, handle, declared):
                return []

            def merge(self, handle, paths=None):
                return None

        isolation = FailingIsolation()
        dag = TaskDAG(nodes={
            "A": DAGNode(node_id="A", instruction="a", target_files=("a.py",)),
            "B": DAGNode(node_id="B", instruction="b", target_files=("b.py",)),
        })
        with self.assertRaises(HarnessError):
            ConcurrentExecutor(max_workers=2).execute_dag(
                dag, lambda node, **kwargs: {"status": "ok"},
                isolator=isolation)
        self.assertEqual(isolation.discarded, isolation.created)

    def test_isolated_merges_follow_batch_order_not_completion_order(self):
        class Isolation:
            def __init__(self):
                self.merges = []

            def create(self, node_id):
                return {"node_id": node_id, "path": node_id}

            def audit(self, handle, declared):
                return []

            def merge(self, handle, paths=None):
                self.merges.append(handle["node_id"])

            def discard(self, handle):
                pass

        isolation = Isolation()
        dag = TaskDAG(nodes={
            "A": DAGNode(node_id="A", instruction="a", target_files=("a.py",)),
            "B": DAGNode(node_id="B", instruction="b", target_files=("b.py",)),
        })

        def worker(node, **kwargs):
            if node.node_id == "A":
                time.sleep(0.03)
            return {"status": "ok", "node_id": node.node_id}

        results = ConcurrentExecutor(max_workers=2).execute_dag(
            dag, worker, isolator=isolation)
        self.assertEqual(results["A"]["status"], "ok")
        self.assertEqual(isolation.merges, ["A", "B"])

    def test_execute_dag_stages(self):
        executor = ConcurrentExecutor(max_workers=4)
        # A -> B, A -> C, B -> D, C -> D
        a = DAGNode(node_id="A", instruction="a", target_files=("a.py",))
        b = DAGNode(node_id="B", instruction="b", dependencies=("A",), target_files=("b.py",))
        c = DAGNode(node_id="C", instruction="c", dependencies=("A",), target_files=("c.py",))
        d = DAGNode(node_id="D", instruction="d", dependencies=("B", "C"), target_files=("d.py",))
        dag = TaskDAG(nodes={"A": a, "B": b, "C": c, "D": d})

        order_executed = []
        lock = threading.Lock()

        def worker(node):
            with lock:
                order_executed.append(node.node_id)
            return {"status": "ok", "node_id": node.node_id}

        results = executor.execute_dag(dag, worker, keep_going=False)
        self.assertEqual(len(results), 4)
        self.assertEqual(order_executed[0], "A")
        self.assertIn(order_executed[1], ("B", "C"))
        self.assertIn(order_executed[2], ("B", "C"))
        self.assertEqual(order_executed[3], "D")

    def test_execute_dag_dependency_failure(self):
        executor = ConcurrentExecutor(max_workers=4)
        a = DAGNode(node_id="A", instruction="a")
        b = DAGNode(node_id="B", instruction="b", dependencies=("A",))
        dag = TaskDAG(nodes={"A": a, "B": b})

        def worker(node):
            if node.node_id == "A":
                return {"status": "verify_failed", "node_id": "A"}
            return {"status": "ok", "node_id": node.node_id}

        # Under keep_going=True, B should report dependency_failed
        results = executor.execute_dag(dag, worker, keep_going=True)
        self.assertEqual(results["A"]["status"], "verify_failed")
        self.assertEqual(results["B"]["status"], "dependency_failed")


class BatchParallelIntegrationTests(unittest.TestCase):
    def test_run_batch_parallel(self):
        class MockEngine:
            def apply_edit(self, task_id=None, file_path=None, **kwargs):
                return {
                    "status": "ok",
                    "cost": 0.0005,
                    "task_id": task_id,
                    "file": file_path,
                    "verify": {"command": "pytest", "passed": True},
                }

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f1 = os.path.join(td, "f1.py")
            f2 = os.path.join(td, "f2.py")
            open(f1, "w").close()
            open(f2, "w").close()

            engine = MockEngine()
            options = BatchOptions(instruction="test instruction")
            result = run_batch(engine, [f1, f2], parallel=True, max_workers=2, options=options)

            self.assertTrue(result["batch"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["results"]), 2)
            self.assertTrue(result["verify"]["passed"])


if __name__ == "__main__":
    unittest.main()
