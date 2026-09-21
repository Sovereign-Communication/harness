"""Concurrent multi-threaded executor and file-locking manager (#PR-2).

Provides:
- FileLockManager: per-file mutual exclusion for concurrent workers.
- ConcurrentExecutor: thread-pooled execution for independent batch files
  and topological DAG batches.
- PlanExecutor: the plan lane's ONE execution assembly (worker count, cost
  reservations, worktree isolation, per-node routing) shared by the CLI,
  MCP, and the agent's edit lane.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import replace
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set

from .dag import DAGNode, TaskDAG
from .errors import HarnessError
from .filesafety import VERIFY_TIMEOUT, default_run_verify
from .output import eprint
from .repo_scope import _rebase_path, discover_verification_gate, rebase_gate
from .results import SUCCESS_STATUSES
from .spend import NodeReserver
from .waist import node_apply_kwargs
from .worktree import WorktreeIsolation

# Auto-scaling hourglass default for concurrent node dispatch (the CLI
# parser, MCP schema, and the agent lane all mean this number).
DEFAULT_PLAN_WORKERS = 4


def partition_by_target_overlap(nodes):
    """HG-hybrid-isolate: split a concurrent stage by declared target_files.

    Returns ``(isolated_nodes, shared_serial_nodes)``:
    * overlap-free nodes (no shared declared target with any other node in
      the stage) may run in git worktrees in parallel;
    * nodes that share a declared target file are serialized under the
      shared-tree mutex -- concurrent writers on one path cannot be
      worktree-isolated without turning into a merge problem.
    """
    file_to_nodes = {}
    for node in nodes:
        targets = tuple(node.target_files or ())
        if not targets:
            # A node that declares no target cannot prove overlap-free;
            # treat it as shared-tree serial.
            file_to_nodes.setdefault((), []).append(node)
            continue
        for path in targets:
            file_to_nodes.setdefault(path, []).append(node)
    overlapping = set()
    for path, ns in file_to_nodes.items():
        if path != () and len(ns) > 1:
            overlapping.update(ns)
        elif path == () and len(ns) >= 1:
            overlapping.update(ns)
    isolated = [n for n in nodes if n not in overlapping]
    serial = [n for n in nodes if n in overlapping]
    return isolated, serial


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
                try:
                    if isolator is not None and parallel:
                        # HG-hybrid-isolate: worktrees only for overlap-free
                        # nodes; overlapping declared targets serialize in the
                        # shared tree under the per-path mutex.
                        iso_nodes, shared_nodes = partition_by_target_overlap(
                            executable_nodes)
                        # Create isolated handles inside the cleanup boundary.
                        for node in iso_nodes:
                            handles[node] = isolator.create(node.node_id)
                    else:
                        iso_nodes, shared_nodes = list(executable_nodes), []

                    stage_results: Dict[DAGNode, Dict[str, Any]] = {}

                    # Parallel: isolated worktree nodes, or (when isolation is
                    # off) every concurrent node under shared-tree locks.
                    parallel_nodes = iso_nodes if isolator is not None and parallel \
                        else list(executable_nodes)
                    if parallel_nodes:
                        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(parallel_nodes))) as pool:
                            future_to_node = {
                                pool.submit(self._run_node_reserved, node, worker_fn,
                                            reserver,
                                            (handles.get(node) or {}).get("path")): node
                                for node in parallel_nodes
                            }
                            for future in as_completed(future_to_node):
                                node = future_to_node[future]
                                try:
                                    stage_results[node] = future.result()
                                except Exception as exc:
                                    stage_results[node] = {
                                        "status": "fatal", "error": str(exc),
                                        "node_id": node.node_id,
                                    }
                                if (not keep_going and
                                        stage_results[node].get("status") not in SUCCESS_STATUSES):
                                    for f in future_to_node:
                                        f.cancel()

                    # Serial shared-tree arm: overlapping targets never share
                    # a worktree; they run one-at-a-time under the mutex.
                    for node in shared_nodes:
                        if (not keep_going and any(
                                stage_results.get(n, {}).get("status") not in SUCCESS_STATUSES
                                for n in parallel_nodes)):
                            stage_results[node] = {
                                "status": "dependency_failed",
                                "node_id": node.node_id,
                                "error": "prior stage node failed; shared-tree serial arm skipped",
                            }
                            continue
                        try:
                            stage_results[node] = self._run_node_reserved(
                                node, worker_fn, reserver, None)
                        except Exception as exc:
                            stage_results[node] = {
                                "status": "fatal", "error": str(exc),
                                "node_id": node.node_id,
                            }

                    for node in executable_nodes:
                        res = stage_results[node]
                        if node in handles:
                            res = self._settle_isolated(node, res, handles[node], isolator)
                        all_results[node.node_id] = res
                        if res.get("status") not in SUCCESS_STATUSES:
                            failed_nodes.add(node.node_id)
                finally:
                    if isolator is not None:
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
        if not isinstance(res, dict):
            # A worker violating its result contract must become an explicit
            # node failure, not an exception that skips liability settlement.
            res = {"status": "fatal", "node_id": node.node_id,
                   "error": "worker returned a non-mapping result"}
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
            # The node's declared work is committed before the branch merge:
            # an uncommitted worktree merges as "Already up to date", so the
            # edit would silently never land while the node reported ok.
            isolator.merge(handle, node.target_files)
        except HarnessError as exc:
            return {"status": "merge_conflict", "node_id": node.node_id,
                    "error": str(exc)}
        return res


class PlanExecutor:
    """The plan lane's execution assembly -- ONE owner for CLI, MCP, and the
    agent's edit lane: worker count, per-node cost reservations (MR-6),
    git-worktree isolation (MR-5), per-node routing kwargs, and the caller's
    apply callback.

    It owns *how* a planned DAG runs and nothing about what a node does: the
    lane supplies ``apply(target, node, route_kwargs, task_runner)`` (the
    default calls ``engine.apply_edit``; the agent lane adds its healing
    retry), so parallelism/isolation/reservation policy is derived once
    instead of three times.

    ``route_kwargs_fn(route) -> dict`` is the lane's per-node routing kwargs
    (default :func:`harness.waist.node_apply_kwargs`); the same function
    feeds the reserver, so a tier ceiling bounds both the request and the
    pre-dispatch reservation.
    """

    def __init__(self, engine, node_routes, *, parallel=True, isolate=True,
                 max_workers=DEFAULT_PLAN_WORKERS, keep_going=False,
                 require_diff_authorization=False, route_kwargs_fn=None,
                 base_apply_kwargs=None, apply=None, task_max_cost=None,
                 run_ceiling=None, repo=None, on_stage_done=None,
                 final_gate=None, run_gate=None, final_gate_runner=None):
        self.engine = engine
        # The tree the plan was made in: nodes' verification gates are rooted
        # here (the planner derives them from the plan's own root), so a node
        # running somewhere else can have them re-rooted truthfully.
        self.repo = os.path.abspath(repo) if repo else os.getcwd()
        self.node_routes = node_routes or {}
        self.parallel = bool(parallel)
        self.workers = max_workers if self.parallel else 1
        self.keep_going = bool(keep_going)
        self.require_diff_authorization = bool(require_diff_authorization)
        self.route_kwargs_fn = route_kwargs_fn or node_apply_kwargs
        self.base_apply_kwargs = dict(base_apply_kwargs or {})
        self.apply = apply or self._apply_edit
        self.executor = ConcurrentExecutor(max_workers=self.workers)

        # HG-final-gate: default ON when a verify command was discovered or
        # declared. ``final_gate`` is tri-state:
        #   False          -- operator opt-out (--no-final-gate)
        #   str            -- explicit command override
        #   None / True    -- auto: run_gate if set, else a discovered gate
        #                     from the plan's targets / node local_gates
        # ``final_gate_runner`` is the hermetic seam (defaults to
        # filesafety.default_run_verify).
        self.final_gate = final_gate
        self.run_gate = run_gate
        self.final_gate_runner = final_gate_runner or default_run_verify

        # MR-5 partition rule (hourglass default: on): concurrent nodes
        # execute in isolated git worktrees; serial nodes share the tree
        # under the per-path mutex. Unavailable git degrades loudly.
        # ``repo`` is the tree being edited (the CLI edits its CWD; the
        # agent lane edits its own root_dir), so isolation never branches a
        # different repository than the one the node targets.
        self.isolator = None
        if self.parallel and isolate:
            iso = WorktreeIsolation(repo=repo)
            if iso.available():
                self.isolator = iso
            else:
                eprint("[plan] git worktree isolation unavailable; "
                       "falling back to shared-tree mutex execution")
        # ``run_ceiling`` is the budget this lane is really running under
        # (the session's ``max_cost``). The governor already holds it, so
        # None means "ask the governor"; a lane that runs under a different
        # ceiling passes it, and the reserver never invents a bound of its
        # own -- an unrelated nominal fallback larger than the run ceiling
        # used to refuse every node (free nodes included) before any work.
        self.run_ceiling = run_ceiling
        default_amount = task_max_cost
        if default_amount is None:
            default_amount = getattr(engine, "default_task_max_cost", None)
        self.reserver = NodeReserver(
            getattr(engine, "governor", None), self.node_routes, default_amount,
            route_kwargs_fn=self.route_kwargs_fn, run_ceiling=run_ceiling)
        self.on_stage_done = on_stage_done

    def resolve_final_gate(self, dag: TaskDAG) -> Optional[str]:
        """The final verification command for this run, or None to skip.

        Default ON when a verify command was discovered or declared; an
        explicit override wins; ``final_gate=False`` disables the gate.
        """
        if self.final_gate is False:
            return None
        if isinstance(self.final_gate, str) and self.final_gate.strip():
            return self.final_gate.strip()
        if self.run_gate:
            return str(self.run_gate).strip()
        declared = [n.local_gate for n in dag.nodes.values() if n.local_gate]
        if not declared:
            return None
        targets = []
        for node in dag.nodes.values():
            targets.extend(node.target_files or ())
        discovered = discover_verification_gate(targets, self.repo)
        if discovered:
            return discovered
        unique = list(dict.fromkeys(str(g).strip() for g in declared if str(g).strip()))
        return unique[0] if unique else None

    def route_kwargs(self, node) -> Dict[str, Any]:
        """This node's routing kwargs from the plan's own route detail."""
        return self.route_kwargs_fn(self.node_routes.get(node.node_id))

    def _apply_edit(self, target, node, route_kwargs, task_runner):
        kwargs = dict(self.base_apply_kwargs)
        # Route kwargs win: a tier ceiling bounds the request unless the
        # lane pinned one (node_apply_kwargs suppresses it when pinned).
        kwargs.update(route_kwargs)
        # The attestation switch is this assembly's, so no lane can forget
        # to thread it into the write it is gating.
        kwargs["require_diff_authorization"] = self.require_diff_authorization
        if task_runner is not None:
            kwargs["task_runner"] = task_runner
        return self.engine.apply_edit(
            file_path=target, instruction=node.instruction,
            verify_cmd=node.local_gate, **kwargs)

    def run_node(self, node: DAGNode, gate_cwd: Optional[str] = None):
        """Worker contract for :meth:`ConcurrentExecutor.execute_dag`.

        Isolated nodes arrive with ``gate_cwd=<worktree path>``: the target
        and every gate command resolve inside that worktree.

        The node's gate was derived at PLAN time against the plan's own tree,
        so it is re-rooted here into the checkout this node really runs in --
        otherwise an isolated node's gate would compile the unedited copy in
        the repo, which passes and proves nothing about the node's write.
        """
        target = node.target_files[0] if node.target_files else None
        if target and gate_cwd:
            # Planned nodes may carry either repo-relative or absolute paths;
            # map both forms into the actual isolated checkout instead of
            # blindly joining (which can duplicate an already absolute root).
            target = _rebase_path(target, self.repo, gate_cwd)
        gate = rebase_gate(node.local_gate, self.repo, gate_cwd or self.repo)
        if gate != node.local_gate:
            node = replace(node, local_gate=gate)
        task_runner = None
        if gate_cwd:
            def task_runner(command, timeout=VERIFY_TIMEOUT):
                return default_run_verify(command, timeout=timeout, cwd=gate_cwd)
        return self.apply(target, node, self.route_kwargs(node), task_runner)

    def execute(self, dag: TaskDAG) -> Dict[str, Any]:
        results = self.executor.execute_dag(
            dag, self.run_node, keep_going=self.keep_going,
            reserver=self.reserver, isolator=self.isolator,
            on_stage_done=self.on_stage_done)
        gate = self.resolve_final_gate(dag)
        if gate:
            try:
                code, out = self.final_gate_runner(
                    gate, timeout=VERIFY_TIMEOUT, cwd=self.repo)
            except TypeError:
                code, out = self.final_gate_runner(gate)
            except HarnessError as exc:
                code, out = 1, str(exc)
            status = "ok" if code == 0 else "verify_failed"
            results["final_gate"] = {
                "status": status,
                "node_id": "final_gate",
                "gate": gate,
                "returncode": code,
                "output": (out or "")[-2000:],
                "cost": 0.0,
            }
        return results

    @staticmethod
    def summarize(results: Dict[str, Any]) -> Dict[str, Any]:
        """The completion/cost summary every plan-lane caller reports."""
        final = results.get("final_gate") if isinstance(results, dict) else None
        values = [res for key, res in (results or {}).items()
                  if key != "final_gate" and isinstance(res, dict)]
        nodes_ok = bool(values) and all(
            res.get("status") in SUCCESS_STATUSES for res in values)
        gate_ok = final is None or final.get("status") in SUCCESS_STATUSES
        return {
            # An empty DAG is not a successful execution. Treating all([]) as
            # true makes an empty/invalid plan report a false ok envelope.
            # A failing final gate also invalidates a green node set (composed
            # tree red: parallel green nodes can still compose into a red tree).
            "all_ok": nodes_ok and gate_ok,
            "completed": sum(1 for res in values
                             if res.get("status") in SUCCESS_STATUSES),
            "total_cost": round(
                sum(float(res.get("cost", 0.0) or 0.0) for res in values)
                + (float(final.get("cost", 0.0) or 0.0) if final else 0.0), 6),
            "final_gate": final,
        }
