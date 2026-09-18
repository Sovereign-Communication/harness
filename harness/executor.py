"""Concurrent multi-threaded executor and file-locking manager (#PR-2).

Provides:
- FileLockManager: per-file mutual exclusion for concurrent workers.
- ConcurrentExecutor: thread-pooled execution for independent batch files
  and topological DAG batches.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set

from .dag import DAGNode, TaskDAG
from .errors import HarnessError
from .results import SUCCESS_STATUSES


class FileLockManager:
    """Process-internal per-path mutual exclusion lock manager.

    Ensures concurrent worker threads never modify or rewrite the same file
    path concurrently.
    """

    def __init__(self):
        self._registry_lock = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def _canonical(self, path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def get_lock(self, path: str) -> threading.Lock:
        canon = self._canonical(path)
        with self._registry_lock:
            lock = self._locks.get(canon)
            if lock is None:
                lock = threading.Lock()
                self._locks[canon] = lock
            return lock

    @contextmanager
    def acquire(self, path: str) -> Iterator[None]:
        lock = self.get_lock(path)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def acquire_all(self, paths: Sequence[str]) -> Iterator[None]:
        """Acquire locks on multiple paths in sorted canonical order (avoids deadlock)."""
        canon_sorted = sorted(set(self._canonical(p) for p in paths))
        locks = [self.get_lock(p) for p in canon_sorted]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()


_GLOBAL_FILE_LOCKS = FileLockManager()


def get_global_file_locks() -> FileLockManager:
    return _GLOBAL_FILE_LOCKS


class ConcurrentExecutor:
    """Thread-pooled concurrent executor for independent batch edits and DAG execution."""

    def __init__(self, max_workers: int = 4, file_locks: Optional[FileLockManager] = None):
        if max_workers < 1:
            raise HarnessError("max_workers must be at least 1")
        self.max_workers = max_workers
        self.file_locks = file_locks or _GLOBAL_FILE_LOCKS

    def execute_files(
        self,
        files: Sequence[str],
        worker_fn: Callable[[int, str], Dict[str, Any]],
        *,
        keep_going: bool = False,
    ) -> List[Dict[str, Any]]:
        """Execute worker_fn(index, file_path) concurrently over files.

        Returns results in the original file order. Under fail-fast (keep_going=False),
        execution stops upon the first non-success result.
        """
        if not files:
            return []

        indexed_files = list(enumerate(files))
        results: Dict[int, Dict[str, Any]] = {}
        first_failure: Optional[Dict[str, Any]] = None

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(files))) as pool:
            future_to_idx = {
                pool.submit(self._run_file_with_lock, idx, fp, worker_fn): idx
                for idx, fp in indexed_files
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                except Exception as exc:
                    res = {"status": "fatal", "error": str(exc), "file": indexed_files[idx][1]}

                results[idx] = res
                if res.get("status") not in SUCCESS_STATUSES:
                    if first_failure is None:
                        first_failure = res
                    if not keep_going:
                        # Cancel pending futures
                        for f in future_to_idx:
                            f.cancel()

        # Re-assemble in original order, stopping at first failure if not keep_going
        ordered_results: List[Dict[str, Any]] = []
        for idx, _ in indexed_files:
            if idx in results:
                ordered_results.append(results[idx])
                if not keep_going and results[idx].get("status") not in SUCCESS_STATUSES:
                    break

        return ordered_results

    def _run_file_with_lock(
        self,
        idx: int,
        file_path: str,
        worker_fn: Callable[[int, str], Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self.file_locks.acquire(file_path):
            return worker_fn(idx, file_path)

    def execute_dag(
        self,
        dag: TaskDAG,
        worker_fn: Callable[[DAGNode], Dict[str, Any]],
        *,
        keep_going: bool = False,
        reserver=None,
        isolator=None,
        on_stage_done=None,
    ) -> Dict[str, Any]:
        """Execute a TaskDAG stage-by-stage (batch by batch) concurrently.

        reserver: optional cost-liability seam (MR-6) with
            ``reserve(node) -> token|None`` and ``reconcile(token, actual)``.
            Per-call preflight alone cannot bound concurrent dispatch (W
            workers preflight against the full remaining ceiling before any
            records); with a reserver, ``spent + outstanding`` never exceeds
            the ceiling and each node's worst case is reserved before
            dispatch, then settled against the billed actual.
        isolator: optional git-worktree seam (MR-5) with ``create(node_id)
            -> handle``, ``audit(handle, declared) -> undeclared``,
            ``merge(handle)``, ``discard(handle)``. Engaged ONLY for stages
            that actually run in parallel (the partition rule: concurrent
            nodes are isolated; serial nodes share the tree under the
            mutex). Isolated workers are called with ``gate_cwd=<worktree
            path>`` (gates + target paths resolve inside the worktree).
            Undeclared writes reject the node; merge conflicts become
            ``merge_conflict`` results (never force-merged).

        Returns {node_id: result_dict}.
        """
        batches = dag.topological_batches()
        all_results: Dict[str, Dict[str, Any]] = {}
        failed_nodes: Set[str] = set()

        for batch in batches:
            # Check if any node in this batch depends on a failed node
            executable_nodes: List[DAGNode] = []
            for node in batch:
                if any(dep in failed_nodes for dep in node.dependencies):
                    all_results[node.node_id] = {
                        "status": "dependency_failed",
                        "node_id": node.node_id,
                        "dependencies": list(node.dependencies),
                    }
                    failed_nodes.add(node.node_id)
                else:
                    executable_nodes.append(node)

            if not executable_nodes:
                continue

            if self.max_workers == 1:
                for node in executable_nodes:
                    try:
                        res = self._run_node_with_locks(node, worker_fn)
                    except Exception as exc:
                        res = {"status": "fatal", "error": str(exc), "node_id": node.node_id}

                    all_results[node.node_id] = res
                    if res.get("status") not in SUCCESS_STATUSES:
                        failed_nodes.add(node.node_id)
                        if not keep_going:
                            break
            else:
                parallel = len(executable_nodes) > 1
                handles: Dict[DAGNode, Any] = {}
                if isolator is not None and parallel:
                    for node in executable_nodes:
                        handles[node] = isolator.create(node.node_id)
                try:
                    # Run executable nodes in parallel
                    with ThreadPoolExecutor(max_workers=min(self.max_workers, len(executable_nodes))) as pool:
                        future_to_node = {
                            pool.submit(self._run_node_reserved, node, worker_fn,
                                        reserver,
                                        (handles.get(node) or {}).get("path")): node
                            for node in executable_nodes
                        }

                        for future in as_completed(future_to_node):
                            node = future_to_node[future]
                            try:
                                res = future.result()
                            except Exception as exc:
                                res = {"status": "fatal", "error": str(exc), "node_id": node.node_id}

                            if node in handles:
                                res = self._settle_isolated(node, res, handles[node], isolator)
                            all_results[node.node_id] = res
                            if res.get("status") not in SUCCESS_STATUSES:
                                failed_nodes.add(node.node_id)
                                if not keep_going:
                                    for f in future_to_node:
                                        f.cancel()
                finally:
                    for handle in handles.values():
                        isolator.discard(handle)

            if on_stage_done is not None:
                on_stage_done(executable_nodes, all_results)

            if failed_nodes and not keep_going:
                break

        return all_results

    def _run_node_with_locks(
        self,
        node: DAGNode,
        worker_fn: Callable[[DAGNode], Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self.file_locks.acquire_all(node.target_files):
            return worker_fn(node)

    def _run_node_reserved(
        self,
        node: DAGNode,
        worker_fn: Callable[[DAGNode], Dict[str, Any]],
        reserver,
        gate_cwd=None,
    ) -> Dict[str, Any]:
        token = reserver.reserve(node) if reserver is not None else None
        try:
            with self.file_locks.acquire_all(node.target_files):
                # Worker contract: when isolation is engaged the worker is
                # called as ``worker_fn(node, gate_cwd=<worktree path>)``;
                # otherwise the historical single-argument shape is kept
                # (DAGNode is frozen -- state rides the call, not the node).
                if gate_cwd is not None:
                    res = worker_fn(node, gate_cwd=gate_cwd)
                else:
                    res = worker_fn(node)
        except Exception:
            if token is not None:
                reserver.reconcile(token, 0.0)
            raise
        if token is not None:
            reserver.reconcile(token, res.get("cost") or 0.0)
        return res

    def _settle_isolated(self, node, res, handle, isolator):
        """Audit undeclared writes, merge on success, discard always."""
        try:
            undeclared = isolator.audit(handle, node.target_files)
        except HarnessError as exc:
            return {"status": "fatal", "error": str(exc), "node_id": node.node_id}
        if res.get("status") not in SUCCESS_STATUSES:
            return res
        if undeclared:
            return {"status": "fatal", "node_id": node.node_id,
                    "error": f"undeclared writes outside target_files: {undeclared}"}
        try:
            isolator.merge(handle)
        except HarnessError as exc:
            return {"status": "merge_conflict", "node_id": node.node_id,
                    "error": str(exc)}
        return res
